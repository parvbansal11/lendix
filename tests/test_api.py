import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.model import FakeClient
from app.nonilion.adapter import NonilionAdapter, verify_nonilion_request
from app.store import utcnow
from app.uploads import MAX_UPLOAD_BYTES


@pytest.fixture
def settings(tmp_path):
    return Settings(
        gemini_api_key="synthetic-test-value",
        model_fast="test-fast",
        model_deep="test-deep",
        db_path=str(tmp_path / "office.db"),
        output_dir=str(tmp_path / "documents"),
        env="production",
        public_base_url="",
        demo_skip_cooling=True,
    )


@pytest.fixture
def office(settings, monkeypatch):
    monkeypatch.delenv("NONILION_DEV_ALLOW_UNVERIFIED", raising=False)
    model = FakeClient()
    app = create_app(settings, model)
    with TestClient(app) as client:
        yield client, app, model


def completed_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"completed", "failed", "interrupted"}:
            return job
        time.sleep(0.01)
    pytest.fail("The background job did not reach a terminal state.")


def register_document(app, settings, *, expired=False, outside=False):
    directory = Path(settings.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory.parent / "private.txt" if outside else directory / "report.txt"
    path.write_text("SPECIMEN · SYNTHETIC DEMO DATA · NOT A LEGAL DOCUMENT")
    record = app.state.store.register_file({
        "path": str(path), "filename": "report.txt", "mime_type": "text/plain",
        "loan_id": "L-1043", "kind": "risk_report", "created_at": utcnow().isoformat(),
    })
    if expired:
        record["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        app.state.store.put("file", record["token"], record)
    return record


def test_public_console_and_state_identify_the_synthetic_workspace(office):
    client, app, _ = office
    home = client.get("/")
    assert home.status_code == 200
    assert "Demo with synthetic data. Do not enter real personal information." in home.text
    assert "no real authentication" in home.text
    assert client.get("/static/app.js").status_code == 200
    state = client.get("/api/state").json()
    assert state["demo_role_switching"] is True
    assert state["nonilion_connected"] is False
    assert len(state["loans"]) >= 25
    assert {"L-1042", "L-1043", "L-1044"} <= {loan["id"] for loan in state["loans"]}
    assert {user["role"] for user in state["users"]} == {"credit_officer", "ops", "borrower"}
    assert client.get("/healthz").json()["status"] == "ok"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_documentation_is_disabled_in_production(office, path):
    assert office[0].get(path).status_code == 404


def test_api_documentation_can_be_enabled_in_development(settings):
    settings.env = "dev"
    with TestClient(create_app(settings, FakeClient())) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200


def test_security_headers_and_private_response_caching(office):
    client, _, _ = office
    response = client.get("/api/state")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "unsafe-inline" not in policy


def test_missing_model_key_stops_boot(settings):
    settings.gemini_api_key = ""
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is required"):
        with TestClient(create_app(settings, FakeClient())):
            pytest.fail("An app with no model key must not finish booting.")


@pytest.mark.parametrize("field", ["model_fast", "model_deep"])
def test_missing_model_identifiers_stop_boot(settings, field):
    setattr(settings, field, "")
    with pytest.raises(RuntimeError, match="GEMINI_MODEL_FAST and GEMINI_MODEL_DEEP"):
        with TestClient(create_app(settings, FakeClient())):
            pytest.fail("An app with missing model configuration must not boot.")


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes"])
def test_unverified_webhooks_are_rejected_by_default(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("NONILION_DEV_ALLOW_UNVERIFIED", raising=False)
    else:
        monkeypatch.setenv("NONILION_DEV_ALLOW_UNVERIFIED", value)
    assert verify_nonilion_request(None) is False


def test_webhook_is_closed_without_explicit_development_bypass(office):
    client, app, _ = office
    response = client.post("/nonilion/webhook", json={"message": "hello"})
    assert response.status_code == 403
    assert app.state.store.all("job") == []


@pytest.mark.asyncio
async def test_webhook_acknowledges_before_adapter_work_finishes(settings, monkeypatch):
    monkeypatch.setenv("NONILION_DEV_ALLOW_UNVERIFIED", "1")
    app = create_app(settings, FakeClient())
    entered = asyncio.Event()
    release = asyncio.Event()
    received = []

    async def blocked_adapter(event):
        received.append(event)
        entered.set()
        await release.wait()

    async with app.router.lifespan_context(app):
        app.state.nonilion.on_message = blocked_adapter
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://office.test") as client:
            try:
                response = await asyncio.wait_for(client.post("/nonilion/webhook", content=b'{"opaque":"event"}'), timeout=1)
                assert response.status_code == 200
                assert response.json()["accepted"] is True
                await asyncio.wait_for(entered.wait(), timeout=1)
                assert received == [b'{"opaque":"event"}']
                job = app.state.store.get("job", response.json()["job_id"])
                assert job["status"] == "running"
                assert not release.is_set()
            finally:
                release.set()
                await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_chat_acknowledges_before_agent_work_finishes(settings):
    app = create_app(settings, FakeClient())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_agent(event):
        assert event["role"] == "ops"
        assert event["conversation_id"] == "background-check"
        entered.set()
        await release.wait()

    async with app.router.lifespan_context(app):
        app.state.adapter.on_message = blocked_agent
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://office.test") as client:
            try:
                response = await asyncio.wait_for(client.post("/api/chat", json={"message": "Review today's 20 applications", "role": "ops", "conversation_id": "background-check"}), timeout=1)
                assert response.status_code == 202
                await asyncio.wait_for(entered.wait(), timeout=1)
                assert app.state.store.get("job", response.json()["job_id"])["status"] == "running"
            finally:
                release.set()
                await asyncio.sleep(0)


@pytest.mark.parametrize("method,args", [
    ("on_message", ({},)),
    ("send_reply", ("conversation", "hello")),
    ("send_file", ("conversation", "/files/specimen", "specimen.pdf")),
    ("post_update", ("working",)),
    ("mention", ("officer", "review requested")),
    ("office_action", ("walk",)),
])
@pytest.mark.asyncio
async def test_unconfigured_external_adapter_fails_explicitly(method, args):
    with pytest.raises(NotImplementedError, match="fill from the Nonilion Bot API guide"):
        await getattr(NonilionAdapter(), method)(*args)


@pytest.mark.parametrize("role", ["ops", "borrower"])
@pytest.mark.parametrize("command", ["approve L-1043", "decline L-1043"])
def test_api_preserves_officer_only_decision_boundary(office, role, command):
    client, app, model = office
    before = app.state.store.get("loan", "L-1043")
    response = client.post("/api/chat", json={"message": command, "role": role, "conversation_id": "role-check"})
    assert response.status_code == 202
    assert completed_job(client, response.json()["job_id"])["status"] == "completed"
    assert app.state.store.get("loan", "L-1043") == before
    replies = [event["text"] for event in app.state.store.all_events() if event["kind"] == "reply"]
    assert any("Only the credit officer" in reply for reply in replies)
    assert model.calls == []


@pytest.mark.parametrize("payload", [
    {"message": "hello", "role": "admin"},
    {"message": ""},
    {"message": "  "},
    {"message": "x" * 8001},
    {"message": "hello", "conversation_id": "../another-room"},
])
def test_invalid_chat_inputs_are_rejected_before_job_creation(office, payload):
    client, app, _ = office
    assert client.post("/api/chat", json=payload).status_code == 422
    assert app.state.store.all("job") == []


def test_message_limit_is_twenty_per_ip_not_per_claimed_forwarding_header(office):
    client, app, _ = office

    async def noop(event):
        return None

    app.state.adapter.on_message = noop
    for index in range(20):
        response = client.post("/api/chat", json={"message": "list loans"}, headers={"X-Forwarded-For": f"192.0.2.{index + 1}"})
        assert response.status_code == 202
    response = client.post("/api/chat", json={"message": "list loans"}, headers={"X-Forwarded-For": "198.51.100.1"})
    assert response.status_code == 429
    assert "20 messages per 10 minutes" in response.json()["detail"]
    assert len(app.state.store.all("job")) == 20


def test_background_failure_is_visible_without_leaking_internal_exception(office):
    client, app, _ = office

    async def broken_agent(event):
        raise RuntimeError("private-provider-diagnostic")

    app.state.adapter.on_message = broken_agent
    response = client.post("/api/chat", json={"message": "hello", "conversation_id": "failure-check"})
    assert response.status_code == 202
    assert completed_job(client, response.json()["job_id"])["status"] == "failed"
    events = app.state.store.all_events()
    assert any(event["kind"] == "error" and "could not be completed" in event["text"] for event in events)
    assert all("private-provider-diagnostic" not in event["text"] for event in events)
    assert events[-1]["data"]["status"] == "failed"


def test_live_document_download_hides_server_paths_and_record_token(office, settings):
    client, app, _ = office
    record = register_document(app, settings)
    response = client.get(record["url"])
    assert response.status_code == 200
    assert "SYNTHETIC DEMO DATA" in response.text
    assert "report.txt" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    expires_at = datetime.fromisoformat(record["expires_at"])
    assert utcnow() + timedelta(hours=23, minutes=59) < expires_at < utcnow() + timedelta(hours=24, minutes=1)
    item = client.get("/api/state").json()["documents"][0]
    assert item["url"] == record["url"]
    assert "path" not in item
    assert "token" not in item


def test_expired_document_is_unavailable_and_omitted_from_state(office, settings):
    client, app, _ = office
    record = register_document(app, settings, expired=True)
    assert client.get(record["url"]).status_code == 404
    assert client.get("/api/state").json()["documents"] == []


def test_download_record_cannot_escape_output_directory(office, settings):
    client, app, _ = office
    record = register_document(app, settings, outside=True)
    assert client.get(record["url"]).status_code == 404
    assert client.get("/files/does-not-exist").status_code == 404


def test_upload_over_five_megabytes_is_rejected(office):
    client, app, _ = office
    response = client.post("/api/upload", files={"file": ("statement.csv", b"a" * (MAX_UPLOAD_BYTES + 1), "text/csv")}, data={"loan_id": "L-1043", "role": "ops"})
    assert response.status_code == 413
    assert "5 MB" in response.json()["detail"]
    assert app.state.store.all("job") == []


@pytest.mark.parametrize("filename,content,mimetype", [
    ("script.exe", b"not executable in this office", "application/octet-stream"),
    ("broken.json", b"{invalid}", "application/json"),
    ("fake.pdf", b"not a PDF", "application/pdf"),
])
def test_bad_upload_types_and_contents_fail_explicitly(office, filename, content, mimetype):
    client, app, _ = office
    response = client.post("/api/upload", files={"file": (filename, content, mimetype)}, data={"loan_id": "L-1043"})
    assert response.status_code == 400
    assert response.json()["detail"]
    assert app.state.store.all("job") == []


def test_upload_rejects_unknown_loan_and_changed_post_review_documents(office):
    client, app, _ = office
    files = {"file": ("application.json", b'{"name":"Synthetic Applicant"}', "application/json")}
    assert client.post("/api/upload", files=files, data={"loan_id": "L-9999"}).status_code == 404
    loan = app.state.store.get("loan", "L-1043")
    loan["status"] = "approved"
    app.state.store.put("loan", loan["id"], loan)
    response = client.post("/api/upload", files=files, data={"loan_id": loan["id"]})
    assert response.status_code == 409
    assert "cannot change after review" in response.json()["detail"]
    assert app.state.store.all("job") == []
