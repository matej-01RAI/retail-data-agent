"""Report search, the deletion preview, history trimming and stopped turns."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic_ai import ToolDenied
from pydantic_ai.exceptions import ToolFailed

from retail_agent.agent import Deps, _run_sql
from retail_agent.approvals import DeleteReportsPolicy
from retail_agent.identity import get_user
from retail_agent.observability import TurnStats

from .conftest import ITEMS, Script, say


def make_deps(services, settings, user="ceo", session="s1"):
    return Deps(services, get_user(settings.users_path, user), session, "t1", TurnStats(), lambda m: None)


def test_report_search_is_literal_and_unicode_aware(services):
    store = services.reports
    store.save("ceo", "s1", "Café São Paulo review", "Sales up")
    store.save("ceo", "s1", "Margin 50% target", "x")
    store.save("ceo", "s1", "Margin 50 target", "x")
    store.save("ceo", "s1", "MÜLLER account", "x")
    assert [r.title for r in store.find("ceo", match_text="SÃO PAULO")] == ["Café São Paulo review"]
    assert [r.title for r in store.find("ceo", match_text="50%")] == ["Margin 50% target"]
    assert store.find("ceo", match_text="_") == []
    assert [r.title for r in store.find("ceo", match_text="müller")] == ["MÜLLER account"]


def test_deletion_preview_reads_arguments_like_the_tool(services, settings):
    store = services.reports
    store.save("ceo", "s1", "Acme Q1", "x")
    store.save("ceo", "s1", "Other", "x")
    deps = make_deps(services, settings)
    plan = DeleteReportsPolicy().plan({"match_text": "Acme", "all_my_reports": "false"}, deps)
    assert [i.split()[1] for i in plan.items] == ["Acme"]
    assert isinstance(DeleteReportsPolicy().plan({"report_ids": 7}, deps), ToolDenied)
    assert isinstance(DeleteReportsPolicy().plan({"all_my_reports": "maybe"}, deps), ToolDenied)


def test_a_stopped_turn_starts_no_query_and_changes_nothing(services, settings):
    deps = make_deps(services, settings)
    deps.stop()
    with pytest.raises(ToolFailed):
        _run_sql(deps, f"SELECT COUNT(*) FROM {ITEMS}", "count", False)
    assert deps.stats.sql_queries == 0


async def test_this_turns_messages_are_recorded_after_history_is_trimmed(make_assistant, services, settings):
    services.settings = replace(settings, history_turns=1)
    script = Script(say("one"), say("two"), say("three"))
    assistant = make_assistant(script)
    for question in ("first question", "second question", "third question"):
        result = await assistant.ask(question)
    detail = services.telemetry.turn_detail(result.turn_id)
    assert "third question" in detail["messages_json"] and "three" in detail["messages_json"]
    assert "second question" not in detail["messages_json"]
    assert "second question" in str(script.seen[-1]) and "first question" not in str(script.seen[-1])


async def test_history_turns_zero_sends_no_history(make_assistant, services, settings):
    services.settings = replace(settings, history_turns=0)
    script = Script(say("one"), say("two"))
    assistant = make_assistant(script)
    await assistant.ask("first question")
    await assistant.ask("second question")
    assert "first question" not in str(script.seen[-1])
