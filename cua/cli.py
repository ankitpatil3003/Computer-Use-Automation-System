"""Command line: `cua discover` (LLM, dev time), `cua replay` (deterministic, production path),
and `cua operator` (the human side of a handoff)."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from cua.handoff import ControlError, Lease
from cua.policy import Policy
from cua.replay import Replayer
from cua.schema import Capability, Profile
from cua.surface import PlaywrightSurface

EXIT = {"success": 0, "failed": 1, "business_outcome": 2, "needs_human": 3}


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())


def load_profile(ref: str, profiles_dir: str = "profiles") -> Profile:
    profile = Profile.load(Path(profiles_dir) / f"{ref.split('@')[0]}.json")
    if profile.id != ref:
        sys.exit(f"profile {ref} not found (have {profile.id})")
    return profile


def cmd_replay(args: argparse.Namespace) -> int:
    cap = Capability.load(args.capability)
    policy = Policy.load(args.policy)
    inputs = dict(kv.split("=", 1) for kv in args.input)
    if args.handoff_wait and not args.headed:
        sys.exit("--handoff-wait needs --headed: the operator works in the visible browser window")
    with PlaywrightSurface(headless=not args.headed, allow_url=policy.allows_url) as surface:
        result = Replayer(cap, load_profile(cap.app.profile), policy, surface, secrets=os.environ,
                          tenant={"base_url": args.base_url.rstrip("/")}, runs_dir=args.runs,
                          confirm_irreversible=args.confirm_irreversible,
                          handoff_wait_s=args.handoff_wait, allow_draft=args.allow_draft).run(inputs)
    # stdout is the caller channel: outputs are returned in full here; persisted copies are redacted.
    print(json.dumps(result.model_dump(exclude_none=True), indent=2))
    return EXIT[result.status]


def cmd_discover(args: argparse.Namespace) -> int:
    from openai import OpenAI

    from cua.agent import Discovery

    model, key = os.environ.get("LLM_MODEL", ""), os.environ.get("LLM_API_KEY", "")
    if not key or not model or model.startswith("<"):
        sys.exit("discovery needs LLM_API_KEY and LLM_MODEL (and LLM_BASE_URL) in .env — see .env.example")
    client = OpenAI(base_url=os.environ.get("LLM_BASE_URL") or None, api_key=key, max_retries=5)
    policy = Policy.load(args.policy)
    base_url = args.base_url.rstrip("/")
    secrets = {n: os.environ[n] for n in args.secrets.split(",") if os.environ.get(n)}
    inputs = dict(kv.split("=", 1) for kv in args.input)
    if args.handoff_wait and not args.headed:
        sys.exit("--handoff-wait needs --headed: the operator works in the visible browser window")
    with PlaywrightSurface(headless=not args.headed, allow_url=policy.allows_url) as surface:
        summary = Discovery(args.goal, base_url + "/", inputs, client=client, model=model, policy=policy,
                            profile=load_profile(args.profile), surface=surface, secrets=secrets,
                            base_url=base_url, runs_dir=args.runs, max_steps=args.max_steps,
                            min_interval_s=float(os.environ.get("LLM_MIN_INTERVAL_S", "13")),
                            timeout_s=args.timeout, handoff_wait_s=args.handoff_wait).run()
    if summary["status"] == "success":
        cap = Capability.load(summary["artifact"])
        Path(args.out).mkdir(parents=True, exist_ok=True)
        cap.save(Path(args.out) / f"{cap.id}.json")
        summary["saved_to"] = str(Path(args.out) / f"{cap.id}.json")
    print(json.dumps(summary, indent=2))
    return EXIT[summary["status"]]


def cmd_operator(args: argparse.Namespace) -> int:
    if args.op == "list":
        for path in sorted(Path(args.runs).glob("*/control.json")):
            lease = json.loads(path.read_text(encoding="utf-8"))
            if lease["state"] in ("AWAITING_HUMAN", "HUMAN"):
                req = path.with_name("intervention.json")
                reason = json.loads(req.read_text(encoding="utf-8")).get("reason", "") if req.exists() else ""
                print(f"{lease['run_id']}  {lease['state']:<15} {lease['holder']:<20} {reason}")
        return 0
    if not args.run_id:
        sys.exit(f"operator {args.op} needs a run id (see: cua operator list)")
    me = f"human:{args.as_ or getpass.getuser()}"
    to = {"claim": "HUMAN", "abort": "ABORTED",
          "release": "COMPLETED_BY_HUMAN" if args.completed else "AUTOMATION"}[args.op]
    try:
        lease = Lease.for_run(args.runs, args.run_id).transition(to, me, args.note)
    except ControlError as e:
        sys.exit(f"refused: {e}")
    print(f"{args.run_id}: {lease['state']} (held by {lease['holder']})")
    if to == "HUMAN":
        req = json.loads((Path(args.runs) / args.run_id / "intervention.json").read_text(encoding="utf-8"))
        print(f"why: {req['reason']}  (step {req.get('step_id')})\n"
              f"You now control the open browser window; automation is paused. When done:\n"
              f"  cua operator release {args.run_id}              # hand back; automation re-checks the screen\n"
              f"  cua operator release {args.run_id} --completed  # you finished the task by hand\n"
              f"  cua operator abort {args.run_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="cua")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="LLM-driven run on a goal; records a capability")
    d.add_argument("goal")
    d.add_argument("--input", "-i", action="append", default=[], metavar="NAME=VALUE")
    d.add_argument("--base-url", default=os.environ.get("MOCKBANK_URL", "http://127.0.0.1:8765"))
    d.add_argument("--profile", default="mockbank-core@2")
    d.add_argument("--secrets", default="MOCKBANK_USER,MOCKBANK_PASSWORD", help="secret names the agent may use")
    d.add_argument("--policy", default="policy.json")
    d.add_argument("--out", default="capabilities/drafts",
                   help="where the validated draft is saved; approval (review) promotes it to capabilities/")
    d.add_argument("--runs", default="runs")
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--timeout", type=float, default=600, metavar="SECONDS",
                   help="wall-clock budget, excluding time a human holds the session")
    d.add_argument("--handoff-wait", type=float, default=0, metavar="SECONDS",
                   help="on escalation, hold the live session this long for an operator (needs --headed)")
    d.add_argument("--headed", action="store_true")
    d.set_defaults(fn=cmd_discover)
    r = sub.add_parser("replay", help="replay a capability deterministically (no LLM)")
    r.add_argument("capability")
    r.add_argument("--input", "-i", action="append", default=[], metavar="NAME=VALUE")
    r.add_argument("--base-url", default=os.environ.get("MOCKBANK_URL", "http://127.0.0.1:8765"))
    r.add_argument("--policy", default="policy.json")
    r.add_argument("--runs", default="runs")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--confirm-irreversible", action="store_true")
    r.add_argument("--allow-draft", action="store_true", help="run a draft capability (testing; production needs approved)")
    r.add_argument("--handoff-wait", type=float, default=0, metavar="SECONDS",
                   help="on needs-human, hold the live session this long for an operator to claim it")
    r.set_defaults(fn=cmd_replay)
    o = sub.add_parser("operator", help="human side of a handoff: list | claim | release | abort")
    o.add_argument("op", choices=["list", "claim", "release", "abort"])
    o.add_argument("run_id", nargs="?")
    o.add_argument("--completed", action="store_true", help="release: the task was finished by hand")
    o.add_argument("--note", default="", help="free text recorded in the lease history (redacted in run logs)")
    o.add_argument("--as", dest="as_", help="operator name (default: OS user)")
    o.add_argument("--runs", default="runs")
    o.set_defaults(fn=cmd_operator)
    sub.add_parser("schema", help="print the capability artifact JSON Schema").set_defaults(
        fn=lambda _: print(json.dumps(Capability.model_json_schema(), indent=2)) or 0)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
