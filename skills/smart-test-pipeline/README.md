# Smart Test Pipeline - Guarded PR Review and Fix Loop

## What it does

1. Captures a baseline and triggers the configured review bots on the PR.
2. Waits for each configured bot to publish a completion signal.
3. Collects unresolved, non-outdated review threads through GitHub GraphQL.
4. Spawns a fix agent with a structured brief containing review and CI findings.
5. Runs the configured tests and optional lint in a credential-free disposable sandbox before committing or pushing changes.
6. Optionally waits for at least one completed, successful CI check.
7. Repeats until no unresolved findings remain or the maximum iteration count is reached.

## Quick start

```bash
# Basic - point at a PR URL
./run.sh "https://github.com/owner/repo/pull/123"

# With options
./run.sh "https://github.com/owner/repo/pull/123" \
  --max-iterations 10 \
  --fix-agent pi \
  --wait-ci true
```

## Configuration

Copy `config.example.sh` to `config.sh` beside `run.sh`, or set `SMART_TEST_CONFIG` to a trusted operator-owned config path. CLI flags override config, and config overrides built-in defaults. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_ITERATIONS` | 10 | Max loop cycles |
| `FIX_AGENT` | pi | Agent to spawn for fixes (pi, claude, codex, opencode) |
| `WAIT_CI` | true | Wait for CI green before next iteration |
| `CI_TIMEOUT` | 3600 | Seconds to wait for CI |
| `AGENT_TIMEOUT` | 1800 | Seconds before a stalled fix agent is terminated |
| `REVIEW_BOTS` | coderabbit greptile | Bots to trigger |
| `TEST_CMD` | `python -m pytest --tb=short -q` | Local test command |
| `LINT_CMD` | (empty) | Local lint command |
| `VALIDATION_SANDBOX` | auto | Disposable backend: `macos` (`sandbox-exec`), `bwrap`, or Docker |
| `AGENT_SANDBOX` | auto | Disposable agent backend |
| `ALLOWED_SUPPORT_GLOBS` | test patterns | Narrow patterns for reviewed supporting files |

## Safety

The pipeline does not merge pull requests, refuses closed/merged PRs, isolates concurrent PR runs, and protects Git control data. Its complete safety contract is documented in [SKILL.md](SKILL.md).
Use `--force` only when a force-with-lease push is explicitly intended.
The pipeline stops at the configured iteration limit and writes a full report.
Use `--dry-run` to preview the run without review triggers, agent execution, commits, pushes, or CI polling.

## Output

By default, the pipeline writes each run under `${TMPDIR:-/tmp}/greploop-data/<owner>/<repo>/pr-<pr>/<run-id>/`, so stale iteration artifacts cannot be reused.
