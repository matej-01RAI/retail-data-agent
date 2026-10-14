"""Command-line interface.

    python -m retail_agent chat  --user ceo          interactive chat (default command)
    python -m retail_agent ask   --user tom "..."    one question, then exit
    python -m retail_agent doctor                    check keys, credentials, dataset access
    python -m retail_agent stats                     agent health metrics
    python -m retail_agent trace <turn-id>           deep dive into one turn
    python -m retail_agent sample-db                 build the offline synthetic dataset

Add --offline to chat/ask/doctor to use the local synthetic dataset instead of BigQuery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .approvals import ApprovalRequest
from .settings import Settings, load_settings

console = Console()

HELP = """\
[bold]Ask anything about sales, customers and products.[/bold] Examples:
  • What data do we have and what can I analyse with it?
  • Who are our top 10 customers and how much have they spent?
  • Compare Calvin Klein and Columbia, and explain why they perform differently.
  • Why are users in Texas underspending compared to California?
  • Create a Q1 report with insights and action items for Q2.
  • Delete all the reports we made in this conversation.

[bold]Commands[/bold]
  /reports            list your saved reports        /show <id>     print a saved report
  /undo               restore the last deleted batch /prefs [clear] your remembered preferences
  /trace [turn-id]    deep dive into the last turn   /stats         agent health metrics
  /user <id>          switch user (new conversation) /new           start a new conversation
  /chaos bq <n> | llm <n> | off   simulate outages   /whoami        who you are, your scope
  /help  /quit
"""


class RichUI:
    """Progress lines and the destructive-action confirmation panel."""

    def __init__(self) -> None:
        self.status: Any = None

    def progress(self, message: str) -> None:
        console.print(f"  [dim]↳ {escape(message)}[/dim]")

    async def confirm(self, request: ApprovalRequest) -> bool:
        if self.status:
            self.status.stop()
        try:
            body = "\n".join(f"  • {escape(item)}" for item in request.items[:25])
            if len(request.items) > 25:
                body += f"\n  … and {len(request.items) - 25} more"
            body += f"\n\n[dim]{escape(request.warning)}[/dim]\n"
            body += f'Type [bold]{request.confirm_phrase}[/bold] to confirm. Anything else cancels.'
            console.print(Panel(body, title=f"[bold yellow]⚠ {escape(request.title)}[/bold yellow]", border_style="yellow"))
            answer = await asyncio.to_thread(console.input, "[yellow]confirm › [/yellow]")
            ok = answer.strip().lower() == request.confirm_phrase.lower()
            console.print("[green]  Confirmed.[/green]" if ok else "[dim]  Cancelled. Nothing was deleted.[/dim]")
            return ok
        finally:
            if self.status:
                self.status.start()


# --------------------------------------------------------------------------- setup


def _settings(args: argparse.Namespace) -> Settings:
    from .runtime import offline

    settings = load_settings()
    return offline(settings) if getattr(args, "offline", False) else settings


def _build(settings: Settings, user_id: str):
    from .agent import build_agent
    from .assistant import Assistant
    from .identity import get_user
    from .llm import build_model
    from .runtime import build_services

    user = get_user(settings.users_path, user_id)
    services = build_services(settings)
    model, primary = build_model(settings)
    agent = build_agent(services, model)
    ui = RichUI()
    return Assistant(services, agent, user, ui, primary), ui


def _fail_setup(error: Exception) -> None:
    name = type(error).__name__
    console.print(f"[red]Could not start: {escape(name)}: {escape(str(error))}[/red]")
    if "DefaultCredentials" in name or "credentials" in str(error).lower():
        console.print(
            "BigQuery needs Google Cloud credentials: run [bold]gcloud auth application-default login[/bold] "
            "and set GOOGLE_CLOUD_PROJECT in .env, or use [bold]--offline[/bold] (synthetic data)."
        )
    console.print("Run [bold]python -m retail_agent doctor[/bold] for a full check.")
    sys.exit(1)


# --------------------------------------------------------------------------- chat


async def _answer(assistant, ui: RichUI, question: str) -> None:
    with console.status("[cyan]Thinking…[/cyan]", spinner="dots") as status:
        ui.status = status
        result = await assistant.ask(question)
        ui.status = None
    style = "red" if result.outcome not in ("answered", "blocked") else "cyan"
    console.print(Panel(Markdown(result.text), title="[bold]Analyst[/bold]", border_style=style, title_align="left"))
    bits = [f"turn {result.turn_id}", f"{result.latency_ms / 1000:.1f}s"]
    if result.llm_requests:
        bits.append(f"{result.llm_requests} model calls")
    if result.sql_queries:
        bits.append(f"{result.sql_queries} queries" + (f" ({result.self_corrected} self-corrected)" if result.self_corrected else ""))
    if result.outcome != "answered":
        bits.append(f"outcome: {result.outcome}")
    console.print(f"[dim]{' · '.join(bits)} · /trace for details[/dim]\n")


async def chat(args: argparse.Namespace) -> None:
    settings = _settings(args)
    try:
        assistant, ui = _build(settings, args.user)
    except Exception as e:  # noqa: BLE001
        _fail_setup(e)
        return
    _banner(assistant)
    last_turn: str | None = None
    while True:
        try:
            text = (await asyncio.to_thread(console.input, "[bold green]you › [/bold green]")).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not text:
            continue
        if text.startswith("/"):
            keep_going, assistant = await _command(text, assistant, ui, settings, last_turn)
            if not keep_going:
                break
            continue
        try:
            await _answer(assistant, ui, text)
        except KeyboardInterrupt:
            console.print("[dim]Interrupted.[/dim]")
        recent = assistant.services.telemetry.recent_turns(1, assistant.session_id)
        last_turn = recent[0]["turn_id"] if recent else last_turn
    assistant.services.telemetry.flush()


def _banner(assistant) -> None:
    s = assistant.services
    user = assistant.user
    mode = "BigQuery: " + s.settings.dataset if s.warehouse.name == "bigquery" else "OFFLINE synthetic sample (not real thelook data)"
    console.print(Panel(
        f"Signed in as [bold]{escape(user.name)}[/bold], {escape(user.title)}\n"
        f"Data scope: {escape(user.scope.describe())}\n"
        f"Model: {escape(s.settings.model)} (fallback: {escape(', '.join(s.settings.fallback_models) or 'none')})\n"
        f"Data: {escape(mode)}\n"
        f"[dim]/help for examples and commands[/dim]",
        title="[bold cyan]Retail Data Analyst[/bold cyan]", border_style="cyan", title_align="left",
    ))


async def _command(text: str, assistant, ui: RichUI, settings: Settings, last_turn: str | None):
    parts = text.split()
    cmd, rest = parts[0].lower(), parts[1:]
    s = assistant.services
    user = assistant.user
    if cmd in ("/quit", "/exit", "/q"):
        return False, assistant
    if cmd == "/help":
        console.print(HELP)
    elif cmd == "/whoami":
        console.print(f"{escape(user.name)} ({user.id}), {escape(user.title)} · scope: {escape(user.scope.describe())} · session {assistant.session_id}")
    elif cmd == "/new":
        assistant.new_conversation()
        console.print(f"[dim]New conversation {assistant.session_id}.[/dim]")
    elif cmd == "/user":
        if not rest:
            console.print("Usage: /user <id>")
        else:
            try:
                s.telemetry.flush()
                assistant, ui = _build(settings, rest[0])
                _banner(assistant)
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]{escape(str(e))}[/red]")
    elif cmd == "/reports":
        reports = s.reports.find(user.id)
        if not reports:
            console.print("[dim]No saved reports.[/dim]")
        table = Table("ID", "Title", "Created", "This conversation", box=None) if reports else None
        for r in reports:
            table.add_row(r.id, escape(r.title), r.created_at[:16].replace("T", " "), "yes" if r.session_id == assistant.session_id else "")
        if table:
            console.print(table)
    elif cmd == "/show":
        report = s.reports.get(user.id, rest[0]) if rest else None
        if report is None:
            console.print("Usage: /show <report-id> (one of your saved reports)")
        else:
            console.print(Panel(Markdown(f"# {report.title}\n\n{report.body}"), title=report.id))
    elif cmd == "/undo":
        ids = s.reports.restore_last_batch(user.id, assistant.session_id)
        console.print(f"Restored {len(ids)} report(s): {', '.join(ids)}" if ids else "[dim]Nothing to restore.[/dim]")
    elif cmd == "/prefs":
        if rest and rest[0] == "clear":
            s.prefs.clear(user.id)
            console.print("[dim]Preferences cleared.[/dim]")
        else:
            prefs = s.prefs.get(user.id)
            console.print(json.dumps(prefs, indent=2) if prefs else "[dim]No preferences remembered yet.[/dim]")
    elif cmd == "/trace":
        turn_id = rest[0] if rest else last_turn
        if not turn_id:
            console.print("[dim]No turn yet.[/dim]")
        else:
            show_trace(s.telemetry, turn_id)
    elif cmd == "/stats":
        show_stats(s.telemetry)
    elif cmd == "/chaos":
        _chaos(rest, assistant)
    else:
        console.print(f"Unknown command {escape(cmd)}. /help lists commands.")
    return True, assistant


def _chaos(rest: list[str], assistant) -> None:
    wh = assistant.services.warehouse
    primary = assistant.primary_model
    if not rest or rest[0] == "off":
        wh.faults.remaining = 0
        if primary:
            primary.fail_next = 0
        console.print("[dim]Fault injection off.[/dim]")
        return
    target, n = rest[0], int(rest[1]) if len(rest) > 1 else 1
    if target == "bq":
        wh.faults.remaining = n
        console.print(f"[yellow]The next {n} warehouse call(s) will fail with a simulated 503.[/yellow]")
    elif target == "llm" and primary:
        primary.fail_next = n
        console.print(f"[yellow]The next {n} primary-model call(s) will fail with a simulated 503 (fallback model takes over).[/yellow]")
    else:
        console.print("Usage: /chaos bq <n> | llm <n> | off")


# --------------------------------------------------------------------------- trace & stats


def show_trace(telemetry, turn_id: str) -> None:
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    detail = telemetry.turn_detail(turn_id)
    if not detail:
        console.print(f"[red]No turn {escape(turn_id)}.[/red]")
        return
    console.print(Panel(
        f"turn {detail['turn_id']} · user {detail['user_id']} · session {detail['session_id']} · {detail['ts']}\n"
        f"outcome [bold]{detail['outcome']}[/bold]{' · ' + escape(detail['error_type']) if detail['error_type'] else ''}\n"
        f"latency {detail['latency_ms']} ms · model calls {detail['llm_requests']} · tokens {detail['input_tokens']} in / "
        f"{detail['output_tokens']} out · models {detail['models']}{' · FALLBACK USED' if detail['fallback_used'] else ''}\n"
        f"sql {detail['sql_queries']} ({detail['sql_failures']} failed, {detail['sql_rejected']} rejected by guard, "
        f"{detail['empty_results']} empty, {detail['self_corrected']} self-corrected) · PII redactions {detail['pii_redactions']}\n"
        f"precedents {detail['precedents']} · persona {detail['persona_version']} · trace_id {detail['trace_id']}",
        title="Turn", border_style="magenta", title_align="left",
    ))
    if detail["messages_json"]:
        console.print("[bold]Message correspondence[/bold]")
        for msg in ModelMessagesTypeAdapter.validate_json(detail["messages_json"]):
            for part in msg.parts:
                kind = part.part_kind
                if kind == "user-prompt":
                    console.print(f"  [green]user[/green]      {escape(_short(part.content))}")
                elif kind == "text":
                    console.print(f"  [cyan]assistant[/cyan] {escape(_short(part.content, 300))}")
                elif kind == "tool-call":
                    console.print(f"  [yellow]→ {part.tool_name}[/yellow] {escape(_short(json.dumps(part.args_as_dict()), 400))}")
                elif kind == "tool-return":
                    console.print(f"  [yellow]← {part.tool_name}[/yellow] {escape(_short(str(part.content), 300))}")
                elif kind == "retry-prompt":
                    console.print(f"  [red]↺ retry {part.tool_name or ''}[/red] {escape(_short(str(part.content), 300))}")
    if detail["sql_events"]:
        table = Table("status", "kind", "purpose", "rows", "ms", "error", title="SQL attempts", box=None, title_justify="left")
        for e in detail["sql_events"]:
            table.add_row(e["status"], e["error_kind"] or "", escape(_short(e["purpose"] or "", 50)), str(e["rows"] or ""),
                          str(e["elapsed_ms"] or ""), escape(_short(e["error"] or "", 70)))
        console.print(table)
    if detail["spans"]:
        table = Table("span", "ms", "details", title="Spans (OpenTelemetry)", box=None, title_justify="left")
        for span in detail["spans"]:
            a = span["attributes"]
            info = []
            for key in ("gen_ai.request.model", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
                        "gen_ai.tool.name", "sql.status", "sql.error_kind", "sql.rows", "app.outcome"):
                if key in a:
                    info.append(f"{key.split('.')[-1]}={a[key]}")
            table.add_row(escape(span["name"]), str(span["duration_ms"]), escape(" ".join(info)))
        console.print(table)
        console.print(f"[dim]Full spans with message content: {telemetry.spans_path} (trace_id {detail['trace_id']})[/dim]")


def show_stats(telemetry) -> None:
    stats = telemetry.stats()
    if not stats.get("turns"):
        console.print("[dim]No turns recorded yet.[/dim]")
        return
    table = Table("metric", "value", title="Agent health (all recorded turns)", box=None, title_justify="left")
    for key, value in stats.items():
        table.add_row(key, json.dumps(value) if isinstance(value, (dict, list)) else str(value))
    console.print(table)


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- other commands


async def ask(args: argparse.Namespace) -> None:
    settings = _settings(args)
    try:
        assistant, ui = _build(settings, args.user)
    except Exception as e:  # noqa: BLE001
        _fail_setup(e)
        return
    console.print(f"[bold green]{escape(assistant.user.name)} ›[/bold green] {escape(args.question)}")
    await _answer(assistant, ui, args.question)
    assistant.services.telemetry.flush()


def doctor(args: argparse.Namespace) -> None:
    from .data.sql_guard import SqlGuard
    from .identity import ProductScope
    from .knowledge import DataDictionary
    from .runtime import build_warehouse

    settings = _settings(args)
    ok = True

    def line(passed: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        console.print(f"{'[green]✓[/green]' if passed else '[red]✗[/red]'} {label} [dim]{escape(detail)}[/dim]")

    line(sys.version_info >= (3, 11), "Python >= 3.11", sys.version.split()[0])
    line(bool(settings.gemini_api_key), "GEMINI_API_KEY set", f"model {settings.model}, fallback {settings.fallback_models}")
    if settings.gemini_api_key:
        try:
            from google import genai

            client = genai.Client(api_key=settings.gemini_api_key)
            reply = client.models.generate_content(model=settings.model, contents="Reply with the single word OK.")
            line(True, "Gemini reachable", (reply.text or "").strip()[:40])
        except Exception as e:  # noqa: BLE001
            line(False, "Gemini reachable", f"{type(e).__name__}: {str(e)[:200]}")
    try:
        warehouse = build_warehouse(settings)
        line(True, f"Warehouse client ({settings.warehouse})", settings.gcp_project or "default project")
        dictionary = DataDictionary.load(settings.knowledge_dir / "data_dictionary.yaml")
        live = warehouse.live_columns()
        line(live is not None, "Read table schemas", ", ".join(sorted(live)) if live else "failed")
        if live:
            for table, spec in dictionary.tables.items():
                missing = sorted(set(spec.columns) - set(live.get(table, [])))
                line(not missing, f"Dictionary matches {table}", f"missing: {missing}" if missing else f"{len(live[table])} columns")
        guard = SqlGuard(dictionary, live)
        q = guard.prepare(
            "SELECT COUNT(*) AS orders, MAX(created_at) AS latest FROM `bigquery-public-data.thelook_ecommerce.orders`",
            ProductScope(),
        )
        result = warehouse.query(q.sql, labels={"app": "retail-agent", "user": "doctor"}, max_rows=1)
        line(True, "Test query", f"{result.rows[0]} ({result.bytes_processed} bytes)")
        from .identity import load_users

        for user in load_users(settings.users_path).values():
            if user.scope.unrestricted:
                continue
            q = guard.prepare(
                "SELECT COUNT(*) AS products FROM `bigquery-public-data.thelook_ecommerce.products`", user.scope
            )
            n = warehouse.query(q.sql, labels={"app": "retail-agent", "user": "doctor"}, max_rows=1).rows[0][0]
            line(n > 0, f"Scope of '{user.id}' matches products", f"{user.scope.describe()}: {n} products")
    except Exception as e:  # noqa: BLE001
        line(False, "Warehouse", f"{type(e).__name__}: {str(e)[:300]}")
    console.print("[green]All checks passed.[/green]" if ok else "[yellow]Some checks failed (see above).[/yellow]")
    if not ok:
        sys.exit(1)


def stats_cmd(args: argparse.Namespace) -> None:
    from .observability import Telemetry
    from .store import Database

    settings = load_settings()
    show_stats(Telemetry(Database(settings.db_path), None, settings.traces_path))


def trace_cmd(args: argparse.Namespace) -> None:
    from .observability import Telemetry
    from .store import Database

    settings = load_settings()
    show_trace(Telemetry(Database(settings.db_path), None, settings.traces_path), args.turn_id)


def sample_db(args: argparse.Namespace) -> None:
    from .data.sample_db import build

    settings = load_settings()
    counts = build(settings.duckdb_path, users=args.users)
    console.print(f"Built {settings.duckdb_path}: {counts}")


def _run_interactive(coro) -> None:
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        console.print("\n[dim]Bye.[/dim]")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m retail_agent", description="Retail data analysis chat agent")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("chat", help="interactive chat (default)")
    p.add_argument("--user", default="ceo")
    p.add_argument("--offline", action="store_true", help="use the local synthetic dataset instead of BigQuery")

    p = sub.add_parser("ask", help="ask one question and exit")
    p.add_argument("question")
    p.add_argument("--user", default="ceo")
    p.add_argument("--offline", action="store_true")

    p = sub.add_parser("doctor", help="check configuration and access")
    p.add_argument("--offline", action="store_true")

    sub.add_parser("stats", help="agent health metrics")
    p = sub.add_parser("trace", help="deep dive into one turn")
    p.add_argument("turn_id")
    p = sub.add_parser("sample-db", help="build the offline synthetic dataset")
    p.add_argument("--users", type=int, default=4000)

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in sub.choices and argv[0] not in ("-h", "--help"):
        argv = ["chat", *argv]
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"), format="%(levelname)s %(name)s: %(message)s")
    commands = {
        "chat": lambda: _run_interactive(chat(args)),
        "ask": lambda: asyncio.run(ask(args)),
        "doctor": lambda: doctor(args),
        "stats": lambda: stats_cmd(args),
        "trace": lambda: trace_cmd(args),
        "sample-db": lambda: sample_db(args),
    }
    commands[args.command]()
