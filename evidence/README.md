# Evidence

Real runs against the local MockBank Core app (`python -m mockbank.server`), synthetic data only.
Every folder is a copy of a `runs/<run_id>/` directory, exactly as the system wrote it. Logs and results are
redacted at write time. Screenshots (`.png`) are only taken on failure or handoff, and they show the synthetic screen unredacted.

| Folder | What it shows | Result |
|---|---|---|
| `01-discovery/` | **Real LLM discovery**: `gemini-2.5-flash`, 8 calls, ~10k tokens. `log.jsonl` has every observation (as the model saw it, redacted) and every tool call with its `intent`. `validation-replay/` is the immediate deterministic replay that gates saving. | `success`, `outputs_match: true` |
| `capability/…1.0.0-draft.json` | The artifact exactly as discovery emitted it: `status: draft`, `outcomes: []` | |
| `capability/…1.1.0-approved.json` | After human review: business outcomes declared, input described, `approved`. Same as `/capabilities/`. | |
| `02-replay-success/` | Replay with no LLM: sign on → inquiry → detail → typed output | `success` |
| `03-replay-undeclared-state-draft/` | **Draft** artifact, member `99999`: "no member" is not declared yet, so the replay refuses to guess | `failed` / `unclassified_state` (+ screenshot, screen text) |
| `04-replay-business-not-found/` | Approved artifact, member `99999` | `business_outcome` / `member_not_found` |
| `05-replay-business-invalid-number/` | Approved artifact, member `00000` (app-side validation) | `business_outcome` / `invalid_member_number` |
| `06-replay-input-invalid/` | Member `12a45`: rejected by the input contract before the UI is touched | `failed` / `input_invalid` |
| `07-replay-recoverable/` | Injected interstitial notice, slow load, and a one-time HTTP 500 | `success`, `recoveries: [system_notice→click, server_error→restart]` |
| `08-replay-hard-failure/` | Persistent HTTP 500: recovery budget (3) exhausted | `failed` / `recovery_exhausted` (+ screenshot) |
| `09-replay-needs-human-no-operator/` | Member `13579` (supervisor override), no operator wait configured | `needs_human` + `intervention.json` |
| `10-replay-handoff-live/` | **Live handoff**: same case, headed with `--handoff-wait 600`. A person ran `cua operator claim`, entered the override code in the paused browser, and ran `cua operator release`. Automation re-classified the screen and finished. | `success`. `handoffs[0].actions` = typed Override Code (`[SECRET]`), clicked Authorize; full lease history in `control.json` |

## Reproduce

```sh
python -m mockbank.server                                   # terminal 1
cua discover "Look up the member and read their savings balance" -i member_id=12345   # needs LLM_* in .env
CAP=capabilities/mockbank_core.get_savings_balance.json
cua replay $CAP -i member_id=12345                          # 02
cua replay <draft artifact> -i member_id=99999              # 03
cua replay $CAP -i member_id=99999                          # 04
cua replay $CAP -i member_id=00000                          # 05
cua replay $CAP -i member_id=12a45                          # 06
curl "http://127.0.0.1:8765/__faults?reset=1&interstitial=1&slow=1&slow_ms=2000&error500=1"
cua replay $CAP -i member_id=12345                          # 07
curl "http://127.0.0.1:8765/__faults?reset=1&error500=9"
cua replay $CAP -i member_id=12345                          # 08
curl "http://127.0.0.1:8765/__faults?reset=1"
cua replay $CAP -i member_id=13579                          # 09
cua replay $CAP -i member_id=13579 --headed --handoff-wait 600   # 10, then in terminal 2:
cua operator claim <run_id>   # enter override code 4242 in the browser, click Authorize
cua operator release <run_id>
```

Exit codes: `0` success, `2` business outcome, `1` failed, `3` needs human.
