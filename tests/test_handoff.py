"""Handoff: the lease state machine, the control gate, and live takeover on the same session.

The "human" is simulated by driving the raw Playwright page (not Surface, which a human never
goes through) from inside the replayer's wait loop — same thread, same live browser session.
"""

import json
import urllib.request
from pathlib import Path

import pytest

from cua.cli import load_profile
from cua.handoff import ControlError, Lease
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
OPERATOR = "human:supervisor1"


def test_lease_transitions(tmp_path):
    lease = Lease(tmp_path / "control.json", "r1")
    with pytest.raises(ControlError):
        lease.transition("HUMAN", OPERATOR)  # nobody asked for help yet
    lease.transition("AWAITING_HUMAN", "automation")
    lease.transition("HUMAN", OPERATOR)
    with pytest.raises(ControlError, match="held by"):
        lease.transition("AUTOMATION", "human:someone_else")  # only the holder hands back
    state = lease.transition("AUTOMATION", OPERATOR)
    assert state["holder"] == "automation" and state["seq"] == 3
    assert [h["to"] for h in state["history"]] == ["AWAITING_HUMAN", "HUMAN", "AUTOMATION"]
    lease.transition("AWAITING_HUMAN", "automation")
    lease.transition("ABORTED", "automation")
    with pytest.raises(ControlError):
        lease.transition("AWAITING_HUMAN", "automation")  # terminal


@pytest.fixture(scope="module")
def surface():
    with PlaywrightSurface(allow_url=POLICY.allows_url) as s:
        yield s


def replay(surface, base, tmp_path, monkeypatch, member_id, human=None, cap=CAP, wait_s=30):
    """Run a replay; `human(page)` is performed once, as the operator, after claiming the lease."""
    urllib.request.urlopen(f"{base}/__faults?reset=1").read()
    r = Replayer(cap, PROFILE, POLICY, surface, secrets=SECRETS, tenant={"base_url": base},
                 runs_dir=tmp_path, handoff_wait_s=wait_s, allow_draft=True)
    real_idle, done = surface.idle, []

    def idle(ms):
        if human and not done and r.lease.state == "AWAITING_HUMAN":
            done.append(1)
            r.lease.transition("HUMAN", OPERATOR)
            with pytest.raises(ControlError):  # automation is locked out while the human holds it
                surface.perform("click", CAP.steps[-1].target, None, 500)
            release_to = human(surface.page)
            surface.idle(300)  # let the page report the human's last actions
            r.lease.transition(release_to, OPERATOR)
        real_idle(ms)

    monkeypatch.setattr(surface, "idle", idle)
    return r.run({"member_id": member_id}), Path(r.dir)


def authorize_override(page):
    main = page.frame(name="main")
    main.fill("input[name=f2]", server.OVERRIDE_CODE)
    main.click("input[value=Authorize]")
    return "AUTOMATION"


def test_operator_resolves_override_and_automation_resumes(surface, base, tmp_path, monkeypatch):
    res, run_dir = replay(surface, base, tmp_path, monkeypatch, "13579", authorize_override)
    assert res.status == "success", res.error
    assert res.outputs["savings_balance"] == "602.19"  # extracted by automation after resume
    [h] = res.handoffs
    assert h["code"] == "supervisor_override_required" and h["claimed_by"] == OPERATOR
    assert h["resolution"] == "AUTOMATION" and h["step_id"] == "submit_search"
    assert {"kind": "change", "role": "textbox", "name": "Override Code", "value": "[SECRET]"} in [
        {k: a[k] for k in ("kind", "role", "name", "value") if k in a} for a in h["actions"]]
    assert any(a["kind"] == "click" and a["name"] == "Authorize" for a in h["actions"])
    persisted = "".join(p.read_text(encoding="utf-8") for p in run_dir.iterdir() if p.suffix in (".json", ".jsonl", ".txt"))
    assert server.OVERRIDE_CODE not in persisted and "13579" not in persisted
    request = json.loads((run_dir / "intervention.json").read_text())
    assert request["live_session"] and request["step_id"] == "submit_search" and request["evidence"]
    assert json.loads((run_dir / "control.json").read_text())["state"] == "AUTOMATION"


def test_release_without_fixing_hands_off_again_then_stops(surface, base, tmp_path, monkeypatch):
    res, _ = replay(surface, base, tmp_path, monkeypatch, "13579", human=lambda page: "AUTOMATION")
    # re-classification on resume sees the override screen again → second request, unclaimed
    assert res.status == "needs_human" and len(res.handoffs) == 2


def test_unclaimed_request_times_out_to_needs_human(surface, base, tmp_path, monkeypatch):
    res, run_dir = replay(surface, base, tmp_path, monkeypatch, "13579", wait_s=0.5)
    assert res.status == "needs_human" and res.handoffs[0]["resolution"] == "ABORTED"
    assert res.handoffs[0]["resolved_by"] == "automation"


def test_irreversible_step_is_never_performed_on_a_release(surface, base, tmp_path, monkeypatch):
    close = Step(id="close_account", action="click", intent="Close the account",
                 target={"frame": ["main"], "strategies": [RoleStrategy(role="button", name="Close Account")]},
                 expect={"frame": ["main"], "text": "Closure request submitted"})
    cap = CAP.model_copy(update={"steps": [*CAP.steps, close]})
    res, _ = replay(surface, base, tmp_path, monkeypatch, "12345", human=lambda page: "AUTOMATION", cap=cap)
    assert res.status == "failed" and res.error.code == "checkpoint_failed" and res.error.step_id == "close_account"
    assert "Closure request submitted" not in surface.observe()


def test_operator_completes_by_hand_and_success_is_still_verified(surface, base, tmp_path, monkeypatch):
    def finish(page):
        authorize_override(page)
        return "COMPLETED_BY_HUMAN"
    res, _ = replay(surface, base, tmp_path, monkeypatch, "13579", finish)
    assert res.status == "success" and res.outputs["savings_balance"] == "602.19"
    assert res.handoffs[0]["resolution"] == "COMPLETED_BY_HUMAN"
