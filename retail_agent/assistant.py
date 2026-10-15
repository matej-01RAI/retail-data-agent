"""One conversation with one user: the turn loop around the agent.

    user text
      -> input guard (injection / size / PII masking)       no model call if blocked
      -> agent run (tools: SQL, knowledge, reports, prefs)  bounded by UsageLimits + timeout
      -> approvals for paused tool calls (delete...)        the human confirms, not the model
      -> output PII scrub
      -> metrics + trace                                    every turn, whatever the outcome

Failures never escape to the UI as exceptions: each one becomes a clear
message and an `outcome` in the metrics, and the conversation history is only
advanced by turns that completed, so a failed turn can simply be retried.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage, UsageLimits

from . import approvals
from .agent import AgentOutput, Deps, Services
from .identity import User
from .llm import FaultInjectingModel
from .observability import TurnStats
from .safety.input_guard import screen
from .safety.pii import scrub

logger = logging.getLogger(__name__)

APPROVAL_HEADROOM = 3  # model requests always available after the user confirms an action


class UI(Protocol):
    def progress(self, message: str) -> None: ...

    async def confirm(self, request: approvals.ApprovalRequest) -> bool: ...


@dataclass
class TurnResult:
    text: str
    turn_id: str
    outcome: str
    latency_ms: int
    llm_requests: int = 0
    sql_queries: int = 0
    self_corrected: int = 0


FRIENDLY = {
    "limit": "That question needed more steps than I'm allowed for one answer, so I stopped to keep costs "
    "in check. Could you narrow it down (one metric, one period, or one segment at a time)?",
    "timeout": "That took too long and I stopped it. Please try again, or ask a narrower question.",
    "llm_unavailable": "The AI service is not responding right now (I tried a backup model too). Your "
    "conversation is kept; please try again in a minute.",
    "llm_config_error": "The AI service rejected the request because of a configuration problem (API key, "
    "model name or permissions), so retrying won't help. Please ask an administrator to check the setup "
    "(`python -m retail_agent doctor`).",
    "model_error": "I got confused putting that answer together. Please try again, perhaps rephrasing the question.",
    "interrupted": "Stopped, as you asked. Ask again whenever you're ready.",
    "error": "Something went wrong on my side and the answer was not completed. Please try again.",
}


class Assistant:
    def __init__(
        self,
        services: Services,
        agent: Agent[Deps, AgentOutput],
        user: User,
        ui: UI,
        primary_model: FaultInjectingModel | None = None,
    ):
        self.services = services
        self.agent = agent
        self.user = user
        self.ui = ui
        self.primary_model = primary_model
        self.session_id = uuid.uuid4().hex[:12]
        self.history: list[ModelMessage] = []

    def new_conversation(self) -> None:
        self.session_id = uuid.uuid4().hex[:12]
        self.history = []

    # ------------------------------------------------------------------ turn

    async def ask(self, text: str) -> TurnResult:
        settings = self.services.settings
        tel = self.services.telemetry
        turn_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        stats = TurnStats()
        persona_version = self.services.persona.current()[1]

        decision = screen(text)
        with tel.tracer.start_as_current_span(
            "agent_turn",
            attributes={"app.user": self.user.id, "app.session": self.session_id, "app.turn": turn_id},
        ) as span:
            trace_id = f"{span.get_span_context().trace_id:032x}"
            if not decision.allowed:
                span.set_attribute("app.outcome", "blocked")
                latency = int((time.monotonic() - started) * 1000)
                self._record(turn_id, trace_id, decision.text, "blocked", decision.reason, latency, stats,
                             RunUsage(), [], persona_version, 0)
                return TurnResult(decision.reply or "", turn_id, "blocked", latency)

            stats.pii_redactions += decision.pii_redacted
            deps = Deps(self.services, self.user, self.session_id, turn_id, stats, self.ui.progress)
            usage = RunUsage()
            limits = UsageLimits(request_limit=settings.max_llm_requests_per_turn, total_tokens_limit=400_000)
            primary_before = self.primary_model.served if self.primary_model else 0
            outcome, error_type, reply = "answered", None, ""
            new_messages: list[ModelMessage] = []
            try:
                output, all_messages = await self._run(decision.text, deps, usage, limits)
                reply = output
                new_messages = all_messages[len(self.history):]
                self.history = all_messages
            except UsageLimitExceeded as e:
                outcome, error_type = "limit", f"UsageLimitExceeded: {e}"
            except asyncio.TimeoutError:
                outcome, error_type = "timeout", "TimeoutError"
            except (FallbackExceptionGroup, ModelAPIError) as e:
                # 400/401/403/404 from every model = configuration (key, model name), not an outage.
                outcome = "llm_config_error" if _config_error(e) else "llm_unavailable"
                error_type = _describe(e)
            except UnexpectedModelBehavior as e:
                outcome, error_type = "model_error", _describe(e)
            except asyncio.CancelledError:
                # Ctrl-C in the CLI: end this turn cleanly (recorded, side effects reported), keep the session.
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                outcome, error_type = "interrupted", "Interrupted by the user"
            except Exception as e:  # never crash the UI
                logger.exception("turn %s failed", turn_id)
                outcome, error_type = "error", _describe(e)
            if outcome != "answered":
                reply = FRIENDLY[outcome]
                span.set_attribute("app.error", error_type or "")
                if stats.side_effects:
                    # A confirmed action already ran (e.g. reports deleted): say so, and record the
                    # turn in the history so the next question knows too.
                    reply += "\n\n**Done before this stopped:** " + " ".join(stats.side_effects)
                    new_messages = [
                        ModelRequest(parts=[UserPromptPart(decision.text)]),
                        ModelResponse(parts=[TextPart(reply)]),
                    ]
                    self.history = [*self.history, *new_messages]

            clean = scrub(reply)
            stats.pii_redactions += clean.redacted
            span.set_attribute("app.outcome", outcome)
            latency = int((time.monotonic() - started) * 1000)
            served = (self.primary_model.served - primary_before) if self.primary_model else usage.requests
            fallback = int(usage.requests > served)
            self._record(turn_id, trace_id, decision.text, outcome, error_type, latency, stats, usage,
                         new_messages, persona_version, fallback)
        return TurnResult(clean.text, turn_id, outcome, latency, usage.requests, stats.sql_queries, stats.self_corrected)

    async def _run(
        self, prompt: str, deps: Deps, usage: RunUsage, limits: UsageLimits
    ) -> tuple[str, list[ModelMessage]]:
        timeout = self.services.settings.turn_timeout_s
        history = self._trimmed_history()
        result = await asyncio.wait_for(
            self.agent.run(prompt, message_history=history, deps=deps, usage=usage, usage_limits=limits), timeout
        )
        # Paused for approval: resolve with the human (no timeout on their thinking time), then resume.
        while isinstance(result.output, DeferredToolRequests):
            deferred = await self._resolve_approvals(result.output, deps)
            # Headroom for the model to report the outcome, however many requests the turn has used.
            resume_limits = UsageLimits(
                request_limit=max(limits.request_limit or 0, usage.requests + APPROVAL_HEADROOM),
                total_tokens_limit=limits.total_tokens_limit,
            )
            result = await asyncio.wait_for(
                self.agent.run(
                    message_history=result.all_messages(),
                    deferred_tool_results=deferred,
                    deps=deps,
                    usage=usage,
                    usage_limits=resume_limits,
                ),
                timeout,
            )
        return str(result.output), result.all_messages()

    async def _resolve_approvals(self, requests: DeferredToolRequests, deps: Deps) -> DeferredToolResults:
        out = DeferredToolResults()
        for call in requests.calls:  # external tools are not used; refuse defensively
            out.calls[call.tool_call_id] = ToolDenied("This tool cannot run here.")
        for call in requests.approvals:
            policy = approvals.POLICIES.get(call.tool_name)
            if policy is None:
                out.approvals[call.tool_call_id] = ToolDenied("This action is not permitted.")
                continue
            try:
                args = call.args_as_dict()
            except Exception:
                out.approvals[call.tool_call_id] = ToolDenied("The request was malformed. Nothing was changed.")
                continue
            plan = policy.plan(args, deps)
            store = self.services.reports
            if isinstance(plan, ToolDenied):
                store.audit(deps.user.id, deps.session_id, f"{call.tool_name}.nothing_to_confirm", {"args": args})
                out.approvals[call.tool_call_id] = plan
                continue
            store.audit(deps.user.id, deps.session_id, f"{call.tool_name}.confirmation_requested", plan.audit)
            confirmed = await self.ui.confirm(plan)
            store.audit(
                deps.user.id, deps.session_id,
                f"{call.tool_name}.{'confirmed' if confirmed else 'cancelled'}", plan.audit,
            )
            out.approvals[call.tool_call_id] = approvals.approved(plan) if confirmed else approvals.cancelled()
        return out

    def _trimmed_history(self) -> list[ModelMessage]:
        """Keep the last N user turns (cut only at a user prompt, so tool call/return pairs stay intact)."""
        keep = self.services.settings.history_turns
        starts = [
            i for i, m in enumerate(self.history)
            if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)
        ]
        if len(starts) <= keep:
            return list(self.history)
        return self.history[starts[-keep]:]

    # ------------------------------------------------------------------ metrics

    def _record(
        self,
        turn_id: str,
        trace_id: str,
        question: str,
        outcome: str,
        error_type: str | None,
        latency_ms: int,
        stats: TurnStats,
        usage: RunUsage,
        messages: list[ModelMessage],
        persona_version: str,
        fallback_used: int,
    ) -> None:
        models = sorted({m.model_name for m in messages if isinstance(m, ModelResponse) and m.model_name})
        messages_json = scrub_messages(messages)
        self.services.telemetry.record_turn(
            turn_id=turn_id,
            user_id=self.user.id,
            session_id=self.session_id,
            trace_id=trace_id,
            question=scrub(question).text[:2000],  # blocked prompts arrive unmasked
            outcome=outcome,
            error_type=(error_type or None) and scrub(error_type).text[:500],
            latency_ms=latency_ms,
            llm_requests=usage.requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            tool_calls=usage.tool_calls,
            sql_queries=stats.sql_queries,
            sql_failures=stats.sql_failures,
            sql_rejected=stats.sql_rejected,
            empty_results=stats.empty_results,
            self_corrected=stats.self_corrected,
            bytes_processed=stats.bytes_processed,
            pii_redactions=stats.pii_redactions,
            models=json.dumps(models),
            fallback_used=fallback_used,
            persona_version=persona_version,
            precedents=json.dumps(stats.precedents),
            messages_json=messages_json,
        )


def scrub_messages(messages: list[ModelMessage]) -> str | None:
    """The turn's messages as JSON for /trace, with PII masked in every prompt, reply, tool call and result."""
    if not messages:
        return None

    def clean(value, free_text: bool = False):
        if isinstance(value, dict):
            return {k: clean(v, free_text or k in ("content", "args")) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v, free_text) for v in value]
        if free_text and isinstance(value, str):
            return scrub(value).text
        return value

    try:
        return json.dumps(clean(ModelMessagesTypeAdapter.dump_python(messages, mode="json")))
    except Exception:  # noqa: BLE001 - observability must not fail the turn
        logger.warning("could not serialise the turn's messages", exc_info=True)
        return None


def _config_error(error: BaseException) -> bool:
    errors = list(error.exceptions) if isinstance(error, BaseExceptionGroup) else [error]
    codes = [getattr(e, "status_code", None) for e in errors]
    return bool(codes) and all(code in (400, 401, 403, 404) for code in codes)


def _describe(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        inner = "; ".join(_describe(e) for e in error.exceptions)
        return f"{type(error).__name__}[{inner}]"
    return f"{type(error).__name__}: {str(error)[:300]}"
