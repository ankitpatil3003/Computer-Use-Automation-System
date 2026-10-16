from pathlib import Path

from cua.policy import Policy, Redactor
from cua.schema import Step

POLICY = Policy.load(Path(__file__).parent.parent / "policy.json")


def test_allowlist():
    assert POLICY.allows_url("http://127.0.0.1:8765/inquiry")
    assert not POLICY.allows_url("https://example.com/")
    assert not POLICY.allows_url("http://127.0.0.1:8765/__faults?reset=1")
    assert not POLICY.allows_url("file:///etc/passwd")


def test_risk_is_the_stricter_of_artifact_and_policy():
    def step(name, risk="safe"):
        return Step(id="s", action="click", intent="i", risk=risk,
                    target={"strategies": [{"by": "role", "role": "button", "name": name}]})
    assert POLICY.risk(step("Close Account")) == "irreversible"
    assert POLICY.risk(step("Authorize")) == "irreversible"
    assert POLICY.risk(step("Go")) == "safe"
    assert POLICY.risk(step("Go", risk="irreversible")) == "irreversible"


def test_redaction():
    r = Redactor()
    r.add("12345", "pii_id", "member_id")
    r.add("hunter22", "secret", "PW")
    r.add("Jordan A. Rivera", "pii", "member_name")
    out = r("member 12345 Jordan A. Rivera pw=hunter22 ssn 123-45-6789 acct S-0012345-01 a@b.com")
    for raw in ["12345", "Jordan", "hunter22", "123-45-6789", "a@b.com"]:
        assert raw not in out
    assert "***45" in out and "[SECRET]" in out
    assert r.scrub({"k": ["hunter22"]}) == {"k": ["[SECRET]"]}


def test_profile_labelled_fields_and_amounts_are_masked_without_knowing_the_values():
    screen = "Name | Casey M. Lee |\nSSN | ***-**-3366 |\nShare Savings | S-0013579-01 | ($602.19) |\nNamed | ok |"
    out = Redactor(["Name", "SSN"])(screen)
    assert out.splitlines() == ["Name | [PII:Name] |", "SSN | [PII:SSN] |",
                                "Share Savings | ****9-01 | [AMOUNT] |", "Named | ok |"]
