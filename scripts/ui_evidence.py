"""Repo-provided, bounded UI screenshot evidence for review phases."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


PROJECT_CONFIG_NAME = ".autonomous-development.toml"
MANIFEST_NAME = "manifest.json"
SUPPORTED_MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 256 * 1024
MAX_SCREEN_COUNT = 20
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 50 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
SCREEN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
MAX_SELECTOR_COUNT = 20


@dataclass(frozen=True)
class RendererConfig:
    command: list[str]
    timeout_seconds: float
    scenario_flag: str | None = None
    group_flag: str | None = None


@dataclass
class UIEvidenceResult:
    status: str
    configured: bool
    output_dir: Path | None = None
    manifest: dict[str, Any] | None = None
    image_paths: list[Path] | None = None
    detail: str = ""
    selection: dict[str, list[str]] | None = None
    selection_applied: bool = False

    @property
    def available(self) -> bool:
        return self.status == "success" and bool(self.image_paths)


class ManifestValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_selection(
    selection: dict[str, list[str]] | None,
) -> tuple[dict[str, list[str]], str]:
    """Validate persisted/agent selector data before it can become process argv."""
    if selection is None:
        return {"scenarios": [], "groups": []}, ""
    if not isinstance(selection, dict):
        return {"scenarios": [], "groups": []}, "UI review selection must be an object"
    normalized: dict[str, list[str]] = {}
    for field in ("scenarios", "groups"):
        values = selection.get(field, [])
        if not isinstance(values, list) or len(values) > MAX_SELECTOR_COUNT:
            return {"scenarios": [], "groups": []}, (
                f"UI review selection {field} must be an array of at most "
                f"{MAX_SELECTOR_COUNT} IDs"
            )
        if any(
            not isinstance(value, str) or not SCREEN_ID_PATTERN.fullmatch(value)
            for value in values
        ):
            return {"scenarios": [], "groups": []}, (
                f"UI review selection {field} contains an invalid ID"
            )
        if len(values) != len(set(values)):
            return {"scenarios": [], "groups": []}, (
                f"UI review selection {field} contains duplicate IDs"
            )
        normalized[field] = list(values)
    return normalized, ""


def load_renderer_config(repo_root: Path) -> tuple[RendererConfig | None, str]:
    """Load the optional repository-owned renderer configuration."""
    path = repo_root / PROJECT_CONFIG_NAME
    if not path.exists():
        return None, ""
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"invalid {PROJECT_CONFIG_NAME}: {exc}"

    if payload.get("version", 1) != 1:
        return None, f"unsupported {PROJECT_CONFIG_NAME} version"
    section = payload.get("ui_review")
    if section is None:
        return None, ""
    if not isinstance(section, dict):
        return None, "ui_review must be a TOML table"
    command = section.get("command")
    if (
        not isinstance(command, list)
        or not command
        or len(command) > 64
        or any(not isinstance(arg, str) or not arg or len(arg) > 4096 for arg in command)
    ):
        return None, "ui_review.command must be a non-empty bounded array of strings"
    timeout = section.get("timeout_seconds", 120)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        return None, "ui_review.timeout_seconds must be positive"
    flags: dict[str, str | None] = {}
    for field in ("scenario_flag", "group_flag"):
        value = section.get(field)
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > 128
        ):
            return None, f"ui_review.{field} must be a non-empty bounded string"
        flags[field] = value
    return RendererConfig(list(command), float(timeout), **flags), ""


def _validate_optional_text(screen: dict[str, Any], field: str) -> None:
    value = screen.get(field)
    if value is not None and (not isinstance(value, str) or len(value) > 2000):
        raise ManifestValidationError(
            "invalid_manifest", f"screen {field!r} must be a bounded string"
        )


def validate_manifest(output_dir: Path) -> tuple[dict[str, Any], list[Path]]:
    """Validate the versioned manifest and return its confined image paths."""
    manifest_path = output_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ManifestValidationError("missing_manifest", f"missing required {MANIFEST_NAME}")
    if manifest_path.is_symlink():
        raise ManifestValidationError(
            "unsafe_path", f"{MANIFEST_NAME} must not be a symbolic link"
        )
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ManifestValidationError("excessive_size", "manifest exceeds size limit")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestValidationError("malformed_manifest", f"malformed manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ManifestValidationError("malformed_manifest", "manifest must be a JSON object")
    if manifest.get("version") != SUPPORTED_MANIFEST_VERSION:
        raise ManifestValidationError(
            "unsupported_manifest",
            f"unsupported manifest version {manifest.get('version')!r}",
        )
    screens = manifest.get("screens")
    if not isinstance(screens, list) or not 1 <= len(screens) <= MAX_SCREEN_COUNT:
        raise ManifestValidationError(
            "excessive_count", f"screens must contain 1 to {MAX_SCREEN_COUNT} entries"
        )

    base = output_dir.resolve()
    ids: set[str] = set()
    paths: list[Path] = []
    total_bytes = 0
    for index, screen in enumerate(screens):
        if not isinstance(screen, dict):
            raise ManifestValidationError("invalid_manifest", f"screen {index} must be an object")
        screen_id = screen.get("id")
        if not isinstance(screen_id, str) or not SCREEN_ID_PATTERN.fullmatch(screen_id):
            raise ManifestValidationError("invalid_manifest", f"screen {index} has an invalid id")
        if screen_id in ids:
            raise ManifestValidationError("invalid_manifest", f"duplicate screen id {screen_id!r}")
        ids.add(screen_id)
        relative = screen.get("path")
        if not isinstance(relative, str) or not relative:
            raise ManifestValidationError("invalid_manifest", f"screen {screen_id!r} requires path")
        lexical = Path(relative)
        if lexical.is_absolute() or ".." in lexical.parts:
            raise ManifestValidationError(
                "unsafe_path", f"screen {screen_id!r} path must be relative and confined"
            )
        image_path = (output_dir / lexical).resolve()
        try:
            image_path.relative_to(base)
        except ValueError as exc:
            raise ManifestValidationError(
                "unsafe_path", f"screen {screen_id!r} path escapes output directory"
            ) from exc
        if image_path.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
            raise ManifestValidationError(
                "unsupported_format", f"screen {screen_id!r} uses unsupported image format"
            )
        if not image_path.is_file():
            raise ManifestValidationError(
                "missing_image", f"screen {screen_id!r} image is missing"
            )
        size = image_path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise ManifestValidationError(
                "excessive_size", f"screen {screen_id!r} exceeds per-file size limit"
            )
        total_bytes += size
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ManifestValidationError("excessive_size", "screens exceed total size limit")
        for field in ("title", "description", "viewport"):
            _validate_optional_text(screen, field)
        paths.append(image_path)
    return manifest, paths


def render_ui_evidence(
    repo_root: Path,
    output_dir: Path,
    runner: Callable[..., Any],
    selection: dict[str, list[str]] | None = None,
) -> UIEvidenceResult:
    """Run configured argv, optional declared selectors, then the output directory."""
    config, config_error = load_renderer_config(repo_root)
    if config is None:
        status = "invalid_config" if config_error else "not_configured"
        return UIEvidenceResult(status, bool(config_error), detail=config_error)

    normalized, selection_error = validate_selection(selection)
    if selection_error:
        return UIEvidenceResult(
            "invalid_selection", True, detail=selection_error, selection=normalized
        )
    scenarios = list(normalized.get("scenarios") or [])
    groups = list(normalized.get("groups") or [])
    selector_args: list[str] = []
    selector_capable = bool(config.scenario_flag or config.group_flag)
    unsupported = []
    if selector_capable and scenarios and not config.scenario_flag:
        unsupported.append("scenarios")
    if selector_capable and groups and not config.group_flag:
        unsupported.append("groups")
    if unsupported:
        return UIEvidenceResult(
            "unsupported_selection",
            True,
            detail=(
                "repository renderer configuration does not declare selector flags for: "
                + ", ".join(unsupported)
            ),
            selection=normalized,
        )
    if config.group_flag:
        for group in groups:
            selector_args.extend((config.group_flag, group))
    if config.scenario_flag:
        for scenario in scenarios:
            selector_args.extend((config.scenario_flag, scenario))
    selection_applied = bool(selector_args)

    output_dir.mkdir(parents=True, exist_ok=False)
    command = [*config.command, *selector_args, str(output_dir)]
    try:
        result = runner(command, cwd=repo_root, timeout=config.timeout_seconds)
    except Exception as exc:  # runner normalizes platform-specific spawn failures
        detail = str(exc)
        status = "command_unavailable" if "not found" in detail.lower() else "renderer_error"
        return UIEvidenceResult(
            status,
            True,
            output_dir=output_dir,
            detail=detail,
            selection=normalized,
            selection_applied=selection_applied,
        )

    (output_dir / "renderer.stdout.log").write_text(result.stdout or "", encoding="utf-8")
    (output_dir / "renderer.stderr.log").write_text(result.stderr or "", encoding="utf-8")
    if result.returncode == 124:
        return UIEvidenceResult(
            "timeout",
            True,
            output_dir=output_dir,
            detail="renderer timed out",
            selection=normalized,
            selection_applied=selection_applied,
        )
    if result.returncode != 0:
        stderr = " ".join((result.stderr or "").strip().split())[:1000]
        if selection_applied:
            detail = "renderer rejected requested UI review selection"
            if stderr:
                detail += f": {stderr}"
            status = "selector_rejected"
        else:
            detail = f"renderer exited with status {result.returncode}"
            status = "nonzero_exit"
        return UIEvidenceResult(
            status,
            True,
            output_dir=output_dir,
            detail=detail,
            selection=normalized,
            selection_applied=selection_applied,
        )
    try:
        manifest, image_paths = validate_manifest(output_dir)
    except ManifestValidationError as exc:
        return UIEvidenceResult(
            exc.code,
            True,
            output_dir=output_dir,
            detail=str(exc),
            selection=normalized,
            selection_applied=selection_applied,
        )
    return UIEvidenceResult(
        "success",
        True,
        output_dir=output_dir,
        manifest=manifest,
        image_paths=image_paths,
        selection=normalized,
        selection_applied=selection_applied,
    )


def visual_prompt(result: UIEvidenceResult, attached: bool) -> str:
    """Describe current-round visual evidence without changing non-visual prompts."""
    if not result.configured:
        return ""
    if not result.available or not attached:
        reason = result.detail or result.status
        return (
            "\n\nCURRENT-ROUND UI SCREENSHOT EVIDENCE\n"
            f"Visual evidence is unavailable to this reviewer ({reason}). "
            "Continue the normal code and correctness review; do not assume screenshots exist."
        )
    metadata = json.dumps(result.manifest, indent=2, ensure_ascii=False)
    selection_note = ""
    if result.selection_applied:
        selection_note = (
            "These fresh screenshots were selected as relevant to the current "
            "implementation. They are not necessarily exhaustive UI coverage.\n"
        )
    return (
        "\n\nCURRENT-ROUND UI SCREENSHOT EVIDENCE\n"
        "The attached images were generated for this review round only. "
        f"{selection_note}Manifest metadata:\n"
        f"{metadata}\n\n"
        "Use the images as additive evidence. Check clipping/truncation, control placement, "
        "excessive dimensions, translation/layout breakage, visual hierarchy, enabled/disabled "
        "states, dark-mode regressions, confusing workflows, and obvious requirement mismatches. "
        "Do not invent pixel-perfect requirements that were not specified. Keep the normal code, "
        "correctness, persistence, API/schema, regression, and test review unchanged."
    )


def codex_supports_images(runner: Callable[..., Any], cwd: Path) -> bool:
    """Capability-detect the installed CLI instead of assuming an image flag."""
    try:
        result = runner(["codex", "exec", "--help"], cwd=cwd, timeout=10)
    except Exception:
        return False
    return result.returncode == 0 and bool(
        re.search(r"(?m)^\s+-i,\s+--image\s+<FILE>", result.stdout or "")
    )


def looks_like_image_input_failure(stderr: str) -> bool:
    text = stderr.lower()
    return any(term in text for term in ("image", "vision", "multimodal", "media input"))
