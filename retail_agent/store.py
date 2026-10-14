"""Application state in SQLite: saved reports (+ audit log), user preferences,
and the telemetry tables used by observability.py.

SQLite keeps the prototype dependency-free; production uses Cloud SQL for
PostgreSQL with the same tables (docs/architecture.md, "Data stores").
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id          TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    deleted_at  TEXT,
    delete_batch TEXT
);
CREATE INDEX IF NOT EXISTS reports_owner ON reports(owner, deleted_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    session_id TEXT,
    action     TEXT NOT NULL,
    details    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_prefs (
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id        TEXT PRIMARY KEY,
    ts             TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    trace_id       TEXT,
    question       TEXT,
    outcome        TEXT NOT NULL,
    error_type     TEXT,
    latency_ms     INTEGER,
    llm_requests   INTEGER DEFAULT 0,
    input_tokens   INTEGER DEFAULT 0,
    output_tokens  INTEGER DEFAULT 0,
    tool_calls     INTEGER DEFAULT 0,
    sql_queries    INTEGER DEFAULT 0,
    sql_failures   INTEGER DEFAULT 0,
    sql_rejected   INTEGER DEFAULT 0,
    empty_results  INTEGER DEFAULT 0,
    self_corrected INTEGER DEFAULT 0,
    bytes_processed INTEGER DEFAULT 0,
    pii_redactions INTEGER DEFAULT 0,
    models         TEXT,
    fallback_used  INTEGER DEFAULT 0,
    persona_version TEXT,
    precedents     TEXT,
    messages_json  TEXT
);
CREATE INDEX IF NOT EXISTS turns_ts ON turns(ts);

CREATE TABLE IF NOT EXISTS sql_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id      TEXT NOT NULL,
    ts           TEXT NOT NULL,
    purpose      TEXT,
    sql          TEXT,
    status       TEXT NOT NULL,
    error_kind   TEXT,
    error        TEXT,
    rows         INTEGER,
    bytes        INTEGER,
    elapsed_ms   INTEGER,
    attempts     INTEGER,
    cache_hit    INTEGER
);
CREATE INDEX IF NOT EXISTS sql_events_turn ON sql_events(turn_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()


# --------------------------------------------------------------------------- reports


@dataclass(frozen=True)
class Report:
    id: str
    owner: str
    session_id: str
    title: str
    body: str
    created_at: str

    def summary(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title, "created_at": self.created_at[:16].replace("T", " ")}


@dataclass(frozen=True)
class DeletionPlan:
    """The exact set of reports a deletion request resolves to, shown to the user."""

    owner: str
    selector: str  # human-readable description of what was asked
    reports: tuple[Report, ...]

    @property
    def ids(self) -> list[str]:
        return [r.id for r in self.reports]


class ReportStore:
    def __init__(self, db: Database, export_dir: Path | None = None):
        self.db = db
        self.export_dir = export_dir

    def _row(self, row: sqlite3.Row) -> Report:
        return Report(row["id"], row["owner"], row["session_id"], row["title"], row["body"], row["created_at"])

    def save(self, owner: str, session_id: str, title: str, body: str) -> Report:
        report = Report(f"R-{secrets.token_hex(3).upper()}", owner, session_id, title.strip()[:200], body, now_iso())
        self.db.execute(
            "INSERT INTO reports (id, owner, session_id, title, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (report.id, report.owner, report.session_id, report.title, report.body, report.created_at),
        )
        self.audit(owner, session_id, "report.save", {"report_id": report.id, "title": report.title})
        if self.export_dir:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            (self.export_dir / f"{report.id}.md").write_text(f"# {report.title}\n\n{report.body}\n")
        return report

    def find(
        self,
        owner: str,
        *,
        session_id: str | None = None,
        match_text: str | None = None,
        report_ids: list[str] | None = None,
    ) -> list[Report]:
        """Live reports owned by `owner` matching ALL given filters. Never another user's."""
        sql = "SELECT * FROM reports WHERE owner = ? AND deleted_at IS NULL"
        params: list[Any] = [owner]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if match_text:
            pattern = f"%{match_text.lower().replace('%', '').replace('_', '')}%"
            sql += " AND (LOWER(title) LIKE ? OR LOWER(body) LIKE ?)"
            params += [pattern, pattern]
        if report_ids is not None:
            if not report_ids:
                return []
            sql += f" AND id IN ({','.join('?' * len(report_ids))})"
            params += [r.upper() for r in report_ids]
        sql += " ORDER BY created_at"
        return [self._row(r) for r in self.db.query(sql, params)]

    def get(self, owner: str, report_id: str) -> Report | None:
        found = self.find(owner, report_ids=[report_id])
        return found[0] if found else None

    def plan_deletion(
        self,
        owner: str,
        current_session: str,
        *,
        match_text: str | None = None,
        this_conversation_only: bool = False,
        report_ids: list[str] | None = None,
        all_my_reports: bool = False,
    ) -> DeletionPlan:
        if not (match_text or this_conversation_only or report_ids or all_my_reports):
            raise ValueError("A deletion needs a selector (text, this conversation, ids or all).")
        reports = self.find(
            owner,
            session_id=current_session if this_conversation_only else None,
            match_text=match_text,
            report_ids=report_ids,
        )
        parts = []
        if report_ids:
            parts.append("ids " + ", ".join(report_ids))
        if match_text:
            parts.append(f'mentioning "{match_text}"')
        if this_conversation_only:
            parts.append("created in this conversation")
        if all_my_reports and not parts:
            parts.append("all of your reports")
        return DeletionPlan(owner, "reports " + " and ".join(parts), tuple(reports))

    def soft_delete(self, owner: str, session_id: str, report_ids: list[str], batch: str) -> list[Report]:
        """Mark exactly these reports deleted (owner-checked). Restorable via `restore`."""
        live = self.find(owner, report_ids=report_ids)
        if live:
            ids = [r.id for r in live]
            self.db.execute(
                f"UPDATE reports SET deleted_at = ?, delete_batch = ? WHERE owner = ? AND id IN ({','.join('?' * len(ids))})",
                [now_iso(), batch, owner, *ids],
            )
        self.audit(owner, session_id, "report.delete", {"batch": batch, "report_ids": [r.id for r in live]})
        return live

    def restore_last_batch(self, owner: str, session_id: str) -> list[str]:
        rows = self.db.query(
            "SELECT delete_batch FROM reports WHERE owner = ? AND deleted_at IS NOT NULL "
            "ORDER BY deleted_at DESC LIMIT 1",
            [owner],
        )
        if not rows:
            return []
        batch = rows[0]["delete_batch"]
        ids = [r["id"] for r in self.db.query("SELECT id FROM reports WHERE owner = ? AND delete_batch = ?", [owner, batch])]
        self.db.execute(
            "UPDATE reports SET deleted_at = NULL, delete_batch = NULL WHERE owner = ? AND delete_batch = ?",
            [owner, batch],
        )
        self.audit(owner, session_id, "report.restore", {"batch": batch, "report_ids": ids})
        return ids

    def audit(self, actor: str, session_id: str | None, action: str, details: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO audit_log (ts, actor, session_id, action, details) VALUES (?, ?, ?, ?, ?)",
            [now_iso(), actor, session_id, action, json.dumps(details)],
        )


# --------------------------------------------------------------------------- preferences

PREFERENCE_KEYS = {
    "format": "How results are laid out, e.g. 'tables', 'bullet points', 'short paragraphs'.",
    "depth": "How deep the analysis goes, e.g. 'headline numbers only', 'detailed with drivers'.",
    "visuals": "Charts vs text, e.g. 'prefers charts', 'text only'.",
    "tone": "Personal tone preference within the company persona, e.g. 'very concise'.",
    "focus": "Recurring interests, e.g. 'always compare with last year'.",
}


class PreferenceStore:
    def __init__(self, db: Database):
        self.db = db

    def get(self, user_id: str) -> dict[str, str]:
        rows = self.db.query("SELECT key, value FROM user_prefs WHERE user_id = ? ORDER BY key", [user_id])
        return {r["key"]: r["value"] for r in rows}

    def set(self, user_id: str, key: str, value: str) -> None:
        if key not in PREFERENCE_KEYS:
            raise KeyError(key)
        self.db.execute(
            "INSERT INTO user_prefs (user_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            [user_id, key, value.strip()[:200], now_iso()],
        )

    def clear(self, user_id: str) -> None:
        self.db.execute("DELETE FROM user_prefs WHERE user_id = ?", [user_id])
