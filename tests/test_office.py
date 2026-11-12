import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.model import FakeClient
from app.nonilion.mcp import NonilionError, NonilionMCP
from app.nonilion.office import CONVERSATION, OfficeBridge, parse_role


class FakeNonilion:
    """Records tool calls; hands out queued listen events once."""

    def __init__(self, inbox=None):
        self.calls, self.inbox, self.dm_fails = [], list(inbox or []), False

    def factory(self, token):
        outer = self

        class Client:
            def __init__(self):
                self.token = token

            async def call(self, name, args=None):
                outer.calls.append((token, name, args or {}))
                if name == "whoami":
                    return "me", {"owner": "riskhead", "room": "room-1"}
                if name == "look_around":
                    return "seen", {"people": [{"name": "Parv · Risk Head"}]}
                if name == "send_direct_message" and outer.dm_fails:
                    raise NonilionError("send_direct_message: nobody by that name")
                if name == "listen":
                    events, outer.inbox = outer.inbox, []
                    if not events:
                        await asyncio.sleep(0.05)
                    return "events", {"events": events}
                return "ok", None

            async def close(self):
                pass

        return Client()

    def names(self, token=None):
        return [n for t, n, _ in self.calls if token is None or t == token]


@pytest.fixture
def settings(tmp_path):
    return Settings(gemini_api_key="synthetic-test-value", model_fast="test-fast", model_deep="test-deep",
                    db_path=str(tmp_path / "office.db"), output_dir=str(tmp_path / "documents"), env="production",
                    public_base_url="https://lendix.example", demo_skip_cooling=True,
                    nonilion_mcp_token="nmcp_front", nonilion_agent_tokens={"Reporter": "nmcp_reporter"})


def wait_for(predicate, seconds=10):
    import time
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_parse_role():
    assert parse_role("Review L-1043", "riskhead", "riskhead") == ("risk_head", "Review L-1043")
    assert parse_role("Review L-1043", "someone", "riskhead") == ("ops", "Review L-1043")
    assert parse_role("borrower: accept KFS for L-1043", "riskhead", "riskhead") == ("borrower", "accept KFS for L-1043")
    assert parse_role("as ops, review L-1042", "x", "y") == ("ops", "review L-1042")


def test_office_off_without_token(tmp_path):
    settings = Settings(gemini_api_key="k", model_fast="f", model_deep="d", db_path=str(tmp_path / "o.db"),
                        output_dir=str(tmp_path / "out"), nonilion_mcp_token="")
    with TestClient(create_app(settings, FakeClient())) as client:
        assert client.get("/healthz").json()["nonilion"] == "not_connected"


def test_room_message_runs_pipeline_and_mirrors_back(settings):
    fake = FakeNonilion([{"type": "message", "from": "riskhead", "text": "Review L-1043"}])
    app = create_app(settings, FakeClient(), office_client=fake.factory)
    with TestClient(app) as client:
        assert wait_for(lambda: client.get("/healthz").json()["nonilion"] == "connected")
        # Both avatars entered with their own names.
        entered = [args["name"] for _, n, args in fake.calls if n == "enter_room"]
        assert entered == ["Lendix · Front Desk", "Lendix · Reporter"]
        assert wait_for(lambda: any(n == "reply" for n in fake.names("nmcp_front")), 20)
        store = app.state.store
        assert wait_for(lambda: any(e["conversation_id"] == CONVERSATION and e["kind"] == "decision"
                                    for e in store.all_events()), 20)
        # L-1043 is clean: the decision is spoken and a status shows the working agent.
        assert wait_for(lambda: "say_out_loud" in fake.names(), 10)
        assert "set_status" in fake.names()
        assert client.get("/api/state").json()["nonilion_connected"] is True


def test_escalation_walks_and_messages_risk_head(settings):
    fake = FakeNonilion([{"type": "message", "from": "someone", "text": "Review L-1045"}])
    app = create_app(settings, FakeClient(), office_client=fake.factory)
    with TestClient(app):
        assert wait_for(lambda: any(n == "send_direct_message" for n in fake.names()), 20)
        walks = [a for t, n, a in fake.calls if n == "walk_to"]
        assert walks and walks[0] == {"person": "Parv · Risk Head"}
        dm = [a for t, n, a in fake.calls if n == "send_direct_message"][0]
        assert dm["to"] == "Parv · Risk Head" and "L-1045" in dm["text"]
        # The Reporter avatar (own token) does the walking and messaging.
        assert all(t == "nmcp_reporter" for t, n, _ in fake.calls if n in {"walk_to", "send_direct_message"})


def test_office_failures_never_break_the_pipeline(settings):
    fake = FakeNonilion([{"type": "message", "from": "riskhead", "text": "Review L-1043"}])

    def flaky(token):
        client = fake.factory(token)
        original = client.call

        async def call(name, args=None):
            if name in {"set_status", "say_out_loud", "emote"}:
                raise NonilionError(f"{name}: rate limited")
            return await original(name, args)
        client.call = call
        return client

    app = create_app(settings, FakeClient(), office_client=flaky)
    with TestClient(app):
        assert wait_for(lambda: any(e["kind"] == "decision" for e in app.state.store.all_events()), 20)


def test_mcp_client_parses_text_and_json_and_hides_token():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        body = json.loads(request.content)
        if body["params"]["name"] == "broken":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": [{"type": "text", "text": "nope"}]}})
        if body["params"]["name"] == "sse":
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "plain"}]}})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=f"event: message\ndata: {payload}\n\n")
        if body["params"]["name"] == "denied":
            return httpx.Response(401, json={})
        text = 'You are here.\n\n{"owner":"riskhead"}'
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}})

    async def run():
        client = NonilionMCP("nmcp_secret_value", transport=httpx.MockTransport(handler))
        assert await client.call("whoami") == ("You are here.", {"owner": "riskhead"})
        assert await client.call("sse") == ("plain", None)
        with pytest.raises(NonilionError) as broken:
            await client.call("broken")
        with pytest.raises(NonilionError) as denied:
            await client.call("denied")
        await client.close()
        return str(broken.value) + str(denied.value)

    errors = asyncio.run(run())
    assert seen["auth"] == "Bearer nmcp_secret_value"
    assert "nmcp_secret_value" not in errors and "HTTP 401" in errors


def test_escalation_falls_back_to_room_chat_when_dm_fails(settings):
    fake = FakeNonilion([{"type": "message", "from": "someone", "text": "Review L-1045"}])
    fake.dm_fails = True
    app = create_app(settings, FakeClient(), office_client=fake.factory)
    with TestClient(app):
        assert wait_for(lambda: any(n == "post_room_chat" and "mention" in a for _, n, a in fake.calls), 20)
        post = [a for _, n, a in fake.calls if n == "post_room_chat" and "mention" in a][0]
        assert post["mention"] == ["@riskhead"] and "L-1045" in post["text"]


def test_bad_agent_token_drops_only_that_avatar(settings):
    settings.nonilion_agent_tokens = {"Reporter": "nmcp_reporter", "Risk": "nmcp_bad"}
    fake = FakeNonilion()

    def factory(token):
        client = fake.factory(token)
        if token == "nmcp_bad":
            async def call(name, args=None):
                raise NonilionError(f"{name}: HTTP 401")
            client.call = call
        return client

    app = create_app(settings, FakeClient(), office_client=factory)
    with TestClient(app) as client:
        assert wait_for(lambda: client.get("/healthz").json()["nonilion"] == "connected")
        assert set(app.state.office.avatars) == {"Front Desk", "Reporter"}
