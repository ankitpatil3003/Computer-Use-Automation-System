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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
