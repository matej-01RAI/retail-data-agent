"""Runtime settings, read from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # LLM
    gemini_api_key: str | None
    model: str
    fallback_models: list[str]
    llm_requests_per_minute: int

    # Data warehouse
    warehouse: str  # "bigquery" or "duckdb" (offline synthetic sample)
    gcp_project: str | None
    dataset: str
    max_bytes_billed: int
    query_timeout_s: float
    duckdb_path: Path

    # Per-turn budgets (cost and loop control)
    max_llm_requests_per_turn: int
    max_queries_per_turn: int
    max_consecutive_sql_failures: int
    max_rows_to_llm: int
    turn_timeout_s: float
    history_turns: int

    # Files
    data_dir: Path
    config_dir: Path = field(default=ROOT / "config")
    knowledge_dir: Path = field(default=ROOT / "knowledge")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def traces_path(self) -> Path:
        return self.data_dir / "traces" / "spans.jsonl"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def persona_path(self) -> Path:
        return Path(os.getenv("PERSONA_FILE", self.config_dir / "persona.md"))

    @property
    def users_path(self) -> Path:
        return self.config_dir / "users.yaml"


def _service_account_from_env(data_dir: Path) -> None:
    """Support credentials passed as an env var (CI, cloud sandboxes) instead of a key file.

    If GCP_SA_KEY_JSON holds a service-account key and GOOGLE_APPLICATION_CREDENTIALS is
    not set, write the key to a private file under DATA_DIR and point ADC at it.
    """
    raw = os.getenv("GCP_SA_KEY_JSON")
    if not raw or os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    path = data_dir / "gcp-service-account.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(raw)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)


def load_settings(env_file: str | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env", override=False)
    data_dir = Path(os.getenv("DATA_DIR", ROOT / ".data"))
    _service_account_from_env(data_dir)
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
        # Aliases keep the prototype working as Google rotates model versions.
        # Pin a dated model in production (see docs/architecture.md).
        model=os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
        fallback_models=_list("GEMINI_FALLBACK_MODELS", ["gemini-flash-lite-latest"]),
        llm_requests_per_minute=_int("LLM_REQUESTS_PER_MINUTE", 10),
        warehouse=os.getenv("WAREHOUSE", "bigquery").lower(),
        gcp_project=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
        dataset=os.getenv("BQ_DATASET", "bigquery-public-data.thelook_ecommerce"),
        max_bytes_billed=_int("MAX_BYTES_BILLED", 1_000_000_000),
        query_timeout_s=_float("QUERY_TIMEOUT_S", 60.0),
        duckdb_path=Path(os.getenv("DUCKDB_PATH", data_dir / "thelook_sample.duckdb")),
        max_llm_requests_per_turn=_int("MAX_LLM_REQUESTS_PER_TURN", 16),
        max_queries_per_turn=_int("MAX_QUERIES_PER_TURN", 10),
        max_consecutive_sql_failures=_int("MAX_SQL_ATTEMPTS", 3),
        max_rows_to_llm=_int("MAX_ROWS_TO_LLM", 50),
        turn_timeout_s=_float("TURN_TIMEOUT_S", 240.0),
        history_turns=_int("HISTORY_TURNS", 8),
        data_dir=data_dir,
    )
