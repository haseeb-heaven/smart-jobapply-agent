# Runtime Support Matrix

Support labels describe recorded evidence, not intended compatibility.

| Runtime / transport | Contract tests | Synthetic browser tests | Live agent test | Human test | Current label |
|---|---:|---:|---:|---:|---|
| Generic two-operation host binding | passed | n/a | not run | not run | contract-only |
| Codex legacy/generic binding | passed | n/a | not run | not run | contract-only |
| Playwright injected context | passed | local engine blocked; CI configured | not run | not run | contract-only |
| Chromium in CI (MVP) | configured | pending CI run | n/a | n/a | not run |
| Firefox / WebKit | configured for a later expansion | deferred | n/a | n/a | not run |
| Codex cloud computer-use session | n/a | n/a | no active structured session | not run | blocked |
| External argv bridge | existing protocol tests passed | host-dependent | not run | not run | contract-only |

`verified` requires a successful recorded live test in the exact environment,
with the isolated human test required by the runbook. A cloud runtime must also
demonstrate a durable private storage boundary across the restart lifecycle it
claims. A user-selected browser becomes verified only after its own conforming
bridge run; Chromium or WebKit CI does not certify a different browser product.
