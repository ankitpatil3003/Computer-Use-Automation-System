"""Replay against the live mock app: one test per outcome class, plus policy and drift."""

import urllib.request
from pathlib import Path

import pytest

from cua.cli import load_profile
from cua.policy import Policy
from cua.replay import Replayer
from cua.schema import Capability, RoleStrategy, Step
from cua.surface import PlaywrightSurface
from mockbank import server

ROOT = Path(__file__).parent.parent
CAP = Capability.load(Path(__file__).parent / "fixtures" / "get_savings_balance.hand.json")
POLICY = Policy.load(ROOT / "policy.json")
PROFILE = load_profile(CAP.app.profile, ROOT / "profiles")
SECRETS = {"MOCKBANK_USER": server.USER, "MOCKBANK_PASSWORD": server.PASSWORD}


@pytest.fixture(scope="module")
def surface():
    with PlaywrightSurface(allow_url=POLICY.allows_url) as s:
        yield s


@pytest.fixture
def replay(surface, base, tmp_path):
    def run(member_id="12345", cap=CAP, faults="", secrets=SECRETS, **kw):
        urllib.request.urlopen(f"{base}/__faults?reset=1&{faults}").read()
        surface.blocked.clear()
        kw.setdefault("allow_draft", True)  # the hand-written fixture is a draft
        r = Replayer(cap, PROFILE, POLICY, surface, secrets=secrets, tenant={"base_url": base},
                     runs_dir=tmp_path, **kw)
        return r.run({"member_id": member_id}), Path(r.dir)
    return run


def with_steps(*steps, **over) -> Capability:
    return CAP.model_copy(update={"steps": [*CAP.steps, *steps], **over})


def test_success_returns_typed_outputs_and_logs_nothing_sensitive(replay):
    res, run_dir = replay()
    assert res.status == "success", res.error
    assert res.outputs == {"savings_balance": "4210.55", "member_name": "Jordan A. Rivera"}
    persisted = (run_dir / "log.jsonl").read_text() + (run_dir / "result.json").read_text()
    for raw in [server.PASSWORD, "12345", "4210.55", "4,210.55", "Jordan"]:
        assert raw not in persisted
    assert not list(run_dir.glob("*.png"))  # no screenshots on the happy path


@pytest.mark.parametrize("member_id, code", [
    ("99999", "member_not_found"), ("00000", "invalid_member_number"), ("24680", "permission_denied")])
def test_business_outcomes(replay, member_id, code):
    res, _ = replay(member_id)
    assert res.status == "business_outcome" and res.outcome.code == code and res.error is None


def test_contract_violation_never_touches_the_ui(replay):
    res, run_dir = replay("12a45")
    assert res.status == "failed" and res.error.code == "input_invalid"
    assert "step_start" not in (run_dir / "log.jsonl").read_text()


@pytest.mark.parametrize("faults, codes", [
    ("interstitial=1&slow=1&slow_ms=1500", ["system_notice"]),
    ("error500=1", ["server_error"]),
    ("expire=1", ["session_expired"]),
])
def test_recoverable_conditions(replay, faults, codes):
    res, _ = replay(faults=faults)
    assert res.status == "success", res.error
    assert [r["code"] for r in res.recoveries] == codes


def test_persistent_error_exhausts_budget_with_evidence(replay):
    res, run_dir = replay(faults="error500=9")
    assert res.status == "failed" and res.error.code == "recovery_exhausted"
    assert any(p.endswith(".png") for p in res.error.evidence)
    assert all((run_dir / p).exists() for p in res.error.evidence)


def test_hard_failure_bad_credentials(replay):
    res, _ = replay(secrets={**SECRETS, "MOCKBANK_PASSWORD": "wrong"})
    assert res.status == "failed" and res.error.code == "app_error" and "sign_on_rejected" in res.error.observed
    assert "wrong" not in res.model_dump_json()


def test_human_required_state(replay):
    res, _ = replay("13579")
    assert res.status == "needs_human" and res.outcome.code == "supervisor_override_required"
    assert res.outcome.evidence


def test_irreversible_step_is_gated(replay):
    close = Step(id="close_account", action="click", intent="Close the account",
                 target={"frame": ["main"], "strategies": [RoleStrategy(role="button", name="Close Account")]},
                 expect={"frame": ["main"], "text": "Closure request submitted"})
    res, _ = replay(cap=with_steps(close))  # declared safe, but policy classifies it irreversible
    assert res.status == "needs_human" and res.outcome.code == "irreversible_step"
    res, _ = replay(cap=with_steps(close, status="approved"), confirm_irreversible=True)
    assert res.status == "failed" and res.error.code == "checkpoint_failed"  # it ran; success page is gone


def test_policy_blocks_off_allowlist_navigation(replay):
    res, _ = replay(cap=with_steps(Step(id="leave", action="goto", value="https://example.com/", intent="x")))
    assert res.status == "failed" and res.error.code == "policy_violation"


def test_drift_is_tolerated_and_reported(replay):
    steps = [s.model_copy(deep=True) for s in CAP.steps]
    steps[-1].target.strategies.insert(0, RoleStrategy(role="button", name="Search"))  # renamed in "v2.5"
    res, run_dir = replay(cap=CAP.model_copy(update={"steps": steps}))
    assert res.status == "success"
    assert '"locator_degraded"' in (run_dir / "log.jsonl").read_text()


def test_missing_control_is_a_hard_failure(replay):
    steps = [s.model_copy(deep=True) for s in CAP.steps]
    steps[-1].target.strategies = [RoleStrategy(role="button", name="Search")]
    steps[-1].timeout_ms = 500
    res, _ = replay(cap=CAP.model_copy(update={"steps": steps}))
    assert res.status == "failed" and res.error.code == "locator_not_found" and res.error.step_id == "submit_search"
    assert res.error.evidence


def test_unattended_replay_requires_an_approved_capability(replay):
    res, run_dir = replay(allow_draft=False)
    assert res.status == "failed" and res.error.code == "not_approved"
    assert "step_start" not in (run_dir / "log.jsonl").read_text()  # refused before touching the UI


def test_unexpected_native_dialog_is_cancelled_and_stops_the_run(replay):
    res, run_dir = replay(faults="confirm=1")
    assert res.status == "failed" and res.error.code == "unclassified_state"
    assert "native dialog" in res.error.observed and res.error.step_id == "submit_search"
    assert "Member Detail" not in res.error.observed  # Cancel meant the search never ran
    assert any(p.endswith(".png") for p in res.error.evidence)
