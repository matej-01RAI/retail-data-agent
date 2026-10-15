"""CLI commands: they must never crash the chat, whatever is stored or typed."""

from __future__ import annotations

import asyncio

from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import FunctionModel

from retail_agent import cli
from retail_agent.agent import build_agent

from .conftest import Script, StubUI, call, say


def run_command(text, assistant, settings, last_turn=None):
    with cli.console.capture() as captured:
        keep_going, assistant = cli._command(text, assistant, settings, last_turn)
    return keep_going, captured.get()


def test_prefs_with_markup_do_not_crash(make_assistant, services, settings):
    assistant = make_assistant(Script())
    services.prefs.set(assistant.user.id, "format", "[/red] tables [bold]")
    keep_going, out = run_command("/prefs", assistant, settings)
    assert keep_going and "[/red] tables" in out


def test_chaos_bad_arguments_print_usage(make_assistant, settings):
    assistant = make_assistant(Script())
    for text in ("/chaos bq x", "/chaos bq -3", "/chaos bq 999999", "/chaos nonsense 2"):
        keep_going, out = run_command(text, assistant, settings)
        assert keep_going and "Usage: /chaos" in out
    assert assistant.services.warehouse.faults.remaining == 0


def test_chaos_off_closes_the_circuit(make_assistant, settings):
    assistant = make_assistant(Script())
    breaker = assistant.services.warehouse.breaker
    for _ in range(breaker.threshold):
        breaker.record_failure()
    assert breaker.state == "open"
    run_command("/chaos off", assistant, settings)
    assert breaker.state == "closed"


async def test_trace_shows_only_your_own_turns(make_assistant, settings):
    mine = make_assistant(Script(say("ok")), user="tom")
    result = await mine.ask("hello")
    other = make_assistant(Script(), user="maria")
    _, out = run_command(f"/trace {result.turn_id}", other, settings)
    assert "No turn" in out
    _, out = run_command(f"/trace {result.turn_id[:6]}", mine, settings)
    assert result.turn_id in out


async def test_ctrl_c_ends_the_turn_and_still_reports_a_deletion(make_assistant, services):
    ui = StubUI(answer=True)
    assistant = make_assistant(Script(call("save_report", title="T", body_markdown="x"), say("saved")), ui=ui)
    await assistant.ask("save a report")
    steps = [ModelResponse(parts=call("delete_reports", this_conversation_only=True).parts), "slow"]

    async def model(messages, info):
        step = steps.pop(0)
        if step == "slow":
            await asyncio.sleep(30)
        return step

    assistant.agent = build_agent(services, FunctionModel(model))
    task = asyncio.create_task(assistant.ask("delete this conversation's reports"))
    while steps:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    task.cancel()
    result = await task
    assert result.outcome == "interrupted"
    assert "deleted 1 report(s)" in result.text
    assert services.reports.find("ceo") == []
    assert services.telemetry.turn_detail(result.turn_id)["outcome"] == "interrupted"
