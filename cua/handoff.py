"""Control transfer between automation and a human operator, on the same live session.

Who may act is a lease stored in runs/<id>/control.json — a file, so a separate process (the
operator CLI, or a real console later) can move it. Transitions are compare-and-swap on the
current state; anything not in TRANSITIONS is refused.

    AUTOMATION ──request──▶ AWAITING_HUMAN ──claim──▶ HUMAN ──release──▶ AUTOMATION
                                 │                      ├──complete──▶ COMPLETED_BY_HUMAN
                                 └──abort/timeout──▶ ABORTED ◀──abort──┘

Surface.perform() refuses unless the lease says AUTOMATION, so automation cannot act while a
human holds the session — the lease is enforced, not advisory.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MAX_HANDOFFS = 2  # per run: a human who could not fix it twice will not on the third try

TRANSITIONS = {
    "AUTOMATION": {"AWAITING_HUMAN"},
    "AWAITING_HUMAN": {"HUMAN", "ABORTED"},
    "HUMAN": {"AUTOMATION", "COMPLETED_BY_HUMAN", "ABORTED"},
    "ABORTED": set(),
    "COMPLETED_BY_HUMAN": set(),
}


class ControlError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Lease:
    def __init__(self, path: str | Path, run_id: str | None = None):
        self.path = Path(path)
        if run_id is not None and not self.path.exists():
            self._write({"run_id": run_id, "state": "AUTOMATION", "holder": "automation", "seq": 0,
                         "updated_at": _now(), "history": []})

    @classmethod
    def for_run(cls, runs_dir: str | Path, run_id: str) -> Lease:
        path = Path(runs_dir) / run_id / "control.json"
        if not path.exists():
            raise ControlError(f"no control file for run {run_id!r} under {runs_dir}")
        return cls(path)

    def read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    @property
    def state(self) -> str:
        return self.read()["state"]

    def transition(self, to: str, by: str, note: str = "") -> dict[str, Any]:
        cur = self.read()
        if to not in TRANSITIONS[cur["state"]]:
            raise ControlError(f"cannot go {cur['state']} -> {to} (allowed: {sorted(TRANSITIONS[cur['state']]) or 'none'})")
        if cur["state"] == "HUMAN" and by != cur["holder"]:
            raise ControlError(f"session is held by {cur['holder']}; only they can release or abort it")
        holder = "automation" if to == "AUTOMATION" else by
        cur["history"].append({"from": cur["state"], "to": to, "by": by, "at": _now(), **({"note": note} if note else {})})
        cur.update(state=to, holder=holder, seq=cur["seq"] + 1, updated_at=_now())
        # ponytail: read-check-replace has a tiny race between two writers; a row lock in a DB fixes it
        self._write(cur)
        return cur

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: readers never see a half-written file


def write_request(run_dir: Path, live: bool, **fields: Any) -> Path:
    """The intervention request: everything an operator needs to decide and act. `live` means the
    run is holding its session open for takeover; otherwise the request is for follow-up only."""
    run_id = run_dir.name
    request = {"run_id": run_id, "requested_at": _now(), "live_session": live, **fields}
    if live:
        request["operator"] = {
            "claim": f"cua operator claim {run_id}",
            "resume": f"cua operator release {run_id}",
            "finished_by_hand": f"cua operator release {run_id} --completed",
            "abort": f"cua operator abort {run_id}",
        }
    path = run_dir / "intervention.json"
    path.write_text(json.dumps(request, indent=2), encoding="utf-8")
    return path


def await_operator(lease: Lease, surface: Any, wait_s: float, redact: Any, log: Callable[..., None],
                   code: str, step_id: str | None = None) -> dict[str, Any]:
    """Hold the live session for an operator. The lease must already be AWAITING_HUMAN.

    Returns the handoff record; `resolution` is the final lease state (AUTOMATION,
    COMPLETED_BY_HUMAN or ABORTED). No timeout once claimed: automation never takes the session
    back from a human mid-task. Shared by replay and discovery.
    """
    run_id = lease.read()["run_id"]
    asked = lease.read()["seq"]
    print(f"[cua] run {run_id} needs a human ({code})\n"
          f"[cua]   take over the open browser:  cua operator claim {run_id}", file=sys.stderr)
    record: dict[str, Any] = {"code": code, "step_id": step_id, "requested_at": _now()}
    deadline, claim_logged = time.monotonic() + wait_s, False
    while True:
        surface.idle(250)  # keeps the browser serviced: human-action capture runs here
        cur = lease.read()
        if cur["state"] == "HUMAN" and not claim_logged:
            claim_logged = True
            log("handoff_claimed", by=cur["holder"])
        if cur["state"] in ("AUTOMATION", "COMPLETED_BY_HUMAN", "ABORTED"):
            break
        if cur["state"] == "AWAITING_HUMAN" and time.monotonic() > deadline:
            try:
                lease.transition("ABORTED", "automation", f"unclaimed after {wait_s:g}s")
            except ControlError:
                pass  # claimed in the meantime; keep waiting
    record["actions"] = redact.scrub(surface.human_events)
    surface.human_events.clear()
    last = cur["history"][-1]
    # the lease history is the record of who held the session (a claim+release can fall between
    # two polls), so read the claim from it rather than from what this loop happened to see
    claim = next((h for h in cur["history"][asked:] if h["to"] == "HUMAN"), None)
    if claim:
        record["claimed_by"], record["claimed_at"] = claim["by"], claim["at"]
    record.update(resolution=cur["state"], resolved_by=last["by"], resolved_at=last["at"])
    if last.get("note"):
        record["note"] = redact(last["note"])
    log("handoff_resolved", **record)
    return record
