"""Static read-only check on Cypher text. It is a fast first barrier: the executor also checks the
EXPLAIN query type and runs queries in READ access mode, so the server is the final authority."""

import re

_WRITE_CLAUSES = [
    r"CREATE",
    r"MERGE",
    r"DELETE",
    r"DETACH",
    r"SET",
    r"REMOVE",
    r"DROP",
    r"FOREACH",
    r"LOAD\s+CSV",
    r"IN\s+TRANSACTIONS",
    r"GRANT",
    r"DENY",
    r"REVOKE",
    r"ALTER",
    r"RENAME",
    r"START\s+DATABASE",
    r"STOP\s+DATABASE",
    r"TERMINATE",
]
# A clause keyword is not preceded by "." (apoc.create.vNode), ":" (label) or "$" (parameter)
_WRITE_PATTERN = re.compile(r"(?<![.\w:$])(" + "|".join(_WRITE_CLAUSES) + r")\b", re.IGNORECASE)
_LITERALS_AND_COMMENTS = re.compile(
    r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`[^`]*`|//[^\n]*|/\*.*?\*/""",
    re.DOTALL,
)
_CODE_FENCE = re.compile(r"^\s*```(?:cypher)?\s*|\s*```\s*$", re.IGNORECASE)


def clean_query(query: str) -> str:
    """Remove markdown fences and trailing semicolons often added by LLMs."""
    return _CODE_FENCE.sub("", query).strip().rstrip(";").strip()


def find_write_clause(query: str) -> str | None:
    """Return the first write clause found outside strings, identifiers and comments, or None."""
    stripped = _LITERALS_AND_COMMENTS.sub(" ", query)
    match = _WRITE_PATTERN.search(stripped)
    return match.group(1).upper() if match else None
