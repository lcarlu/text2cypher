"""Load the HR dummy knowledge graph in Neo4j.

uv run text2cypher-load                 # full dataset (~300k employees, ~2.8M salaries)
uv run text2cypher-load --limit 10000   # first 10k employees only
uv run text2cypher-load --reset         # wipe the database first (asked for confirmation)
"""

import argparse
import logging
import time
from collections.abc import Iterable, Iterator
from itertools import islice

import neo4j

from text2cypher_backend.config import PROJECT_DIR, get_settings
from text2cypher_backend.graph.driver import get_driver
from text2cypher_backend.loader import h8, synthetic

logger = logging.getLogger("text2cypher.loader")

BATCH_SIZE = 10_000

CONSTRAINTS = [
    "CREATE CONSTRAINT employee_id IF NOT EXISTS FOR (n:Employee) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT department_id IF NOT EXISTS FOR (n:Department) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT title_name IF NOT EXISTS FOR (n:Title) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT survey_id IF NOT EXISTS FOR (n:SurveyResponse) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT candidate_id IF NOT EXISTS FOR (n:Candidate) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT requisition_id IF NOT EXISTS FOR (n:JobRequisition) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT skill_name IF NOT EXISTS FOR (n:Skill) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT project_id IF NOT EXISTS FOR (n:Project) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT office_code IF NOT EXISTS FOR (n:Office) REQUIRE n.code IS UNIQUE",
    "CREATE INDEX employee_email IF NOT EXISTS FOR (n:Employee) ON (n.email)",
    "CREATE INDEX employee_last_name IF NOT EXISTS FOR (n:Employee) ON (n.lastName)",
    "CREATE INDEX salary_current IF NOT EXISTS FOR (n:Salary) ON (n.isCurrent)",
    "CREATE FULLTEXT INDEX employee_names IF NOT EXISTS FOR (n:Employee) ON EACH [n.firstName, n.lastName]",
]

Q_DEPARTMENTS = "UNWIND $rows AS row MERGE (d:Department {id: row.id}) SET d.name = row.name"
Q_EMPLOYEES = """
UNWIND $rows AS row
MERGE (e:Employee {id: row.id})
SET e.firstName = row.firstName, e.lastName = row.lastName, e.gender = row.gender,
    e.birthDate = row.birthDate, e.hireDate = row.hireDate, e.email = row.email
"""
Q_DEPARTMENT_LINK = """
UNWIND $rows AS row
MATCH (e:Employee {{id: row.employeeId}}), (d:Department {{id: row.departmentId}})
CREATE (e)-[:{rel} {{fromDate: row.fromDate, toDate: row.toDate, isCurrent: row.isCurrent}}]->(d)
"""
Q_TITLES = """
UNWIND $rows AS row
MATCH (e:Employee {id: row.employeeId})
MERGE (t:Title {name: row.title})
CREATE (e)-[:HAS_TITLE {fromDate: row.fromDate, toDate: row.toDate, isCurrent: row.isCurrent}]->(t)
"""
Q_SALARIES = """
UNWIND $rows AS row
MATCH (e:Employee {id: row.employeeId})
CREATE (e)-[:HAS_SALARY]->(:Salary {amount: row.amount, fromDate: row.fromDate, toDate: row.toDate, isCurrent: row.isCurrent})
"""
Q_REPORTS_TO = """
MATCH (m:Employee)-[:MANAGES {isCurrent: true}]->(d:Department)<-[:WORKS_IN {isCurrent: true}]-(e:Employee)
WHERE e <> m
CALL (e, m) { CREATE (e)-[:REPORTS_TO]->(m) } IN TRANSACTIONS OF 10000 ROWS
"""
Q_SURVEY = """
UNWIND $rows AS row
MATCH (e:Employee {id: row.employeeId})
CREATE (e)-[:ANSWERED]->(s:SurveyResponse {id: row.id})
SET s += row {.recruitmentScore, .benefitsScore, .communicationScore, .supportScore, .developmentScore,
              .payrollScore, .topStrength, .improvementArea, .submittedAt}
"""
Q_OFFICES = "UNWIND $rows AS row MERGE (o:Office {code: row.code}) SET o.city = row.city, o.country = row.country, o.location = row.location"
Q_SKILLS = "UNWIND $rows AS row MERGE (s:Skill {name: row.name}) SET s.category = row.category"
Q_CANDIDATES = "UNWIND $rows AS row MERGE (c:Candidate {id: row.id}) SET c += row"
Q_REQUISITIONS = """
UNWIND $rows AS row
MATCH (d:Department {id: row.departmentId})
MERGE (r:JobRequisition {id: row.id})
SET r.title = row.title, r.status = row.status, r.openedAt = row.openedAt, r.closedAt = row.closedAt
MERGE (r)-[:OPENED_IN]->(d)
"""
Q_APPLICATIONS = """
UNWIND $rows AS row
MATCH (c:Candidate {id: row.candidateId}), (r:JobRequisition {id: row.requisitionId})
CREATE (c)-[:APPLIED_TO {appliedAt: row.appliedAt, status: row.status}]->(r)
"""
Q_PROJECTS = """
UNWIND $rows AS row
MATCH (d:Department {id: row.departmentId})
MERGE (p:Project {id: row.id})
SET p.name = row.name, p.status = row.status, p.startDate = row.startDate, p.endDate = row.endDate, p.budget = row.budget
MERGE (p)-[:OWNED_BY]->(d)
"""
Q_BASED_IN = """
UNWIND $rows AS row
MATCH (e:Employee {id: row.employeeId}), (o:Office {code: row.officeCode})
CREATE (e)-[:BASED_IN]->(o)
"""
Q_HAS_SKILL = """
UNWIND $rows AS row
MATCH (e:Employee {id: row.employeeId}), (s:Skill {name: row.skill})
CREATE (e)-[:HAS_SKILL {level: row.level}]->(s)
"""
Q_WORKS_ON = """
UNWIND $rows AS row
MATCH (e:Employee {id: row.employeeId}), (p:Project {id: row.projectId})
CREATE (e)-[:WORKS_ON {role: row.role, allocation: row.allocation}]->(p)
"""


def _batches(rows: Iterable[dict], size: int = BATCH_SIZE) -> Iterator[list[dict]]:
    iterator = iter(rows)
    while batch := list(islice(iterator, size)):
        yield batch


def write_rows(session: neo4j.Session, query: str, rows: Iterable[dict], name: str) -> int:
    start, total, next_log = time.perf_counter(), 0, 200_000
    for batch in _batches(rows):
        session.execute_write(lambda tx, b=batch: tx.run(query, rows=b).consume())
        total += len(batch)
        if total >= next_log:
            logger.info("  %s: %s rows...", name, f"{total:,}")
            next_log += 200_000
    logger.info("%s: %s rows in %.1fs", name, f"{total:,}", time.perf_counter() - start)
    return total


def reset_database(session: neo4j.Session) -> None:
    logger.info("Deleting all nodes and relationships...")
    session.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()
    for index in session.run("SHOW INDEXES YIELD name, owningConstraint WHERE owningConstraint IS NULL RETURN name"):
        if not index["name"].startswith(("index_", "__")):
            session.run(f"DROP INDEX `{index['name']}` IF EXISTS").consume()
    for constraint in session.run("SHOW CONSTRAINTS YIELD name RETURN name"):
        session.run(f"DROP CONSTRAINT `{constraint['name']}` IF EXISTS").consume()


def load(driver: neo4j.Driver, limit: int | None, database: str) -> None:
    sql_path = h8.download_dump(PROJECT_DIR / "data" / "cache")
    with driver.session(database=database) as session:
        for statement in CONSTRAINTS:
            session.run(statement).consume()
        session.run("CALL db.awaitIndexes(300)").consume()

        departments = h8.read_departments(sql_path)
        write_rows(session, Q_DEPARTMENTS, departments, "Department")
        employees = h8.read_employees(sql_path, limit)
        employee_ids = {e["id"] for e in employees}
        write_rows(session, Q_EMPLOYEES, employees, "Employee")

        current_departments: dict[int, str] = {}

        def works_in_rows():
            for row in h8.iter_department_links(sql_path, "department_employee", employee_ids):
                if row["isCurrent"]:
                    current_departments[row["employeeId"]] = row["departmentId"]
                yield row

        write_rows(session, Q_DEPARTMENT_LINK.format(rel="WORKS_IN"), works_in_rows(), "WORKS_IN")
        write_rows(
            session,
            Q_DEPARTMENT_LINK.format(rel="MANAGES"),
            h8.iter_department_links(sql_path, "department_manager", employee_ids),
            "MANAGES",
        )
        write_rows(session, Q_TITLES, h8.iter_titles(sql_path, employee_ids), "HAS_TITLE")
        write_rows(session, Q_SALARIES, h8.iter_salaries(sql_path, employee_ids), "Salary")
        start = time.perf_counter()
        session.run(Q_REPORTS_TO).consume()
        logger.info("REPORTS_TO derived from current managers in %.1fs", time.perf_counter() - start)

        department_ids = [d["id"] for d in departments]
        write_rows(session, Q_SURVEY, synthetic.survey_responses(employees), "SurveyResponse")
        write_rows(session, Q_OFFICES, synthetic.offices(), "Office")
        write_rows(session, Q_SKILLS, synthetic.skills(), "Skill")
        candidates, requisitions, applications = synthetic.recruitment(department_ids)
        write_rows(session, Q_CANDIDATES, candidates, "Candidate")
        write_rows(session, Q_REQUISITIONS, requisitions, "JobRequisition")
        write_rows(session, Q_APPLICATIONS, applications, "APPLIED_TO")
        projects = synthetic.projects(department_ids)
        write_rows(session, Q_PROJECTS, projects, "Project")
        based_in, has_skill, works_on = synthetic.employee_assignments(
            current_departments, sorted(employee_ids), projects
        )
        write_rows(session, Q_BASED_IN, based_in, "BASED_IN")
        write_rows(session, Q_HAS_SKILL, has_skill, "HAS_SKILL")
        write_rows(session, Q_WORKS_ON, works_on, "WORKS_ON")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the HR dummy knowledge graph in Neo4j")
    parser.add_argument("--limit", type=int, default=None, help="Only load the first N employees (default: all)")
    parser.add_argument("--reset", action="store_true", help="Delete everything in the database before loading")
    parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation with --reset")
    parser.add_argument("--skip-semantic-layer", action="store_true", help="Do not build the semantic layer")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("neo4j").setLevel(logging.WARNING)

    settings = get_settings()
    driver = get_driver()
    driver.verify_connectivity()
    start = time.perf_counter()
    try:
        with driver.session(database=settings.neo4j_database) as session:
            node_count = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            if args.reset:
                target = f"{settings.neo4j_uri} / database '{settings.neo4j_database}' ({node_count:,} nodes)"
                if not args.yes and input(f"Delete EVERYTHING in {target}? Type 'yes': ") != "yes":
                    raise SystemExit("Aborted.")
                reset_database(session)
            elif node_count:
                raise SystemExit(
                    f"Database '{settings.neo4j_database}' on {settings.neo4j_uri} already has {node_count:,} nodes. "
                    "Use --reset to wipe it first."
                )
        load(driver, args.limit, settings.neo4j_database)
        if not args.skip_semantic_layer:
            from text2cypher_backend.semantic.layer import build_semantic_layer

            build_semantic_layer(driver)
    finally:
        driver.close()
    logger.info("Done in %.0fs", time.perf_counter() - start)


if __name__ == "__main__":
    main()
