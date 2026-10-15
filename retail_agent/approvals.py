"""Human-in-the-loop approval for actions that change or send things.

A tool declared with `requires_approval=True` never runs on the model's say-so.
When the model calls it, the agent run pauses and returns the call to the
application. The matching `ApprovalPolicy` (code, not the model) resolves
what the call would actually do, the user sees that and confirms or cancels,
and only then does the run resume.

On confirmation the call is executed with arguments *pinned by the policy*
(`override_args`), e.g. the exact report IDs the user saw. A request like
"delete reports mentioning X" therefore cannot delete a report that was
created between the preview and the confirmation, or anything the model
adds afterwards.

New destructive or outward-facing capabilities (emailing a report, sharing,
scheduling) register a policy here; nothing else in the flow changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ValidationError
from pydantic_ai import ToolApproved, ToolDenied

if TYPE_CHECKING:
    from .agent import Deps


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    title: str
    items: list[str]
    warning: str
    confirm_phrase: str
    override_args: dict[str, Any]
    audit: dict[str, Any] = field(default_factory=dict)


class ApprovalPolicy(Protocol):
    tool_name: str

    def plan(self, args: dict[str, Any], deps: Deps) -> ApprovalRequest | ToolDenied: ...


class DeleteReportsArgs(BaseModel):
    """The delete_reports parameters, validated the way the tool itself would read them
    (so "false" is False, not a truthy string that previews every report)."""

    match_text: str | None = None
    this_conversation_only: bool = False
    report_ids: list[str] | None = None
    all_my_reports: bool = False


class DeleteReportsPolicy:
    tool_name = "delete_reports"

    def plan(self, args: dict[str, Any], deps: Deps) -> ApprovalRequest | ToolDenied:
        store = deps.services.reports
        try:
            parsed = DeleteReportsArgs.model_validate(args)
        except ValidationError as e:
            return ToolDenied(f"The deletion request was not valid ({e.error_count()} bad argument(s)). Nothing was deleted.")
        try:
            plan = store.plan_deletion(
                deps.user.id,
                deps.session_id,
                match_text=parsed.match_text or None,
                this_conversation_only=parsed.this_conversation_only,
                report_ids=parsed.report_ids or None,
                all_my_reports=parsed.all_my_reports,
            )
        except (ValueError, TypeError) as e:
            return ToolDenied(f"The deletion request was not valid: {e} Nothing was deleted.")
        if not plan.reports:
            return ToolDenied(f"No saved reports matched ({plan.selector}). Nothing was deleted.")
        n = len(plan.reports)
        return ApprovalRequest(
            tool_name=self.tool_name,
            title=f"Delete {n} saved report{'s' if n != 1 else ''}?",
            items=[f"{r.id}  {r.title}  ({r.created_at[:16].replace('T', ' ')})" for r in plan.reports],
            warning=f"Matched: {plan.selector}. Deleted reports can be restored with /undo.",
            confirm_phrase=f"delete {n}",
            override_args={
                "report_ids": plan.ids,
                "match_text": None,
                "this_conversation_only": False,
                "all_my_reports": False,
            },
            audit={"selector": plan.selector, "report_ids": plan.ids},
        )


POLICIES: dict[str, ApprovalPolicy] = {p.tool_name: p for p in [DeleteReportsPolicy()]}


def approved(request: ApprovalRequest) -> ToolApproved:
    return ToolApproved(override_args=request.override_args)


def cancelled() -> ToolDenied:
    return ToolDenied("The user cancelled at the confirmation step. Nothing was deleted or changed.")
