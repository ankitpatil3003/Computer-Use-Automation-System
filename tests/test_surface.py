"""Locator recording + resolution against the live (hostile) mock app, headless."""

import re

import pytest

from cua.schema import Check, CssStrategy, Locator, RoleStrategy
from cua.surface import LocatorError, PlaywrightSurface
from mockbank import server


@pytest.fixture
def surface():
    with PlaywrightSurface() as s:
        yield s


def ref(obs: str, pattern: str) -> str:
    return re.search(r"\[(e\d+) " + pattern, obs).group(1)


def test_record_then_drive_frameset_flow(surface, base):
    s = surface
    s.goto(base + "/")
    obs = s.observe()
    user = s.locator_for(ref(obs, 'textbox ""'))
    assert user.strategies[0].by == "text_anchor" and user.strategies[0].anchor == "User ID"
    assert user.strategies[-1].by == "css"  # structural fallback always ranks last
    s.perform("fill", user, server.USER, 5000)
    s.perform("fill", s.locator_for(ref(obs, 'textbox "" password')), server.PASSWORD, 5000)
    s.perform("click", s.locator_for(ref(obs, 'button "Sign On"')), None, 5000)

    assert s.wait_for(Check(frame=["menu"], text="Member Inquiry"), 5000)
    link = s.locator_for(ref(s.observe(), 'link "Member Inquiry"'))
    assert link.frame == ["menu"] and link.strategies[0] == RoleStrategy(role="link", name="Member Inquiry")
    s.perform("click", link, None, 5000)

    assert s.wait_for(Check(frame=["main"], text="Member #"), 5000)
    obs = s.observe()
    s.perform("fill", s.locator_for(ref(obs, 'textbox ""')), "12345", 5000)
    s.perform("click", s.locator_for(ref(obs, 'button "Go"')), None, 5000)
    assert s.wait_for(Check(frame=["main"], text="Member Detail"), 5000)

    balance = s.cell_locator(["main"], "Share Savings", "Balance")
    assert s.read(balance, 1000) == "$4,210.55"
    assert s.read(s.cell_locator(["main"], "Name", None), 1000) == "Jordan A. Rivera"
    assert not s.holds(Check(frame=["main"], text="E404"))


def test_resolution_rules(surface, base):
    s = surface
    s.goto(base + "/login")
    # Ambiguity is a failure, never "pick the first".
    with pytest.raises(LocatorError) as e:
        s.resolve(Locator(strategies=[CssStrategy(selector="input")]), 300)
    assert e.value.code == "locator_ambiguous"
    # A lower-ranked strategy winning is reported (drift signal), not hidden.
    drifted = Locator(strategies=[RoleStrategy(role="button", name="Log In"), CssStrategy(selector='input[type="submit"]')])
    assert s.resolve(drifted, 300)[1] == 1
    # Missing frame / element → not found after the timeout.
    with pytest.raises(LocatorError) as e:
        s.resolve(Locator(frame=["main"], strategies=[CssStrategy(selector="input")]), 300)
    assert e.value.code == "locator_not_found"
