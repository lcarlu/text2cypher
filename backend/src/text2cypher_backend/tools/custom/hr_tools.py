"""Domain tools for the HR dummy graph (example of custom tools: delete this file when you plug your own KG)."""

from langchain_core.tools import BaseTool, tool

from text2cypher_backend.graph.executor import CypherError
from text2cypher_backend.tools.context import ToolContext

EMPLOYEE_PROFILE = """
MATCH (e:Employee)
WHERE e.id = $id OR toLower(e.email) = toLower($email)
OPTIONAL MATCH (e)-[w:WORKS_IN {isCurrent: true}]->(d:Department)
OPTIONAL MATCH (e)-[:HAS_TITLE {isCurrent: true}]->(t:Title)
OPTIONAL MATCH (e)-[:HAS_SALARY]->(s:Salary {isCurrent: true})
OPTIONAL MATCH (e)-[:REPORTS_TO]->(m:Employee)
OPTIONAL MATCH (e)-[:BASED_IN]->(o:Office)
OPTIONAL MATCH (e)-[hs:HAS_SKILL]->(sk:Skill)
RETURN e.id AS id, e.firstName + ' ' + e.lastName AS name, e.email AS email, e.hireDate AS hireDate,
       d.name AS department, t.name AS title, s.amount AS currentSalary,
       m.firstName + ' ' + m.lastName AS manager, o.city AS office,
       collect(DISTINCT sk.name + ' (' + toString(hs.level) + '/5)') AS skills
"""


def create_tools(ctx: ToolContext) -> list[BaseTool]:
    @tool
    def get_employee_profile(employee_id: int | None = None, email: str | None = None) -> str:
        """Return the current profile of ONE employee (department, title, salary, manager, office, skills)
        from their employee id or email. Do not use it for aggregations over many employees."""
        if employee_id is None and not email:
            return "Provide employee_id or email."
        try:
            result = ctx.run_cypher(
                EMPLOYEE_PROFILE, {"id": employee_id, "email": email or ""}, tool="get_employee_profile"
            )
        except CypherError as e:
            return f"Error: {e.message}"
        return result.to_llm_text() if result.rows else "No employee found."

    return [get_employee_profile]
