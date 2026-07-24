# CLI-Emulated Providers: Codex CLI, Claude Code CLI, and Cowork as First-Class Hermes Providers

**Date:** 2026-07-24
**Status:** Approved design, pending implementation
**Author:** Brainstormed by Ole + Claude Code

## Context

Ole currently delegates work to the Codex CLI and Claude Code CLI manually, outside
Hermes. This design makes both CLIs — plus Cowork — appear inside Hermes as fully
functional providers: selectable with `/model`, usable as MoA (Mixture of Agents)
reference or aggregator slots, indistinguishable from API providers to the rest of
the system.

The fork already contains the exact pattern to copy: the `copilot-acp` provider
(`plugins/model-providers/copilot-acp/` + `agent/copilot_acp_client.py`) wraps the
GitHub Copilot CLI behind an OpenAI-client-shaped shim, selected in
`create_openai_client` (`agent/agent_runtime_helpers.py`) via `auth_type="external_process"`
and a sentinel `acp://` base URL.

## Decisions (agreed during brainstorming)

1. **Advisor mode** for the two CLI providers: text in, text out. No tools, no file
   edits. Hermes keeps its own agent loop.
2. **Local Hermes only.** The providers work where the CLIs are installed and
   authenticated (Ole's Mac). Docker-deployed instances lack the binaries; the
   providers register but calls fail with a clear message there.
3. **Add-only.** No existing delegation mechanism (`delegate_task`, codex
   app-server, copilot-acp) is removed or changed.
4. **Cowork:** keeps its existing MCP tool connection unchanged, **and** gains a
   third provider shim so it is also callable as an emulated API model. Its
   contract differs (plugin-capable, side effects possible — see below).

## Goals

- `/model` lists three new providers: `codex-cli`, `claude-code`, `cowork`.
- A direct chat turn against each returns a real answer.
- A MoA preset can use any of them as reference or aggregator slots with zero MoA
  changes, e.g.:

  ```yaml
  reference_models:
    - { provider: codex-cli, model: gpt-5.5-codex }   # any id from the codex-cli catalog
    - { provider: claude-code, model: opus }
  ```

## Non-Goals

- Full main-model tool-calling emulation (Hermes tool calls round-tripping through
  a CLI). Explicitly out of scope; possible future project.
- Docker/remote support and credential plumbing.
- Streaming token-by-token output (responses return whole, like the Copilot shim).
- Auto-routing / task-based provider selection (MoA presets remain user-declared).

## Architecture

### New provider plugins (declarative layer)

Three plugin dirs under `plugins/model-providers/`, each registering a
`ProviderProfile`:

| Provider name | Sentinel base_url | auth_type | Backend |
|---|---|---|---|
| `codex-cli` | `cli://codex` | `external_process` | `codex exec` subprocess |
| `claude-code` | `cli://claude-code` | `external_process` | `claude -p` subprocess |
| `cowork` | `cli://cowork` | `external_process` | Cowork run via the existing cowork-mcp server |

- `api_mode="chat_completions"` for all three (they masquerade as OpenAI clients).
- `supports_vision=False` initially.
- Name `codex-cli` deliberately avoids colliding with the existing `openai-codex`
  API provider.

### Model catalogs

- `claude-code`: the CLI's model aliases (`opus`, `sonnet`, `haiku`), passed as
  `--model`. Default: the CLI's own default (omit the flag).
- `codex-cli`: the model identifiers the installed Codex CLI accepts for `-m`/
  `--model`, enumerated from the CLI's documentation at implementation time.
  Default: the CLI's configured default (omit the flag).
- `cowork`: a single logical model `default` (Cowork controls its own model).

### Shim clients (transport layer)

One new module, `agent/cli_provider_client.py`, mirroring
`agent/copilot_acp_client.py`: classes exposing `.chat.completions.create(**kwargs)`
and returning OpenAI-shaped response objects with usage where reported.

- `CodexCLIClient` — spawns `codex exec` per call: `--model` when a model is
  selected, JSON/last-message output, **read-only sandbox**, empty scratch working
  directory.
- `ClaudeCodeCLIClient` — spawns `claude -p` per call: `--model` when selected,
  JSON output format, **all tools disabled**, system prompt via the CLI's
  system-prompt flag, empty scratch working directory.
- `CoworkClient` — drives a Cowork run through the cowork-mcp server using Hermes'
  existing MCP client machinery: submit the flattened conversation, await
  completion, return the final text.

Common behavior (shared helper):

- **Message flattening:** system prompt separated out; remaining history rendered
  into a single self-contained prompt per call. No CLI-side session state between
  calls (Hermes always sends full history).
- **Environment sanitization** via the existing `hermes_subprocess_env()` helper
  (same as the codex app-server path) so Hermes secrets do not leak into
  subprocesses.
- **Timeouts:** default 300 s for `codex-cli`/`claude-code`, 600 s for `cowork`;
  all overridable via the existing per-provider `request_timeout_seconds` config.
  On expiry the subprocess and its children are killed and the call fails.

### Wiring

One branch in `create_openai_client` (`agent/agent_runtime_helpers.py`), keyed on
the `cli://` base-url prefix (analogous to the existing `acp://copilot` branch),
returning the matching shim client.

## Provider contracts

- `codex-cli` and `claude-code` are **pure advisors**: no tools, no side effects,
  read-only sandbox, empty working directory. Safe as default MoA advisors.
- `cowork` is **plugin-capable — the one provider whose calls may have side
  effects.** That is its purpose: answers informed by Cowork-only plugins. The
  shim prepends an advisory instruction to every request ("use your plugins to
  research and answer; do not create, modify, or send anything"). This is a soft
  guardrail, not a hard sandbox, and the docs must say so. Use deliberately; do
  not put `cowork` in every default MoA preset.
- All three run on subscription capacity and CLI pacing: slower than API
  providers (process startup for the CLIs; full agentic runs for Cowork) and
  subject to plan usage limits.

## Data flow (one call)

1. Hermes makes a normal model call (chat turn or MoA slot) and reaches the shim
   via `create_openai_client`.
2. Shim flattens messages, launches the backend (subprocess or Cowork run) with
   the selected model, sanitized environment, and timeout guard.
3. Shim parses the backend's structured output, extracts answer text and token
   usage where available.
4. Shim returns an OpenAI-shaped response object; Hermes cannot tell it from a
   real API reply.

MoA fan-out (up to 8 parallel advisors) needs no special handling: each call is
its own subprocess/run. No pooling or queuing — subscription limits throttle
harder than process spawns do.

## Error handling

Only failures that can really occur at this boundary; no speculative layers:

- **Binary missing / not on PATH** (or cowork-mcp unavailable): immediate error
  naming the binary and an install/login hint. No retry.
- **Not authenticated / usage limit reached:** captured stderr (or MCP error)
  trimmed and surfaced so the cause is visible.
- **Timeout:** process tree killed; clean timeout error. No automatic retry — MoA
  already tolerates a failed advisor; in direct chat the user should see the
  error.
- **Malformed output:** error including the leading portion of raw output for
  diagnosis.

## Testing

1. **Unit tests with fake backends:** stub executables mimicking `claude -p` /
   `codex exec` JSON output, and a stubbed MCP interaction for Cowork. Cover
   message flattening, model flag pass-through, response shaping, usage
   extraction, timeout kill, stderr surfacing, missing-binary error. Runs in CI
   with no real CLIs or subscriptions.
2. **Gated integration smoke tests:** run only when the real CLI/Cowork is
   present and authenticated (skipped in CI). One real advisory call per
   provider asserting a non-empty answer.
3. **Manual acceptance (feature success criteria):**
   - `/model` shows all three providers; a direct chat answer returns from each.
   - A MoA preset with a `codex-cli` advisor and a `claude-code` advisor
     completes with both advisors contributing.

## Risks and caveats

- **Latency:** seconds of process startup per CLI call; minutes possible for
  Cowork runs. Acceptable for advisory use; noticeable as a main chat model.
- **Cowork side effects:** soft guardrail only (see Provider contracts).
- **CLI output formats can change** with CLI updates; the fake-backend unit tests
  pin the expected shapes so breakage is caught explicitly.
- **Plan limits:** heavy MoA presets can exhaust subscription quotas; errors
  surface the CLI's own limit message.
