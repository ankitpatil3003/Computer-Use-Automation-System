# Report

## Architecture

One synchronous Python process. Three entry points share one core:

```
cua discover ──▶ agent (LLM loop) ──▶ Surface ◀── replay (no LLM) ◀── cua replay
                     │                   ▲   ▲            │
                 recorder ──draft──▶ validation replay    └─▶ RunResult {success|business_outcome|failed|needs_human}
                     │                   │   │
               capabilities/  ◀─saved only if outputs match   └── lease (control.json) ◀── cua operator claim|release|abort
```

Key decisions:

- **Perception: a text rendering of the accessibility view, not the DOM.** Frames are rendered in reading order, one line
  per table row, with controls inlined as `[e12 button "Go"]`. The model never sees CSS. It works on a frameset with
  nameless inputs, maps 1:1 onto desktop accessibility trees (UIA/AX), and costs about 1.2k tokens per turn.
  *Trade-off:* visual-only state (colour, canvas) is missed.
- **The model never writes the artifact.** Agent actions go through the same `Surface.perform` + policy path as replay.
  The recorder builds each step from what was verified on the live page: a locator unique to that element, a checkpoint
  (static text the action made appear) and templated inputs. A draft is saved only after an immediate deterministic
  replay reproduces the outputs (`evidence/01-discovery/validation-replay`).
- **The target is a self-built hostile mock,** so error states and faults are controllable (`/__faults`) and no real data
  is involved. *Risk:* building for my own app. *Mitigation:* the engine has no app knowledge; it all lives in the
  artifact or in `profiles/mockbank-core.json`.
- **LLM: Gemini 2.5 Flash** (free tier, OpenAI-compatible endpoint). The provider is three environment variables with no
  abstraction layer. I chose it for availability: the 3.x Flash models returned 503/429 on most calls during the build.
  Discovery took 8 calls and about 10k tokens.
- **No services or queues.** The seams exist: `Replayer(artifact, profile, policy, surface, secrets, tenant) → RunResult`
  is a pure job, run state lives in `runs/<id>/`, and the lease is a file another process can own.

## Artifact schema

Pydantic models with `extra="forbid"`; `cua schema` emits the JSON Schema. An abridged real artifact:

```jsonc
{ "id": "mockbank_core.get_savings_balance", "version": "1.1.0", "status": "approved",
  "app": { "product": "mockbank-core", "versions": "2.x", "surface": "web", "profile": "mockbank-core@2" },
  "entry": "{{tenant.base_url}}/",
  "inputs":  { "member_id": { "type": "string", "pattern": "^\\d{5}$", "sensitivity": "pii_id" } },
  "outputs": { "savings_balance": { "type": "decimal", "sensitivity": "financial", "transform": "currency",
               "source": { "frame": ["main"], "strategies": [
                 { "by": "table_cell", "row": "Share Savings", "column": "Balance" },
                 { "by": "css", "selector": "body > font:nth-of-type(1) > table:nth-of-type(3) > …" } ] } } },
  "steps": [ …
    { "id": "fill_member", "action": "fill", "value": "{{inputs.member_id}}",
      "target": { "frame": ["main"], "strategies": [
        { "by": "text_anchor", "anchor": "Member #", "role": "textbox" }, { "by": "css", "selector": "input[name=\"f1\"]" } ] },
      "risk": "safe", "intent": "Fill in the member ID to search for the member." },
    { "id": "click_go", "action": "click", …,
      "expect": { "frame": ["main"], "text": "Member Detail" } } ],
  "success":  [ { "frame": ["main"], "text": "Member Detail" } ],
  "outcomes": [ { "code": "member_not_found", "kind": "business",
                  "when": { "frame": ["main"], "text": "E404 NO MEMBER ON FILE" }, "message": "No member exists…" } ],
  "provenance": { "recorded_by": "discovery:gemini-2.5-flash", "run_id": "20261019-152637-discover-627656", … } }
```

Why it is shaped this way:

- **Contract on top, flow below.** A calling agent reads `inputs`, `outputs`, `success` and `outcomes`. Replay executes
  `steps`. A reviewer reads each step's `intent`.
- **Locators are an explicit frame path plus ordered strategies,** from most semantic to most structural: `role+name` →
  `text_anchor` (the control in the row labelled X) → `table_cell` → `css`. Only strategies verified unique for that
  element at record time are kept. The member-number box has no accessible name, so it gets `text_anchor "Member #"` and
  `css`, and no `role+name`.
- **Templates** (`{{inputs.x}}`, `{{secrets.NAME}}`, `{{tenant.x}}`) are validated at load time, so a reference to an
  undeclared input makes the artifact invalid. Secrets appear by name only. `sensitivity` drives redaction everywhere.
- **Stable step `id`s** exist so tenant overlays can patch a step by id. They come from the action and the control
  (`click_go`), never from the model's wording, so re-recording the same flow keeps them.
- **`draft → approved`, with business outcomes declared at review.** Discovery only ever sees the happy path. Undeclared
  states fail safe: the same `99999` replay is `unclassified_state` on the draft (evidence 03) and
  `business_outcome / member_not_found` on the reviewed 1.1.0 (evidence 04). Unattended replay requires `approved`
  (`not_approved`, evidence 08); drafts run only with `--allow-draft`, and discovery writes them to
  `capabilities/drafts/`, so it can never overwrite a reviewed artifact.
- **App-wide states live in a shared profile,** once per vendor product and major version (`mockbank-core@2`): session
  expiry, the system notice, HTTP 500, permission denied, supervisor override, rejected sign-on, and PII labels. One fix
  there repairs every capability of that product.
- **Semver:** a contract change bumps the major version, added outcomes bump the minor. `app.versions` records the
  product versions it is known to work on.

## Determinism & error handling

Each step runs **settle** (classify the screen) → **guard** (policy) → **act** → **verify** (poll the checkpoint). There
are no fixed sleeps.

- **Resolution:** poll, until the step's timeout, for the first strategy in rank order that matches exactly one visible
  element. If several still match at the deadline, the step fails with `locator_ambiguous`; replay never picks the first
  of several. A lower-ranked winner logs `locator_degraded`, the drift signal. A test covers this: the top strategy names
  a button that no longer exists, the next one wins, and the step still succeeds.
- **Checkpoints:** every navigating click carries an `expect` derived at record time ("text in frame" and/or "exactly
  one element").
- **Classification:** every poll matches the screen against detectors, capability `outcomes` first and then the profile.

| kind | response | result |
|---|---|---|
| `business` | stop cleanly | `business_outcome {code, message}` (evidence 04, 05, 06) |
| `recoverable` | `click` a dismiss target, or `restart` from entry (re-signs on); budget of 3, logged in `recoveries[]` | success (09, 10) or `failed/recovery_exhausted` (12) |
| `hard` | stop | `failed {code, step_id, expected, observed, evidence}` |
| `human_required` | intervention request; live handoff if configured | `needs_human` (13) or resume (14) |
| nothing matches and the checkpoint times out | never guess forward | `failed/unclassified_state` + screenshot + redacted screen text (03) |
| a native `alert`/`confirm` the flow never declared | answer Cancel (commits nothing), stop | `failed/unclassified_state` with the dialog text (11) |

- Inputs are checked against the contract before the browser is touched (`input_invalid`, evidence 07).
- `restart` is refused after an irreversible step, because repeating the flow would repeat the side effect.

Limits: detectors match on text. That suits slow-changing legacy screens but is sensitive to locale and branding, which
belongs in tenant overlays.

## Heterogeneity & multi-tenant

**Surface seam.** The artifact and the replay engine speak only `Locator`, `Check` and `Step`. Everything
technology-specific sits behind the `Surface` protocol (`observe`, `locator_for`, `perform`, `holds`, `read`,
`screenshot`, `idle`).

- **Legacy web** is what was built: framesets, nested tables, no ids.
- **A desktop surface** would implement the same protocol over UIA or AX. The schema already carries
  `app.surface: web|desktop`.

  | Web strategy | Desktop equivalent |
  |---|---|
  | frame path | window/pane path |
  | `role+name` | ControlType + Name |
  | `text_anchor` | the label next to a field in a grid or form |
  | `css` | AutomationId or tree path, still ranked last |

- **Screen-only surfaces** (Citrix, terminals) would be a third surface: `observe` via OCR, with `text_anchor` doing most
  of the work.

**Multi-tenant reuse.** There are three layers, resolved at run time:

1. **Profile** per vendor product and major version: app-wide states and PII labels.
2. **Base capability**, recorded once per product.
3. **Tenant overlay**: a JSON merge-patch keyed by step `id` (relabelled button, extra confirmation step, different
   error text), plus tenant config (`base_url`, which secrets to use). It is validated by the same schema.

`{{tenant.base_url}}` and per-tenant secrets are built; overlays are designed only.

**Drift:** track `locator_degraded` and `unclassified_state` rates per (tenant, product version, capability version), and
run canary replays when a tenant upgrades. A tenant that diverges gets an overlay, or a re-discovery seeded with the base
artifact, so the LLM handles only the difference.

## Escalation & handoff

**Detecting "stuck":**

- **In discovery:** the agent's `ask_human` tool, a policy-irreversible target (blocked *before* the click), no progress
  (the screen is unchanged after 3 consecutive clicks), the 25-step budget, or the wall-clock timeout (600 s, not
  counting time a human holds the session).
- **In replay:** a `human_required` detector (the supervisor override), or an irreversible step without an approved
  artifact and an explicit confirmation.

Each case writes `intervention.json`: the capability or goal, the step id and intent, the reason, the redacted screen, a
screenshot, and the operator commands.

**Control model.** A lease in `runs/<id>/control.json`:

```
AUTOMATION → AWAITING_HUMAN → HUMAN → AUTOMATION | COMPLETED_BY_HUMAN | ABORTED   (AWAITING_HUMAN → ABORTED on timeout)
```

- Transitions are compare-and-swap on the current state, with an append-only history. Only the holder can release.
- It is **enforced, not advisory**: `Surface.perform` raises unless the lease reads `AUTOMATION`.
- Automation never takes the session back: the timeout applies only *before* a claim.

**Taking the live session.** With `--handoff-wait`, replay *and discovery* keep the headed browser open (same session,
same cookies) while they wait on the lease, and the human works in that exact window. Both use one wait function
(`handoff.await_operator`). The operator UI is mocked as the CLI
(`cua operator list|claim|release|abort`) plus the window itself. In production the same lease would sit behind a
console that streams the remote browser (CDP screencast or VNC), with operator authentication.

**Recording the human.** A listener in every frame reports clicks and field changes by role and name. Password values
never leave the page, and other values go through the run's redactor. Actions are stored in `result.handoffs[].actions`
next to the lease history (evidence 14: `Override Code = [SECRET]`, then a click on `Authorize`).

**Handing back:**
- **`release`:** automation **re-classifies the screen** (detectors, then the step checkpoint). If the problem is still
  there, it raises a second request; after 2 handoffs the run returns `needs_human`.
- **`--completed`:** the success checks still decide the result, and automation extracts the outputs.
- **`abort`:** `failed/aborted`.
- **Irreversible steps are performed by the human.** Automation only verifies the checkpoint; a release is never treated
  as approval to click.
- **In discovery,** a release returns the human's actions to the model as context, and the model continues from the
  current screen. Those actions are *not* recorded as steps. If the flow needs them every time, the save rule's
  validation replay fails and the capability is not saved. That is correct: it needs a `human_required` outcome
  declared at review. A test covers this.

## Safety

- **Allowlist** (`policy.json`): hosts, denied paths (`/__faults`) and action types. It is enforced before every action
  and at the network layer: a Playwright route aborts any off-list request from any frame before it leaves the browser
  (`policy_violation`).
- **Risk:** policy classifies a step as irreversible from its target labels (close account, transfer, post, approve,
  authorize…). Only committing actions count; typing into a field labelled "Transfer Amount" is safe, and the click
  that submits it is not. The effective risk is the *stricter* of the artifact's declaration and the policy's inference, so an
  artifact cannot declare its way out.
  - **In discovery,** irreversible actions are blocked and escalated: the LLM is the least trusted actor, and in banking
    these actions have no undo.
  - **In replay,** they require `status: approved` and `--confirm-irreversible`; otherwise the run returns `needs_human`.
- **Data:** one `Redactor` covers logs, results, evidence text, intervention requests, human-action capture *and the
  LLM's view*. It masks known values (secrets, and inputs and outputs declared sensitive), patterns (SSN, account
  numbers, long digit runs, email addresses, money amounts) and values next to the profile's PII labels (`Name`, `SSN`).
  - **The model sees secret names, never their values, and no PII-labelled field or balance.** Extraction reads the live
    cell, so labels are all it needs; the discovery log in evidence 01 shows `[PII:Name]` and `[AMOUNT]`.
  - It sees the inputs it has to type; those are masked in everything persisted.
  - Outputs reach the caller in full only on stdout.
- **Screenshots** are taken only on failure or handoff, and are **masked at capture**: every input value, cells matching
  the redactor's patterns or holding a known sensitive value, and cells next to PII labels are blacked out.

**Limits:**
- **Gemini's free tier may use prompts for training.** That is acceptable here only because the data is synthetic.
  Production needs a no-retention enterprise endpoint or a self-hosted model.
- **Unlabelled free-text PII** (a name inside a note) reaches the model, the logs and the screenshots.
- **Native `confirm()` dialogs are always answered Cancel,** even while a human holds the session. Accepting one needs
  a real operator console.
- **Label-based risk misses icon-only buttons;** those must be declared at review.
- **Lease security is weak:** the file has no authentication, operator identity is the OS user, and notes are stored raw.
  Secrets come from environment variables, not a vault.

## Cuts

Deliberately left out, in the order I'd build them next:

1. **Tenant overlays,** demonstrated on a second variant of the mock app.
2. **A real operator console:** a streamed browser with claim and release buttons, authentication, accepting native
   dialogs, and a DB-backed lease (the file lease has a small read-then-replace race, marked `ponytail:` in the code).
3. **Recording human steps in discovery** as candidate steps for review, instead of context only.
4. **Bounded LLM fallback for one failed step,** policy-checked and recorded as evidence. Never open-ended.
5. **Stability scoring:** replay N times and gate `approved` on the pass rate, on top of today's approval gate.
6. **A desktop surface** (UIA): the protocol exists; no implementation.
7. **Smaller gaps:**
   - screenshot perception for visual-only state;
   - detectors for known native dialogs (today every native dialog stops the run);
   - anonymous iframes: locators require named frames;
   - review tooling: review today is editing the JSON, bumping the version and moving it out of `drafts/`.
