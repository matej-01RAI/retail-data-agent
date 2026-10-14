"""Shared fixtures: an offline dataset, services wired to temp dirs, a scripted model and a stub UI.

The scripted model (pydantic_ai FunctionModel) plays the LLM's part
deterministically, so these tests exercise the real agent loop, tools, SQL
guard, warehouse, approvals, stores and telemetry with no API key and no
Google Cloud project.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import pytest

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from retail_agent.agent import build_agent
from retail_agent.approvals import ApprovalRequest
from retail_agent.assistant import Assistant
from retail_agent.data.sample_db import build as build_sample
from retail_agent.identity import get_user
from retail_agent.llm import FaultInjectingModel
from retail_agent.runtime import build_services
from retail_agent.settings import load_settings

ORDERS = "`bigquery-public-data.thelook_ecommerce.orders`"
ITEMS = "`bigquery-public-data.thelook_ecommerce.order_items`"
PRODUCTS = "`bigquery-public-data.thelook_ecommerce.products`"
USERS = "`bigquery-public-data.thelook_ecommerce.users`"


@pytest.fixture(scope="session")
def sample_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("data") / "thelook_sample.duckdb"
    build_sample(path, users=800, seed=11)
    return path


@pytest.fixture
def settings(tmp_path, sample_db, monkeypatch):
    base = load_settings(env_file=os.devnull)
    persona = tmp_path / "persona.md"
    persona.write_text("Tone: concise.")
    monkeypatch.setenv("PERSONA_FILE", str(persona))
    return replace(
        base,
        warehouse="duckdb",
        duckdb_path=sample_db,
        data_dir=tmp_path,
        max_queries_per_turn=6,
        max_consecutive_sql_failures=3,
        max_llm_requests_per_turn=12,
        turn_timeout_s=30,
    )


@pytest.fixture
def services(settings):
    svc = build_services(settings)
    svc.warehouse.sleep = lambda s: None  # no real backoff waits in tests
    return svc


class StubUI:
    def __init__(self, answer: bool | Callable[[ApprovalRequest], bool] = True):
        self.answer = answer
        self.requests: list[ApprovalRequest] = []
        self.progress_lines: list[str] = []

    def progress(self, message: str) -> None:
        self.progress_lines.append(message)

    async def confirm(self, request: ApprovalRequest) -> bool:
        self.requests.append(request)
        return self.answer(request) if callable(self.answer) else self.answer


Step = Callable[[list[ModelMessage], AgentInfo], ModelResponse] | ModelResponse


class Script:
    """A FunctionModel that replays steps in order and records what the model saw."""

    def __init__(self, *steps: Step):
        self.steps = list(steps)
        self.seen: list[list[ModelMessage]] = []
        self.infos: list[AgentInfo] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen.append(list(messages))
        self.infos.append(info)
        if not self.steps:
            return ModelResponse(parts=[TextPart("(script exhausted)")])
        step = self.steps.pop(0)
        return step(messages, info) if callable(step) else step

    @property
    def model(self) -> FunctionModel:
        return FunctionModel(self, model_name="scripted")

    def last_parts(self, kind: type) -> list[Any]:
        return [p for p in self.seen[-1][-1].parts if isinstance(p, kind)]


def call(tool: str, **args: Any) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool, args)])


def say(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def last_tool_feedback(messages: list[ModelMessage]) -> str:
    """Content of the tool return / retry prompt the model just received."""
    parts = [p for p in messages[-1].parts if isinstance(p, (ToolReturnPart, RetryPromptPart))]
    return " ".join(str(p.content) for p in parts)


@pytest.fixture
def make_assistant(services, settings):
    def factory(script: Script, user: str = "ceo", ui: StubUI | None = None, model=None):
        primary = FaultInjectingModel(script.model) if model is None else None
        agent = build_agent(services, model or primary)
        return Assistant(services, agent, get_user(settings.users_path, user), ui or StubUI(), primary)

    return factory
