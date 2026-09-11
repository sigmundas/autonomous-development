# Autonomous-development configuration contract

This document defines the JSON contract the controller exposes for
programmatic clients — primarily the [autonomous-development-vsplugin][vsx]
VS Code extension, but usable by any tool. The controller is authoritative:
clients read/write the autonomous configuration exclusively through the
`config-*` subcommands documented here rather than parsing or rewriting the
TOML file directly.

[vsx]: https://github.com/quaat/autonomous-development-vsplugin

The controller owns:

- the config schema and its versioning;
- validation (structure, phase names, reasoning effort, workflow modes,
  secret-shaped-key rejection);
- Codex profile discovery under `$CODEX_HOME` (or `~/.codex`);
- effective-value resolution (defaults → config → env → CLI);
- persistence (atomic write, temp-then-rename);
- run-state snapshots that pin an active run to its init-time decisions.

The VS Code extension owns:

- pre-run interaction and configuration UI;
- dropdown population from `config-list-profiles` / `config-list-presets` /
  `config-list-claude-runtimes`;
- displaying effective values from `config-show`;
- invoking `config-set-*` to persist changes;
- launching the selected Claude runtime through a safe argument array.

Neither side depends on private implementation details of the other.

## Invocation

Every command below is invoked as:

```
controller.py [--project-root PATH] [--state-dir PATH] [--config-path PATH] <config-*> [args]
```

All responses are UTF-8 JSON written to stdout. Non-zero exit codes signal
failure and the reason is either on stderr (as `error: <message>`) or
inside a structured JSON payload — the individual command sections below
call out which. Config subcommands do not require an active run and do not
touch `run-state.json`.

## Precedence

The precedence rules have two distinct scopes:

### Init-time resolution (`controller.py init`)

Precedence for per-phase Codex configuration when building the run's
snapshot, highest to lowest:

1. environment-variable overrides
   (`CLAUDE_AUTONOMOUS_PHASE_PROFILES` JSON,
    `CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>`);
2. selected preset (via `--preset NAME` or `active_preset`);
3. built-in controller defaults.

Precedence for the workflow rigor mode:

1. explicit `--mode` on the CLI (`mode_origin = "cli"`);
2. selected preset's `workflow_mode` (`mode_origin = "preset"`);
3. config-file top-level `[workflow].workflow_mode`
   (`mode_origin = "config"`);
4. built-in default `auto` (`mode_origin = "default"`), which retains
   the existing risk-escalation semantics.

Explicit `--mode rigorous` (or an escalated `auto`) still requires an
adversarial review to complete, as before.

### Post-init resolution (`controller.py codex --phase …`)

The `config_snapshot` stored inside `run-state.json` at init is the
SOLE source of per-phase Codex configuration for the lifetime of the
run. Environment-variable overrides captured at init are ALREADY baked
into the snapshot; they are NOT reapplied on each phase invocation.
Changing `CLAUDE_AUTONOMOUS_PHASE_PROFILES` or
`CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>` after init has no effect on an
active run.

Legacy runs (initialized before this feature and therefore lacking
`config_snapshot`) preserve the historical env-var fallback behavior
for backward compatibility.

## Configuration file

Default location:

- `$CLAUDE_AUTONOMOUS_STATE_HOME/config.toml`, else
- macOS: `~/Library/Application Support/claude-autonomous/config.toml`
- Linux (XDG): `$XDG_STATE_HOME/claude-autonomous/config.toml` else
  `~/.local/state/claude-autonomous/config.toml`
- Windows: `%LOCALAPPDATA%\claude-autonomous\config.toml`

Explicit overrides: `--state-dir DIR` (moves the whole state root) or
`--config-path PATH` (only the config file).

Shape (all fields optional except `version`):

```toml
version = 1
active_preset = "azure-autonomous"

[workflow]
max_review_rounds = 3
process_timeout_seconds = 3600
workflow_mode = "standard"           # optional default; presets override
reuse_codex_review_context = false    # snapshotted; legacy/default is fresh reviews
codex_review_session_max_turns = 3    # bounded reviewer-session rotation
executable_search_paths = ["/opt/homebrew/bin"] # optional run-check PATH additions

[presets.azure-autonomous]
workflow_mode = "standard"           # auto | lean | standard | rigorous
claude_runtime = "azure-claude"      # name of a [claude_runtimes.*] entry
claude_model = "sonnet"               # optional [claude_models.*] entry

[presets.azure-autonomous.codex.enhance]
profile = "azure-gpt5p6-sol"         # Codex profile id
reasoning_effort = "medium"          # minimal | low | medium | high | xhigh

[presets.azure-autonomous.codex.plan]
profile = "azure-gpt5p6-sol"
reasoning_effort = "high"

[presets.azure-autonomous.codex.review]
profile = "azure-gpt5p6-sol"
reasoning_effort = "xhigh"

[presets.azure-autonomous.codex.adversarial]
profile = "azure-gpt5p6-sol"
reasoning_effort = "xhigh"

[claude_runtimes.anthropic-claude]
display_name = "Anthropic · Claude"
launcher = "/Users/example/bin/claude-anthropic"

[claude_runtimes.azure-claude]
display_name = "Azure · Claude"
launcher = "/Users/example/bin/claude-azure"
allowed_commands = ["ruff", "npm run test"]
executable_paths = ["/opt/homebrew/bin", "~/.local/bin"]

[claude_models.fable]
display_name = "Fable"
model = "fable"                       # exact value passed to claude --model

[claude_models.opus]
display_name = "Opus"
model = "opus"

[claude_models.sonnet]
display_name = "Sonnet"
model = "sonnet"

[claude_models.haiku]
display_name = "Haiku"
model = "haiku"
```

These are examples, not a built-in catalog. Define the stable ids, labels, and
exact `claude --model` values supported by your Claude Code account/provider.
The runtime selects the launcher environment; the model independently selects
the Claude session model. Omit `claude_model` from a preset for Default behavior
with no explicit `--model` argument.

The recommended catalog uses Claude Code's family aliases (`fable`, `opus`,
`sonnet`, `haiku`) as both the id and the `model` value, so the front-facing
names stay provider-neutral. Claude Code resolves an alias to a concrete model
per provider: on the first-party API it picks the latest model of that family,
and on Microsoft Foundry, Bedrock, or Vertex it reads the deployment name from
`ANTHROPIC_DEFAULT_FABLE_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`,
`ANTHROPIC_DEFAULT_SONNET_MODEL`, and `ANTHROPIC_DEFAULT_HAIKU_MODEL`. Put that
translation in the provider-specific launcher's environment (the script named
by `claude_runtimes.<name>.launcher`), not in this file: the controller never
maps model names itself and passes `model` through verbatim.

Constraints enforced at validation time:

- `version` must be `1` (only supported version).
- Phase names in `presets.*.codex.*` must be one of
  `enhance | plan | review | adversarial`.
- `reasoning_effort` values must be one of
  `minimal | low | medium | high | xhigh`.
- `workflow_mode` must be one of `auto | lean | standard | rigorous`.
- Reviewer-context reuse is opt-in and affects new runs only. Regular and
  adversarial reviewers use independent session families; planning and
  enhancement remain fresh. Unsupported resume safely falls back to fresh.
- `active_preset`, when set, must name a defined preset.
- A preset's `claude_runtime`, when set, must name a defined runtime.
- Runtime `allowed_commands` entries are simple executable/subcommand prefixes.
  Shell syntax, shell launchers, and destructive or unbounded Git commands are rejected.
- Runtime `executable_paths` entries are prepended to the inherited Claude/controller
  PATH. They provide predictable direct executable lookup without a login-shell wrapper.
  The VS Code launcher also prepends the directory containing an absolute Claude launcher.
- A preset's optional `claude_model` should name a defined model. Omitting it is
  the Default selection and does not pass `--model` to Claude Code. A reference
  to an undefined model (for example after renaming a `[claude_models.*]` entry
  by hand) is a validation *warning*, not an error: the file stays loadable and
  every `config-*` command keeps working so the reference can be repaired with
  `config-set-claude-model`. Until then `config-show` reports `claude_model: null`
  (Default), and `controller.py init` refuses to start a run with the dangling
  reference rather than silently dropping `--model`.
- Claude model definitions are user-extensible; no provider catalog is
  hardcoded. The exact CLI value is persisted into each run snapshot.
- New runs snapshot the selected model id, display name, and exact CLI value;
  later global changes cannot alter resume or fresh-session rollover behavior.
- Secret-shaped keys (`api_key`, `token`, `bearer`, `password`,
  `credential(s)`, `authorization`) are refused at any depth. The
  autonomous config never stores credentials.
- Unknown top-level or nested keys produce warnings but do not fail.

## Commands

### `config-show`

Print the effective configuration and origin metadata.

Response:

```json
{
  "config_path": "/…/config.toml",
  "config_exists": true,
  "active_preset": "azure-autonomous",
  "effective": {
    "workflow": { "max_review_rounds": 3, "process_timeout_seconds": 3600,
                  "workflow_mode": "standard" },
    "codex": {
      "plan":   { "profile": "azure-gpt5p6-sol", "reasoning_effort": "high" },
      "review": { "profile": "azure-gpt5p6-sol", "reasoning_effort": "xhigh" }
    },
    "claude_runtime": "azure-claude"
  },
  "presets": ["azure-autonomous", "openai-anthropic"],
  "claude_runtimes": ["azure-claude", "anthropic-claude"],
  "warnings": []
}
```

Exit code 0 on success. Environment-variable overrides are **not** baked
into `effective` — they apply per-invocation only, so `config-show` remains
stable and representative of the on-disk configuration.

### `config-validate`

Response on success:

```json
{
  "config_path": "/…/config.toml",
  "config_exists": true,
  "valid": true,
  "warnings": ["Unknown key in preset 'p': 'foo' (ignored)."]
}
```

Response on failure (exit code 1, JSON on stdout — not stderr):

```json
{
  "config_path": "/…/config.toml",
  "config_exists": true,
  "valid": false,
  "error": "presets.p.codex.plan.reasoning_effort 'warp-9' is invalid; …",
  "warnings": []
}
```

### `config-list-profiles`

Discover Codex profiles under `$CODEX_HOME` (else `~/.codex`).

```json
{
  "codex_home": "/Users/…/.codex",
  "profiles": [
    {
      "id": "azure-gpt5p6-sol",
      "label": "Azure · gpt-5.6-sol",
      "provider": "azure",
      "model": "gpt-5.6-sol",
      "path": "/Users/…/.codex/azure-gpt5p6-sol.config.toml",
      "valid": true
    },
    {
      "id": "broken",
      "path": "/Users/…/.codex/broken.config.toml",
      "valid": false,
      "error": "Invalid statement (at line 3, column 6)"
    }
  ]
}
```

The base `config.toml` under the Codex home is intentionally excluded.
Malformed profiles never crash the listing — they appear with
`valid: false` and an `error` string. Descriptors never carry API keys or
other secrets, even if the source profile file contains them: only `id`,
`label`, `provider`, `model`, `path`, and validity are returned.

### `config-list-presets`

```json
{
  "config_path": "/…/config.toml",
  "active_preset": "azure-autonomous",
  "presets": [
    {
      "name": "azure-autonomous",
      "workflow_mode": "standard",
      "claude_runtime": "azure-claude",
      "claude_model": "sonnet",
      "phases": ["enhance", "plan", "review", "adversarial"]
    }
  ]
}
```

### `config-list-claude-runtimes`

```json
{
  "config_path": "/…/config.toml",
  "claude_runtimes": [
    {
      "name": "azure-claude",
      "display_name": "Azure · Claude",
      "launcher": "/Users/…/bin/claude-azure",
      "args": [],
      "allowed_commands": ["ruff"],
      "executable_paths": ["/opt/homebrew/bin"],
      "launcher_exists": true,
      "launcher_executable": true
    }
  ]
}
```

`launcher_exists` / `launcher_executable` are best-effort filesystem
checks; the controller never invokes the launcher during validation.

### `config-list-claude-models`

Returns the user-defined stable ids, display names, and exact Claude CLI model
values. The list is configuration-driven and is not restricted to a built-in
catalog.

### `config-set-claude-model [NAME]`

Sets the active preset's `claude_model` reference. Omitting `NAME` selects
Default by removing the reference, so new launches do not pass `--model`.

### `config-set-active-preset NAME`

```json
{
  "config_path": "/…/config.toml",
  "active_preset": "azure-autonomous"
}
```

Fails when `NAME` is not a defined preset.

### `config-set-phase --preset P --phase PHASE [--profile ID] [--model M] [--reasoning-effort E] [--reasoning-summary S] [--verbosity V]`

Response:

```json
{
  "config_path": "/…/config.toml",
  "preset": "azure-autonomous",
  "phase": "plan",
  "effective": {
    "profile": "azure-gpt5p6-sol",
    "reasoning_effort": "high"
  }
}
```

Any of the scalar flags may be omitted; omitted flags leave the existing
value unchanged. Fields are validated before persistence:

- `--phase` ∈ {enhance, plan, review, adversarial}
- `--reasoning-effort` ∈ {minimal, low, medium, high, xhigh}
- `--profile`, `--model`, `--reasoning-summary`, `--verbosity` must be
  non-empty strings when supplied.

The command creates the referenced preset if it doesn't yet exist. Unrelated
presets and unrelated phases are preserved by-value.

### `config-set-claude-runtime NAME`

Sets `claude_runtime = "NAME"` on the currently-active preset. Requires an
active preset to have been selected. `NAME` must be defined in
`[claude_runtimes.*]`.

```json
{
  "config_path": "/…/config.toml",
  "active_preset": "azure-autonomous",
  "claude_runtime": "azure-claude"
}
```

## Init & run snapshot

`controller.py init` accepts an optional `--preset NAME` to override the
config's `active_preset` for the resulting run. At init time the controller
resolves the effective preset, validates that every referenced Codex
profile exists and is well-formed under the effective Codex home, and
snapshots the result into the new run's `run-state.json`:

```json
{
  "schema_version": 2,
  "run_id": "20260806T091439Z-ab08221b",
  "preset": "azure-autonomous",
  "requested_mode": "standard",
  "effective_mode": "standard",
  "mode_origin": "preset",
  "mode_reasons": [
    "origin=preset: explicit mode requested: standard"
  ],
  "config_snapshot": {
    "preset": "azure-autonomous",
    "workflow": { "max_review_rounds": 3, "process_timeout_seconds": 3600,
                  "workflow_mode": "standard" },
    "codex": {
      "plan":   { "profile": "azure-gpt5p6-sol", "reasoning_effort": "high",
                  "model": "gpt-init-time" }
    },
    "claude_runtime": "azure-claude"
  }
  /* … other run fields … */
}
```

Rules that follow from the snapshot:

- An active run continues to execute with its snapshotted configuration
  even after `config-set-active-preset` changes the global default.
- Environment-variable overrides in effect at init time ARE baked into the
  snapshot (per phase). Changing them later has no effect on an active
  run.
- Existing (pre-feature) runs have no `config_snapshot`. For them the
  controller falls back to the historical behavior: built-in phase
  defaults with `CLAUDE_AUTONOMOUS_PHASE_PROFILES` /
  `CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>` applied on every `cmd_codex`
  invocation. This is intentionally opt-out — creating any new run under a
  config file switches on the snapshot-pinned semantics.
- A phase configuration that references a Codex profile which is
  missing OR malformed under the effective `CODEX_HOME` (or `~/.codex`)
  is a hard init failure, regardless of whether the preset was chosen
  via `--preset` or via `active_preset`. An omitted profile is valid
  and means "let Codex resolve the model normally".

Never snapshotted: API keys, bearer tokens, or any other credential-shaped
value. The controller rejects such keys at validation time and the
snapshot is a subset of the validated effective configuration.

## Compatibility

Existing environment variables continue to be honored:

- `CLAUDE_AUTONOMOUS_STATE_HOME` — state root selection (unchanged);
- `CLAUDE_AUTONOMOUS_PROCESS_TIMEOUT` — subprocess timeout (unchanged);
- `CLAUDE_AUTONOMOUS_PHASE_PROFILES` — JSON per-phase profile overrides;
- `CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>` — per-phase model override.

The two Codex profile env vars now participate in configuration only at
init time and on legacy runs. For runs that were initialized with a
`config_snapshot`, changing them later has no effect — the snapshot is
authoritative. Legacy runs (no snapshot) preserve the historical
per-invocation env-var behavior.

Adding `CODEX_HOME` is the only new environment variable consulted; it
selects the Codex home for profile discovery.

The controller never writes to `~/.codex/config.toml` or any
`~/.codex/*.config.toml`. Selecting a profile passes `--profile <id>` to
`codex exec`; Codex resolves the id itself.

## Cross-repository responsibilities

The VS Code extension consumes this contract to implement:

- pre-run configuration UI that is available before any run exists;
- QuickPick / dropdown lists of profiles, presets, and Claude runtimes
  populated from `config-list-*`;
- start-run flow that reviews the effective preset, displays the resolved
  Codex/Claude selections, then invokes `controller.py init` with the
  chosen `--preset`;
- a “Launch Claude for Selected Preset” action that resolves the
  configured launcher via `config-list-claude-runtimes` and spawns it with
  a safe argument array (no shell interpolation).

The extension MUST NOT parse or rewrite the TOML file itself when the
controller is available. Observer-only fallback — when the controller path
is unset — may open the raw config file for editing but must not invent a
second configuration format.

## Change management

- The `version` field is the contract version. Bumping `version` is a
  breaking change and must be coordinated with the extension.
- New optional fields may be added to any command response without a
  version bump; clients should tolerate unknown fields.
- Removing or renaming fields, or changing semantics, requires a version
  bump.
