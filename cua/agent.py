"""Discovery: an LLM-driven observe → decide → act loop that records a capability.

The model sees the redacted text rendering of the screen (never CSS/DOM) and picks one tool per
turn. Every action is executed through the same locator/policy path replay uses, so whatever the
recorder writes down has already worked once. The artifact is saved only if an immediate
deterministic replay of it succeeds and returns the same outputs.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from openai import APIError
from playwright.sync_api import Error as PWError

from cua.handoff import MAX_HANDOFFS, Lease, await_operator, write_request
from cua.policy import Policy, PolicyViolation, Redactor
from cua.recorder import Recorder, frame_path
from cua.replay import Replayer, RunLog, convert, new_run_dir
from cua.schema import Check, Output, Profile, Step
from cua.surface import LocatorError, Surface

SYSTEM = """You operate a legacy back-office web application at a bank, on behalf of an automation
system that will turn your actions into a reusable, replayable procedure.

Each turn you receive the current screen as text: one line per row, table cells separated by "|",
frames introduced by "--- frame: <name>", and controls shown as [eN role "name"]. Refs like e3 are
valid only for the screen they came from.

Rules:
- Call exactly one tool per turn. Give every action a short, human-readable `intent`.
- Sign on with fill_secret using the secret names provided; you never see secret values.
- Type the provided input values exactly as given.
- Do only what the goal needs. Never perform irreversible actions (closing accounts, transfers,
  posting, approving, authorizing). If the goal seems to need one, call ask_human.
- If a system notice or dialog appears, dismiss it and continue.
- If you are blocked, the screen is not what you expect, or a supervisor/approval is required,
  call ask_human with the reason rather than guessing. A human may take over and hand back.
- When the requested data is visible, call extract once per value, then finish."""


def _tools(input_names: list[str]) -> list[dict[str, Any]]:
    def fn(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {"type": "function", "function": {"name": name, "description": description, "parameters": {
            "type": "object", "properties": props, "required": required}}}
    s = {"type": "string"}
    intent = {"type": "string", "description": "why: what this action accomplishes, for a human reviewer"}
    return [
        fn("click", "Click a control on the current screen.", {"ref": s, "intent": intent}, ["ref", "intent"]),
        fn("fill", "Type a value into a text field.", {"ref": s, "value": s, "intent": intent}, ["ref", "value", "intent"]),
        fn("fill_secret", "Type a named secret (e.g. a password) into a field without seeing it.",
           {"ref": s, "secret": s, "intent": intent}, ["ref", "secret", "intent"]),
        fn("extract", "Record a value shown in a table as an output of this procedure.", {
            "output": {"type": "string", "description": "snake_case output name, e.g. savings_balance"},
            "frame": {"type": "string", "description": "frame name from the screen header, or 'top'"},
            "row": {"type": "string", "description": "exact text of the first cell of the row"},
            "column": {"type": "string", "description": "exact header text of the column; omit for label/value rows"},
            "type": {"type": "string", "enum": ["string", "decimal", "integer"]},
            "sensitivity": {"type": "string", "enum": ["none", "pii", "financial"]},
            "description": s,
        }, ["output", "frame", "row", "type", "sensitivity", "description"]),
        fn("ask_human", "Escalate to a human operator when stuck, blocked, or a risky step needs a decision.",
           {"reason": s}, ["reason"]),
        fn("finish", "Declare the goal achieved.", {
            "success_text": {"type": "string", "description": "static text now visible that proves the goal "
                                                              "state was reached (a heading, not data)"},
            "frame": {"type": "string", "description": "frame containing success_text, or 'top'"},
            "capability_name": {"type": "string", "description": "snake_case name, e.g. get_savings_balance"},
            "description": {"type": "string", "description": "one sentence: what the procedure does and returns"},
            "input_descriptions": {"type": "object", "properties": {n: s for n in input_names},
                                   "required": input_names},
        }, ["success_text", "frame", "capability_name", "description", "input_descriptions"]),
    ]


class _Done(Exception):
    def __init__(self, status: str, **info: Any):
        self.status, self.info = status, info


class Discovery:
    def __init__(self, goal: str, start_url: str, inputs: Mapping[str, str], *, client: Any, model: str,
                 policy: Policy, profile: Profile, surface: Surface, secrets: Mapping[str, str],
                 base_url: str, runs_dir: str = "runs", max_steps: int = 25, min_interval_s: float = 0.0,
                 timeout_s: float = 600, handoff_wait_s: float = 0):
        self.goal, self.start_url, self.inputs = goal, start_url, dict(inputs)
        self.client, self.model = client, model
        self.policy, self.profile, self.surface = policy, profile, surface
        self.secrets, self.base_url, self.runs_dir = secrets, base_url, runs_dir
        self.max_steps, self.min_interval_s = max_steps, min_interval_s
        self.timeout_s, self.handoff_wait_s = timeout_s, handoff_wait_s
        self.handoffs: list[dict[str, Any]] = []
        self.paused_s = 0.0  # time a human held the session; does not count against timeout_s
        self.dir = new_run_dir(runs_dir, "discover")
        # Two views. The model's: secrets, PII-labelled fields and PII/money patterns masked, but the
        # inputs visible (it has to type them). What is persisted: that, plus the inputs masked.
        self.redact, self.persist = Redactor(profile.pii_labels), Redactor(profile.pii_labels)
        for name, value in secrets.items():
            self.redact.add(value, "secret", name)
            self.persist.add(value, "secret", name)
        for name, value in self.inputs.items():
            self.persist.add(value, "pii_id", name)
        self.log = RunLog(self.dir, self.persist)
        self.lease = Lease(self.dir / "control.json", self.dir.name)
        self.recorder = Recorder(surface, profile, self.inputs)
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self._last_call = 0.0

    # --- loop -----------------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        t0 = time.monotonic()
        self.log("discovery_start", goal=self.goal, start_url=self.start_url, model=self.model,
                 inputs=list(self.inputs), secrets=list(self.secrets))
        history: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Goal: {self.goal}\nInputs: {json.dumps(self.inputs)}\n"
                                        f"Secrets available for fill_secret: {sorted(self.secrets)}"},
        ]
        tools = _tools(list(self.inputs))
        unchanged = 0
        try:
            self.policy.check_url(self.start_url)
            self.surface.lease = self.lease
            self.surface.reset_session()
            self.surface.goto(self.start_url)
            obs = self.surface.observe()
            for turn in range(1, self.max_steps + 1):
                if time.monotonic() - t0 - self.paused_s > self.timeout_s:
                    self._escalate(f"timeout: goal not reached within {self.timeout_s:g}s", turn, resumable=False)
                screen = self.redact(obs)
                if self.surface.dialogs:  # the model must know a native dialog came and went
                    screen = f"--- native dialog shown and answered Cancel: {self.surface.dialogs}\n{screen}"
                    self.surface.dialogs.clear()
                self.log("observe", turn=turn, screen=screen)
                msg = self._chat([*history, {"role": "user", "content": f"Current screen:\n{screen}"}], tools)
                history.append(_assistant(msg))
                if not msg.tool_calls:
                    history.append({"role": "user", "content": "Call exactly one tool."})
                    continue
                call, extra = msg.tool_calls[0], msg.tool_calls[1:]
                before = obs
                result, obs = self._execute(call.function.name, call.function.arguments, obs, turn)
                unchanged = unchanged + 1 if obs == before and call.function.name == "click" else 0
                history.append({"role": "tool", "tool_call_id": call.id, "content": result})
                for c in extra:
                    history.append({"role": "tool", "tool_call_id": c.id, "content": "ignored: one action per turn"})
                if unchanged >= 3:
                    note = self._escalate("no progress: the screen did not change after 3 consecutive actions", turn)
                    history.append({"role": "user", "content": note})
                    obs, unchanged = self.surface.observe(), 0
            self._escalate(f"step budget exhausted ({self.max_steps} turns) before the goal was reached",
                           self.max_steps, resumable=False)
        except _Done as d:
            status, info = d.status, d.info
        except PolicyViolation as e:
            status, info = "failed", {"reason": str(e)}
        except APIError as e:  # provider outage/quota: end as a logged run, not a traceback
            status, info = "failed", {"reason": f"llm_unavailable: {type(e).__name__}: {e.message}"}
        summary = {"run_id": self.dir.name, "status": status, "goal": self.persist(self.goal),
                   "steps_recorded": len(self.recorder.steps), "llm": self.usage, "handoffs": self.handoffs,
                   "duration_s": round(time.monotonic() - t0, 1), **info}
        self.log("discovery_end", **summary)
        (self.dir / "result.json").write_text(json.dumps(self.persist.scrub(summary), indent=2), encoding="utf-8")
        return summary

    def _chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        wait = self.min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)  # stay under free-tier requests/minute
        t0 = time.monotonic()
        resp = self.client.chat.completions.create(model=self.model, messages=messages, tools=tools,
                                                   tool_choice="required", temperature=0)
        self._last_call = time.monotonic()
        msg = resp.choices[0].message
        usage = getattr(resp, "usage", None)
        self.usage["calls"] += 1
        if usage:
            self.usage["prompt_tokens"] += usage.prompt_tokens or 0
            self.usage["completion_tokens"] += usage.completion_tokens or 0
        self.log("decision", ms=int((time.monotonic() - t0) * 1000), text=msg.content,
                 tool_calls=[{"name": c.function.name, "args": c.function.arguments} for c in msg.tool_calls or []])
        return msg

    # --- tools ----------------------------------------------------------------------------------

    def _execute(self, name: str, raw_args: str, obs: str, turn: int) -> tuple[str, str]:
        """Run one tool. Returns (result for the model, new observation)."""
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return "error: arguments were not valid JSON", obs
        try:
            if name in ("click", "fill", "fill_secret"):
                return self._act(name, args, obs, turn)
            if name == "extract":
                return self._extract(args), obs
            if name == "ask_human":
                return self._escalate(f"agent asked for help: {args.get('reason', '')}", turn), self.surface.observe()
            if name == "finish":
                self._finish(args, turn)
            return f"error: unknown tool {name!r}", obs
        except LocatorError as e:
            return f"error: {e.detail}", self.surface.observe()
        except KeyError as e:
            return f"error: missing argument {e}", obs

    def _act(self, name: str, args: dict[str, Any], obs: str, turn: int) -> tuple[str, str]:
        action = "click" if name == "click" else "fill"
        self.policy.check_action(action)
        target = self.surface.locator_for(args["ref"])
        intent = args.get("intent") or f"{action} {target.description}"
        if self.policy.risk(Step(id="probe", action=action, target=target, value="", intent=intent)) == "irreversible":
            self.log("policy_block", turn=turn, target=target.description)
            note = self._escalate(f"policy: {target.description} is irreversible; a human must decide", turn)
            return f"BLOCKED: you may not use {target.description}. {note}", self.surface.observe()
        if name == "fill_secret":
            if args["secret"] not in self.secrets:
                return f"error: unknown secret {args['secret']!r}; available: {sorted(self.secrets)}", obs
            value, recorded = self.secrets[args["secret"]], "{{secrets.%s}}" % args["secret"]
        else:
            value = recorded = args.get("value")
        transient = action == "click" and self.recorder.is_transient(target)
        try:
            self.surface.perform(action, target, value, 10_000)
        except PWError as e:
            return f"error: {e.message.splitlines()[0]}", self.surface.observe()
        if self.surface.blocked:
            blocked, self.surface.blocked[:] = list(self.surface.blocked), []
            self.log("policy_block", turn=turn, blocked=blocked)
            return f"BLOCKED by policy: navigation to {blocked} is outside the allowlist", self.surface.observe()
        after = self._settled(obs)
        if transient:
            self.log("transient", turn=turn, target=target.description)
            return f"ok: dismissed {target.description} (handled by the app profile at replay, not recorded)", after
        expect = self.recorder.checkpoint(obs, after) if action == "click" else None
        step = self.recorder.record(action, target, recorded, intent, expect)
        self.log("recorded", turn=turn, step=step.model_dump(exclude_none=True))
        note = f" — checkpoint: {expect.text!r} in {'/'.join(expect.frame) or 'top'}" if expect else ""
        return f"ok: {action} {target.description}{note}", after

    def _settled(self, before: str, change_s: float = 1.5, max_s: float = 5.0) -> str:
        """Wait for the screen to change (if it's going to) and then stop changing."""
        start, last = time.monotonic(), None
        while True:
            time.sleep(0.25)
            cur = self.surface.observe()
            elapsed = time.monotonic() - start
            if (cur == last and (cur != before or elapsed > change_s)) or elapsed > max_s:
                return cur
            last = cur

    def _extract(self, args: dict[str, Any]) -> str:
        frame = frame_path(args["frame"])
        source = self.surface.cell_locator(frame, args["row"], args.get("column") or None)
        raw = self.surface.read(source, 3000)
        out = Output(type=args["type"], description=args.get("description") or args["output"],
                     sensitivity=args["sensitivity"], source=source,
                     transform="currency" if args["type"] == "decimal" and "$" in raw else "text")
        try:
            value = convert(raw, out)
        except ValueError as e:
            return f"error: {e}"
        self.recorder.extract(args["output"], out, raw, value)
        self.log("extracted", output=args["output"], source=source.model_dump(exclude_none=True))
        self.persist.add(raw, out.sensitivity, args["output"])
        self.persist.add(value, out.sensitivity, args["output"])
        return f"ok: recorded {args['output']} ({out.type}); the value is returned to the caller, not to you"

    def _finish(self, args: dict[str, Any], turn: int) -> None:
        success = Check(frame=frame_path(args["frame"]), text=args["success_text"])
        if not self.surface.holds(success):
            raise LocatorError("checkpoint_failed", f"success_text {args['success_text']!r} is not visible in "
                                                    f"frame {args['frame']!r}; pick static text shown on screen")
        if self.recorder.data_dependent(args["success_text"]):
            raise LocatorError("checkpoint_failed", "success_text contains run-specific data; pick a static heading")
        cap = self.recorder.compile(
            name=args["capability_name"], description=args["description"],
            input_descriptions=args.get("input_descriptions") or {}, success=success, entry=self.start_url,
            base_url=self.base_url, model=self.model, run_id=self.dir.name, goal=self.persist(self.goal))
        cap.save(self.dir / "artifact.draft.json")
        self.log("compiled", capability=cap.id, steps=len(cap.steps), outputs=list(cap.outputs))

        # Save rule: the artifact must replay deterministically, right now, to the same outputs.
        result = Replayer(cap, self.profile, self.policy, self.surface, secrets=self.secrets,
                          tenant={"base_url": self.base_url}, runs_dir=self.runs_dir, allow_draft=True).run(self.inputs)
        validation = {"run_id": result.run_id, "status": result.status,
                      "outputs_match": result.outputs == self.recorder.values,
                      "error": result.error.model_dump(exclude_none=True) if result.error else None}
        self.log("validation_replay", **validation)
        if result.status != "success" or not validation["outputs_match"]:
            raise _Done("failed", reason="validation replay did not reproduce the run", validation=validation,
                        artifact=str(self.dir / "artifact.draft.json"))
        raise _Done("success", capability=cap.id, artifact=str(self.dir / "artifact.draft.json"),
                    validation=validation)

    def _escalate(self, reason: str, turn: int, resumable: bool = True) -> str:
        """Route an intervention request. With an operator wait configured, hold this live session
        for a human; returns a note for the model if they hand it back, otherwise ends the run."""
        shot = self.dir / f"escalation-turn{turn:02d}.png"
        try:
            self.surface.screenshot(shot, self.profile.pii_labels, self.persist.known)
        except PWError:
            pass
        live = resumable and self.handoff_wait_s > 0 and len(self.handoffs) < MAX_HANDOFFS
        path = write_request(self.dir, live=live, kind="discovery", goal=self.persist(self.goal), turn=turn,
                             reason=self.persist(reason), url=self.persist(self.surface.url), screenshot=shot.name,
                             screen=self.persist(self.surface.observe()))
        self.lease.transition("AWAITING_HUMAN", "automation", reason)
        self.log("escalation", reason=reason, turn=turn, live=live)
        if not live:
            self.lease.transition("ABORTED", "automation", "not resumable" if not resumable else
                                  "handoff budget exhausted" if self.handoff_wait_s > 0 else "no operator wait configured")
            raise _Done("needs_human", reason=reason, intervention=str(path))
        t = time.monotonic()
        rec = await_operator(self.lease, self.surface, self.handoff_wait_s, self.persist, self.log, "agent_escalation")
        self.paused_s += time.monotonic() - t
        self.handoffs.append(rec)
        if rec["resolution"] == "AUTOMATION":
            # The human's actions are context for the model, not recorded steps: if the flow needs
            # them every time, the validation replay fails and the save rule keeps the artifact out.
            did = "; ".join(f'{a["kind"]} {a["role"]} "{a["name"]}"' for a in rec["actions"]) or "no recorded actions"
            return (f"A human operator took over the live session and handed it back. They did: {did}. "
                    "Those actions are not part of the recorded procedure. Continue from the current screen.")
        if rec["resolution"] == "COMPLETED_BY_HUMAN":
            raise _Done("needs_human", reason=f"{reason}; the operator finished the task by hand, so no "
                                              "capability was recorded", intervention=str(path))
        if rec["resolved_by"] == "automation":  # nobody claimed it in time
            raise _Done("needs_human", reason=reason, intervention=str(path))
        raise _Done("failed", reason=f"operator aborted: {reason}", intervention=str(path))


def _assistant(msg: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [{"id": c.id, "type": "function",
                              "function": {"name": c.function.name, "arguments": c.function.arguments}}
                             for c in msg.tool_calls]
    return out
