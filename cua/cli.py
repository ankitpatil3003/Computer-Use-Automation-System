"""Command line: `cua replay` (production path). Discovery and operator commands land in later stages."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

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
    with PlaywrightSurface(headless=not args.headed, allow_url=policy.allows_url) as surface:
        result = Replayer(cap, load_profile(cap.app.profile), policy, surface, secrets=os.environ,
                          tenant={"base_url": args.base_url.rstrip("/")}, runs_dir=args.runs,
                          confirm_irreversible=args.confirm_irreversible).run(inputs)
    # stdout is the caller channel: outputs are returned in full here; persisted copies are redacted.
    print(json.dumps(result.model_dump(exclude_none=True), indent=2))
    return EXIT[result.status]


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="cua")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="replay a capability deterministically (no LLM)")
    r.add_argument("capability")
    r.add_argument("--input", "-i", action="append", default=[], metavar="NAME=VALUE")
    r.add_argument("--base-url", default=os.environ.get("MOCKBANK_URL", "http://127.0.0.1:8765"))
    r.add_argument("--policy", default="policy.json")
    r.add_argument("--runs", default="runs")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--confirm-irreversible", action="store_true")
    r.set_defaults(fn=cmd_replay)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
