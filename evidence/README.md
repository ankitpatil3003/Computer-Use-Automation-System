# Evidence

Real runs against the local MockBank Core app (`python -m mockbank.server`), synthetic data only.
Every folder is a copy of a `runs/<run_id>/` directory, exactly as the system wrote it.
- Logs and results are redacted when they are written.
- Screenshots (`.png`) are taken only on failure or handoff. Sensitive regions (input values, PII-labelled fields,
  account numbers, amounts, known input values) are blacked out at capture time.

| Folder | What it shows | Result |
|---|---|---|
| `01-discovery/` | **Real LLM discovery**: `gemini-2.5-flash`, 8 calls, ~10k tokens. `log.jsonl` has every observation (as the model saw it, redacted) and every tool call with its `intent`. `validation-replay/` is the immediate deterministic replay that gates saving. | `success`, `outputs_match: true` |
| `capability/…1.0.0-draft.json` | The artifact exactly as discovery emitted it: `status: draft`, `outcomes: []` | |
| `capability/…1.1.0-approved.json` | After human review: business outcomes declared, input described, `approved`. Same as `/capabilities/`. | |
| `02-replay-success/` | Replay with no LLM: sign on → inquiry → detail → typed output | `success` |
| `03-replay-undeclared-state-draft/` | **Draft** artifact (`--allow-draft`), member `99999`: "no member" is not declared yet, so replay refuses to guess | `failed` / `unclassified_state` (+ masked screenshot, screen text) |
| `04-replay-business-not-found/` | Approved artifact, member `99999` | `business_outcome` / `member_not_found` |
| `05-replay-business-invalid-number/` | Member `00000` (app-side validation) | `business_outcome` / `invalid_member_number` |
| `06-replay-business-permission-denied/` | Member `24680` (restricted record; detector comes from the app profile) | `business_outcome` / `permission_denied` |
| `07-replay-input-invalid/` | Member `12a45`: rejected by the input contract before the UI is touched | `failed` / `input_invalid` |
| `08-replay-draft-not-approved/` | The draft artifact without `--allow-draft`: unattended replay requires `approved` | `failed` / `not_approved` (UI never touched) |
| `09-replay-recoverable-notice-slow-500/` | Injected interstitial notice, slow load, and a one-time HTTP 500 | `success`, `recoveries: [system_notice→click, server_error→restart]` |
| `10-replay-recoverable-session-expired/` | Session forcibly expired mid-flow | `success`, `recoveries: [session_expired→restart]` (signs on again) |
| `11-replay-unexpected-native-dialog/` | A native `confirm()` the flow never saw: answered Cancel (safe) and the run stops | `failed` / `unclassified_state`, observed shows the dialog text |
| `12-replay-hard-failure/` | Persistent HTTP 500: recovery budget (3) exhausted | `failed` / `recovery_exhausted` (+ screenshot) |
| `13-replay-needs-human-no-operator/` | Member `13579` (supervisor override), no operator wait configured | `needs_human` + `intervention.json` |
| `14-replay-handoff-live/` | **Live handoff**: same case, headed, with `--handoff-wait`. A person ran `cua operator claim`, entered the override code in the paused browser, and ran `cua operator release`. Automation re-classified the screen and finished. | `success`. `handoffs[0].actions` = typed Override Code (`[SECRET]`), clicked Authorize; full lease history in `control.json` |

Discovery-side handoff (an agent that asks for help, a human unblocks it on the live session, the model continues)
is covered by `tests/test_agent.py::test_operator_unblocks_discovery_on_the_live_session`.

## Reproduce

```sh
python -m mockbank.server                                   # terminal 1
cua discover "Look up the member and read their savings balance" -i member_id=12345   # needs LLM_* in .env
CAP=capabilities/mockbank_core.get_savings_balance.json
DRAFT=evidence/capability/mockbank_core.get_savings_balance.1.0.0-draft.json
fault() { curl -s "http://127.0.0.1:8765/__faults?reset=1&$1" > /dev/null; }
fault; cua replay $CAP -i member_id=12345                   # 02
cua replay $DRAFT -i member_id=99999 --allow-draft          # 03
cua replay $CAP -i member_id=99999                          # 04
cua replay $CAP -i member_id=00000                          # 05
cua replay $CAP -i member_id=24680                          # 06
cua replay $CAP -i member_id=12a45                          # 07
cua replay $DRAFT -i member_id=12345                        # 08
fault "interstitial=1&slow=1&slow_ms=2000&error500=1"; cua replay $CAP -i member_id=12345   # 09
fault "expire=1";   cua replay $CAP -i member_id=12345      # 10
fault "confirm=1";  cua replay $CAP -i member_id=12345      # 11
fault "error500=9"; cua replay $CAP -i member_id=12345      # 12
fault; cua replay $CAP -i member_id=13579                   # 13
cua replay $CAP -i member_id=13579 --headed --handoff-wait 600   # 14, then in terminal 2:
cua operator claim <run_id>   # enter override code 4242 in the browser, click Authorize
cua operator release <run_id>
```

Exit codes: `0` success, `2` business outcome, `1` failed, `3` needs human.
