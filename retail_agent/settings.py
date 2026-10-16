"""Runtime settings, read from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class SettingsError(ValueError):
    """A setting has a value the app cannot use; the message says which and why."""


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw.replace("_", ""))
    except ValueError:
        raise SettingsError(f"{name} must be a whole number, got {raw!r}.") from None
    if value < minimum:
        raise SettingsError(f"{name} must be at least {minimum}, got {value}.")
    return value


def _float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a number, got {raw!r}.") from None
    if value < minimum:
        raise SettingsError(f"{name} must be at least {minimum}, got {value}.")
    return value


def _path(raw: str | os.PathLike) -> Path:
    """~ is expanded; a relative path is relative to the project folder (where .env lives), not the
    directory the command happens to be run from."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else ROOT / path


def log_level() -> str:
    """LOG_LEVEL (from the environment or .env), any case; an unknown name falls back to WARNING."""
    level = (os.getenv("LOG_LEVEL") or "WARNING").strip().upper()
    return level if level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "WARNING"


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
    # A service-account key (JSON) for environments without a gcloud login. Used in memory only,
    # never written to disk, and kept out of repr() so it cannot leak into logs.
    gcp_service_account_json: str | None = field(default=None, repr=False)
    # HTTP API bearer tokens, "user_id:token,..." (retail_agent/api.py). Never logged.
    api_tokens: str | None = field(default=None, repr=False)

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
        return _path(os.getenv("PERSONA_FILE") or self.config_dir / "persona.md")

    @property
    def users_path(self) -> Path:
        return self.config_dir / "users.yaml"


def load_settings(env_file: str | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env", override=False)
    data_dir = _path(os.getenv("DATA_DIR") or ROOT / ".data")
    warehouse = (os.getenv("WAREHOUSE") or "bigquery").strip().lower()
    if warehouse not in ("bigquery", "duckdb"):
        raise SettingsError(f"WAREHOUSE must be 'bigquery' or 'duckdb', got {warehouse!r}.")
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
        # Aliases keep the prototype working as Google rotates model versions.
        # Pin a dated model in production (see docs/architecture.md).
        model=os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
        fallback_models=_list("GEMINI_FALLBACK_MODELS", ["gemini-flash-lite-latest"]),
        llm_requests_per_minute=_int("LLM_REQUESTS_PER_MINUTE", 10, minimum=1),
        warehouse=warehouse,
        gcp_project=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
        dataset=os.getenv("BQ_DATASET", "bigquery-public-data.thelook_ecommerce"),
        max_bytes_billed=_int("MAX_BYTES_BILLED", 1_000_000_000, minimum=1),
        query_timeout_s=_float("QUERY_TIMEOUT_S", 60.0, minimum=1),
        duckdb_path=_path(os.getenv("DUCKDB_PATH") or data_dir / "thelook_sample.duckdb"),
        max_llm_requests_per_turn=_int("MAX_LLM_REQUESTS_PER_TURN", 16, minimum=2),
        max_queries_per_turn=_int("MAX_QUERIES_PER_TURN", 10, minimum=1),
        max_consecutive_sql_failures=_int("MAX_SQL_ATTEMPTS", 3, minimum=1),
        max_rows_to_llm=_int("MAX_ROWS_TO_LLM", 50, minimum=1),
        turn_timeout_s=_float("TURN_TIMEOUT_S", 240.0, minimum=5),
        history_turns=_int("HISTORY_TURNS", 8),
        data_dir=data_dir,
        gcp_service_account_json=os.getenv("GCP_SA_KEY_JSON") or None,
        api_tokens=os.getenv("API_TOKENS") or None,
    )
