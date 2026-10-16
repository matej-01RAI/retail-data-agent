"""HTTP API (FastAPI): the same assistant as the CLI, for a web or chat front end.

    POST   /v1/sessions                                 start a conversation
    GET    /v1/sessions/{session_id}                    state: idle, running, or waiting for approval
    POST   /v1/sessions/{session_id}/messages           ask; 200 with the answer, or 202 when an action
                                                        (e.g. deleting reports) needs the user's approval
    POST   /v1/sessions/{session_id}/approvals/{id}     confirm (with the phrase shown) or cancel
    DELETE /v1/sessions/{session_id}                    end the conversation
    GET    /v1/reports, /v1/reports/{report_id}         the caller's saved reports
    POST   /v1/reports/restore                          undo the last deletion
    GET    /v1/turns/{turn_id}                          metrics and SQL attempts of one of the caller's turns
    GET    /healthz

Identity. `Authorization: Bearer <token>`, tokens mapped to the users in config/users.yaml by
API_TOKENS ("ceo:<token>,tom:<token>"). With no tokens configured every request is refused: the
API never falls back to a default user. In production the identity comes from IAP (see
docs/architecture.md) and this table goes away.

Approvals. A turn that reaches an approval-required tool pauses inside the assistant exactly as
in the CLI; the HTTP request returns 202 with what would happen and the phrase to type. The turn
waits (at most APPROVAL_TIMEOUT_S, then it is cancelled and nothing changes) until the approval
endpoint answers. Deletion still runs only with the IDs that were shown.

Sessions live in this process (one instance, like the CLI). Production keeps the paused run
(Pydantic AI's DeferredToolRequests and the message history) in Cloud SQL so any instance can
resume it.
"""

from __future__ import annotations

import asyncio
import hmac
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .approvals import ApprovalRequest
from .assistant import Assistant, TurnResult
from .identity import User, load_users
from .settings import Settings, SettingsError

APPROVAL_TIMEOUT_S = 600.0
MAX_SESSIONS_PER_USER = 20
SESSION_IDLE_S = 2 * 3600.0
MIN_TOKEN_CHARS = 16


# --------------------------------------------------------------------------- wire formats


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class ApprovalIn(BaseModel):
    confirm: bool
    phrase: str | None = Field(default=None, max_length=200, description="The confirm phrase shown, e.g. 'delete 2'.")


class ApprovalOut(BaseModel):
    id: str
    title: str
    items: list[str]
    warning: str
    confirm_phrase: str
    expires_in_s: int


class TurnOut(BaseModel):
    turn_id: str
    outcome: str
    text: str
    latency_ms: int
    llm_requests: int
    sql_queries: int
    self_corrected: int


class ReplyOut(BaseModel):
    session_id: str
    status: Literal["answered", "approval_required"]
    turn: TurnOut | None = None
    approval: ApprovalOut | None = None
    progress: list[str] = []


class SessionOut(BaseModel):
    session_id: str
    user_id: str
    status: Literal["idle", "running", "approval_required"]
    approval: ApprovalOut | None = None
    last_turn: TurnOut | None = None


# --------------------------------------------------------------------------- sessions


@dataclass
class _PendingApproval:
    id: str
    request: ApprovalRequest
    future: asyncio.Future
    created: float = field(default_factory=time.monotonic)

    def out(self) -> ApprovalOut:
        left = max(0, int(APPROVAL_TIMEOUT_S - (time.monotonic() - self.created)))
        r = self.request
        return ApprovalOut(id=self.id, title=r.title, items=r.items, warning=r.warning,
                           confirm_phrase=r.confirm_phrase, expires_in_s=left)


class HttpUI:
    """The assistant's UI for HTTP: progress lines are collected, confirmations wait for the
    approval endpoint."""

    def __init__(self) -> None:
        self.pending: _PendingApproval | None = None
        self.changed = asyncio.Event()
        self.progress_lines: list[str] = []

    def progress(self, message: str) -> None:
        self.progress_lines = (self.progress_lines + [message])[-50:]

    async def confirm(self, request: ApprovalRequest) -> bool:
        approval = _PendingApproval(uuid.uuid4().hex[:12], request, asyncio.get_running_loop().create_future())
        self.pending = approval
        self.changed.set()
        try:
            return await asyncio.wait_for(asyncio.shield(approval.future), APPROVAL_TIMEOUT_S)
        except TimeoutError:
            return False  # unanswered: cancelled, nothing changes
        finally:
            self.pending = None


@dataclass
class _Session:
    assistant: Assistant
    ui: HttpUI
    task: asyncio.Task | None = None
    last: TurnResult | None = None
    touched: float = field(default_factory=time.monotonic)

    @property
    def status(self) -> str:
        if self.ui.pending is not None:
            return "approval_required"
        return "running" if self.task is not None and not self.task.done() else "idle"


def _remember(session: _Session, task: asyncio.Task) -> None:
    """Keep the last answer, so a client that lost the connection can still read it."""
    if not task.cancelled() and task.exception() is None:
        session.last = task.result()


def _turn_out(result: TurnResult | None) -> TurnOut | None:
    if result is None:
        return None
    return TurnOut(turn_id=result.turn_id, outcome=result.outcome, text=result.text, latency_ms=result.latency_ms,
                   llm_requests=result.llm_requests, sql_queries=result.sql_queries,
                   self_corrected=result.self_corrected)


def parse_tokens(raw: str | None, users: dict[str, User]) -> dict[str, User]:
    """API_TOKENS="ceo:<token>,tom:<token>" -> {token: user}. Bad entries stop the server."""
    table: dict[str, User] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        user_id, _, token = entry.partition(":")
        user_id, token = user_id.strip(), token.strip()
        if user_id not in users:
            raise SettingsError(f"API_TOKENS names unknown user {user_id!r} (see config/users.yaml).")
        if len(token) < MIN_TOKEN_CHARS:
            raise SettingsError(f"API_TOKENS: the token for {user_id!r} must be at least {MIN_TOKEN_CHARS} characters.")
        table[token] = users[user_id]
    return table


# --------------------------------------------------------------------------- app


def create_app(settings: Settings, *, services=None, agent=None, primary=None) -> FastAPI:
    """Builds the app. Tests pass `services` and `agent` (a scripted model); the server builds them
    from settings on startup."""
    users = load_users(settings.users_path)
    tokens = parse_tokens(settings.api_tokens, users)
    sessions: dict[str, _Session] = {}
    state: dict[str, Any] = {"services": services, "agent": agent, "primary": primary}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["services"] is None:
            from .agent import build_agent
            from .llm import build_model
            from .runtime import build_services

            state["services"] = build_services(settings)
            model, state["primary"] = build_model(settings)
            state["agent"] = build_agent(state["services"], model)
        yield
        for session in sessions.values():
            if session.task is not None and not session.task.done():
                session.task.cancel()
        state["services"].telemetry.flush()

    app = FastAPI(title="Retail Data Analyst API", version="1.0", lifespan=lifespan)

    def current_user(request: Request) -> User:
        if not tokens:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No API tokens are configured (API_TOKENS).")
        scheme, _, presented = (request.headers.get("authorization") or "").partition(" ")
        if scheme.lower() == "bearer" and presented:
            for token, user in tokens.items():
                if hmac.compare_digest(token.encode(), presented.strip().encode()):
                    return user
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid bearer token.",
                            headers={"WWW-Authenticate": "Bearer"})

    def own_session(session_id: str, user: User) -> _Session:
        session = sessions.get(session_id)
        if session is None or session.assistant.user.id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such session.")  # another user's: same answer
        session.touched = time.monotonic()
        return session

    def evict(user: User) -> None:
        now = time.monotonic()
        for sid, s in list(sessions.items()):
            if s.status == "idle" and now - s.touched > SESSION_IDLE_S:
                del sessions[sid]
        mine = sorted((s.touched, sid) for sid, s in sessions.items()
                      if s.assistant.user.id == user.id and s.status == "idle")
        while sum(1 for s in sessions.values() if s.assistant.user.id == user.id) >= MAX_SESSIONS_PER_USER and mine:
            del sessions[mine.pop(0)[1]]
        if sum(1 for s in sessions.values() if s.assistant.user.id == user.id) >= MAX_SESSIONS_PER_USER:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many open conversations; end one first.")

    async def drive(session: _Session, response: Response) -> ReplyOut:
        """Wait until the turn ends or asks for approval."""
        session.ui.changed.clear()
        waiter = asyncio.ensure_future(session.ui.changed.wait())
        try:
            await asyncio.wait({session.task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        sid = session.assistant.session_id
        progress = list(session.ui.progress_lines)
        if session.ui.pending is not None:
            response.status_code = status.HTTP_202_ACCEPTED
            return ReplyOut(session_id=sid, status="approval_required", approval=session.ui.pending.out(),
                            progress=progress)
        result = session.task.result()
        session.task = None
        return ReplyOut(session_id=sid, status="answered", turn=_turn_out(result), progress=progress)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        services = state["services"]
        return {"status": "ok", "warehouse": services.warehouse.name if services else "starting"}

    @app.post("/v1/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(user: User = Depends(current_user)) -> SessionOut:
        evict(user)
        ui = HttpUI()
        assistant = Assistant(state["services"], state["agent"], user, ui, state["primary"])
        sessions[assistant.session_id] = _Session(assistant, ui)
        return SessionOut(session_id=assistant.session_id, user_id=user.id, status="idle")

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str, user: User = Depends(current_user)) -> SessionOut:
        s = own_session(session_id, user)
        return SessionOut(session_id=session_id, user_id=user.id, status=s.status,
                          approval=s.ui.pending.out() if s.ui.pending else None, last_turn=_turn_out(s.last))

    @app.delete("/v1/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def end_session(session_id: str, user: User = Depends(current_user)) -> Response:
        s = own_session(session_id, user)
        if s.task is not None and not s.task.done():
            s.task.cancel()  # the assistant records it as interrupted and reports any change already made
        sessions.pop(session_id, None)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/v1/sessions/{session_id}/messages")
    async def post_message(session_id: str, body: MessageIn, response: Response,
                           user: User = Depends(current_user)) -> ReplyOut:
        s = own_session(session_id, user)
        if s.status != "idle":
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "A question is still being answered or waits for approval in this conversation.")
        s.ui.progress_lines = []
        s.task = asyncio.create_task(s.assistant.ask(body.text))
        s.task.add_done_callback(lambda task: _remember(s, task))
        return await drive(s, response)

    @app.post("/v1/sessions/{session_id}/approvals/{approval_id}")
    async def answer_approval(session_id: str, approval_id: str, body: ApprovalIn, response: Response,
                              user: User = Depends(current_user)) -> ReplyOut:
        s = own_session(session_id, user)
        pending = s.ui.pending
        if pending is None or pending.id != approval_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such pending approval.")
        if pending.future.done():  # a second answer to the same question while the first is applied
            raise HTTPException(status.HTTP_409_CONFLICT, "This approval was already answered.")
        if body.confirm and (body.phrase or "").strip().lower() != pending.request.confirm_phrase.lower():
            raise HTTPException(422,
                                f"Type the confirm phrase exactly: {pending.request.confirm_phrase!r}. Nothing was changed.")
        pending.future.set_result(body.confirm)
        return await drive(s, response)

    @app.get("/v1/reports")
    def list_reports(user: User = Depends(current_user)) -> list[dict[str, str]]:
        return [r.summary() for r in state["services"].reports.find(user.id)]

    @app.get("/v1/reports/{report_id}")
    def get_report(report_id: str, user: User = Depends(current_user)) -> dict[str, str]:
        report = state["services"].reports.get(user.id, report_id)
        if report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such report.")
        return {**report.summary(), "body": report.body}

    @app.post("/v1/reports/restore")
    def restore_reports(user: User = Depends(current_user)) -> dict[str, list[str]]:
        return {"restored": state["services"].reports.restore_last_batch(user.id, "api")}

    @app.get("/v1/turns/{turn_id}")
    def get_turn(turn_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        detail = state["services"].telemetry.turn_detail(turn_id, user_id=user.id)
        if detail is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such turn.")
        detail.pop("messages_json", None)
        detail.pop("spans", None)
        return detail

    return app
