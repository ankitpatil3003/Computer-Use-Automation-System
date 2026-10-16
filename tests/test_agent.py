"""Discovery pipeline with a scripted stand-in for the LLM (the real run is in /evidence).

Tests everything around the model: tool execution through the shared locator/policy path,
checkpoint derivation, parameterization, transient-dialog filtering, escalation, and the
save rule (validation replay must reproduce the outputs).
"""

import json
import re
import urllib.request
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cua.agent import Discovery
from cua.cli import load_profile
from cua.policy import Policy
from cua.schema import Capability
from cua.surface import PlaywrightSurface
from mockbank import server

ROOT = Path(__file__).parent.parent
POLICY = Policy.load(ROOT / "policy.json")
PROFILE = load_profile("mockbank-core@2", ROOT / "profiles")
SECRETS = {"MOCKBANK_USER": server.USER, "MOCKBANK_PASSWORD": server.PASSWORD}


class ScriptedLLM:
    """Reads the screen like the model would and returns the next tool call."""

    def __init__(self, member_id: str, detour: str | None = None):
        self.member_id, self.detour, self.i = member_id, detour, 0
        self.chat = NS(completions=NS(create=self.create))

    def create(self, model, messages, tools, **kw):
        name, args = self.decide(messages[-1]["content"])
        self.i += 1
        call = NS(id=f"c{self.i}", function=NS(name=name, arguments=json.dumps(args)))
        return NS(choices=[NS(message=NS(content=None, tool_calls=[call]))], usage=None)

    def decide(self, s: str):
        def ref(pattern):
            return re.search(pattern, s).group(1)
        if "SYSTEM NOTICE" in s:
            return "click", {"ref": ref(r'\[(e\d+) button "Acknowledge"'), "intent": "Dismiss notice"}
        if "SUPERVISOR OVERRIDE" in s:
            return "ask_human", {"reason": "supervisor override required"}
        if "Sign On" in s and "User ID" in s:
            if "value=" not in s.split("User ID")[1].split("\n")[0]:
                return "fill_secret", {"ref": ref(r"User ID \| \[(e\d+)"), "secret": "MOCKBANK_USER", "intent": "Enter user ID"}
            if not getattr(self, "pw", False):
                self.pw = True
                return "fill_secret", {"ref": ref(r"Password \| \[(e\d+)"), "secret": "MOCKBANK_PASSWORD", "intent": "Enter password"}
            return "click", {"ref": ref(r'\[(e\d+) button "Sign On"'), "intent": "Sign on"}
        if "Member Detail" in s:
            if self.detour == "close":
                return "click", {"ref": ref(r'\[(e\d+) button "Close Account"'), "intent": "Close it"}
            if not getattr(self, "extracted", False):
                self.extracted = True
                return "extract", {"output": "savings_balance", "frame": "main", "row": "Share Savings",
                                   "column": "Balance", "type": "decimal", "sensitivity": "financial",
                                   "description": "Share savings balance"}
            return "finish", {"success_text": "Member Detail", "frame": "main", "capability_name": "get_savings_balance",
                              "description": "Look up a member and return the savings balance.",
                              "input_descriptions": {"member_id": "5-digit member number"}}
        if "Member #" in s:
            if f'value="{self.member_id}"' not in s:
                return "fill", {"ref": ref(r"Member # \| \[(e\d+)"), "value": self.member_id, "intent": "Enter member number"}
            return "click", {"ref": ref(r'\[(e\d+) button "Go"'), "intent": "Search for the member"}
        return "click", {"ref": ref(r'\[(e\d+) link "Member Inquiry"'), "intent": "Open Member Inquiry"}


@pytest.fixture(scope="module")
def surface():
    with PlaywrightSurface(allow_url=POLICY.allows_url) as s:
        yield s


def discover(surface, base, tmp_path, member_id="12345", faults="", detour=None):
    urllib.request.urlopen(f"{base}/__faults?reset=1&{faults}").read()
    d = Discovery("Look up the member and read their savings balance", base + "/", {"member_id": member_id},
                  client=ScriptedLLM(member_id, detour), model="scripted", policy=POLICY, profile=PROFILE,
                  surface=surface, secrets=SECRETS, base_url=base, runs_dir=tmp_path, max_steps=15)
    return d.run(), d


def test_discovery_records_a_replayable_capability(surface, base, tmp_path):
    summary, d = discover(surface, base, tmp_path, faults="interstitial=1")
    assert summary["status"] == "success", summary
    assert summary["validation"]["outputs_match"]
    cap = Capability.load(summary["artifact"])
    assert cap.entry == "{{tenant.base_url}}/" and cap.status == "draft"
    assert [s.action for s in cap.steps] == ["fill", "fill", "click", "click", "fill", "click"]  # notice not recorded
    assert cap.steps[0].value == "{{secrets.MOCKBANK_USER}}"
    assert cap.steps[4].value == "{{inputs.member_id}}"
    assert cap.inputs["member_id"].pattern == r"^\d{5}$"
    for click in [s for s in cap.steps if s.action == "click"]:
        assert click.expect is not None and click.expect.text  # every navigating click has a checkpoint
    assert cap.steps[-1].expect.text == "Member Detail"
    assert server.PASSWORD not in (d.dir / "log.jsonl").read_text()


def test_irreversible_action_escalates(surface, base, tmp_path):
    summary, d = discover(surface, base, tmp_path, detour="close")
    assert summary["status"] == "needs_human" and "irreversible" in summary["reason"]
    assert "Closure request submitted" not in surface.observe()  # the click never happened
    assert json.loads((d.dir / "intervention.json").read_text())["screenshot"]


def test_agent_asks_for_help(surface, base, tmp_path):
    summary, _ = discover(surface, base, tmp_path, member_id="13579")
    assert summary["status"] == "needs_human" and "supervisor" in summary["reason"]
