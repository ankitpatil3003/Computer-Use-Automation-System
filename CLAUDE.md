# CLAUDE.md

Take-home for interface.ai: "Assignment A — Computer-Use Automation System" (PDF in repo root — the source of truth; re-read the relevant section before changing scope).

**Through-line:** the LLM discovers a flow once → recorder emits a typed, versioned capability artifact → deterministic replay (no LLM) runs it in production, with explicit error taxonomy, human handoff on the same live session, and safety guardrails.

**Goal:** a small, complete, end-to-end vertical slice touching every §3 requirement. Depth over breadth. Graders reward judgment (schema, locators, error taxonomy, control-transfer model), not features or infra.

## Required deliverables (exact paths — do not rename)

- `/README.md` — setup, keys/config, how to run without live services, exact demo commands (discover → replay).
- `/REPORT.md` — ~1–3 pages, exactly these seven headings, in order:
  1. `## Architecture`
  2. `## Artifact schema`
  3. `## Determinism & error handling`
  4. `## Heterogeneity & multi-tenant`
  5. `## Escalation & handoff`
  6. `## Safety`
  7. `## Cuts`
- `/evidence/` — saved artifact + logs from a **real** LLM discovery run + replay runs (success, business outcome, recoverable, hard failure, handoff).

## Stack (decided)

- Python 3.11+, Playwright (sync API), Pydantic v2, pytest.
- LLM: **Gemini Flash (free tier)** via its OpenAI-compatible endpoint, using the `openai` SDK as the client only. Provider is config, not code:
  - `LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/`
  - `LLM_MODEL=<current Gemini Flash model id from AI Studio>`
  - `LLM_API_KEY=...`
  - Groq / Ollama are drop-in alternates (same code, different env). No provider abstraction layer.
- Secrets live in `.env` (gitignored); commit `.env.example` only.
- Replay, tests, and the handoff demo must run **without** any LLM key.

## Layout (planned)

```
cua/
  surface.py    Surface protocol (observe/resolve/act/screenshot) + PlaywrightSurface — the heterogeneity seam
  schema.py     Capability artifact + RunResult (pydantic; also emits JSON Schema)
  agent.py      discovery loop: observe → decide → act via LLM tool calls
  recorder.py   live element → verified multi-strategy locator; trace → artifact
  replay.py     deterministic executor + outcome classifier
  policy.py     allowlist, risk classification, redaction
  handoff.py    control lease, intervention requests, human action capture
  cli.py        discover | replay | operator
mockbank/server.py            hostile local target app (stdlib http.server)
profiles/mockbank-core.json   shared app-level detectors (per vendor product)
capabilities/                 saved artifacts
policy.json                   allowlist + risk rules
tests/
evidence/
runs/                         per-run working output (gitignored)
```

## Key design decisions (defend these in REPORT.md; don't silently change them)

- **Target:** local "MockBank Core" — frameset (menu + main), table layouts, no test IDs, generic field names, idle-expiring session, synthetic data only.
  - Data-driven exceptions: `99999` not found, `00000` validation error, `13579` supervisor override (handoff demo).
  - Injectable faults: interstitial dialog, slow load, one-time 500, forced session expiry.
- **Perception:** accessibility tree across all frames with short refs (`e12`); screenshot secondary. The agent never sees CSS. Rationale: a11y trees exist on desktop too (UIA/AX).
- **Locators:** explicit frame path + ordered strategies `role+name` → `text_anchor` (label-less tables) → `css`.
  - First strategy with **exactly one** visible match wins; ambiguity = hard failure (never "pick first").
  - A lower-ranked strategy winning logs `locator_degraded` (drift signal).
  - Locators are verified unique on the live page at record time.
- **Artifact:** contract on top (`inputs`, `outputs`, `success`, `outcomes`), steps below, stable step `id`s (tenant overlays patch by id), `status: draft|approved`, semver `version`, `provenance`. App-wide states (session expiry, interstitials, error pages) live in the shared profile, not each capability.
- **Parameterization:** literal input values in the trace are rewritten to `{{inputs.x}}`.
- **Save rule:** an artifact is saved only after an immediate deterministic replay of it passes.
- **Result statuses:** `success | business_outcome | failed | needs_human`.
  - Business outcome: stop cleanly, return `{code, message}`.
  - Recoverable: profile handler (dismiss / wait+retry / re-login via secret ref), bounded budget, logged in `recoveries[]`; exhausted budget → hard failure.
  - Hard failure: `{code, step_id, expected, observed, evidence}` with screenshot + a11y snapshot. Unmatched state = `unclassified_state` — never guess forward.
  - Business outcomes are declared at human review; undeclared states fail safe.
- **Handoff:** control lease `AUTOMATION → AWAITING_HUMAN → HUMAN → AUTOMATION | ABORTED | COMPLETED_BY_HUMAN` in `runs/<id>/control.json`.
  - `Surface.act()` refuses unless automation holds the lease.
  - Headed browser = the live session the human operates; `cua operator claim|release` moves the lease.
  - Human actions captured by an injected per-frame listener (values redacted).
  - On resume, re-verify state (detectors + step `expect`) before continuing.
- **Safety:**
  - `policy.json` allowlist (origins, paths, action types) checked on every action.
  - Irreversible actions: blocked + escalated in discovery; replay needs an approved artifact + `--confirm-irreversible`.
  - The LLM only sees secret *names*.
  - One redactor for logs, LLM observations, and human-action capture.
  - Screenshots only on failure/handoff.
  - Documented limit: Gemini free-tier prompts may be used by Google — acceptable only because data is synthetic.

## Build order

0. CLAUDE.md ✅
1. Mock app + fault injection
2. Schema + surface + locator resolution
3. Replay + taxonomy + policy/redaction — test against a **hand-written artifact** before any LLM spend
4. Agent + recorder → real discovery run
5. Handoff
6. Evidence runs
7. README.md + REPORT.md

Wait for user approval at the end of each stage before starting the next.

## Working rules

- Keep it small: fewest files, stdlib first, no new deps beyond the stack above without asking.
- No queues, services, multi-tenant plumbing, or desktop implementation — design-only in REPORT.md.
- Never commit secrets, real credentials, or real PII. Evidence uses synthetic data only.
- Tests only where logic branches: locator resolution, outcome classification, policy/redaction, lease transitions.
- Commit only when the user asks.
