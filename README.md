# Computer-Use Automation System

An LLM works out a UI flow once in a legacy back-office app. The recorder turns that run into a typed, versioned
**capability** artifact. Production runs replay the artifact **deterministically, with no LLM**: results are
classified as success, business outcome or failure, a **human can take over the same live session** when needed,
and safety guardrails apply throughout.

The target is a local, deliberately hostile "legacy core banking" app, **MockBank Core** (`mockbank/server.py`). It has a frameset,
table layouts, no ids or test ids, unlabeled `f1`/`f2` fields, an idle-expiring session and synthetic data only. Design
and trade-offs are in [REPORT.md](REPORT.md). Real run output is in [evidence/](evidence/README.md).

## Setup

Requires Python 3.11+.

```sh
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env            # then fill in LLM_API_KEY (only needed for discovery)
```

### Configuration (`.env`, gitignored)

| Variable | Used by | Notes |
|---|---|---|
| `MOCKBANK_USER`, `MOCKBANK_PASSWORD` | replay, discovery | Service account. Capabilities reference them as `{{secrets.NAME}}`; the LLM sees only the names. Defaults match the mock app. |
| `MOCKBANK_URL` | CLI | Default `http://127.0.0.1:8765` |
| `LLM_BASE_URL` | discovery only | Any OpenAI-compatible endpoint. Default: Gemini `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `LLM_MODEL` | discovery only | `gemini-2.5-flash` (used for the evidence run) |
| `LLM_API_KEY` | discovery only | Free key from Google AI Studio. Groq / Ollama work by changing these three values. |
| `LLM_MIN_INTERVAL_S` | discovery only | Spacing between LLM calls. Default 13 s, because the free tier allows ~5 requests/min. |

## Running without live services

Everything except `cua discover` runs **without an LLM key or network access**:

```sh
pytest                          # 41 tests; starts the mock app in-process, uses a scripted stand-in for the LLM
```

Replay and handoff only need the local mock app. A discovered artifact is committed at
`capabilities/mockbank_core.get_savings_balance.json`.

## Demo: discover → replay

```sh
python -m mockbank.server                                   # terminal 1: the target app on :8765
```

**1. Discover** (real LLM; about 8 calls and 2 minutes):

```sh
cua discover "Look up the member and read their savings balance" -i member_id=12345 [--headed]
```

The agent signs on, navigates the frameset and reads the balance. The recorder emits a draft artifact, and it is saved to
`capabilities/` **only if an immediate deterministic replay reproduces the same outputs**. The log is in `runs/<run_id>/`.

**2. Replay** (no LLM):

```sh
CAP=capabilities/mockbank_core.get_savings_balance.json
cua replay $CAP -i member_id=12345      # success        → {"savings_balance": "4210.55"}
cua replay $CAP -i member_id=99999      # business_outcome member_not_found
cua replay $CAP -i member_id=12a45      # failed input_invalid (contract check, UI never touched)
curl "http://127.0.0.1:8765/__faults?reset=1&interstitial=1&error500=1"
cua replay $CAP -i member_id=12345      # success, with recoveries [system_notice, server_error]
curl "http://127.0.0.1:8765/__faults?reset=1&error500=9"
cua replay $CAP -i member_id=12345      # failed recovery_exhausted (+ screenshot)
```

Faults you can inject: `interstitial`, `slow` (+`slow_ms`), `error500`, `expire` (counts; `reset=1` clears them).
Data-driven states: `99999` not found, `00000` invalid, `24680` permission denied, `13579` supervisor override.

The result is JSON on stdout, which is the caller's channel and returns the outputs in full. The persisted copy in
`runs/<id>/result.json` is redacted. Exit codes: `0` success, `2` business outcome, `1` failed, `3` needs human.

**3. Human handoff** on the live session:

```sh
cua replay $CAP -i member_id=13579 --headed --handoff-wait 600
# the run pauses on "SUPERVISOR OVERRIDE REQUIRED" and prints its run id; in terminal 2:
cua operator list
cua operator claim <run_id>             # automation is now locked out of the browser
#   ...in the open browser window: Override Code 4242, click Authorize...
cua operator release <run_id>           # automation re-checks the screen and finishes → success
#   or: cua operator release <run_id> --completed   |   cua operator abort <run_id>
```

Your actions (values redacted) and the full control history end up in `runs/<id>/result.json` and `control.json`.

**Other commands:** `cua schema` prints the artifact JSON Schema. `cua replay --confirm-irreversible` is required,
together with an `approved` artifact, before replay performs any step that policy classifies as irreversible.

## Layout

```
cua/surface.py     Surface protocol + PlaywrightSurface: perception, locator resolution, control gate, human capture
cua/schema.py      Capability artifact, app Profile, RunResult (pydantic; `cua schema` emits JSON Schema)
cua/agent.py       discovery loop: observe → decide (LLM tool call) → act
cua/recorder.py    verified locators, derived checkpoints, parameterization → artifact
cua/replay.py      deterministic executor + outcome classifier + handoff wait
cua/handoff.py     control lease (control.json) + intervention requests
cua/policy.py      allowlist, irreversible-action classification, redaction
cua/cli.py         discover | replay | operator | schema
mockbank/server.py the hostile target app (stdlib http.server)
profiles/          app-wide detectors and PII labels per vendor product (mockbank-core@2)
capabilities/      approved artifacts
policy.json        allowlist + risk rules
evidence/          real discovery + replay + handoff runs
runs/              per-run output (gitignored)
```
