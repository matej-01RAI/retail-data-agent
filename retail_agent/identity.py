"""Who is asking, and which slice of the product catalogue they may analyse."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

SCOPE_DIMENSIONS = ("brand", "category", "department")


@dataclass(frozen=True)
class ProductScope:
    """Product-level entitlement. Empty filters means the whole catalogue."""

    filters: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def unrestricted(self) -> bool:
        return not self.filters

    def describe(self) -> str:
        if self.unrestricted:
            return "all products"
        parts = [f"{dim} in ({', '.join(values)})" for dim, values in self.filters.items()]
        return " and ".join(parts)


@dataclass(frozen=True)
class User:
    id: str
    name: str
    title: str
    scope: ProductScope


class UnknownUser(LookupError):
    pass


def _parse_scope(raw: object, user_id: str) -> ProductScope:
    if raw == "all":
        return ProductScope()
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"user {user_id!r}: scope must be 'all' or a mapping of filters")
    filters: dict[str, tuple[str, ...]] = {}
    for dim, values in raw.items():
        if dim not in SCOPE_DIMENSIONS:
            raise ValueError(f"user {user_id!r}: unknown scope dimension {dim!r}")
        if not isinstance(values, list) or not values:
            raise ValueError(f"user {user_id!r}: scope.{dim} must be a non-empty list")
        filters[dim] = tuple(str(v) for v in values)
    return ProductScope(filters)


def load_users(path: Path) -> dict[str, User]:
    data = yaml.safe_load(path.read_text()) or {}
    users = {}
    for user_id, spec in (data.get("users") or {}).items():
        users[user_id] = User(
            id=user_id,
            name=spec.get("name", user_id),
            title=spec.get("title", ""),
            scope=_parse_scope(spec.get("scope"), user_id),
        )
    return users


def get_user(path: Path, user_id: str) -> User:
    users = load_users(path)
    if user_id not in users:
        raise UnknownUser(f"Unknown user {user_id!r}. Known users: {', '.join(sorted(users))}")
    return users[user_id]
