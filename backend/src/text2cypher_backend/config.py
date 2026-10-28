from functools import lru_cache
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent
DATA_DIR = BACKEND_DIR / "data"


class Settings(BaseSettings):
    """Configuration read from the environment, the project-level .env and backend/.env (last wins)."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Neo4j
    neo4j_uri: str = "neo4j://localhost:7688"
    neo4j_username: str = "neo4j"
    neo4j_password: str = "password123"
    neo4j_database: str = "neo4j"

    # Cypher guard-rails
    cypher_read_only: bool = True
    cypher_max_rows: int = 1000
    cypher_max_rows_for_llm: int = 50
    cypher_timeout_seconds: float = 30

    # Schema introspection
    schema_sample_size: int = 1000
    schema_excluded_labels: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["SchemaElement", "GlossaryTerm"]
    )
    schema_descriptions_path: Path = DATA_DIR / "schema_descriptions.yaml"
    glossary_path: Path = DATA_DIR / "business_glossary.yaml"

    # Semantic layer search: top-k elements per index and minimum cosine score (0-1)
    semantic_top_k: int = 8
    semantic_min_score: float = 0.6

    # LLM
    local_model: bool = False
    openai_chat_model: str = "gpt-5.4-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    azure_openai_endpoint: str | None = None
    ollama_chat_model: str = "devstral-small-2"
    ollama_embedding_model: str = "nomic-embed-text"
    # Leave empty for reasoning models (gpt-5.x) that only accept the default temperature
    llm_temperature: float | None = None
    # low / medium / high for reasoning models (uses the Responses API), empty otherwise
    llm_reasoning_effort: str | None = None

    # Agent
    agent_recursion_limit: int = 30
    agent_history_messages: int = 10

    @field_validator("schema_excluded_labels", mode="before")
    @classmethod
    def _split_csv(cls, value):
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    # LangChain clients (OpenAI / Azure keys...) read os.environ: export the .env files too.
    # Real environment variables win, then backend/.env, then the project .env
    load_dotenv(BACKEND_DIR / ".env")
    load_dotenv(PROJECT_DIR / ".env")
    return Settings()
