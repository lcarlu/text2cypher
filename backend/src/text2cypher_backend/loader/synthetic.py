"""Deterministic synthetic HR data completing the h8 dump (survey, recruitment, skills, projects, offices)."""

import random
from datetime import UTC, date, datetime, timedelta

from neo4j.spatial import WGS84Point

SEED = 42

# Candidates of neo4j_text2SQL data/newTables.sql
CANDIDATES = [
    ("alice.johnson@example.com", "Alice", "Johnson", "555-0101", "https://storage.link/resumes/ajohnson_2024.pdf"),
    ("bob.smith@provider.net", "Bob", "Smith", "555-0102", "https://storage.link/resumes/bsmith_cv.pdf"),
    ("charlie.davis@webmail.com", "Charlie", "Davis", "555-0103", "https://storage.link/resumes/cdavis_final.pdf"),
    ("diana.prince@corp.com", "Diana", "Prince", "555-0104", "https://storage.link/resumes/dprince_resume.pdf"),
    ("evan.wright@email.org", "Evan", "Wright", "555-0105", "https://storage.link/resumes/ewright_portfolio.pdf"),
    ("fiona.gomez@career.io", "Fiona", "Gomez", "555-0106", "https://storage.link/resumes/fgomez_2023.pdf"),
    ("george.miller@tech.com", "George", "Miller", "555-0107", "https://storage.link/resumes/gmiller_dev.pdf"),
    ("hannah.abbot@service.net", "Hannah", "Abbot", "555-0108", "https://storage.link/resumes/habbot_cv.pdf"),
    ("ian.malcolm@dino.res", "Ian", "Malcolm", "555-0109", "https://storage.link/resumes/imalcolm_theory.pdf"),
    ("jenna.ortega@cinema.com", "Jenna", "Ortega", "555-0110", "https://storage.link/resumes/jortega_profile.pdf"),
    ("kevin.hart@comedy.org", "Kevin", "Hart", "555-0111", "https://storage.link/resumes/khart_bio.pdf"),
    ("laura.palmer@peaks.com", "Laura", "Palmer", "555-0112", "https://storage.link/resumes/lpalmer_v3.pdf"),
]

OFFICES = [
    ("PAR", "Paris", "France", 2.3522, 48.8566),
    ("TRS", "Trieste", "Italy", 13.7768, 45.6495),
    ("MIL", "Milan", "Italy", 9.1900, 45.4642),
    ("LON", "London", "United Kingdom", -0.1276, 51.5072),
    ("MAD", "Madrid", "Spain", -3.7038, 40.4168),
    ("MUC", "Munich", "Germany", 11.5820, 48.1351),
    ("NYC", "New York", "United States", -74.0060, 40.7128),
    ("SGP", "Singapore", "Singapore", 103.8198, 1.3521),
]

SKILLS = {
    "Programming": ["Python", "Java", "SQL", "Cypher", "JavaScript", "Go", "C++"],
    "Data": ["Machine Learning", "Data Visualization", "Statistics", "Data Engineering", "Graph Databases"],
    "Cloud": ["AWS", "Azure", "Kubernetes", "Terraform"],
    "Business": ["Project Management", "Negotiation", "Accounting", "Marketing Strategy", "Sales"],
    "People": ["Leadership", "Coaching", "Public Speaking", "Recruiting"],
    "Quality": ["Testing Automation", "ISO 9001", "Six Sigma"],
    "Language": ["English", "French", "Italian", "German", "Spanish"],
}
# Department id -> skill categories more frequent in that department
DEPARTMENT_SKILLS = {
    "d001": ["Business", "Language"],
    "d002": ["Business", "Data"],
    "d003": ["People", "Language"],
    "d004": ["Quality", "Programming"],
    "d005": ["Programming", "Cloud", "Data"],
    "d006": ["Quality", "Data"],
    "d007": ["Business", "Language"],
    "d008": ["Data", "Programming"],
    "d009": ["People", "Language"],
}
PROJECT_WORDS = [
    "Phoenix",
    "Atlas",
    "Orion",
    "Nova",
    "Helix",
    "Zephyr",
    "Titan",
    "Aurora",
    "Nimbus",
    "Vega",
    "Lynx",
    "Quasar",
]
PROJECT_DOMAINS = ["Migration", "Platform", "Analytics", "Campaign", "Audit", "Automation", "Portal", "Optimization"]
PROJECT_ROLES = ["Contributor", "Contributor", "Contributor", "Lead", "Reviewer"]
REQUISITION_TITLES = {
    "d001": "Marketing Specialist",
    "d002": "Financial Analyst",
    "d003": "HR Business Partner",
    "d004": "Production Engineer",
    "d005": "Software Engineer",
    "d006": "Quality Engineer",
    "d007": "Account Executive",
    "d008": "Research Scientist",
    "d009": "Customer Support Agent",
}
APPLICATION_STATUSES = ["applied", "screening", "interview", "offer", "rejected", "hired"]


def offices() -> list[dict]:
    return [
        {"code": code, "city": city, "country": country, "location": WGS84Point((lon, lat))}
        for code, city, country, lon, lat in OFFICES
    ]


def skills() -> list[dict]:
    return [{"name": name, "category": category} for category, names in SKILLS.items() for name in names]


def survey_responses(employees: list[dict], size: int = 1000, today: date | None = None) -> list[dict]:
    """Same distribution as neo4j_text2SQL data/addDBData.sql: 1000 random employees, gender-skewed 1-5 scores."""
    rng = random.Random(SEED)
    now = datetime.combine(today or date.today(), datetime.min.time(), tzinfo=UTC)
    sample = rng.sample(employees, min(size, len(employees)))

    def score(low: int, span: int) -> int:
        return rng.randint(low, low + span - 1)

    responses = []
    for i, e in enumerate(sample, start=1):
        female = e["gender"] == "F"
        responses.append(
            {
                "id": i,
                "employeeId": e["id"],
                "recruitmentScore": score(2, 4),
                "benefitsScore": score(2, 4) if female else score(1, 5),
                "communicationScore": score(2, 4) if female else score(1, 5),
                "supportScore": score(1, 5) if female else score(2, 4),
                "developmentScore": score(2, 4),
                "payrollScore": score(3, 3) if female else score(1, 4),
                "topStrength": f"Auto-generated positive feedback for {e['email']}",
                "improvementArea": f"Auto-generated improvement area for {e['email']}",
                "submittedAt": now - timedelta(seconds=rng.randint(0, 30 * 24 * 3600)),
            }
        )
    return responses


def recruitment(department_ids: list[str], today: date | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (candidates, requisitions, applications)."""
    rng = random.Random(SEED)
    today = today or date.today()
    candidates = [
        {"id": i, "email": email, "firstName": first, "lastName": last, "phone": phone, "resumeUrl": url}
        for i, (email, first, last, phone, url) in enumerate(CANDIDATES, start=1)
    ]
    requisitions = []
    for dept in sorted(department_ids):
        for j in range(rng.randint(1, 2)):
            opened = today - timedelta(days=rng.randint(10, 200))
            closed = opened + timedelta(days=rng.randint(20, 90)) if rng.random() < 0.3 else None
            requisitions.append(
                {
                    "id": len(requisitions) + 1,
                    "departmentId": dept,
                    "title": ("Senior " if j else "") + REQUISITION_TITLES.get(dept, "Specialist"),
                    "openedAt": opened,
                    "closedAt": closed if closed and closed < today else None,
                    "status": "closed" if closed and closed < today else "open",
                }
            )
    applications = []
    for candidate in candidates:
        for requisition in rng.sample(requisitions, rng.randint(1, 3)):
            applications.append(
                {
                    "candidateId": candidate["id"],
                    "requisitionId": requisition["id"],
                    "appliedAt": requisition["openedAt"] + timedelta(days=rng.randint(1, 10)),
                    "status": rng.choice(APPLICATION_STATUSES),
                }
            )
    return candidates, requisitions, applications


def projects(department_ids: list[str], count: int = 150) -> list[dict]:
    rng = random.Random(SEED)
    result = []
    for i in range(1, count + 1):
        start = date(1995, 1, 1) + timedelta(days=rng.randint(0, 2500))
        end = start + timedelta(days=rng.randint(60, 900))
        status = rng.choice(["completed", "completed", "active", "on_hold", "cancelled"])
        result.append(
            {
                "id": f"P{i:04d}",
                "name": f"{rng.choice(PROJECT_WORDS)} {rng.choice(PROJECT_DOMAINS)} {i}",
                "departmentId": rng.choice(sorted(department_ids)),
                "status": status,
                "startDate": start,
                "endDate": None if status == "active" else end,
                "budget": round(rng.uniform(20_000, 2_000_000), 2),
            }
        )
    return result


def employee_assignments(
    current_departments: dict[int, str], employee_ids: list[int], projects_list: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (employee office, employee skills, project members) relationships."""
    rng = random.Random(SEED)
    office_codes = [o[0] for o in OFFICES]
    office_weights = [30, 20, 15, 12, 8, 7, 5, 3]
    by_category = {category: names for category, names in SKILLS.items()}
    all_skills = [name for names in SKILLS.values() for name in names]
    projects_by_dept: dict[str, list[str]] = {}
    for p in projects_list:
        projects_by_dept.setdefault(p["departmentId"], []).append(p["id"])

    based_in, has_skill, works_on = [], [], []
    for employee_id in employee_ids:
        based_in.append({"employeeId": employee_id, "officeCode": rng.choices(office_codes, office_weights)[0]})
        dept = current_departments.get(employee_id)
        preferred = [s for c in DEPARTMENT_SKILLS.get(dept, []) for s in by_category[c]]
        chosen: set[str] = set()
        for _ in range(rng.randint(0, 4)):
            pool = preferred if preferred and rng.random() < 0.75 else all_skills
            chosen.add(rng.choice(pool))
        has_skill.extend({"employeeId": employee_id, "skill": s, "level": rng.randint(1, 5)} for s in chosen)
        if dept and rng.random() < 0.2 and projects_by_dept.get(dept):
            for project_id in rng.sample(projects_by_dept[dept], min(rng.randint(1, 2), len(projects_by_dept[dept]))):
                works_on.append(
                    {
                        "employeeId": employee_id,
                        "projectId": project_id,
                        "role": rng.choice(PROJECT_ROLES),
                        "allocation": rng.choice([0.2, 0.5, 0.8, 1.0]),
                    }
                )
    return based_in, has_skill, works_on
