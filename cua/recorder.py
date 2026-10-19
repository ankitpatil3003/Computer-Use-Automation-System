"""Recorder: turns what the agent did on the live surface into a capability artifact.

Nothing is copied from the model's transcript. Each step is rebuilt from facts verified on the
live page at the moment it happened:
  - the target locator (strategies verified to match exactly that element),
  - the checkpoint (static text that appeared because of the action and holds now),
  - parameterization (literal input values become {{inputs.x}} templates).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from cua.schema import AppRef, Capability, Check, Locator, Output, Param, Profile, Provenance, Step
from cua.surface import Surface

_REF = re.compile(r"\[e\d+[^\]]*\]")


def fragments(obs: str) -> dict[str, list[str]]:
    """Static text runs per frame label, in reading order (controls and their values removed)."""
    out: dict[str, list[str]] = {}
    frame = "top"
    for line in obs.splitlines():
        if line.startswith("--- frame: "):
            frame = line[len("--- frame: "):]
            out.setdefault(frame, [])
            continue
        for frag in _REF.sub("", line).split("|"):
            if frag.strip():
                out.setdefault(frame, []).append(frag.strip())
    return out


def frame_path(label: str) -> list[str]:
    return [] if label == "top" else label.split("/")


def slug(text: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40].strip("_") or "step"
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}_{n}", n + 1
    return sid


class Recorder:
    def __init__(self, surface: Surface, profile: Profile, inputs: dict[str, str]):
        self.surface, self.profile, self.inputs = surface, profile, inputs
        self.steps: list[Step] = []
        self.outputs: dict[str, Output] = {}
        self.values: dict[str, Any] = {}  # extracted during discovery; validation replay must match
        self._raw: list[str] = []

    def is_transient(self, target: Locator) -> bool:
        """A click that dismisses a known app-wide interstitial is the profile's job at replay
        time, not part of this capability's flow — don't bake it into the artifact."""
        return any(d.recover == "click" and d.target.frame == target.frame
                   and any(s in target.strategies for s in d.target.strategies) and self.surface.holds(d.when)
                   for d in self.profile.detectors)

    def template(self, literal: str) -> str:
        for name, value in sorted(self.inputs.items(), key=lambda kv: -len(kv[1])):
            if value and value in literal:
                literal = literal.replace(value, "{{inputs.%s}}" % name)
        return literal

    def data_dependent(self, text: str) -> bool:
        """Text that varies per invocation can't be a checkpoint (it would bind to this run's data)."""
        values = [*self.inputs.values(), *self._raw]
        return any(v and v in text for v in values) or sum(c.isdigit() for c in text) * 3 > len(text)

    def checkpoint(self, before: str, after: str) -> Check | None:
        """Pick static text that the action caused to appear, verified to hold now. Prefers text
        that was nowhere on screen before, then text new to its frame."""
        b, a = fragments(before), fragments(after)
        seen_anywhere = {f for frags in b.values() for f in frags}
        for tier in (lambda f, frame: f not in seen_anywhere, lambda f, frame: f not in b.get(frame, [])):
            for frame, frags in a.items():
                if frame == "(unnamed)":
                    continue
                for f in frags:
                    if len(f) >= 4 and tier(f, frame) and not self.data_dependent(f):
                        check = Check(frame=frame_path(frame), text=f)
                        if self.surface.holds(check):
                            return check
        return None

    def record(self, action: str, target: Locator, value: str | None, intent: str, expect: Check | None) -> Step:
        # The id comes from what the step does (action + control name), never from the model's
        # wording: re-recording the same flow yields the same ids, so tenant overlays keep applying.
        label = m[1] if (m := re.search(r'"(.+)"', target.description)) else target.description
        step = Step(id=slug(f"{action} {label}", {s.id for s in self.steps}), action=action, target=target,
                    value=None if value is None else self.template(value), intent=intent, expect=expect)
        self.steps.append(step)
        return step

    def extract(self, name: str, out: Output, raw: str, value: Any) -> None:
        self.outputs[name] = out
        self.values[name] = value
        self._raw += [raw, str(value)]

    def compile(self, *, name: str, description: str, input_descriptions: dict[str, str], success: Check,
                entry: str, base_url: str, model: str, run_id: str, goal: str) -> Capability:
        inputs = {
            # the model sometimes "describes" an input with its example value — never persist that
            n: Param(type="string", description=d if (d := input_descriptions.get(n)) and v not in d else n,
                     pattern=rf"^\d{{{len(v)}}}$" if v.isdigit() else None, sensitivity="pii_id")
            for n, v in self.inputs.items()
        }
        return Capability(
            id=f"{self.profile.product.replace('-', '_')}.{slug(name, set())}",
            version="1.0.0",
            status="draft",
            description=description,
            app=AppRef(product=self.profile.product, versions=f"{self.profile.id.split('@')[-1]}.x",
                       profile=self.profile.id),
            entry=entry.replace(base_url, "{{tenant.base_url}}", 1),
            inputs=inputs,
            outputs=self.outputs,
            steps=self.steps,
            success=[success],
            outcomes=[],  # business outcomes are declared at review; undeclared states fail safe
            provenance=Provenance(recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                  recorded_by=f"discovery:{model}", run_id=run_id, goal=goal),
        )
