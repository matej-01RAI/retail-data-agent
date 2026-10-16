"""HTTP API: identity, conversation ownership and the approval flow over two requests."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from retail_agent.agent import build_agent
from retail_agent.api import create_app, parse_tokens
from retail_agent.identity import load_users
from retail_agent.llm import FaultInjectingModel
from retail_agent.settings import SettingsError

from .conftest import Script, call, say

CEO, TOM = "ceo-token-0123456789abcdef", "tom-token-0123456789abcdef"


@pytest.fixture
def api(services, settings):
    def factory(script: Script, tokens: str | None = f"ceo:{CEO},tom:{TOM}"):
        app = create_app(replace(settings, api_tokens=tokens), services=services,
                         agent=build_agent(services, FaultInjectingModel(script.model)))
        return TestClient(app)

    return factory


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_identity_is_required(api):
    with api(Script()) as client:
        assert client.post("/v1/sessions").status_code == 401
        assert client.post("/v1/sessions", headers=auth("not-a-real-token-at-all")).status_code == 401
        assert client.post("/v1/sessions", headers=auth(CEO)).status_code == 201
    with api(Script(), tokens=None) as client:
        assert client.post("/v1/sessions", headers=auth(CEO)).status_code == 503  # never a default user


def test_tokens_are_validated_at_startup(settings):
    users = load_users(settings.users_path)
    with pytest.raises(SettingsError):
        parse_tokens("nobody:0123456789abcdefgh", users)
    with pytest.raises(SettingsError):
        parse_tokens("ceo:short", users)
    assert parse_tokens(f"ceo:{CEO}", users)[CEO].id == "ceo"


def test_a_question_is_answered_and_conversations_are_private(api):
    with api(Script(say("Revenue was 10."))) as client:
        sid = client.post("/v1/sessions", headers=auth(CEO)).json()["session_id"]
        reply = client.post(f"/v1/sessions/{sid}/messages", json={"text": "Revenue?"}, headers=auth(CEO))
        assert reply.status_code == 200
        body = reply.json()
        assert body["status"] == "answered" and body["turn"]["text"] == "Revenue was 10."
        turn_id = body["turn"]["turn_id"]
        # Tom cannot read, post to or trace the CEO's conversation.
        assert client.get(f"/v1/sessions/{sid}", headers=auth(TOM)).status_code == 404
        assert client.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"}, headers=auth(TOM)).status_code == 404
        assert client.get(f"/v1/turns/{turn_id}", headers=auth(TOM)).status_code == 404
        trace = client.get(f"/v1/turns/{turn_id}", headers=auth(CEO)).json()
        assert trace["outcome"] == "answered" and "messages_json" not in trace
        assert client.get(f"/v1/sessions/{sid}", headers=auth(CEO)).json()["last_turn"]["turn_id"] == turn_id
        assert client.delete(f"/v1/sessions/{sid}", headers=auth(CEO)).status_code == 204
        assert client.get(f"/v1/sessions/{sid}", headers=auth(CEO)).status_code == 404


def test_deletion_needs_the_phrase_in_a_second_request(api, services):
    script = Script(
        call("save_report", title="Q1 review", body_markdown="Revenue up."), say("Saved."),
        call("delete_reports", this_conversation_only=True), say("Deleted."),
    )
    with api(script) as client:
        sid = client.post("/v1/sessions", headers=auth(CEO)).json()["session_id"]
        client.post(f"/v1/sessions/{sid}/messages", json={"text": "Save a Q1 report"}, headers=auth(CEO))
        report_id = client.get("/v1/reports", headers=auth(CEO)).json()[0]["id"]
        assert client.get("/v1/reports", headers=auth(TOM)).json() == []  # owner only

        paused = client.post(f"/v1/sessions/{sid}/messages", json={"text": "Delete this conversation's reports"},
                             headers=auth(CEO))
        assert paused.status_code == 202
        approval = paused.json()["approval"]
        assert approval["confirm_phrase"] == "delete 1" and approval["items"][0].startswith(report_id)
        url = f"/v1/sessions/{sid}/approvals/{approval['id']}"
        # Busy until answered; a wrong phrase changes nothing and keeps it pending; others cannot answer.
        assert client.post(f"/v1/sessions/{sid}/messages", json={"text": "and?"}, headers=auth(CEO)).status_code == 409
        assert client.post(url, json={"confirm": True, "phrase": "delete all"}, headers=auth(CEO)).status_code == 422
        assert client.post(url, json={"confirm": True, "phrase": "delete 1"}, headers=auth(TOM)).status_code == 404
        assert services.reports.find("ceo")

        done = client.post(url, json={"confirm": True, "phrase": "Delete 1"}, headers=auth(CEO))
        assert done.status_code == 200 and done.json()["turn"]["text"] == "Deleted."
        assert services.reports.find("ceo") == []
        assert client.post("/v1/reports/restore", headers=auth(CEO)).json() == {"restored": [report_id]}


def test_cancelling_an_approval_deletes_nothing(api, services):
    script = Script(
        call("save_report", title="Keep me", body_markdown="x"), say("Saved."),
        call("delete_reports", all_my_reports=True), say("Nothing was deleted."),
    )
    with api(script) as client:
        sid = client.post("/v1/sessions", headers=auth(CEO)).json()["session_id"]
        client.post(f"/v1/sessions/{sid}/messages", json={"text": "Save it"}, headers=auth(CEO))
        approval = client.post(f"/v1/sessions/{sid}/messages", json={"text": "Delete everything"},
                               headers=auth(CEO)).json()["approval"]
        done = client.post(f"/v1/sessions/{sid}/approvals/{approval['id']}", json={"confirm": False}, headers=auth(CEO))
        assert done.status_code == 200 and done.json()["status"] == "answered"
        assert [r.title for r in services.reports.find("ceo")] == ["Keep me"]
        assert "cancelled" in str(script.seen[-1][-1].parts[0].content)


async def test_an_approval_is_answered_once(api, services):
    import asyncio

    import httpx

    script = Script(
        call("save_report", title="Q1 review", body_markdown="x"), say("Saved."),
        call("delete_reports", this_conversation_only=True), say("Deleted."),
    )
    app = api(script).app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        sid = (await client.post("/v1/sessions", headers=auth(CEO))).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/messages", json={"text": "Save it"}, headers=auth(CEO))
        approval = (await client.post(f"/v1/sessions/{sid}/messages", json={"text": "Delete it"},
                                      headers=auth(CEO))).json()["approval"]
        url = f"/v1/sessions/{sid}/approvals/{approval['id']}"
        yes, no = await asyncio.gather(
            client.post(url, json={"confirm": True, "phrase": "delete 1"}, headers=auth(CEO)),
            client.post(url, json={"confirm": False}, headers=auth(CEO)),
        )
        codes = sorted([yes.status_code, no.status_code])
        assert codes[0] == 200 and codes[1] in (404, 409)  # one answer wins, the other changes nothing
        assert (services.reports.find("ceo") == []) == (yes.status_code == 200)
