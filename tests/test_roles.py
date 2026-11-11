import pytest

from app.agents import AGENTS
from app.config import ROOT, Settings
from app.model import FakeClient
from app.orchestrator import Orchestrator
from app.store import Store


@pytest.fixture
def office(tmp_path):
    settings = Settings(gemini_api_key="test-only", model_fast="fast-test", model_deep="deep-test",
                        db_path=str(tmp_path / "loans.db"), output_dir=str(tmp_path / "out"), demo_skip_cooling=True)
    store = Store(settings.db_path)
    store.seed(ROOT / "data" / "seed.json")
    model = FakeClient()
    yield Orchestrator(store, settings, model), store, model
    store.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["borrower", "ops"])
@pytest.mark.parametrize("command", ["approve L-1043", "approve L-1043 at 150000", "decline L-1043", "approve it anyway"])
async def test_non_officers_cannot_decide(office, role, command):
    runner, store, model = office
    reply = await runner.run(command, role=role)
    assert "Only the credit officer" in reply
    assert store.get("loan", "L-1043")["status"] == "new"
    assert not model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("message,expected", [
    ("Will this person default?", "cannot predict"),
    ("Message the borrower's employer", "Fair practices"),
    ("Look up this person's social media", "consented application data"),
    ("Threaten L-1044 so they pay", "cannot threaten"),
    ("Plan a holiday", "Lendix handles loan applications"),
    ("review L-9999", "cannot find that loan"),
    ("help", "Which loan or task"),
])
async def test_impossible_unsafe_unknown_and_ambiguous_requests(office, message, expected):
    runner, _, _ = office
    assert expected in await runner.run(message, role="ops")


@pytest.mark.asyncio
async def test_high_risk_override_requires_reason_and_cannot_bypass_account_check(office):
    runner, store, _ = office
    await runner.run("add bank statement L-1042", role="ops")
    denied = await runner.run("approve L-1042 at 150000", role="credit_officer")
    assert "typed reason" in denied
    assert store.get("loan", "L-1042")["status"] == "awaiting_decision"
    approved = await runner.run("approve L-1042 at 150000 because I reviewed supporting verification for this synthetic exception", role="credit_officer")
    assert "approval recorded" in approved.lower()
    loan = store.get("loan", "L-1042")
    assert loan["override_reason"]
    assert any(event["kind"] == "decision" and event["data"].get("override_reason") for event in store.all_events())
    await runner.run("accept KFS L-1042", role="borrower")
    blocked = await runner.run("disburse L-1042", role="ops")
    assert "own verified" in blocked
    assert store.get("loan", "L-1042")["status"] == "approved"


@pytest.mark.asyncio
async def test_acceptance_audit_batch_and_payout_roles(office):
    runner, store, _ = office
    assert "Only the credit officer" in await runner.run("export audit", role="ops")
    assert "Portfolio review is for" in await runner.run("review today's applications", role="borrower")
    assert "Only ops or the credit officer" in await runner.run("disburse L-1043", role="borrower")
    assert "Only ops or the credit officer" in await runner.run("close L-1044", role="borrower")
    await runner.run("review L-1043", role="ops")
    await runner.run("approve L-1043", role="credit_officer")
    assert "must be given by the borrower" in await runner.run("accept KFS L-1043", role="ops")
    assert not store.get("loan", "L-1043")["kfs_accepted"]
    assert "audit log is ready" in await runner.run("export audit", role="credit_officer")


@pytest.mark.parametrize("name", list(AGENTS))
def test_agents_have_strict_tool_allowlists_and_structured_outputs(name):
    agent = AGENTS[name]
    assert agent.role and agent.output_schema and agent.system_instruction
    assert "UNTRUSTED_DATA" in agent.system_instruction
    assert agent.tools
    for tool in agent.tools:
        agent.authorize(tool)
    with pytest.raises(PermissionError):
        agent.authorize("arbitrary_shell_execution")
    if name != "Disbursement":
        with pytest.raises(PermissionError):
            agent.authorize("simulate_payout")


def test_orchestrator_checks_allowlist_before_any_tool_executes(office):
    runner, _, _ = office
    called = []
    with pytest.raises(PermissionError):
        runner.tool("Intake", "simulate_payout", lambda: called.append(True))
    assert called == []
