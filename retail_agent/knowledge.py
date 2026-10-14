"""The agent's knowledge: the data dictionary (semantic layer) and the Golden
Knowledge trios (question -> SQL -> analyst report) with lexical retrieval.

Production swaps the in-memory BM25 for hybrid (BM25 + embedding) search over
the Golden bucket index; the interface (`search(question, k)`) stays the same.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    description: str = ""
    pii: bool = False
    aggregate_only: bool = False  # quasi-identifier: only as an aggregate across customers
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    grain: str
    columns: dict[str, Column]

    @property
    def safe_columns(self) -> list[str]:
        return [c.name for c in self.columns.values() if not c.pii]

    @property
    def pii_columns(self) -> list[str]:
        return [c.name for c in self.columns.values() if c.pii]


@dataclass
class DataDictionary:
    dataset: str
    tables: dict[str, Table]
    relationships: list[str] = field(default_factory=list)
    metrics: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> DataDictionary:
        raw = yaml.safe_load(path.read_text())
        tables = {}
        for tname, tspec in raw["tables"].items():
            cols = {}
            for cname, cspec in tspec["columns"].items():
                cols[cname] = Column(
                    name=cname,
                    type=cspec.get("type", ""),
                    description=cspec.get("description", ""),
                    pii=bool(cspec.get("pii", False)),
                    aggregate_only=bool(cspec.get("aggregate_only", False)),
                    values=tuple(cspec.get("values", ())),
                )
            tables[tname] = Table(tname, tspec.get("description", ""), tspec.get("grain", ""), cols)
        return cls(
            dataset=raw["dataset"],
            tables=tables,
            relationships=list(raw.get("relationships", [])),
            metrics=dict(raw.get("metrics", {})),
            notes=list(raw.get("notes", [])),
        )

    @property
    def pii_columns(self) -> set[str]:
        return {c for t in self.tables.values() for c in t.pii_columns}

    def fq(self, table: str) -> str:
        return f"`{self.dataset}.{table}`"

    def prompt_summary(self) -> str:
        """Compact schema for the system prompt (PII columns are not listed as usable)."""
        lines = [f"Dataset `{self.dataset}` (BigQuery). Always use fully-qualified table names."]
        for t in self.tables.values():
            cols = []
            for c in t.columns.values():
                if c.pii:
                    continue
                extra = f" in {list(c.values)}" if c.values else ""
                extra += " (aggregates only)" if c.aggregate_only else ""
                cols.append(f"{c.name} {c.type}{extra}")
            lines.append(f"- {self.fq(t.name)}: {t.description} Columns: {', '.join(cols)}")
            if t.pii_columns:
                lines.append(f"  RESTRICTED (PII, never available): {', '.join(t.pii_columns)}")
        lines.append("Joins: " + "; ".join(self.relationships))
        lines.append("Business definitions (use these):")
        lines.extend(f"- {k}: {v}" for k, v in self.metrics.items())
        lines.extend(f"Note: {n}" for n in self.notes)
        return "\n".join(lines)

    def describe(self, table: str | None = None) -> dict[str, Any]:
        """Structured description for the `describe_data` tool."""
        names = [table] if table else list(self.tables)
        out: dict[str, Any] = {"dataset": self.dataset, "tables": {}}
        for name in names:
            t = self.tables.get(name)
            if t is None:
                raise KeyError(name)
            out["tables"][name] = {
                "description": t.description,
                "grain": t.grain,
                "columns": {
                    c.name: {
                        "type": c.type,
                        "description": c.description,
                        **({"values": list(c.values)} if c.values else {}),
                        **({"rule": "aggregates across customers only"} if c.aggregate_only else {}),
                    }
                    for c in t.columns.values()
                    if not c.pii
                },
                "restricted_pii_columns": t.pii_columns,
            }
        if table is None:
            out["relationships"] = self.relationships
            out["metric_definitions"] = self.metrics
            out["notes"] = self.notes
        return out


# --------------------------------------------------------------------------- golden knowledge

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by did do does for from how i in is it me my of on or our the their this to "
    "us we what which who why with you your vs".split()
)


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass(frozen=True)
class Trio:
    id: str
    question: str
    sql: str
    report: str
    tags: tuple[str, ...] = ()

    def as_prompt(self) -> str:
        return (
            f"[{self.id}] Q: {self.question}\n"
            f"SQL:\n{self.sql.strip()}\n"
            f"Analyst interpretation: {self.report.strip()}"
        )


class GoldenKnowledge:
    """BM25 over question + tags of the curated trios."""

    def __init__(self, trios: list[Trio], k1: float = 1.4, b: float = 0.75):
        self.trios = trios
        self.k1, self.b = k1, b
        self._docs = [_tokens(t.question + " " + " ".join(t.tags)) for t in trios]
        self._avgdl = sum(map(len, self._docs)) / max(len(self._docs), 1)
        df: Counter[str] = Counter()
        for doc in self._docs:
            df.update(set(doc))
        n = len(self._docs)
        self._idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    @classmethod
    def load(cls, path: Path) -> GoldenKnowledge:
        raw = yaml.safe_load(path.read_text()) or {}
        trios = [
            Trio(
                id=item["id"],
                question=item["question"],
                sql=item["sql"],
                report=item["report"],
                tags=tuple(item.get("tags", ())),
            )
            for item in raw.get("trios", [])
        ]
        return cls(trios)

    def search(self, query: str, k: int = 3, min_score: float = 1.0) -> list[tuple[Trio, float]]:
        q = _tokens(query)
        scored = []
        for trio, doc in zip(self.trios, self._docs):
            tf = Counter(doc)
            score = 0.0
            for term in q:
                if term not in tf:
                    continue
                f = tf[term]
                denom = f + self.k1 * (1 - self.b + self.b * len(doc) / self._avgdl)
                score += self._idf.get(term, 0.0) * f * (self.k1 + 1) / denom
            if score >= min_score:
                scored.append((trio, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]
