# Jev Codex Router sidecar for OpenRouter

This package is the persistent Python sidecar used by the patched Codex TUI. It communicates only
through newline-delimited JSON on stdin/stdout; diagnostics go to stderr.

## Install

From the Codex checkout:

```bash
python -m pip install ./router
```

Set `OPENROUTER_API_KEY`, build the patched Codex CLI, and launch the router build. After model
discovery, the TUI eagerly starts and owns one persistent sidecar process; no separate server
command is needed.

The sidecar uses OpenRouter's native Decisions API, not Chat Completions:

```text
POST https://openrouter.ai/api/alpha/decisions
model = "~typesafe/jev-latest"
```

The request contains the shared routing `state` and one typed `choice` question. Choice
probabilities come from `answers.route.probabilities`, while confidence comes from
`answers.route.confidence`.

## Configure

Add this to `~/.codex/config.toml`:

```toml
[auto_router]
enabled = true
provider = "jev"
mode = "auto" # or "shadow"

[auto_router.jev]
model = "~typesafe/jev-latest"
attempt_timeout_ms = 1200
max_retries = 1
total_deadline_ms = 2500
min_confidence = 0.60
# Optional when Python is not on PATH:
# sidecar_command = ["/absolute/path/to/python", "-m", "jev_codex_router.server"]

[auto_router.routing]
route_only_when_idle = true
allow_keep_current = true
upgrade_min_confidence = 0.60
downgrade_min_confidence = 0.80
downgrade_max_context_tokens = 20000
allowed_models = ["gpt-5.6-luna", "gpt-5.6-sol"]
min_profile = "luna_medium"
max_profile = "sol_high"
allow_luna_high = true

[auto_router.context]
include_previous_turn_status = true
include_failure_count = true
include_previous_prompt = false
include_diff_stats = false
include_context_tokens = true

[auto_router.logging]
enabled = true
log_prompts = false
retain_recent_decisions = 20
# Keep a short prompt preview only in TUI memory; never write it to disk.
store_prompt_text = "session_only" # or "off"
```

The actual model slugs must match the values returned by the current Codex `model/list`. Unsupported
profiles are removed before Jev is called. Luna/low and Sol/xhigh are never generated.

Runtime controls:

```text
/autoroute status
/autoroute on
/autoroute off
/autoroute auto
/autoroute explain
/autoroute pin luna medium
/autoroute pin luna high
/autoroute pin sol medium
/autoroute pin sol high
```

`explain` reads the most recent in-memory decision and never calls Jev. A concrete selection in
Codex's `/model` picker pauses automatic routing until `/autoroute auto`. Explicit prompt
instructions such as `use Sol high` or `这次用 Luna` take precedence over Jev. These commands do
not rewrite `config.toml`.

Downgrades require the higher confidence threshold and are rejected when the native Codex context
size exceeds `downgrade_max_context_tokens`, avoiding an uneconomical prompt-cache rebuild. Every
new-turn decision receives a stable policy reason. Active-turn steering and tool-loop
continuations remain on the model selected at that turn's native start boundary.

## Routing benchmark

The package includes an explicit benchmark driver for the six policy groups in the v3
comparison: three fixed baselines, prompt-only model routing, prompt-only model+effort
routing, and execution-aware model+effort routing. Its input is the same validated route
request JSONL accepted by the sidecar (an optional `case` field is copied to the results):

```powershell
jev-codex-router-benchmark --input .\cases.jsonl --output .\routing-results.jsonl
```

The model-only arm exposes only each model's medium-effort profile. The prompt-only arms
retain `context_tokens` for cache-aware policy but remove failure/diff/tool feedback, so
the execution-aware arm isolates the effect of those metrics. Each experiment records its
choice, confidence distribution, routing latency, or a per-case error without aborting the
remaining corpus.

## End-to-end benchmark

The end-to-end driver measures whether the selected Codex profile actually completes small coding
tasks. Every task contains multiple user turns in one persisted Codex thread. The first turn starts
with `codex exec`; later turns use `codex exec resume`, and every new turn is independently routed
with its new prompt, current profile, and the latest reported context-token usage. This matches the
current TUI implementation, which still sends `previous_turn_status = unknown` and does not yet
populate diff/tool/failure metrics. It makes profile switches inside a task observable instead of
testing only one initial choice without giving Jev data the production router does not have. The
driver copies a fresh workspace for every case and strategy, then runs a deterministic verifier kept
outside the agent's writable workspace. A zero Codex exit code is not a success by itself. A run
passes only when all turns exit normally, no files outside the case's allowlist change, and every
hidden verifier succeeds.

The bundled quick suite deliberately stays small: one easy string-normalization task, one medium
stateful boundary-condition task, and one hard atomic multi-object task. Each has two short turns
(implementation/review, implementation/bug report, or analysis/implementation). The default
comparison is three profiles/policies, so one repetition creates nine Codex sessions containing 18
turns. Only the six turns in the dynamic-router arm call Jev:

```powershell
$env:OPENROUTER_API_KEY = "your-key"

jev-codex-router-e2e `
  --codex <path-to-codex> `
  --output .\e2e-report.json `
  --artifacts .\e2e-artifacts
```

From an uninstalled source checkout, use the equivalent module entry point:

```powershell
cd <path-to-JevCodex>
$env:PYTHONPATH = ".\router"
python -m jev_codex_router.e2e_benchmark `
  --codex <path-to-codex>
```

Add `--dry-run` to print the exact Codex session, Codex turn, and Jev call counts without making any
API request. Default `--max-runs 12` and `--max-turns 20` safety caps prevent an accidentally large
strategy/repetition matrix.

The default policies are `always_luna_medium`, `always_sol_medium`, and
`jev_execution_model_effort`. To run all six policies from the routing-only benchmark, pass an
explicit comma-separated list:

```powershell
jev-codex-router-e2e `
  --codex <path-to-codex> `
  --max-runs 18 `
  --max-turns 36 `
  --experiments always_luna_medium,always_sol_medium,always_sol_high,jev_prompt_model_only,jev_prompt_model_effort,jev_execution_model_effort
```

Use `--repetitions 3` only when estimating variance is worth the extra model usage. The JSON report
keeps two success rates: `strict_success_rate` uses every attempted run as the denominator, while
`conditional_success_rate` covers runs that reached deterministic verification. Timeouts and agent
startup failures remain visible as separate outcomes and therefore cannot silently inflate the
strict rate. Per-run artifacts include the isolated final workspace and, for every turn, its Codex
JSONL events, stderr, last message, route choice, final policy decision, and latency. Codex reports
thread-cumulative token usage on `turn.completed`; the report stores that cumulative value plus a
per-turn delta computed against the preceding turn, avoiding double counting resumed-thread usage.
The summary totals input, cached input, non-cached input, cache-write, output, and reasoning tokens
when those fields are reported. It also reports profile distribution, within-task routing switches,
and Jev fail-open routing fallbacks, so a task completed by the current model after a routing outage
is not mistaken for a successful Jev decision.

The Jev arm mirrors the current router behavior: `keep_current` remains available, the concrete
current profile is removed from Jev's criteria, probabilities are aggregated by final action, and a
downgrade is accepted when two of confidence >= 0.80, selected probability >= 0.65, and probability
margin >= 0.25 pass. The existing 20,000-token downgrade guard is also applied.

### Rerun and merge one failed case

Use `--case` to rerun only one case. `--merge-existing` reads the report named by `--output`, and
after every selected run finishes, atomically replaces results with the same case, experiment, and
repetition. If the rerun is interrupted, the existing report remains unchanged; partial artifacts
stay in their new timestamped directory.

```powershell
python -m jev_codex_router.e2e_benchmark `
  --codex <path-to-codex> `
  --case atomic_order `
  --max-runs 3 `
  --max-turns 6 `
  --output .\e2e-report.json `
  --artifacts .\e2e-artifacts `
  --merge-existing
```

The resume command explicitly reapplies `sandbox_mode = "workspace-write"`. This is necessary
because `codex exec resume` does not expose the initial command's `--sandbox` option and otherwise
may restore the follow-up turn as read-only.

## Test

```bash
PYTHONPATH=router python -m unittest discover -s router/tests -v
```
