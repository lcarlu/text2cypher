"""Thin HTTP client of the Text2Cypher API."""

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = httpx.Timeout(300.0, connect=5.0)


class ApiError(Exception):
    pass


def _request(method: str, path: str, **kwargs) -> dict | list:
    try:
        with httpx.Client(base_url=API_BASE, timeout=TIMEOUT) as client:
            response = client.request(method, path, **kwargs)
    except httpx.RequestError as e:
        raise ApiError(f"API unreachable at {API_BASE}: {e}") from e
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ApiError(f"{response.status_code}: {detail}")
    return response.json()


def health() -> dict:
    return _request("GET", "/health")


def chat(message: str, history: list[dict]) -> dict:
    return _request("POST", "/chat", json={"message": message, "history": history})


def run_cypher(query: str, parameters: dict | None = None, max_rows: int | None = None) -> dict:
    return _request("POST", "/cypher", json={"query": query, "parameters": parameters or {}, "max_rows": max_rows})


def schema_graph() -> dict:
    return _request("GET", "/schema/graph")


def schema_text() -> str:
    return _request("GET", "/schema")["text"]
