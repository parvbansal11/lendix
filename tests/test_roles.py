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
@pytest.mark.parametrize("command", ["approve L-1043", "approve L-1043 at 150000", "decline L-1043", "approve it anyway", "override L-1043 because Verified affordability and recorded mitigants"])
async def test_non_risk_heads_cannot_decide(office, role, command):
    runner, store, model = office
    reply = await runner.run(command, role=role)
    assert "Only the risk head" in reply
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
    denied = await runner.run("override L-1042", role="risk_head")
    assert "typed reason" in denied
    assert store.get("loan", "L-1042")["status"] == "declined"
    await runner.run("override L-1042 because Additional verification requires review of the recorded exception", role="risk_head")
    assert store.get("loan", "L-1042")["status"] == "escalated"
    approved = await runner.run("approve L-1042 at 150000 because I reviewed supporting verification for this synthetic exception", role="risk_head")
    assert "approval recorded" in approved.lower()
    loan = store.get("loan", "L-1042")
    assert loan["approval_source"] == "risk_head"
    assert any(event["kind"] == "decision" and event["data"].get("role") == "risk_head"
               and "I reviewed supporting verification" in str(event["data"]) for event in store.all_events())
    await runner.run("accept KFS L-1042", role="borrower")
    blocked = await runner.run("disburse L-1042", role="ops")
    assert "own verified" in blocked
    assert store.get("loan", "L-1042")["status"] == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("loan_id,band", [("L-1042", "high"), ("L-1043", "low"), ("L-1045", "medium")])
@pytest.mark.parametrize("command", ["approve", "decline"])
async def test_all_escalated_decisions_require_specific_typed_reason(office, loan_id, band, command):
    runner, store, _ = office
    await runner.run(f"{'add bank statement' if loan_id == 'L-1042' else 'review'} {loan_id}", role="ops")
    if loan_id != "L-1045":
        await runner.run(f"override {loan_id} because Verify the recorded exception before changing its decision", role="risk_head")
    before = store.get("loan", loan_id)
    assert before["status"] == "escalated" and before["risk"]["band"] == band
    for suffix in ("", " because", " because ok", " reason:"):
        reply = await runner.run(f"{command} {loan_id}{suffix}", role="risk_head")
        assert "typed reason" in reply.lower()
        assert store.get("loan", loan_id) == before
    assert not any(event["kind"] == "decision" and event["loan_id"] == loan_id
                   and event["data"].get("role") == "risk_head" for event in store.all_events())


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["approve", "decline"])
@pytest.mark.parametrize("loan_id,setup", [("L-1043", None), ("L-1043", "review L-1043"), ("L-1042", "add bank statement L-1042"), ("L-1044", None)])
async def test_risk_head_cannot_directly_decide_non_escalated_application(office, command, loan_id, setup):
    runner, store, _ = office
    if setup:
        await runner.run(setup, role="ops")
    before = store.get("loan", loan_id)
    reply = await runner.run(f"{command} {loan_id} because Verified affordability and recorded mitigants", role="risk_head")
    assert "escalat" in reply.lower()
    assert store.get("loan", loan_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ops", "borrower"])
async def test_automatic_decision_override_is_restricted_to_risk_head(office, role):
    runner, store, _ = office
    await runner.run("review L-1043", role="ops")
    before = store.get("loan", "L-1043")
    reply = await runner.run("override L-1043 because Verified affordability and recorded mitigants", role=role)
    assert "Only the risk head" in reply
    assert store.get("loan", "L-1043") == before


@pytest.mark.asyncio
async def test_override_requires_reason_and_explicit_second_decision(office):
    runner, store, _ = office
    await runner.run("review L-1043", role="ops")
    before = store.get("loan", "L-1043")
    for command in ("override L-1043", "override L-1043 because", "override L-1043 because ok"):
        assert "typed reason" in (await runner.run(command, role="risk_head")).lower()
        assert store.get("loan", "L-1043") == before
    reason = "New information requires review of the automatic decision"
    await runner.run(f"override L-1043 because {reason}", role="risk_head")
    escalated = store.get("loan", "L-1043")
    assert escalated["status"] == "escalated"
    assert not any(event["kind"] == "decision" and event["data"].get("role") == "risk_head" for event in store.all_events())
    assert any(event["kind"] == "escalation" and reason in str(event) for event in store.all_events())
    assert "typed reason" in (await runner.run("decline L-1043", role="risk_head")).lower()
    assert store.get("loan", "L-1043")["status"] == "escalated"
    decision_reason = "The new evidence does not meet the recorded affordability requirements"
    await runner.run(f"decline L-1043 because {decision_reason}", role="risk_head")
    assert store.get("loan", "L-1043")["status"] == "declined"
    assert any(event["kind"] == "decision" and event["data"].get("role") == "risk_head"
               and decision_reason in str(event["data"]) for event in store.all_events())


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["approve", "decline"])
async def test_restructure_escalation_cannot_be_processed_as_new_credit_approval(office, command):
    runner, store, _ = office
    await runner.run("I lost my job L-1044", role="borrower")
    before = store.get("loan", "L-1044")
    assert before["status"] == "restructure_requested"
    reply = await runner.run(f"{command} L-1044 because Verified affordability and recorded mitigants", role="risk_head")
    assert "restructur" in reply.lower() or "escalat" in reply.lower()
    assert store.get("loan", "L-1044") == before


@pytest.mark.asyncio
async def test_acceptance_audit_batch_and_payout_roles(office):
    runner, store, _ = office
    assert "Only the risk head" in await runner.run("export audit", role="ops")
    assert "Portfolio review is for" in await runner.run("review today's applications", role="borrower")
    assert "Only ops or the risk head" in await runner.run("disburse L-1043", role="borrower")
    assert "Only ops or the risk head" in await runner.run("close L-1044", role="borrower")
    await runner.run("review L-1043", role="ops")
    assert "must be given by the borrower" in await runner.run("accept KFS L-1043", role="ops")
    assert not store.get("loan", "L-1043")["kfs_accepted"]
    assert "audit log is ready" in await runner.run("export audit", role="risk_head")


def test_underwriter_owns_policy_decisions_and_credit_only_computes_numbers():
    assert len(AGENTS) == 9
    assert AGENTS["Underwriter"].tools == frozenset({"apply_policy", "draft_decision_note", "escalate"})
    assert AGENTS["Credit"].tools == frozenset({"compute_affordability", "bureau_lookup"})
    for name, agent in AGENTS.items():
        if name != "Underwriter":
            with pytest.raises(PermissionError):
                agent.authorize("apply_policy")


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
