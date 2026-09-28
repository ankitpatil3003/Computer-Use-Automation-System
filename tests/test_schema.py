import pytest
from pydantic import ValidationError

from cua.schema import Capability

BASE = {
    "id": "mockbank.member.get_savings_balance",
    "version": "1.0.0",
    "description": "d",
    "app": {"product": "mockbank-core", "versions": ">=2,<3", "profile": "mockbank-core@2"},
    "entry": "{{tenant.base_url}}/",
    "inputs": {"member_id": {"type": "string", "description": "d", "pattern": r"^\d{5}$"}},
    "steps": [{"id": "s1", "action": "fill", "value": "{{inputs.member_id}}", "intent": "i",
               "target": {"frame": ["main"], "strategies": [{"by": "text_anchor", "anchor": "Member #", "role": "textbox"}]}}],
    "success": [{"text": "Member Detail"}],
    "provenance": {"recorded_at": "2026-09-28T00:00:00Z", "recorded_by": "hand"},
}


def cap(**over):
    return Capability.model_validate({**BASE, **over})


def test_round_trip(tmp_path):
    c = cap()
    c.save(tmp_path / "c.json")
    assert Capability.load(tmp_path / "c.json") == c


@pytest.mark.parametrize("over", [
    {"steps": [{**BASE["steps"][0], "value": "{{inputs.nope}}"}]},        # undeclared input
    {"steps": [BASE["steps"][0], BASE["steps"][0]]},                       # duplicate step ids
    {"steps": [{**BASE["steps"][0], "action": "goto"}]},                   # goto with a target
    {"success": [{"frame": ["main"]}]},                                    # empty check
    {"outcomes": [{"code": "x", "kind": "recoverable", "message": "m", "when": {"text": "t"}}]},  # no recover
    {"version": "1.0"},
    {"surprise": 1},                                                        # unknown fields rejected
])
def test_rejects_malformed(over):
    with pytest.raises(ValidationError):
        cap(**over)
