"""User configuration for the autonomous-development controller.

This module owns:

* Locating ``<state-home>/config.toml`` (with an explicit override for tests
  and advanced users).
* Parsing and validating the versioned TOML configuration.
* Merging built-in defaults, config values, environment overrides, and
  explicit CLI overrides into an effective, redacted configuration suitable
  for display and run-state persistence.
* Discovering named Codex profiles under ``$CODEX_HOME`` / ``~/.codex``.
* Atomically writing configuration updates.

The configuration NEVER stores API keys, bearer tokens, or embedded
credentials. Codex provider details remain owned by Codex configuration
files under the effective Codex home; this file references those profiles
by identifier only.

Precedence (highest wins) when resolving an effective per-phase profile:

1. explicit command-line overrides (handled by the caller);
2. existing environment-variable overrides
   (``CLAUDE_AUTONOMOUS_PHASE_PROFILES`` JSON,
    ``CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>``);
3. selected autonomous preset;
4. autonomous user configuration defaults;
5. built-in controller defaults (``PHASE_PROFILES`` in ``controller.py``).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tomllib
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


CONFIG_FILE_NAME = "config.toml"
SUPPORTED_CONFIG_VERSIONS = (1,)

VALID_PHASES: tuple[str, ...] = ("enhance", "plan", "review", "adversarial")
VALID_REASONING: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")
VALID_WORKFLOW_MODES: tuple[str, ...] = ("auto", "lean", "standard", "rigorous")

# Keys that must never survive into a saved or displayed configuration. The
# controller intentionally does not own credentials; if any of these appear we
# reject them at validation time and redact defensively before display.
SECRET_LIKE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(^|_)(api[_-]?key|apikey)$"),
    re.compile(r"(?i)(^|_)(secret|token|bearer|password|passwd)$"),
    re.compile(r"(?i)(^|_)(credential|credentials)$"),
    re.compile(r"(?i)authorization$"),
)


class ConfigError(Exception):
    """Raised on unrecoverable configuration problems."""


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_config_path(
    state_home: Path, config_path_arg: str | None = None
) -> Path:
    """Return the effective config-file path.

    Precedence: explicit CLI/tests override > ``<state_home>/config.toml``.
    """
    if config_path_arg:
        return Path(config_path_arg).expanduser().resolve()
    return state_home / CONFIG_FILE_NAME


def resolve_codex_home() -> Path:
    """Return the effective Codex home (``$CODEX_HOME`` else ``~/.codex``)."""
    env_val = os.environ.get("CODEX_HOME", "").strip()
    if env_val:
        return Path(env_val).expanduser().resolve()
    return Path.home() / ".codex"


# ---------------------------------------------------------------------------
# Loading & saving
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict[str, Any]:
    """Load and parse the config TOML. Returns ``{}`` when the file is absent.

    Raises :class:`ConfigError` when the file exists but cannot be parsed.
    """
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"Cannot read config at {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in config at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config at {path} must be a TOML table")
    return data


def save_config(path: Path, config: dict[str, Any]) -> None:
    """Atomically write ``config`` as TOML to ``path``.

    Writes to an invocation-unique temporary file next to the target and then
    renames into place, mirroring :func:`state.atomic_write_json`.
    """
    # Validate before writing so we cannot persist an unusable file.
    validate_config(config)
    _reject_secrets(config, context="save")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = _emit_toml(config)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def default_config() -> dict[str, Any]:
    """Return the built-in default configuration (a fresh, mutable dict).

    Deliberately minimal: presets and Claude runtimes are user-supplied. The
    controller falls back to its baked-in phase profiles when no preset is
    active.
    """
    return {
        "version": 1,
        # `active_preset` is intentionally omitted; TOML has no null and an
        # unset preset means "use built-in phase defaults". Callers set this
        # via ``set_active_preset``.
        "workflow": {
            "max_review_rounds": 3,
            "process_timeout_seconds": 3600,
            # Opt-in for new runs.  Missing/legacy snapshots retain the
            # historical fresh-review behavior.
            "reuse_codex_review_context": False,
            "codex_review_session_max_turns": 3,
            "executable_search_paths": [],
        },
        "presets": {},
        "claude_runtimes": {},
        "claude_models": {},
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_config(config: dict[str, Any]) -> list[str]:
    """Validate a loaded configuration. Returns a list of non-fatal warnings.

    Raises :class:`ConfigError` on unrecoverable problems (unknown version,
    invalid phase, invalid reasoning effort, malformed structural types,
    reference to a preset/profile/runtime that cannot exist under the current
    schema). Unknown keys at the top level produce warnings.
    """
    if not isinstance(config, dict):
        raise ConfigError("Config must be a TOML table (dict).")

    version = config.get("version", 1)
    if version not in SUPPORTED_CONFIG_VERSIONS:
        raise ConfigError(
            f"Unsupported config version {version!r}. "
            f"Supported versions: {SUPPORTED_CONFIG_VERSIONS!r}."
        )

    warnings: list[str] = []

    _reject_secrets(config, context="validate")

    known_top = {
        "version",
        "active_preset",
        "workflow",
        "presets",
        "claude_runtimes",
        "claude_models",
    }
    for key in config:
        if key not in known_top:
            warnings.append(f"Unknown top-level key ignored: {key!r}")

    workflow = config.get("workflow", {})
    if not isinstance(workflow, dict):
        raise ConfigError("`workflow` must be a table.")
    if "max_review_rounds" in workflow:
        value = workflow["max_review_rounds"]
        if not isinstance(value, int) or value < 1 or value > 20:
            raise ConfigError(
                f"workflow.max_review_rounds must be an integer in [1, 20]; got {value!r}."
            )
    if "process_timeout_seconds" in workflow:
        value = workflow["process_timeout_seconds"]
        if not isinstance(value, int) or value < 1:
            raise ConfigError(
                f"workflow.process_timeout_seconds must be a positive integer; got {value!r}."
            )
    if "workflow_mode" in workflow:
        mode = workflow["workflow_mode"]
        if mode not in VALID_WORKFLOW_MODES:
            raise ConfigError(
                f"workflow.workflow_mode {mode!r} is invalid; "
                f"expected one of {VALID_WORKFLOW_MODES!r}."
            )
    if "reuse_codex_review_context" in workflow and not isinstance(
        workflow["reuse_codex_review_context"], bool
    ):
        raise ConfigError("workflow.reuse_codex_review_context must be true or false.")
    if "codex_review_session_max_turns" in workflow:
        value = workflow["codex_review_session_max_turns"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 2 or value > 10:
            raise ConfigError(
                "workflow.codex_review_session_max_turns must be an integer in [2, 10]."
            )
    if "executable_search_paths" in workflow:
        value = workflow["executable_search_paths"]
        if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
            raise ConfigError("workflow.executable_search_paths must be an array of non-empty paths.")
    known_workflow = {
        "max_review_rounds", "process_timeout_seconds", "workflow_mode",
        "reuse_codex_review_context", "codex_review_session_max_turns",
        "executable_search_paths",
    }
    for key in workflow:
        if key not in known_workflow:
            warnings.append(f"Unknown key in [workflow]: {key!r} (ignored).")

    presets = config.get("presets", {})
    if not isinstance(presets, dict):
        raise ConfigError("`presets` must be a table.")
    for name, preset in presets.items():
        _validate_preset_name(name)
        if not isinstance(preset, dict):
            raise ConfigError(f"Preset {name!r} must be a table.")
        _validate_preset(name, preset, warnings)

    runtimes = config.get("claude_runtimes", {})
    if not isinstance(runtimes, dict):
        raise ConfigError("`claude_runtimes` must be a table.")
    for name, runtime in runtimes.items():
        _validate_runtime_name(name)
        if not isinstance(runtime, dict):
            raise ConfigError(f"claude_runtimes.{name} must be a table.")
        _validate_claude_runtime(name, runtime, warnings)

    models = config.get("claude_models", {})
    if not isinstance(models, dict):
        raise ConfigError("`claude_models` must be a table.")
    for name, model in models.items():
        _validate_runtime_name(name)
        if not isinstance(model, dict):
            raise ConfigError(f"claude_models.{name} must be a table.")
        _validate_claude_model(name, model, warnings)

    for name, preset in presets.items():
        model_ref = preset.get("claude_model")
        if model_ref is not None and model_ref not in models:
            raise ConfigError(
                f"presets.{name}.claude_model {model_ref!r} does not name a "
                "defined Claude model."
            )

    active_preset = config.get("active_preset")
    if active_preset is not None:
        if not isinstance(active_preset, str) or not active_preset:
            raise ConfigError("active_preset must be a non-empty string or omitted.")
        if active_preset not in presets:
            raise ConfigError(
                f"active_preset {active_preset!r} does not name a defined preset."
            )

    return warnings


def _validate_preset(
    name: str, preset: dict[str, Any], warnings: list[str]
) -> None:
    known = {"workflow_mode", "claude_runtime", "claude_model", "codex"}
    for key in preset:
        if key not in known:
            warnings.append(
                f"Unknown key in preset {name!r}: {key!r} (ignored)."
            )

    mode = preset.get("workflow_mode")
    if mode is not None and mode not in VALID_WORKFLOW_MODES:
        raise ConfigError(
            f"presets.{name}.workflow_mode {mode!r} is invalid; "
            f"expected one of {VALID_WORKFLOW_MODES!r}."
        )

    runtime_ref = preset.get("claude_runtime")
    if runtime_ref is not None and (
        not isinstance(runtime_ref, str) or not runtime_ref
    ):
        raise ConfigError(
            f"presets.{name}.claude_runtime must be a non-empty string when set."
        )

    model_ref = preset.get("claude_model")
    if model_ref is not None and (
        not isinstance(model_ref, str) or not model_ref
    ):
        raise ConfigError(
            f"presets.{name}.claude_model must be a non-empty string when set."
        )
    codex = preset.get("codex", {})
    if not isinstance(codex, dict):
        raise ConfigError(f"presets.{name}.codex must be a table.")
    for phase, phase_cfg in codex.items():
        if phase not in VALID_PHASES:
            raise ConfigError(
                f"presets.{name}.codex.{phase} is not a recognized phase; "
                f"expected one of {VALID_PHASES!r}."
            )
        if not isinstance(phase_cfg, dict):
            raise ConfigError(
                f"presets.{name}.codex.{phase} must be a table."
            )
        _validate_phase(f"presets.{name}.codex.{phase}", phase_cfg, warnings)


def _validate_phase(
    path: str, phase_cfg: dict[str, Any], warnings: list[str]
) -> None:
    known = {"profile", "model", "reasoning_effort", "reasoning_summary", "verbosity"}
    for key in phase_cfg:
        if key not in known:
            warnings.append(f"Unknown key at {path}: {key!r} (ignored).")

    profile = phase_cfg.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile):
        raise ConfigError(f"{path}.profile must be a non-empty string when set.")

    model = phase_cfg.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ConfigError(f"{path}.model must be a non-empty string when set.")

    effort = phase_cfg.get("reasoning_effort")
    if effort is not None and effort not in VALID_REASONING:
        raise ConfigError(
            f"{path}.reasoning_effort {effort!r} is invalid; "
            f"expected one of {VALID_REASONING!r}."
        )

    for key in ("reasoning_summary", "verbosity"):
        value = phase_cfg.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ConfigError(f"{path}.{key} must be a non-empty string when set.")


def _validate_claude_runtime(
    name: str, runtime: dict[str, Any], warnings: list[str]
) -> None:
    known = {"display_name", "launcher", "args", "allowed_commands", "executable_paths"}
    for key in runtime:
        if key not in known:
            warnings.append(
                f"Unknown key in claude_runtimes.{name}: {key!r} (ignored)."
            )
    display = runtime.get("display_name")
    if display is not None and (not isinstance(display, str) or not display):
        raise ConfigError(
            f"claude_runtimes.{name}.display_name must be a non-empty string when set."
        )
    launcher = runtime.get("launcher")
    if launcher is not None and (not isinstance(launcher, str) or not launcher):
        raise ConfigError(
            f"claude_runtimes.{name}.launcher must be a non-empty string when set."
        )
    extra_args = runtime.get("args", [])
    if not isinstance(extra_args, list) or any(
        not isinstance(x, str) for x in extra_args
    ):
        raise ConfigError(
            f"claude_runtimes.{name}.args must be an array of strings."
        )
    allowed_commands = runtime.get("allowed_commands", [])
    if not isinstance(allowed_commands, list) or any(
        not isinstance(command, str) for command in allowed_commands
    ):
        raise ConfigError(
            f"claude_runtimes.{name}.allowed_commands must be an array of strings."
        )
    for command in allowed_commands:
        _validate_allowed_command(name, command)
    executable_paths = runtime.get("executable_paths", [])
    if not isinstance(executable_paths, list) or any(
        not isinstance(path, str) or not path for path in executable_paths
    ):
        raise ConfigError(
            f"claude_runtimes.{name}.executable_paths must be an array of non-empty strings."
        )


_SAFE_COMMAND = re.compile(r"^[A-Za-z0-9_./+@-]+(?: [A-Za-z0-9_./+@:-]+){0,2}$")
_SHELL_COMMANDS = {"bash", "sh", "zsh", "fish", "cmd", "cmd.exe", "powershell", "pwsh"}
_SAFE_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "rev-parse", "ls-files"}


def _validate_allowed_command(runtime_name: str, command: str) -> None:
    """Validate a Claude Bash permission prefix without accepting shell syntax."""
    if not _SAFE_COMMAND.fullmatch(command):
        raise ConfigError(
            f"claude_runtimes.{runtime_name}.allowed_commands contains unsafe command "
            f"prefix {command!r}; use a simple executable or subcommand without shell operators."
        )
    parts = command.split()
    executable = Path(parts[0]).name.lower()
    if executable in _SHELL_COMMANDS:
        raise ConfigError(
            f"claude_runtimes.{runtime_name}.allowed_commands cannot grant a shell: {command!r}."
        )
    if executable == "git" and (len(parts) < 2 or parts[1] not in _SAFE_GIT_SUBCOMMANDS):
        raise ConfigError(
            f"claude_runtimes.{runtime_name}.allowed_commands cannot grant destructive or "
            f"unbounded Git command {command!r}."
        )


def _validate_claude_model(
    name: str, model: dict[str, Any], warnings: list[str]
) -> None:
    known = {"display_name", "model"}
    for key in model:
        if key not in known:
            warnings.append(
                f"Unknown key in claude_models.{name}: {key!r} (ignored)."
            )
    display = model.get("display_name")
    if display is not None and (not isinstance(display, str) or not display):
        raise ConfigError(
            f"claude_models.{name}.display_name must be a non-empty string when set."
        )
    value = model.get("model")
    if not isinstance(value, str) or not value:
        raise ConfigError(
            f"claude_models.{name}.model must be a non-empty string."
        )


_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def _validate_preset_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_PATTERN.match(name):
        raise ConfigError(
            f"Preset name {name!r} is invalid; must match {_NAME_PATTERN.pattern}."
        )


def _validate_runtime_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_PATTERN.match(name):
        raise ConfigError(
            f"claude_runtime name {name!r} is invalid; "
            f"must match {_NAME_PATTERN.pattern}."
        )


def _reject_secrets(value: Any, context: str, path: str = "") -> None:
    """Refuse to accept or persist keys that look like credentials."""
    if isinstance(value, dict):
        for k, v in value.items():
            key_str = str(k)
            for pattern in SECRET_LIKE_PATTERNS:
                if pattern.search(key_str):
                    raise ConfigError(
                        f"Refusing to {context} config: field "
                        f"{(path + '.' + key_str).lstrip('.')!r} looks like a "
                        "credential. Store secrets in the Codex or Claude "
                        "provider configuration instead."
                    )
            _reject_secrets(v, context, f"{path}.{key_str}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _reject_secrets(item, context, f"{path}[{i}]")


# ---------------------------------------------------------------------------
# Effective resolution
# ---------------------------------------------------------------------------


def resolve_effective(
    config: dict[str, Any], preset_name: str | None = None
) -> dict[str, Any]:
    """Return a redacted, merged effective configuration.

    ``preset_name`` selects a preset explicitly; otherwise ``active_preset``
    from the config is used. Environment-variable overrides are NOT layered
    here — they are per-invocation and applied by :func:`resolve_for_phase`
    when a phase is executed. This keeps the display value stable and the
    per-phase value dynamic.
    """
    validate_config(config)  # cheap and defensive

    effective: dict[str, Any] = {
        "version": config.get("version", 1),
        "active_preset": None,
        "workflow": {**default_config()["workflow"]},
        "codex": {},
        "claude_runtime": None,
        "claude_model": None,
    }

    workflow = config.get("workflow", {})
    if isinstance(workflow, dict):
        for key in (
            "max_review_rounds", "process_timeout_seconds", "workflow_mode",
            "reuse_codex_review_context", "codex_review_session_max_turns",
            "executable_search_paths",
        ):
            if key in workflow:
                effective["workflow"][key] = workflow[key]

    presets = config.get("presets", {}) or {}
    active = preset_name if preset_name is not None else config.get("active_preset")
    if active is not None:
        if active not in presets:
            raise ConfigError(
                f"Preset {active!r} is not defined in the configuration."
            )
        preset = presets[active]
        effective["active_preset"] = active
        # Preset workflow_mode overrides the config-level default.
        if "workflow_mode" in preset:
            effective["workflow"]["workflow_mode"] = preset["workflow_mode"]
        if "claude_runtime" in preset:
            effective["claude_runtime"] = preset["claude_runtime"]
        if "claude_model" in preset:
            effective["claude_model"] = preset["claude_model"]
        codex = preset.get("codex", {}) or {}
        for phase in VALID_PHASES:
            if phase in codex:
                effective["codex"][phase] = dict(codex[phase])

    return effective


def resolve_for_phase(
    config: dict[str, Any],
    phase: str,
    preset_name: str | None = None,
) -> dict[str, Any]:
    """Return the effective phase configuration with env-var overrides applied.

    Merge order (later overrides earlier):

    1. built-in phase defaults (caller supplies via ``controller``);
    2. selected preset's phase table from ``config``;
    3. environment variables ``CLAUDE_AUTONOMOUS_PHASE_PROFILES`` (JSON) and
       ``CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>``.

    The returned dict uses the storage-layer key names: ``profile``, ``model``,
    ``reasoning_effort``, ``reasoning_summary``, ``verbosity``. Absent keys
    are simply not present.
    """
    if phase not in VALID_PHASES:
        raise ConfigError(f"Unknown phase {phase!r}.")

    result: dict[str, Any] = {}

    presets = config.get("presets", {}) or {}
    active = preset_name if preset_name is not None else config.get("active_preset")
    if active and active in presets:
        codex = presets[active].get("codex", {}) or {}
        phase_cfg = codex.get(phase)
        if isinstance(phase_cfg, dict):
            for key in ("profile", "model", "reasoning_effort", "reasoning_summary", "verbosity"):
                if key in phase_cfg:
                    result[key] = phase_cfg[key]

    override_raw = os.environ.get("CLAUDE_AUTONOMOUS_PHASE_PROFILES", "").strip()
    if override_raw:
        try:
            overrides = json.loads(override_raw)
        except json.JSONDecodeError:
            overrides = None
        if isinstance(overrides, dict):
            phase_override = overrides.get(phase)
            if isinstance(phase_override, dict):
                # The env var still uses the legacy short keys `reasoning`,
                # `verbosity`, `reasoning_summary`, `model`. Translate to the
                # storage-layer names for uniform downstream handling.
                translation = {
                    "reasoning": "reasoning_effort",
                    "verbosity": "verbosity",
                    "reasoning_summary": "reasoning_summary",
                    "model": "model",
                    "profile": "profile",
                }
                for legacy_key, canon_key in translation.items():
                    if legacy_key in phase_override:
                        result[canon_key] = str(phase_override[legacy_key])

    model_env = os.environ.get(
        f"CLAUDE_AUTONOMOUS_CODEX_MODEL_{phase.upper()}", ""
    ).strip()
    if model_env:
        result["model"] = model_env

    return result


# ---------------------------------------------------------------------------
# Codex profile discovery
# ---------------------------------------------------------------------------


def list_codex_profiles(codex_home: Path | None = None) -> list[dict[str, Any]]:
    """Return descriptors for every ``*.config.toml`` file under the Codex home.

    The base ``config.toml`` is excluded. Each descriptor has the following
    shape (missing scalar fields are omitted rather than filled with null):

        {"id": "azure-gpt5p6-sol",
         "label": "Azure · GPT-5.6 Sol",
         "provider": "azure",
         "model": "gpt-5.6-sol",
         "path": "/Users/.../.codex/azure-gpt5p6-sol.config.toml",
         "valid": true}

    A malformed unrelated profile does not crash the listing: its entry has
    ``valid: false`` and an ``error`` string explaining the reason.
    """
    home = codex_home if codex_home is not None else resolve_codex_home()
    if not home.exists() or not home.is_dir():
        return []

    results: list[dict[str, Any]] = []
    for entry in sorted(home.iterdir()):
        if not entry.is_file():
            continue
        if entry.name == "config.toml":
            continue
        if not entry.name.endswith(".config.toml"):
            continue
        profile_id = entry.name[: -len(".config.toml")]
        try:
            with entry.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            results.append(
                {
                    "id": profile_id,
                    "path": str(entry),
                    "valid": False,
                    "error": f"{exc}",
                }
            )
            continue
        if not isinstance(data, dict):
            results.append(
                {
                    "id": profile_id,
                    "path": str(entry),
                    "valid": False,
                    "error": "profile file is not a TOML table",
                }
            )
            continue
        provider = _extract_str(data, "model_provider") or _extract_str(data, "provider")
        model = _extract_str(data, "model")
        label = _friendly_label(profile_id, provider, model)
        descriptor: dict[str, Any] = {
            "id": profile_id,
            "label": label,
            "path": str(entry),
            "valid": True,
        }
        if provider:
            descriptor["provider"] = provider
        if model:
            descriptor["model"] = model
        results.append(descriptor)
    return results


def _extract_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _friendly_label(profile_id: str, provider: str | None, model: str | None) -> str:
    left = provider.capitalize() if provider else profile_id.split("-", 1)[0].capitalize()
    right = model or profile_id
    return f"{left} · {right}"


# ---------------------------------------------------------------------------
# Redaction (defensive; validation already blocks secret-shaped keys)
# ---------------------------------------------------------------------------


def redact(config: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``config`` with any secret-shaped values elided.

    Since :func:`validate_config` refuses to accept credential-shaped keys,
    a validated config is already safe. This routine exists so that if a
    caller receives an untrusted dict (e.g. legacy state, external test
    input) it can still be displayed without leaking secrets.
    """
    return _redact_walk(config)


def _redact_walk(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if any(p.search(key_str) for p in SECRET_LIKE_PATTERNS):
                out[key_str] = "***"
                continue
            out[key_str] = _redact_walk(v)
        return out
    if isinstance(value, list):
        return [_redact_walk(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Update helpers (in-memory; the caller passes to save_config to persist)
# ---------------------------------------------------------------------------


def set_active_preset(config: dict[str, Any], preset_name: str) -> dict[str, Any]:
    """Return a copy of ``config`` with ``active_preset`` set to ``preset_name``.

    Raises :class:`ConfigError` when the preset is not defined.
    """
    presets = config.get("presets", {}) or {}
    if preset_name not in presets:
        raise ConfigError(
            f"Cannot set active_preset to {preset_name!r}: preset is not defined."
        )
    updated = _deep_copy(config)
    updated["active_preset"] = preset_name
    return updated


def set_phase(
    config: dict[str, Any],
    preset_name: str,
    phase: str,
    *,
    profile: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    reasoning_summary: str | None = None,
    verbosity: str | None = None,
) -> dict[str, Any]:
    """Return a copy of ``config`` with the phase configuration updated.

    Fields set to ``None`` are left unchanged; pass an explicit empty string
    to clear an existing scalar (rejected here — use the raw file to remove
    keys). The caller must subsequently persist the returned config via
    :func:`save_config` for the change to take effect.
    """
    if phase not in VALID_PHASES:
        raise ConfigError(f"Unknown phase {phase!r}.")

    updated = _deep_copy(config)
    presets = updated.setdefault("presets", {})
    if not isinstance(presets, dict):
        raise ConfigError("`presets` must be a table.")
    preset = presets.get(preset_name)
    if preset is None:
        _validate_preset_name(preset_name)
        preset = {}
        presets[preset_name] = preset
    if not isinstance(preset, dict):
        raise ConfigError(f"Preset {preset_name!r} must be a table.")
    codex = preset.setdefault("codex", {})
    if not isinstance(codex, dict):
        raise ConfigError(f"presets.{preset_name}.codex must be a table.")
    phase_cfg = codex.setdefault(phase, {})
    if not isinstance(phase_cfg, dict):
        raise ConfigError(f"presets.{preset_name}.codex.{phase} must be a table.")

    for key, value in (
        ("profile", profile),
        ("model", model),
        ("reasoning_effort", reasoning_effort),
        ("reasoning_summary", reasoning_summary),
        ("verbosity", verbosity),
    ):
        if value is not None:
            phase_cfg[key] = value

    return updated


def set_claude_runtime(config: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a copy of ``config`` with the active preset's Claude runtime set.

    The preset is the current ``active_preset`` (raises when unset). The
    referenced runtime must already be defined.
    """
    active = config.get("active_preset")
    if not active:
        raise ConfigError(
            "Cannot set claude_runtime: no active preset is selected."
        )
    presets = config.get("presets", {}) or {}
    if active not in presets:
        raise ConfigError(f"Active preset {active!r} is not defined.")
    runtimes = config.get("claude_runtimes", {}) or {}
    if name not in runtimes:
        raise ConfigError(
            f"claude_runtime {name!r} is not defined in `claude_runtimes`."
        )
    updated = _deep_copy(config)
    updated["presets"][active]["claude_runtime"] = name
    return updated


def set_claude_model(config: dict[str, Any], name: str | None) -> dict[str, Any]:
    """Set or clear the active preset's Claude model selection."""
    active = config.get("active_preset")
    if not active:
        raise ConfigError("Cannot set claude_model: no active preset is selected.")
    presets = config.get("presets", {}) or {}
    if active not in presets:
        raise ConfigError(f"Active preset {active!r} is not defined.")
    models = config.get("claude_models", {}) or {}
    if name is not None and name not in models:
        raise ConfigError(
            f"claude_model {name!r} is not defined in `claude_models`."
        )
    updated = _deep_copy(config)
    if name is None:
        updated["presets"][active].pop("claude_model", None)
    else:
        updated["presets"][active]["claude_model"] = name
    return updated


def set_review_context_reuse(config: dict[str, Any], enabled: bool) -> dict[str, Any]:
    updated = _deep_copy(config)
    workflow = updated.setdefault("workflow", {})
    if not isinstance(workflow, dict):
        raise ConfigError("`workflow` must be a table.")
    workflow["reuse_codex_review_context"] = enabled
    validate_config(updated)
    return updated


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Minimal TOML emitter
# ---------------------------------------------------------------------------
#
# We intentionally write TOML ourselves rather than pulling in ``tomli-w``:
# the schema we own is small (nested tables of strings/ints/bools/string
# lists), and dependency budget is scarce (see ``requirements.txt``: "The
# controller uses only Python's standard library"). If we ever need to
# preserve user comments we should switch to ``tomlkit``.


def _emit_toml(value: dict[str, Any]) -> str:
    """Serialize a dict to a valid TOML document.

    Supports strings, ints, bools, and lists of strings/ints/bools at the
    scalar level, and arbitrary nesting of tables. Refuses ``None`` (TOML
    has no null); callers should omit unset keys.
    """
    if not isinstance(value, dict):
        raise TypeError("Top-level TOML value must be a table.")

    scalars, tables = _partition(value)
    lines: list[str] = []
    for key, val in scalars.items():
        if val is None:
            continue
        lines.append(f"{_toml_key(key)} = {_toml_value(val)}")

    def emit_table(prefix: list[str], table: dict[str, Any]) -> None:
        if lines and lines[-1] != "":
            lines.append("")
        header = ".".join(_toml_key(part) for part in prefix)
        lines.append(f"[{header}]")
        table_scalars, table_tables = _partition(table)
        for k, v in table_scalars.items():
            if v is None:
                continue
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
        for k, v in table_tables.items():
            emit_table(prefix + [k], v)

    for key, sub in tables.items():
        emit_table([key], sub)

    text = "\n".join(lines).rstrip() + "\n"
    return text


def _partition(table: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    scalars: dict[str, Any] = {}
    tables: dict[str, Any] = {}
    for k, v in table.items():
        if isinstance(v, dict):
            tables[k] = v
        else:
            scalars[k] = v
    return scalars, tables


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError(f"TOML key must be a non-empty string: {key!r}")
    if _BARE_KEY.match(key):
        return key
    return _toml_string(key)


def _toml_value(value: Any) -> str:
    if value is None:
        raise ValueError("TOML has no representation for None.")
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("TOML does not represent NaN/inf.")
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"Unsupported TOML value type: {type(value).__name__}")


def _toml_string(text: str) -> str:
    # TOML basic string: double-quoted with a small escape set. This is
    # sufficient for identifiers, paths, and human-readable names.
    escapes = {
        "\\": "\\\\",
        '"': '\\"',
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
    }
    out = []
    for ch in text:
        if ch in escapes:
            out.append(escapes[ch])
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


# ---------------------------------------------------------------------------
# Convenience: origin-tagged effective config for display / snapshot
# ---------------------------------------------------------------------------


def effective_with_origin(
    config: dict[str, Any],
    config_path: Path,
    preset_name: str | None = None,
) -> dict[str, Any]:
    """Return a display-ready effective config with an ``origin`` block.

    Suitable for ``controller.py config-show --json`` output and for
    persistence into ``run-state.json`` as a run snapshot (the latter uses a
    subset of the returned dict via :func:`snapshot_for_run`).
    """
    effective = resolve_effective(config, preset_name=preset_name)
    model = _resolved_claude_model(config, effective["claude_model"])
    return {
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "active_preset": effective["active_preset"],
        "effective": {
            "workflow": effective["workflow"],
            "codex": effective["codex"],
            "claude_runtime": effective["claude_runtime"],
            "claude_model": model,
        },
    }


def workflow_mode_default(config: dict[str, Any]) -> str | None:
    """Return ``workflow.workflow_mode`` when set, else ``None``."""
    workflow = config.get("workflow", {}) if isinstance(config, dict) else {}
    if isinstance(workflow, dict):
        value = workflow.get("workflow_mode")
        if isinstance(value, str) and value in VALID_WORKFLOW_MODES:
            return value
    return None


def snapshot_for_run(
    config: dict[str, Any], preset_name: str | None = None
) -> dict[str, Any]:
    """Return the subset of the effective config to persist with a run.

    The snapshot pins the model/profile/reasoning decisions for the lifetime
    of the run. Environment overrides that were in effect at init time ARE
    baked into the snapshot (per phase), so changing an env var later has no
    effect on an active run — the snapshot is the sole source of truth for
    subsequent phase invocations. Secrets are never snapshotted (they are
    refused at load/save time already).
    """
    effective = resolve_effective(config, preset_name=preset_name)
    baked_codex: dict[str, dict[str, Any]] = {}
    for phase in VALID_PHASES:
        merged = dict(effective["codex"].get(phase, {}) or {})
        env_overlay = resolve_for_phase(config, phase, preset_name=preset_name)
        # resolve_for_phase includes both preset values AND env overrides;
        # env-only keys therefore appear as differences from the preset entry.
        for key, value in env_overlay.items():
            merged[key] = value
        if merged:
            baked_codex[phase] = merged
    runtime_snapshot = None
    runtime_name = effective["claude_runtime"]
    runtimes = config.get("claude_runtimes", {}) or {}
    if runtime_name and isinstance(runtimes.get(runtime_name), dict):
        runtime = runtimes[runtime_name]
        runtime_snapshot = {
            "name": runtime_name,
            "display_name": runtime.get("display_name"),
            "launcher": runtime.get("launcher"),
            "args": list(runtime.get("args", [])),
            "safe_commands": list(runtime.get("safe_commands", [])),
        }
    result = {
        "preset": effective["active_preset"],
        "workflow": effective["workflow"],
        "codex": baked_codex,
        "claude_runtime": effective["claude_runtime"],
        "claude_model": _resolved_claude_model(config, effective["claude_model"]),
    }
    if runtime_snapshot is not None:
        result["claude_runtime_snapshot"] = runtime_snapshot
    return result


def _resolved_claude_model(
    config: dict[str, Any], model_id: str | None
) -> dict[str, str] | None:
    if model_id is None:
        return None
    definition = (config.get("claude_models") or {}).get(model_id) or {}
    if not isinstance(definition.get("model"), str) or not definition["model"]:
        return None
    result = {"id": model_id, "model": str(definition["model"])}
    display = definition.get("display_name")
    if isinstance(display, str) and display:
        result["display_name"] = display
    return result


__all__ = [
    "CONFIG_FILE_NAME",
    "ConfigError",
    "SUPPORTED_CONFIG_VERSIONS",
    "VALID_PHASES",
    "VALID_REASONING",
    "VALID_WORKFLOW_MODES",
    "default_config",
    "effective_with_origin",
    "list_codex_profiles",
    "load_config",
    "redact",
    "resolve_codex_home",
    "resolve_config_path",
    "resolve_effective",
    "resolve_for_phase",
    "save_config",
    "set_active_preset",
    "set_claude_runtime",
    "set_claude_model",
    "set_phase",
    "set_review_context_reuse",
    "snapshot_for_run",
    "validate_config",
    "workflow_mode_default",
]
