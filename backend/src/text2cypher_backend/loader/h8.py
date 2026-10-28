"""Parse the h8 employees PostgreSQL dump (https://github.com/h8/employees-database) without PostgreSQL."""

import bz2
import hashlib
import logging
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Same file from two sources: the text2cypher release mirror first, then the h8 repository pinned on a commit.
# The dataset is shared under Creative Commons Attribution-Share Alike 3.0 (see README credits).
DUMP_URLS = [
    "https://github.com/lcarlu/text2cypher/releases/download/data-v1/employees_data.sql.bz2",
    "https://raw.githubusercontent.com/h8/employees-database/2d6007094f93bd887d6e2b8a409967efd75fc587/employees_data.sql.bz2",
]
DUMP_SHA256 = "1f76fc853eacbc6730b4b0f83eccb029ccee1f24b441737dbd88d692bd126f6c"
EMAIL_DOMAIN = "@semantictech.com"
CURRENT_TO_DATE = date(9999, 1, 1)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, target: Path) -> None:
    tmp = target.with_suffix(target.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    checksum = _sha256(tmp)
    if checksum != DUMP_SHA256:
        tmp.unlink()
        raise ValueError(f"checksum mismatch ({checksum})")
    tmp.replace(target)


def download_dump(cache_dir: Path) -> Path:
    """Download (once) the dump, check its SHA-256 and decompress it in cache_dir. Returns the .sql path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    sql_path = cache_dir / "employees_data.sql"
    if sql_path.exists():
        return sql_path
    bz2_path = cache_dir / "employees_data.sql.bz2"
    if not bz2_path.exists() or _sha256(bz2_path) != DUMP_SHA256:
        errors = []
        for url in DUMP_URLS:
            logger.info("Downloading %s", url)
            try:
                _download(url, bz2_path)
                break
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("Download failed from %s: %s", url, e)
                errors.append(f"{url}: {e}")
        else:
            raise RuntimeError("Could not download the h8 employees dump:\n" + "\n".join(errors))
    logger.info("Decompressing %s", bz2_path)
    tmp = sql_path.with_suffix(".sql.part")
    with bz2.open(bz2_path, "rt", encoding="utf-8") as src, open(tmp, "w", encoding="utf-8") as dst:
        for line in src:
            dst.write(line)
    tmp.replace(sql_path)
    return sql_path


def iter_table(sql_path: Path, table: str) -> Iterator[dict[str, str]]:
    """Stream the rows of one `COPY <table> (...) FROM stdin;` block as dicts of raw strings."""
    header = f"COPY {table} ("
    with open(sql_path, encoding="utf-8") as f:
        columns = None
        for line in f:
            if columns is None:
                if line.startswith(header):
                    columns = [c.strip() for c in line[len(header) : line.index(")")].split(",")]
                continue
            if line.startswith("\\."):
                return
            values = line.rstrip("\n").split("\t")
            yield {col: (None if val == "\\N" else val) for col, val in zip(columns, values, strict=True)}


def _period(row: dict) -> dict:
    to_date = date.fromisoformat(row["to_date"])
    return {
        "fromDate": date.fromisoformat(row["from_date"]),
        "toDate": to_date,
        "isCurrent": to_date == CURRENT_TO_DATE,
    }


def read_departments(sql_path: Path) -> list[dict]:
    return [{"id": r["id"], "name": r["dept_name"]} for r in iter_table(sql_path, "department")]


def read_employees(sql_path: Path, limit: int | None = None) -> list[dict]:
    rows = sorted(iter_table(sql_path, "employee"), key=lambda r: int(r["id"]))
    if limit:
        # Keep department managers in the sample so that MANAGES / REPORTS_TO exist
        manager_ids = {r["employee_id"] for r in iter_table(sql_path, "department_manager")}
        rows = rows[:limit] + [r for r in rows[limit:] if r["id"] in manager_ids]
    # Same email rule as neo4j_text2SQL: first.last[-rank]@domain, rank by id among homonyms
    seen: dict[tuple[str, str], int] = {}
    employees = []
    for r in rows:
        key = (r["first_name"].lower(), r["last_name"].lower())
        seen[key] = seen.get(key, 0) + 1
        suffix = f"-{seen[key]}" if seen[key] > 1 else ""
        employees.append(
            {
                "id": int(r["id"]),
                "firstName": r["first_name"],
                "lastName": r["last_name"],
                "gender": r["gender"],
                "birthDate": date.fromisoformat(r["birth_date"]),
                "hireDate": date.fromisoformat(r["hire_date"]),
                "email": f"{key[0]}.{key[1]}{suffix}{EMAIL_DOMAIN}".replace(" ", ""),
            }
        )
    return employees


def iter_department_links(sql_path: Path, table: str, employee_ids: set[int]) -> Iterator[dict]:
    """department_employee / department_manager rows."""
    for r in iter_table(sql_path, table):
        employee_id = int(r["employee_id"])
        if employee_id in employee_ids:
            yield {"employeeId": employee_id, "departmentId": r["department_id"], **_period(r)}


def iter_salaries(sql_path: Path, employee_ids: set[int]) -> Iterator[dict]:
    for r in iter_table(sql_path, "salary"):
        employee_id = int(r["employee_id"])
        if employee_id in employee_ids:
            yield {"employeeId": employee_id, "amount": int(float(r["amount"])), **_period(r)}


def iter_titles(sql_path: Path, employee_ids: set[int]) -> Iterator[dict]:
    for r in iter_table(sql_path, "title"):
        employee_id = int(r["employee_id"])
        if employee_id in employee_ids:
            yield {"employeeId": employee_id, "title": r["title"], **_period(r)}
