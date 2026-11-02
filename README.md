# SHEA

User-sovereign, modular AI agent runtime.

The **model proposes**. **Core authorizes**. The **execution plane** performs
controlled side effects. **Verification** checks outcomes. **Audit** records
the causal chain.

Requires **Python ≥ 3.11**.

Phase 1 is the foundation layer (state machine, persistence, contracts,
config). Phase 2 adds the Decision/Policy/Risk engine — the only subsystem
allowed to move a task into `RUNNING`. Phase 3 adds the Tool Registry and
Executor. Phase 4 closes verification/recovery. Phase 5 adds Intent
Understanding & Planning. Phase 6 adds Security & Trust (SSRF/path-scope,
redaction, injection detection, execution boundary). Phase 7 adds Provider
Routing & Failover. Phase 8 hardens enforcement: mandatory `SecurityService`,
no silent unsafe execution, idempotency, shared `UnitOfWork`, content-bound
authorization, tool schemas with elevated-capability register gates,
tamper-evident audit chain, and `step_verified` → READY. Phase 9 adds real
tools (`filesystem.read`/`write`, opt-in `http.fetch`) under runtime
path/DNS checks. Phase 10 finishes durable multi-step execution via
`PlanRunner`. On top of that, the tree now includes the **app/execution
plane** (`shea.app`), **execution permits**, **bootstrap + CLI**, optional
**model providers**, and **portable browser/network/process/OS adapters**.
Phase 11 (Final Completion) delivers **Universal Reconciliation** (sweeping orphaned transient tasks), **Fault Injection** testing, a narrowed **Facade** interface (`SheaApp`), lightweight **GUI**, and strict **Resource Governance** (`max_output_bytes` via `SandboxedExecutionBoundary`).

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q

# Run via CLI Facade
python -m shea --db shea.db --workspace ./workspace run "write a note"
python -m shea --db shea.db --workspace ./workspace run "read the note"

# Or run the graphical interface (FastAPI + HTML GUI)
uvicorn shea.api.server:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

Demo phrases use deterministic templates (no LLM required).

## Optional extras

| Extra | Purpose |
| --- | --- |
| `[dev]` | "pytest, hypothesis, mypy, ruff" |
| `[browser]` | Playwright (optional engine) |
| `[audio]` | Vosk offline speech recognition |

```bash
pip install -e ".[browser]"
playwright install chromium   # only on supported OS (e.g. macOS 14+)
```

On any OS without Playwright, Shea uses `browser.local` only.
`playwright_available()` gates the engine — same source on every machine.

## Offline audio

Install the optional Vosk dependency and download an offline model from
[Vosk's model catalog](https://alphacephei.com/vosk/models):

```bash
pip install -e ".[audio]"
export SHEA_VOSK_MODEL=/path/to/vosk-model-small-en-us-0.15
export SHEA_WAKE_WORD="hey shea"
python -m shea listen
```

The model stays on disk and speech recognition runs locally. Microphone
capture uses `sounddevice`, installed with the audio extra, and stops after
silence; no separate SoX or FFmpeg executable is needed. The default speech
output uses `say` on macOS, Speech Dispatcher or eSpeak on Linux, and Windows
PowerShell System.Speech. On macOS, grant microphone access to the
terminal/application. `SHEA_WAKE_WORD` is detected in each locally transcribed
utterance; the phrase is removed before the remaining request is sent to Shea.

## Architecture

```text
CLI / InteractionService
  → Planning (templates and/or optional model)
  → Decision / Policy / Risk → content-bound Authorization
  → ExecutionService
       SecurityService.enforce
       authorization binding (plan/step/args/expiry/nonce)
       agent idempotency
       mint ExecutionPermit
  → ExecutionSupervisor (receipt before invoke)
  → ToolExecutorAdapter (verify permit) → ToolExecutor → tools
  → Verification (agent; may prefer app verification metadata)
```

**App / execution plane** (`shea.app`): already-authorized `ExecutionContract`
→ supervisor → adapters. Native adapters are **opt-in** via bootstrap flags.
Default agent side effects go through tools + permit, not by forging EP
contracts.

## Invariants

- PLAN ≠ AUTHORIZATION
- MODEL ≠ AUTHORITY
- Unauthorized tool requests must never reach handlers
- Receipt before adapter invoke
- Adapter/model output is evidence or proposal — never authority

## Strategy C (tools vs EP)

Agent plans use tools (`filesystem.*`, …). EP native adapters
(`filesystem.local`, `network.local`, …) are a separate supervised path for
scoped contracts. They coexist; one does not silently replace the other.

## Bootstrap and CLI

Composition root: `shea.bootstrap.build_runtime` → `SheaRuntime`.

| Flag | Default | Effect |
| --- | --- | --- |
| `allow_unsafe_execution` | `True` | Dev explicit unsafe boundary if none provided |
| `include_http_fetch` | `False` | Register `http.fetch` |
| `include_ep_process` | `False` | `process.local` |
| `include_ep_application` | `False` | OS application adapters |
| `include_ep_network` | `False` | `network.local` + browser adapters |
| `include_audio` | `False` | Register the native `audio.speak` tool |
| `register_demo_intents` | `True` | note write/read templates |
| `register_builtins` | `True` | filesystem tools |

Product text path: `runtime.interaction_service`.

Boot runs `reconcile_app_plane()` for stuck EP work.
`include_audio=True` optionally registers the native `audio.speak` tool.

```bash
python -m shea --db PATH --workspace DIR run "…"
```

| Env | Role |
| --- | --- |
| SHEA_FILESYSTEM_ROOTS | Allowed roots (pathsep-separated) |
| SHEA_VOSK_MODEL | Path to the extracted local Vosk model directory |
| SHEA_WAKE_WORD | Phrase that activates audio requests; removed before dispatch |
| SHEA_MODEL_PROVIDER | `puter` , `ollama` , `ghost` , `openai` |
| SHEA_PUTER_TOKEN / OPENAI_API_KEY / GHOST_* | Credentials |
| SHEA_MODEL_NAME / SHEA_MODEL_BASE_URL / SHEA_MODEL_TIMEOUT | Model settings |
| SHEA_LIVE_NETWORK | 1 for live network/browser tests |

## What's here (packages)

| Package | Responsibility |
| --- | --- |
| `shea.contracts` | Typed, framework-free data shapes: `Request`, `Intent`, `Task`, `Plan`, `PlanStep`, `Decision`, `RiskAssessment`, `Authorization`, `AuditEvent`, `ToolRequest`/`ToolResponse`, `ModelResponse`, `ToolExecutionRecord`, `VerificationRecord`, `RecoveryAttempt`. |
| `shea.ports` | Abstract interfaces (`TaskRepository`, `PlanRepository`, `IntentRepository`, `DecisionRepository`, `RiskAssessmentRepository`, `AuthorizationRepository`, `ToolExecutionRepository`, `VerificationRepository`, `RecoveryAttemptRepository`, `AuditSink`, `Clock`, `IdGenerator`, `ModelProvider`, `UnitOfWork`) — the hexagonal boundary. Nothing concrete lives here. |
| `shea.state_machine` | Authoritative transition table (Appendix A, plus `execution_unknown` and `step_verified`) and `next_state()` — the only function allowed to change task state. Illegal transitions raise `IllegalTransitionError`. |
| `shea.persistence.sqlite` | Concrete adapters implementing the ports above: connection handling, numbered SQL migrations through `0009`, repositories (including `SqliteVaultRepository` and `SqliteMemoryService`), and `audit_chain.py` (`verify_audit_chain` — walks `audit_events` and reports `content_altered`/`link_broken`/`sequence_gap` breaks). SQLite is the source of truth for task/plan state — not an in-memory cache with SQLite as backup. |
| `shea.config` | The six-layer configuration resolver (System → Machine → User → Profile → Project → Session), with `security_invariant_keys` that can only ever be set at the System layer regardless of what any other layer says. |
| `shea.core` | The `Orchestrator` — thin coordination of task lifecycle. Creates tasks, advances them via the state machine, attaches plans, persists, and audits every attempt (success *and* rejection). Each state write and its audit event commit or roll back together via a shared `UnitOfWork`, not as two independent commits. |
| `shea.model` | `ModelProvider` port (`generate()`/`health()`/`capabilities()`) and `ScriptedModelProvider` — a deterministic queued-response double. No real LLM API integration ships here; that's the Provider Routing phase's job. |
| `shea.understanding` | `DeterministicIntentMatcher` (pure) and `IntentParser` (pure) — research doc Section 6.2's hybrid: known commands matched deterministically, everything else falls back to the model, with `AmbiguousIntentError` for low-confidence output and `MalformedModelOutputError` for unparseable output. |
| `shea.planning` | `PlanTemplateRegistry` (pure), `validate_plan()` (pure — the "model suggested this" vs "Shea will act on this" boundary), `capabilities_for_plan()` (pure — bridges Planning to Decision), and `PlanningService` — the integration layer, sole caller of `start_planning`/`plan_ready`/`plan_failed`/`block`/`attach_plan`. |
| `shea.decision` | `PolicyEngine`, `RiskEngine`, confirmation-tier rules, and `DecisionService` — sole caller of `authorize_and_run`; issues content-bound `Authorization` records (plan/step/argument hashes, expiry, nonce) when a plan is present. |
| `shea.tools` | `ToolDeclaration` + `ToolRegistry` (capability profiles; optional `argument_schema`; elevated capabilities require a schema at register) and `ToolExecutor` (capability gate *before* handler lookup, schema validation when declared — defaults it computes are actually applied to the request via `dataclasses.replace`, not just checked and discarded — SUCCESS/FAILURE/UNKNOWN outcomes). `schema.py` is the pure validation engine; `provider.py`'s `ToolProvider` protocol + `load_tools()` let a registration bundle (see `shea.tools.builtin`) attach declarations/handlers/verifiers without ever executing or authorizing anything itself. |
| `shea.tools.builtin` | The first tools that touch the outside world: `filesystem.read`/`filesystem.write` and an opt-in `http.fetch`, registered via `register_builtin_tools()`. Not wired into any default fixture — a caller has to ask for these explicitly, and `http.fetch` needs `include_http_fetch=True` on top of that. |
| `shea.execution` | `ExecutionService` — looks up authorized capabilities from the persisted `Decision`, verifies authorization content-binding, requires `SecurityService`, enforces idempotency (SUCCESS/UNKNOWN suppress), runs one tool call through the supervised app boundary, persists `ToolExecutionRecord`, and advances by outcome. `PlanRunner` walks durable multi-step plans with per-step binding and resume-safe completion states. |
| `shea.verification` | `Verifier`/`VerifierRegistry` and `VerificationService` — sole caller of `verified` / `verification_failed` / `step_verified` (intermediate steps return to READY for the next authorization). Execution success does not force verification to agree. |
| `shea.recovery` | `Compensator` abstraction + `RecoveryService` — bounded Saga-style retry (`FAILED -> RECOVERING -> READY \| FAILED`), counted from persisted attempts, and `resolve_blocked()` for tasks Phase 3's `UNKNOWN` execution outcome routes to `BLOCKED`. `RetryController` is the single source of truth for the attempt budget and supplies the backoff delay persisted on each `RecoveryAttempt`. |
| `shea.security` | `CredentialBroker`/`CredentialService`, `SqliteVault`, `NetworkPolicy`/`FilesystemPolicy` (SSRF and path-scope protection, pure) plus `runtime_checks.py`'s `realpath_under_roots`/`resolve_and_check_url` (the filesystem/DNS re-checks a pure policy can't do — symlink resolution, live DNS resolution), `SecretRedactor` (pattern-based, recursive), `PromptInjectionDetector` (heuristic), `SecurityGate` (pure pre-execution request scanner), `binding.py` (pure content-hashing for `Authorization` — plan/step/argument hashes, nonce generation), `SecurityService` — the only caller of `Orchestrator.advance(task_id, "security_halt")`; its violation path (violation audit -> task halt -> transition audit) commits or rolls back as one transaction. Also `SandboxedExecutionBoundary` — the real "Sandbox" pipeline stage (timeout + redaction). |
| `shea.provider` | `ProviderProfile`/`ProviderTrustLevel` (LOCAL/TRUSTED_REMOTE/UNTRUSTED), `HealthTracker` (sliding-window health), `FailureCategory`/`classify_exception()`, `ProviderRouter` (pure eligibility + ranking), `ProviderRoutingService` — structurally satisfies `ModelProvider` itself, so it's a drop-in for `IntentParser`/`PlanningService`. |
| `shea.audit` | `AuditRecorder` — centralizes event ID / timestamp generation so no call site can emit a malformed audit event; optionally redacts metadata via an injected `Redactor`. `chain.py`'s `hash_audit_event` is the pure SHA-256 hashing function backing the tamper-evident chain (see `shea.persistence.sqlite`'s `verify_audit_chain` for the verification side) — storage-neutral on purpose, so any `AuditSink` implementation could use it. |
| `shea.adapters` | Production implementations of `Clock` and `IdGenerator` (real time, real UUIDs). Tests use fakes instead — see `tests/conftest.py`. |
| `shea.understanding.memory` | Memory extraction, context variables, and FTS-backed `SqliteMemoryService` for semantic task ranking. |
| `shea.audio` | Cross-platform text-to-speech adapters (`macOS`, `Windows`, `Linux`, and `stub`) integrating with the activation pipeline. |
| `shea.extensions` | Plugin manifest resolution, signature checking, and `RestrictedRuntimeProxy` for safe third-party tool execution. |
| `shea.observability` | Tracing abstractions and ContextVar-based propagation. Stamping logs/audit chains with `correlation_id` and `profile_id`. |
| `shea.api` | FastAPI interaction routes, server initialization, and `gui.py` lightweight HTML/JS visual client interface. |

## Execution plane adapters

| Adapter | Notes |
| --- | --- |
| `ToolExecutorAdapter` | Agent tools; requires valid `_execution_permit` |
| `process.local` | No shell; executable allowlist; session/rlimit best-effort |
| `network.local` | `GET/HEAD`; DNS pin; private block; redirects re-checked |
| `browser.local` | HTTP document navigate/read; any OS |
| `browser.playwright` | Full Chromium/Webkit/Firefox headless execution; opt-in. |
| `filesystem.local` | EP-native FS when registered |
| `application.macos` / `.windows` / `.linux` | Platform-gated; ApplicationScope allowlists |
| `application.stub` | Fail-closed fallback |

`evaluate_scope` fails closed on missing FS roots, empty process/app allowlists where required, and required cgroup (still **UNSUPPORTED**).

## Multi-step execution

Plans run through `PlanRunner`:

1. Per pending step (persisted step state; skip already `COMPLETED`)
2. `DecisionService.evaluate_and_authorize` (fresh nonce per step)
3. `ExecutionService.execute` (enforce, bind, permit, supervise)
4. `SecurityService.scan_output`
5. `VerificationService.verify` — intermediate steps `step_verified` → READY; last step `verified` → COMPLETED

Stops early on execution failure/unknown, verification failure, or flagged output.

## Optional model planning

If no template matches and a provider is configured:

```bash
export SHEA_MODEL_PROVIDER=
export SHEA_PUTER_TOKEN=…
# or compatible env names
python -m shea run "something without a demo template"
```

| Case | Result |
| --- | --- |
| Template match | No model call |
| No template + no provider | Plan fails |
| No template + provider | JSON steps → validate tools → READY |
| Bad JSON / HTTP error | Plan failed path |

The model does not authorize or execute.

## Security (summary)

- Required `SecurityService` on execute
- Content-bound auth (plan/step/args, expiry, nonce, `used_at` after durable SUCCESS)
- Elevated capabilities require argument schemas at register
- Process-local **ExecutionPermit** on `tool_executor` contracts
- Idempotency suppresses SUCCESS/UNKNOWN duplicates
- Audit hash chain detects mid-chain tampering (tip truncation needs external anchor)
- Runtime realpath / DNS checks beyond pure string policy

`allow_unsafe_execution` defaults to `False`. The runtime injects a `SandboxedExecutionBoundary` by default to constrain resource allocation (`max_output_bytes`), time bounding, and data redaction.

## Tests

```bash
pytest -q
SHEA_LIVE_NETWORK=1 pytest tests/unit/test_app_browser_live.py -q
```

Unit + property tests under `tests/`.

Includes an intense `test_fault_injection.py` chaos-monkey suite to verify system stability under ungraceful shutdown, LLM timeouts, and SQLite locks, ensuring Universal Reconciliation cleans up stranded state.

## Layout

```text
src/shea/
├── contracts/          # pure data shapes
├── ports/              # hexagonal interfaces (incl. UnitOfWork)
├── state_machine/      # TRANSITIONS + next_state()
├── persistence/sqlite/ # migrations + repositories + audit_chain verification
├── config/             # six-layer resolver
├── core/               # Orchestrator
├── model/              # ModelProvider + ScriptedModelProvider
├── understanding/      # DeterministicIntentMatcher + IntentParser
├── planning/           # templates, validate_plan, PlanningService
├── decision/           # Policy, Risk, DecisionService
├── tools/              # Registry, Executor, schemas, boundary, provider protocol
│   └── builtin/        # filesystem.read/write, http.fetch
├── execution/          # ExecutionService, PlanRunner
├── verification/       # Verifier registry + VerificationService
├── recovery/           # Retry, Idempotency, RecoveryService
├── security/           # Gate, policies, runtime_checks, binding, SecurityService, Vault
├── provider/           # Routing + failover
├── audit/              # AuditRecorder + chain hashing
├── app/                # execution plane: supervisor, adapters, evidence, reconcile, interaction
├── api/                # FastAPI router, GUI interface
├── audio/              # cross-platform TTS adapters
├── extensions/         # Plugin manifest and RestrictedRuntimeProxy
├── observability/      # Tracing and contextual correlation
├── profiles/           # User configuration profiles
├── bootstrap.py        # composition root (wires agent + app plane)
├── __main__.py         # thin CLI
└── adapters/            # concrete Clock / IdGenerator

tests/
├── unit/
└── property/
pyproject.toml
```

The V1 composition root registers the built-in filesystem tools and wires
`PlanningService` with deterministic intent matching and plan templates.
Model-provider routing remains opt-in: pass a configured `ModelProvider` to
`build_runtime()` when model fallback is available. The system is securely locked down by default via `SandboxedExecutionBoundary`, bounding all actions by time and memory. production callers
must provide a real boundary and configure `SHEA_FILESYSTEM_ROOTS` or pass
`filesystem_roots` explicitly.

## Develop

```bash
pip install -e ".[dev]"
pytest -q
mypy --strict
ruff check .
```

Design intent lives in the research / technical / execution-plane documents;
this README describes what the repository implements now.

## Licence

This is licenced under MIT
