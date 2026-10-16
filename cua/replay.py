"""Deterministic replay: the production execution path. No LLM anywhere in this module.

Per step:  settle (classify any known state) → guard (policy) → act → verify checkpoint.
Every state that isn't the expected one is classified against the capability's `outcomes` and
the app profile's detectors:
    business        → stop, return the outcome to the caller (not an error)
    recoverable     → dismiss / restart within a bounded budget, logged in `recoveries`
    hard            → stop with a debuggable failure + evidence
    human_required  → intervention request; with an operator wait configured, the run pauses on
                      the same live session, a human takes the lease, and the run resumes after
                      re-classifying the screen (see handoff.py)
Anything unrecognised is a failure, never a guess.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from playwright.sync_api import Error as PWError

from cua.handoff import ControlError, Lease, write_request
from cua.policy import Policy, PolicyViolation, Redactor
from cua.schema import TEMPLATE_RE, Capability, Check, Detector, Failure, Outcome, Output, Profile, RunResult, Step
from cua.surface import LocatorError, Surface

MAX_RECOVERIES = 3
MAX_HANDOFFS = 2  # a human who could not fix it twice will not on the third try
SUCCESS_TIMEOUT_MS = 10_000
POLL_S = 0.15


def new_run_dir(runs_dir: str | Path, kind: str) -> Path:
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{kind}-{secrets.token_hex(3)}"
    path = Path(runs_dir) / run_id
    path.mkdir(parents=True)
    return path


class RunLog:
    """Append-only JSONL event log. Everything is scrubbed by the redactor before it is written."""

    def __init__(self, run_dir: Path, redact: Redactor):
        self.path = run_dir / "log.jsonl"
        self.redact = redact

    def __call__(self, event: str, **data: Any) -> None:
        rec = self.redact.scrub({"t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                                 "event": event, **data})
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")


class _Finish(Exception):
    def __init__(self, status: str, outcome: Outcome | None = None, error: Failure | None = None):
        self.status, self.outcome, self.error = status, outcome, error


class _Restart(Exception):
    pass


class _HumanCompleted(Exception):
    pass


def convert(raw: str, out: Output) -> Any:
    text = raw.strip()
    if out.transform == "currency":
        negative = text.startswith("(") and text.endswith(")") or text.startswith("-")
        text = re.sub(r"[^\d.]", "", text)
        text = f"-{text}" if negative else text
    if out.type == "decimal":
        try:
            return str(Decimal(text))  # string keeps exact cents across JSON
        except InvalidOperation:
            raise ValueError(f"not a decimal: {raw!r}")
    if out.type == "integer":
        return int(text)
    if out.type == "boolean":
        if text.lower() not in ("true", "false", "yes", "no", "y", "n"):
            raise ValueError(f"not a boolean: {raw!r}")
        return text.lower() in ("true", "yes", "y")
    return text


class Replayer:
    def __init__(self, cap: Capability, profile: Profile, policy: Policy, surface: Surface, *,
                 secrets: Mapping[str, str], tenant: Mapping[str, str], runs_dir: str | Path = "runs",
                 confirm_irreversible: bool = False, handoff_wait_s: float = 0):
        self.cap, self.policy, self.surface = cap, policy, surface
        self.detectors: list[Detector] = [*cap.outcomes, *profile.detectors]  # specific before generic
        self.secrets, self.tenant = secrets, tenant
        self.confirm_irreversible = confirm_irreversible
        self.dir = new_run_dir(runs_dir, "replay")
        self.redact = Redactor()
        self.log = RunLog(self.dir, self.redact)
        self.recoveries: list[dict[str, Any]] = []
        self.handoffs: list[dict[str, Any]] = []
        self.irreversible_done = False
        self.ctx: dict[str, Mapping[str, str]] = {}
        self.handoff_wait_s = handoff_wait_s  # 0 = route the request and return needs_human
        self.lease = Lease(self.dir / "control.json", self.dir.name)

    # --- entry point ---------------------------------------------------------------------------

    def run(self, inputs: Mapping[str, str]) -> RunResult:
        t0 = time.monotonic()
        status, outcome, error, outputs = "success", None, None, {}
        try:
            self._bind(inputs)
            self.log("run_start", capability=self.cap.id, version=self.cap.version,
                     artifact_status=self.cap.status, inputs=dict(inputs))
            self.surface.lease = self.lease
            self._start()
            while True:
                try:
                    try:
                        for step in self.cap.steps:
                            self._step(step)
                    except _HumanCompleted:
                        pass  # the operator finished by hand; still verify success and extract
                    self._verify_success()
                    outputs = self._extract()
                    break
                except _Restart:
                    continue
        except _Finish as f:
            status, outcome, error = f.status, f.outcome, f.error
        result = RunResult(run_id=self.dir.name, capability=self.cap.id, version=self.cap.version,
                           status=status, outputs=outputs, outcome=outcome, error=error,
                           recoveries=self.recoveries, handoffs=self.handoffs, evidence_dir=str(self.dir),
                           duration_ms=int((time.monotonic() - t0) * 1000))
        persisted = self.redact.scrub(result.model_dump(exclude_none=True))
        self.log("run_end", result=persisted)
        (self.dir / "result.json").write_text(json.dumps(persisted, indent=2), encoding="utf-8")
        return result

    def _verify_success(self) -> None:
        while True:
            try:
                return self._await(self.cap.success, SUCCESS_TIMEOUT_MS, None, "checkpoint_failed")
            except _HumanCompleted:
                continue  # the operator says it is done: the success checks still decide

    # --- setup -----------------------------------------------------------------------------------

    def _bind(self, inputs: Mapping[str, str]) -> None:
        for name, value in inputs.items():
            if name in self.cap.inputs:
                self.redact.add(value, self.cap.inputs[name].sensitivity, name)
        problems = [f"unknown input {n!r}" for n in inputs if n not in self.cap.inputs]
        for name, p in self.cap.inputs.items():
            if name not in inputs:
                problems.append(f"missing input {name!r}")
                continue
            v = str(inputs[name])
            try:
                {"integer": int, "decimal": Decimal, "string": str,
                 "boolean": lambda x: {"true": 1, "false": 0}[x.lower()]}[p.type](v)
                ok = p.pattern is None or re.fullmatch(p.pattern, v)
            except (ValueError, InvalidOperation, KeyError):
                ok = False
            if not ok:
                problems.append(f"input {name!r} is not a valid {p.type}" + (f" matching {p.pattern}" if p.pattern else ""))
        wanted = {n for t in [self.cap.entry, *(s.value or "" for s in self.cap.steps)]
                  for ns, n in TEMPLATE_RE.findall(t) if ns == "secrets"}
        secret_values = {}
        for n in sorted(wanted):
            if not self.secrets.get(n):
                problems.append(f"secret {n} is not configured")
            else:
                secret_values[n] = self.secrets[n]
                self.redact.add(secret_values[n], "secret", n)
        if problems:
            raise _Finish("failed", error=Failure(code="input_invalid", expected="inputs matching the capability contract",
                                                  observed="; ".join(problems)))
        self.ctx = {"inputs": {k: str(v) for k, v in inputs.items()}, "secrets": secret_values, "tenant": self.tenant}

    def _render(self, template: str) -> str:
        def sub(m: re.Match) -> str:
            try:
                return self.ctx[m[1]][m[2]]
            except KeyError:
                raise _Finish("failed", error=Failure(code="input_invalid", expected=f"{m[1]}.{m[2]} to be provided",
                                                      observed="missing"))
        return TEMPLATE_RE.sub(sub, template)

    def _start(self) -> None:
        url = self._render(self.cap.entry)
        try:
            self.policy.check_url(url)
        except PolicyViolation as e:
            raise _Finish("failed", error=Failure(code="policy_violation", expected="entry URL inside allowlist", observed=str(e)))
        self.surface.reset_session()
        try:
            self.surface.goto(url)
        except PWError as e:
            raise _Finish("failed", error=Failure(code="app_error", expected=f"entry page {url} loads",
                                                  observed=e.message.splitlines()[0], evidence=self._evidence("entry")))

    # --- the step loop ---------------------------------------------------------------------------

    def _step(self, step: Step) -> None:
        t0 = time.monotonic()
        self.log("step_start", step_id=step.id, action=step.action, intent=step.intent)
        self._settle(step)
        if self._guard(step):
            self.log("step_ok", step_id=step.id, by="human", ms=int((time.monotonic() - t0) * 1000))
            return
        value = self._render(step.value) if step.value is not None else None
        try:
            idx = self.surface.perform(step.action, step.target, value, step.timeout_ms)
        except (LocatorError, PWError) as e:
            if not self._settle(step):  # a known state (e.g. an overlay) may explain it; retry once if so
                self._fail_action(step, e)
            try:
                idx = self.surface.perform(step.action, step.target, value, step.timeout_ms)
            except (LocatorError, PWError) as e2:
                self._fail_action(step, e2)
        if self.surface.blocked:
            raise _Finish("failed", error=Failure(code="policy_violation", step_id=step.id,
                                                  expected="navigation inside allowlist",
                                                  observed=f"blocked request(s): {self.surface.blocked}",
                                                  evidence=self._evidence("policy")))
        if self.policy.risk(step) == "irreversible":
            self.irreversible_done = True
        if idx > 0:
            self.log("locator_degraded", step_id=step.id, winning_strategy=step.target.strategies[idx].by, rank=idx)
        if step.expect:
            self._await([step.expect], step.timeout_ms, step, "unclassified_state")
        self.log("step_ok", step_id=step.id, ms=int((time.monotonic() - t0) * 1000))

    def _guard(self, step: Step) -> bool:
        """Policy checks before acting. Returns True if a human performed the step instead."""
        try:
            self.policy.check_action(step.action)
            if step.action == "goto":
                self.policy.check_url(self._render(step.value))
        except PolicyViolation as e:
            raise _Finish("failed", error=Failure(code="policy_violation", step_id=step.id,
                                                  expected="action inside allowlist", observed=str(e)))
        if self.policy.risk(step) == "irreversible" and not (self.cap.status == "approved" and self.confirm_irreversible):
            self._needs_human("irreversible_step", step,
                              f"step {step.id!r} is irreversible; it needs an approved capability and explicit "
                              f"confirmation (status={self.cap.status}, confirmed={self.confirm_irreversible})")
            # Resumed: the operator was asked to perform (or skip) this step with their own hands.
            # A release is not an approval: automation never clicks an irreversible control itself.
            if step.expect and self.surface.wait_for(step.expect, 2000):
                self.irreversible_done = True
                return True
            raise _Finish("failed", error=Failure(
                code="checkpoint_failed", step_id=step.id,
                expected=f"operator performs irreversible step {step.id!r}" + (f" ({_describe(step.expect)})" if step.expect else ""),
                observed="control was released without the step's checkpoint holding; automation will not perform it",
                evidence=self._evidence("irreversible_not_done")))
        return False

    def _await(self, checks: list[Check], timeout_ms: int, step: Step | None, code: str) -> None:
        """Wait until all checks hold, classifying any known state that shows up meanwhile."""
        deadline = time.monotonic() + timeout_ms / 1000
        while not all(self.surface.holds(c) for c in checks):
            if self._settle(step):
                deadline = time.monotonic() + timeout_ms / 1000
                continue
            if time.monotonic() >= deadline:
                raise _Finish("failed", error=Failure(
                    code=code, step_id=step.id if step else None,
                    expected=" and ".join(_describe(c) for c in checks),
                    observed=self._observed(), evidence=self._evidence(code)))
            time.sleep(POLL_S)

    # --- state classification ------------------------------------------------------------------

    def _settle(self, step: Step | None) -> bool:
        """Classify the current screen. Terminal states raise; returns True if a recovery ran."""
        det = next((d for d in self.detectors if self.surface.holds(d.when)), None)
        if det is None:
            return False
        step_id = step.id if step else None
        self.log("state_detected", code=det.code, kind=det.kind, step_id=step_id)
        if det.kind == "business":
            raise _Finish("business_outcome", outcome=Outcome(code=det.code, message=det.message))
        if det.kind == "human_required":
            self._needs_human(det.code, step, det.message)
            self._settle(step)  # resumed: re-classify the screen the human left us
            return True
        if det.kind == "hard":
            raise _Finish("failed", error=Failure(code="app_error", step_id=step_id,
                                                  expected=step.intent if step else "success state",
                                                  observed=f"{det.code}: {det.message}", evidence=self._evidence(det.code)))
        if len(self.recoveries) >= MAX_RECOVERIES:
            raise _Finish("failed", error=Failure(code="recovery_exhausted", step_id=step_id,
                                                  expected=f"at most {MAX_RECOVERIES} recoveries",
                                                  observed=f"{det.code} still present: {det.message}",
                                                  evidence=self._evidence("recovery_exhausted")))
        rec = {"code": det.code, "action": det.recover, "step_id": step_id}
        self.recoveries.append(rec)
        self.log("recovery", **rec)
        if det.recover == "click":
            try:
                self.surface.perform("click", det.target, None, 5000)
            except (LocatorError, PWError) as e:
                self._fail_action(step, e)
            return True
        if self.irreversible_done:  # restarting would repeat a side effect
            raise _Finish("failed", error=Failure(code="app_error", step_id=step_id,
                                                  expected="restart only before any irreversible step",
                                                  observed=f"{det.code} after an irreversible step; not safe to repeat",
                                                  evidence=self._evidence(det.code)))
        self._start()
        raise _Restart()

    def _needs_human(self, code: str, step: Step | None, message: str) -> None:
        """Route an intervention request. Returns only if a human resumed the run on this session."""
        evidence = self._evidence(code)
        outcome = Outcome(code=code, message=message, evidence=evidence)
        live = self.handoff_wait_s > 0 and len(self.handoffs) < MAX_HANDOFFS
        write_request(self.dir, live=live, kind="replay", capability=self.cap.id, version=self.cap.version,
                      code=code, reason=message, step_id=step.id if step else None,
                      step_intent=step.intent if step else None, url=self.redact(self.surface.url),
                      evidence=evidence, screen=self._observed())
        asked = self.lease.transition("AWAITING_HUMAN", "automation", code)["seq"]
        self.log("handoff_requested", code=code, step_id=step.id if step else None, live=live)
        if not live:
            why = "handoff budget exhausted" if self.handoff_wait_s > 0 else "no operator wait configured"
            self.lease.transition("ABORTED", "automation", why)
            raise _Finish("needs_human", outcome=outcome)
        print(f"[cua] run {self.dir.name} needs a human ({code}): {message}\n"
              f"[cua]   take over the open browser:  cua operator claim {self.dir.name}", file=sys.stderr)
        record: dict[str, Any] = {"code": code, "step_id": step.id if step else None,
                                  "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.handoffs.append(record)
        state, lease = self._wait_for_operator()
        record["actions"] = self.redact.scrub(self.surface.human_events)
        self.surface.human_events.clear()
        last = lease["history"][-1]
        # the lease history is the record of who held the session (a claim+release can fall
        # between two polls), so read the claim from it rather than from what the loop saw
        claim = next((h for h in lease["history"][asked:] if h["to"] == "HUMAN"), None)
        if claim:
            record["claimed_by"], record["claimed_at"] = claim["by"], claim["at"]
        record.update(resolution=state, resolved_by=last["by"], resolved_at=last["at"])
        if last.get("note"):
            record["note"] = self.redact(last["note"])
        self.log("handoff_resolved", **record)
        if state == "AUTOMATION":
            return
        if state == "COMPLETED_BY_HUMAN":
            raise _HumanCompleted()
        if last["by"] == "automation":  # nobody claimed it in time
            raise _Finish("needs_human", outcome=outcome)
        raise _Finish("failed", error=Failure(code="aborted", step_id=record["step_id"],
                                              expected=f"operator resolves {code}",
                                              observed=f"aborted by {last['by']}", evidence=evidence))

    def _wait_for_operator(self) -> tuple[str, dict[str, Any]]:
        """Hold the live session until the lease leaves human hands. No timeout once claimed:
        automation never yanks the session from a human mid-task."""
        deadline, claim_logged = time.monotonic() + self.handoff_wait_s, False
        while True:
            self.surface.idle(250)  # keeps the browser serviced: human-action capture runs here
            lease = self.lease.read()
            if lease["state"] == "HUMAN" and not claim_logged:
                claim_logged = True
                self.log("handoff_claimed", by=lease["holder"])
            if lease["state"] in ("AUTOMATION", "COMPLETED_BY_HUMAN", "ABORTED"):
                return lease["state"], lease
            if lease["state"] == "AWAITING_HUMAN" and time.monotonic() > deadline:
                try:
                    self.lease.transition("ABORTED", "automation", f"unclaimed after {self.handoff_wait_s:g}s")
                except ControlError:
                    pass  # claimed in the meantime; keep waiting

    def _fail_action(self, step: Step | None, e: Exception) -> None:
        code = e.code if isinstance(e, LocatorError) else "action_failed"
        detail = e.detail if isinstance(e, LocatorError) else str(e).splitlines()[0]
        raise _Finish("failed", error=Failure(
            code=code, step_id=step.id if step else None,
            expected=f"{step.action} on {step.target.description if step and step.target else step.value}" if step else "recovery action",
            observed=detail, evidence=self._evidence(code)))

    # --- outputs & evidence --------------------------------------------------------------------

    def _extract(self) -> dict[str, Any]:
        outputs = {}
        for name, out in self.cap.outputs.items():
            try:
                raw = self.surface.read(out.source, 5000)
                outputs[name] = convert(raw, out)
            except (LocatorError, PWError, ValueError) as e:
                raise _Finish("failed", error=Failure(code="extraction_failed", expected=f"{name}: {out.type} at {out.source.description}",
                                                      observed=str(e).splitlines()[0], evidence=self._evidence("extraction")))
            self.redact.add(raw, out.sensitivity, name)
            self.redact.add(outputs[name], out.sensitivity, name)
        self.log("extracted", outputs=outputs)
        return outputs

    def _observed(self) -> str:
        try:
            return self.redact(self.surface.observe())[:1500]
        except PWError as e:
            return f"(page unreadable: {e.message.splitlines()[0]})"

    def _evidence(self, label: str) -> list[str]:
        """Screenshot + redacted text snapshot of the live surface. Only taken on failure/handoff."""
        stem = self.dir / f"{len(list(self.dir.glob('*.png'))):02d}-{label}"
        paths = []
        try:
            self.surface.screenshot(stem.with_suffix(".png"))
            paths.append(stem.with_suffix(".png").name)
        except PWError:
            pass
        stem.with_suffix(".txt").write_text(self._observed(), encoding="utf-8")
        paths.append(stem.with_suffix(".txt").name)
        return paths


def _describe(c: Check) -> str:
    where = "/".join(c.frame) if c.frame else ("top" if c.frame == [] else "any frame")
    parts = []
    if c.text is not None:
        parts.append(f"text {c.text!r} in {where}")
    if c.element is not None:
        parts.append(f"element {c.element.description or c.element.strategies[0]}")
    return " and ".join(parts)
