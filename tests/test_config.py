"""Tests for scripts/config.py and the controller's config-* subcommands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "scripts/controller.py"

sys.path.insert(0, str(ROOT / "scripts"))

import config as user_config  # noqa: E402
import controller  # noqa: E402


class _TempMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdirs: list[Path] = []

    def tearDown(self) -> None:
        for d in self._tmpdirs:
            if d.exists():
                shutil.rmtree(str(d), ignore_errors=True)

    def make_tmp(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self._tmpdirs.append(d)
        return d

    def make_repo(self) -> Path:
        temp = self.make_tmp()
        subprocess.run(["git", "init", "-q", str(temp)], check=True)
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.email", "t@e.com"], check=True
        )
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.name", "t"], check=True
        )
        (temp / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(temp), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(temp), "commit", "-qm", "i"], check=True)
        return temp


# ---------------------------------------------------------------------------
# In-process config module tests
# ---------------------------------------------------------------------------


class ConfigPathTests(_TempMixin):
    def test_default_path_under_state_home(self) -> None:
        state_home = self.make_tmp()
        self.assertEqual(
            user_config.resolve_config_path(state_home),
            state_home / "config.toml",
        )

    def test_explicit_override(self) -> None:
        state_home = self.make_tmp()
        override = self.make_tmp() / "custom.toml"
        self.assertEqual(
            user_config.resolve_config_path(state_home, str(override)),
            override.resolve() if override.exists() else Path(str(override)).expanduser().resolve(),
        )

    def test_codex_home_env(self) -> None:
        override = self.make_tmp()
        os.environ["CODEX_HOME"] = str(override)
        try:
            self.assertEqual(user_config.resolve_codex_home(), override.resolve())
        finally:
            del os.environ["CODEX_HOME"]

    def test_codex_home_default(self) -> None:
        os.environ.pop("CODEX_HOME", None)
        self.assertEqual(user_config.resolve_codex_home(), Path.home() / ".codex")


class LoadValidateTests(_TempMixin):
    def test_missing_file_returns_empty_dict(self) -> None:
        state_home = self.make_tmp()
        cfg = user_config.load_config(state_home / "config.toml")
        self.assertEqual(cfg, {})

    def test_valid_config_roundtrip(self) -> None:
        state_home = self.make_tmp()
        path = state_home / "config.toml"
        cfg = user_config.default_config()
        cfg["presets"]["azure-autonomous"] = {
            "workflow_mode": "standard",
            "codex": {"plan": {"profile": "azure-x", "reasoning_effort": "high"}},
        }
        cfg["active_preset"] = "azure-autonomous"
        user_config.save_config(path, cfg)

        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["active_preset"], "azure-autonomous")
        self.assertEqual(
            parsed["presets"]["azure-autonomous"]["codex"]["plan"]["profile"],
            "azure-x",
        )

    def test_unsupported_version_raises(self) -> None:
        with self.assertRaises(user_config.ConfigError):
            user_config.validate_config({"version": 99})

    def test_invalid_reasoning_effort_raises(self) -> None:
        cfg = user_config.default_config()
        cfg["presets"]["p"] = {
            "codex": {"plan": {"reasoning_effort": "warp-9"}}
        }
        with self.assertRaises(user_config.ConfigError):
            user_config.validate_config(cfg)

    def test_invalid_phase_name_raises(self) -> None:
        cfg = user_config.default_config()
        cfg["presets"]["p"] = {"codex": {"unknown_phase": {}}}
        with self.assertRaises(user_config.ConfigError):
            user_config.validate_config(cfg)

    def test_invalid_workflow_mode_raises(self) -> None:
        cfg = user_config.default_config()
        cfg["presets"]["p"] = {"workflow_mode": "yolo"}
        with self.assertRaises(user_config.ConfigError):
            user_config.validate_config(cfg)

    def test_active_preset_must_exist(self) -> None:
        cfg = user_config.default_config()
        cfg["active_preset"] = "nope"
        with self.assertRaises(user_config.ConfigError):
            user_config.validate_config(cfg)

    def test_unknown_top_level_key_is_warning_not_error(self) -> None:
        cfg = user_config.default_config()
        cfg["mystery"] = 1
        warnings = user_config.validate_config(cfg)
        self.assertTrue(any("mystery" in w for w in warnings))

    def test_secret_shaped_key_rejected(self) -> None:
        cfg = user_config.default_config()
        cfg["presets"]["p"] = {"api_key": "sk-live-abc"}
        with self.assertRaises(user_config.ConfigError):
            user_config.validate_config(cfg)

    def test_save_rejects_secret_shaped_key(self) -> None:
        path = self.make_tmp() / "config.toml"
        cfg = user_config.default_config()
        cfg["presets"]["p"] = {"codex": {"plan": {"authorization": "bearer x"}}}
        with self.assertRaises(user_config.ConfigError):
            user_config.save_config(path, cfg)
        self.assertFalse(path.exists())


class PrecedenceTests(_TempMixin):
    def test_snapshot_pins_run_and_ignores_later_env_changes(self) -> None:
        # An active run's snapshot is authoritative: env vars set AFTER init
        # must not perturb cmd_codex resolution. Env baked in AT init is the
        # snapshot's job (see the CLI test suite below).
        snapshot = {"codex": {"plan": {"profile": "azure-x", "reasoning_effort": "high"}}}
        os.environ["CLAUDE_AUTONOMOUS_PHASE_PROFILES"] = json.dumps(
            {"plan": {"reasoning": "low"}}
        )
        os.environ["CLAUDE_AUTONOMOUS_CODEX_MODEL_PLAN"] = "gpt-5"
        try:
            profile, pid = controller.resolve_phase_execution(
                "plan", snapshot=snapshot
            )
        finally:
            del os.environ["CLAUDE_AUTONOMOUS_PHASE_PROFILES"]
            del os.environ["CLAUDE_AUTONOMOUS_CODEX_MODEL_PLAN"]
        self.assertEqual(pid, "azure-x")
        self.assertEqual(profile["reasoning"], "high")
        self.assertNotIn("model", profile)

    def test_legacy_run_without_snapshot_still_reads_env(self) -> None:
        # Legacy runs (no config_snapshot key) preserve the historical
        # env-var fallback behavior for backward compatibility.
        os.environ["CLAUDE_AUTONOMOUS_PHASE_PROFILES"] = json.dumps(
            {"plan": {"reasoning": "low"}}
        )
        os.environ["CLAUDE_AUTONOMOUS_CODEX_MODEL_PLAN"] = "gpt-5"
        try:
            profile, pid = controller.resolve_phase_execution("plan", snapshot=None)
        finally:
            del os.environ["CLAUDE_AUTONOMOUS_PHASE_PROFILES"]
            del os.environ["CLAUDE_AUTONOMOUS_CODEX_MODEL_PLAN"]
        self.assertEqual(profile["reasoning"], "low")
        self.assertEqual(profile["model"], "gpt-5")
        self.assertIsNone(pid)

    def test_no_snapshot_no_config_matches_builtin(self) -> None:
        os.environ.pop("CLAUDE_AUTONOMOUS_PHASE_PROFILES", None)
        os.environ.pop("CLAUDE_AUTONOMOUS_CODEX_MODEL_PLAN", None)
        profile, pid = controller.resolve_phase_execution("plan")
        self.assertIsNone(pid)
        self.assertEqual(profile["reasoning"], "high")

    def test_config_defaults_absent_when_no_preset(self) -> None:
        cfg = user_config.default_config()
        profile, pid = controller.resolve_phase_execution("plan", loaded_config=cfg)
        self.assertIsNone(pid)
        self.assertEqual(profile["reasoning"], "high")

    def test_config_preset_used_when_no_snapshot(self) -> None:
        cfg = user_config.default_config()
        cfg["presets"]["p"] = {
            "codex": {"review": {"profile": "openai-x", "reasoning_effort": "xhigh"}}
        }
        cfg["active_preset"] = "p"
        profile, pid = controller.resolve_phase_execution("review", loaded_config=cfg)
        self.assertEqual(pid, "openai-x")
        self.assertEqual(profile["reasoning"], "xhigh")


class ProfileDiscoveryTests(_TempMixin):
    def test_lists_valid_profiles_and_excludes_base_config(self) -> None:
        home = self.make_tmp()
        (home / "config.toml").write_text('model = "base"\n')  # excluded
        (home / "azure-x.config.toml").write_text(
            'model = "gpt-5.6-sol"\nmodel_provider = "azure"\n'
        )
        (home / "openai-x.config.toml").write_text(
            'model = "gpt-5.6-sol"\nmodel_provider = "openai"\n'
        )
        profiles = user_config.list_codex_profiles(home)
        ids = sorted(p["id"] for p in profiles)
        self.assertEqual(ids, ["azure-x", "openai-x"])
        self.assertTrue(all(p["valid"] for p in profiles))

    def test_malformed_profile_marked_invalid_not_crashing(self) -> None:
        home = self.make_tmp()
        (home / "good.config.toml").write_text('model = "m"\n')
        (home / "broken.config.toml").write_text("this is not = valid = toml =\n")
        profiles = user_config.list_codex_profiles(home)
        by_id = {p["id"]: p for p in profiles}
        self.assertTrue(by_id["good"]["valid"])
        self.assertFalse(by_id["broken"]["valid"])
        self.assertIn("error", by_id["broken"])

    def test_missing_codex_home_returns_empty(self) -> None:
        profiles = user_config.list_codex_profiles(Path("/nonexistent-path-xyz"))
        self.assertEqual(profiles, [])

    def test_no_secrets_in_descriptor(self) -> None:
        home = self.make_tmp()
        (home / "x.config.toml").write_text(
            'model = "m"\napi_key = "sk-should-not-appear"\n'
        )
        profiles = user_config.list_codex_profiles(home)
        payload = json.dumps(profiles)
        self.assertNotIn("sk-should-not-appear", payload)


class MutationTests(_TempMixin):
    def test_set_phase_preserves_other_presets_and_phases(self) -> None:
        path = self.make_tmp() / "config.toml"
        cfg = user_config.default_config()
        cfg["presets"]["a"] = {"codex": {"review": {"reasoning_effort": "high"}}}
        cfg["presets"]["b"] = {"codex": {"plan": {"reasoning_effort": "low"}}}
        cfg["active_preset"] = "a"
        user_config.save_config(path, cfg)

        loaded = user_config.load_config(path)
        updated = user_config.set_phase(
            loaded, "a", "plan", profile="azure-x", reasoning_effort="high"
        )
        user_config.save_config(path, updated)

        again = user_config.load_config(path)
        self.assertEqual(
            again["presets"]["a"]["codex"]["plan"]["profile"], "azure-x"
        )
        self.assertEqual(
            again["presets"]["a"]["codex"]["review"]["reasoning_effort"], "high"
        )
        self.assertEqual(
            again["presets"]["b"]["codex"]["plan"]["reasoning_effort"], "low"
        )

    def test_atomic_write_no_partial_file_on_error(self) -> None:
        # A save that fails validation must leave the target untouched.
        path = self.make_tmp() / "config.toml"
        good = user_config.default_config()
        good["presets"]["p"] = {"codex": {"plan": {"reasoning_effort": "high"}}}
        user_config.save_config(path, good)
        original = path.read_bytes()

        bad = user_config.default_config()
        bad["presets"]["p"] = {"codex": {"plan": {"reasoning_effort": "warp-9"}}}
        with self.assertRaises(user_config.ConfigError):
            user_config.save_config(path, bad)
        self.assertEqual(path.read_bytes(), original)

    def test_set_active_preset_requires_defined_preset(self) -> None:
        cfg = user_config.default_config()
        with self.assertRaises(user_config.ConfigError):
            user_config.set_active_preset(cfg, "nope")


class RedactionTests(unittest.TestCase):
    def test_redacts_secret_shaped_keys(self) -> None:
        untrusted = {
            "presets": {
                "p": {
                    "api_key": "leaked",
                    "codex": {"plan": {"authorization": "bearer x"}},
                }
            }
        }
        redacted = user_config.redact(untrusted)
        text = json.dumps(redacted)
        self.assertNotIn("leaked", text)
        self.assertNotIn("bearer", text)


# ---------------------------------------------------------------------------
# End-to-end subprocess tests of the controller CLI
# ---------------------------------------------------------------------------


class ConfigCliTests(_TempMixin):
    def _controller(
        self,
        repo: Path,
        state_home: Path,
        *args: str,
        codex_home: Path | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ}
        # Strip legacy env overrides so unrelated shell state does not shift
        # profile precedence during tests.
        env.pop("CLAUDE_AUTONOMOUS_PHASE_PROFILES", None)
        for key in list(env):
            if key.startswith("CLAUDE_AUTONOMOUS_CODEX_MODEL_"):
                del env[key]
        if codex_home is not None:
            env["CODEX_HOME"] = str(codex_home)
        if env_extra:
            env.update(env_extra)
        cmd = [
            sys.executable,
            str(CONTROLLER),
            "--project-root",
            str(repo),
            "--state-dir",
            str(state_home),
            *args,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def _codex_home_with(self, profile_ids: list[str]) -> Path:
        home = self.make_tmp()
        for pid in profile_ids:
            (home / f"{pid}.config.toml").write_text(
                f'model = "{pid}-model"\nmodel_provider = "azure"\n'
            )
        return home

    def test_config_show_with_no_file_prints_defaults(self) -> None:
        repo = self.make_repo()
        state_home = self.make_tmp()
        r = self._controller(
            repo, state_home, "config-show", codex_home=self.make_tmp()
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertFalse(payload["config_exists"])
        self.assertIsNone(payload["active_preset"])

    def test_global_config_commands_work_outside_git(self) -> None:
        non_repo = self.make_tmp()
        non_repo.mkdir(parents=True, exist_ok=True)
        state_home = self.make_tmp()
        state_home.mkdir(parents=True, exist_ok=True)
        codex = self._codex_home_with(["outside-git"])
        (state_home / "config.toml").write_text(
            '[claude_runtimes.local]\nlauncher = "/bin/sh"\n\n'
            '[presets.global]\nworkflow_mode = "standard"\n'
        )

        for command in (
            ("config-show",),
            ("config-list-presets",),
            ("config-list-profiles",),
            ("config-list-claude-runtimes",),
            ("config-validate",),
            ("config-set-active-preset", "global"),
            ("config-set-claude-runtime", "local"),
            (
                "config-set-phase",
                "--preset",
                "global",
                "--phase",
                "plan",
                "--profile",
                "outside-git",
            ),
        ):
            result = self._controller(
                non_repo, state_home, *command, codex_home=codex
            )
            self.assertEqual(result.returncode, 0, (command, result.stderr))

        profiles = json.loads(
            self._controller(
                non_repo,
                state_home,
                "config-list-profiles",
                codex_home=codex,
            ).stdout
        )
        self.assertIn("outside-git", {p["id"] for p in profiles["profiles"]})

    def test_run_command_still_rejects_non_git_project_root(self) -> None:
        non_repo = self.make_tmp()
        non_repo.mkdir(parents=True, exist_ok=True)
        result = self._controller(non_repo, self.make_tmp(), "list-runs")
        self.assertEqual(result.returncode, 2)
        self.assertIn("git repository", result.stderr.lower())

    def test_config_set_active_preset_then_show(self) -> None:
        repo = self.make_repo()
        state_home = self.make_tmp()
        codex = self._codex_home_with(["azure-x"])

        r = self._controller(
            repo,
            state_home,
            "config-set-phase",
            "--preset",
            "azure-autonomous",
            "--phase",
            "plan",
            "--profile",
            "azure-x",
            "--reasoning-effort",
            "high",
            codex_home=codex,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self._controller(
            repo,
            state_home,
            "config-set-active-preset",
            "azure-autonomous",
            codex_home=codex,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self._controller(
            repo, state_home, "config-show", codex_home=codex
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["active_preset"], "azure-autonomous")
        self.assertEqual(
            payload["effective"]["codex"]["plan"]["profile"], "azure-x"
        )

    def test_config_validate_rejects_bad_toml(self) -> None:
        repo = self.make_repo()
        state_home = self.make_tmp()
        state_home.mkdir(parents=True, exist_ok=True)
        (state_home / "config.toml").write_text("this = is = invalid\n")
        r = self._controller(
            repo, state_home, "config-validate", codex_home=self.make_tmp()
        )
        self.assertNotEqual(r.returncode, 0)
        payload = json.loads(r.stdout)
        self.assertFalse(payload["valid"])

    def test_config_list_profiles_reports_valid_and_invalid(self) -> None:
        repo = self.make_repo()
        state_home = self.make_tmp()
        home = self.make_tmp()
        (home / "azure-x.config.toml").write_text('model = "gpt"\n')
        (home / "broken.config.toml").write_text("not = = valid\n")

        r = self._controller(
            repo, state_home, "config-list-profiles", codex_home=home
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        by_id = {p["id"]: p for p in payload["profiles"]}
        self.assertTrue(by_id["azure-x"]["valid"])
        self.assertFalse(by_id["broken"]["valid"])

    def test_init_snapshots_config_into_run_state(self) -> None:
        repo = self.make_repo()
        state_home = self.make_tmp()
        codex = self._codex_home_with(["azure-x"])

        for cmd in (
            [
                "config-set-phase",
                "--preset",
                "azure-autonomous",
                "--phase",
                "plan",
                "--profile",
                "azure-x",
                "--reasoning-effort",
                "high",
            ],
            ["config-set-active-preset", "azure-autonomous"],
        ):
            r = self._controller(repo, state_home, *cmd, codex_home=codex)
            self.assertEqual(r.returncode, 0, r.stderr)

        r = self._controller(
            repo,
            state_home,
            "init",
            "--feature",
            "test snapshot",
            "--mode",
            "standard",
            codex_home=codex,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        state_path = Path(r.stdout.strip())
        data = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(data["preset"], "azure-autonomous")
        self.assertEqual(
            data["config_snapshot"]["codex"]["plan"]["profile"], "azure-x"
        )
        self.assertEqual(
            data["config_snapshot"]["codex"]["plan"]["reasoning_effort"], "high"
        )

    def test_active_run_pinned_when_default_preset_changes(self) -> None:
        repo = self.make_repo()
        state_home = self.make_tmp()
        codex = self._codex_home_with(["profile-a", "profile-b"])

        for cmd in (
            [
                "config-set-phase",
                "--preset",
                "preset-a",
                "--phase",
                "plan",
                "--profile",
                "profile-a",
                "--reasoning-effort",
                "high",
            ],
            [
                "config-set-phase",
                "--preset",
                "preset-b",
                "--phase",
                "plan",
                "--profile",
                "profile-b",
                "--reasoning-effort",
                "low",
            ],
            ["config-set-active-preset", "preset-a"],
        ):
            r = self._controller(repo, state_home, *cmd, codex_home=codex)
            self.assertEqual(r.returncode, 0, r.stderr)

        r = self._controller(
            repo,
            state_home,
            "init",
            "--feature",
            "pin test",
            "--mode",
            "standard",
            codex_home=codex,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        state_path = Path(r.stdout.strip())
        pinned = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(pinned["preset"], "preset-a")
        pinned_profile = pinned["config_snapshot"]["codex"]["plan"]["profile"]
        self.assertEqual(pinned_profile, "profile-a")

        # Rotate the active preset globally. The already-active run must
        # continue to reflect its original snapshot.
        r = self._controller(
            repo,
            state_home,
            "config-set-active-preset",
            "preset-b",
            codex_home=codex,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

        pinned_again = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            pinned_again["config_snapshot"]["codex"]["plan"]["profile"],
            "profile-a",
        )

    def test_init_hard_fails_when_pinned_profile_missing(self) -> None:
        repo = self.make_repo()
        state_home = self.make_tmp()
        empty_codex = self.make_tmp()  # no profiles

        # Manually author a config that references a profile that isn't
        # present in the effective Codex home.
        state_home.mkdir(parents=True, exist_ok=True)
        (state_home / "config.toml").write_text(
            'version = 1\n'
            'active_preset = "p"\n'
            '[presets.p]\n'
            'workflow_mode = "standard"\n'
            '[presets.p.codex.plan]\n'
            'profile = "does-not-exist"\n'
            'reasoning_effort = "high"\n',
            encoding="utf-8",
        )

        r = self._controller(
            repo,
            state_home,
            "init",
            "--feature",
            "missing pin",
            "--mode",
            "standard",
            "--preset",
            "p",
            codex_home=empty_codex,
        )
        # --preset makes the profile-missing check a hard error.
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does-not-exist", r.stderr)


class RunStateBackCompatTests(_TempMixin):
    def test_run_without_snapshot_falls_back_to_builtin(self) -> None:
        # Simulate a run created before this feature: no config_snapshot key.
        # resolve_phase_execution must reproduce the historical behavior.
        os.environ.pop("CLAUDE_AUTONOMOUS_PHASE_PROFILES", None)
        os.environ.pop("CLAUDE_AUTONOMOUS_CODEX_MODEL_PLAN", None)
        profile, pid = controller.resolve_phase_execution("plan", snapshot=None)
        self.assertIsNone(pid)
        self.assertEqual(profile["reasoning"], "high")


class WorkflowModePrecedenceTests(_TempMixin):
    def test_preset_workflow_mode_used_when_no_cli_mode(self) -> None:
        snapshot = {"preset": "p", "workflow": {}, "codex": {}, "claude_runtime": None}
        loaded_config = {
            "version": 1,
            "active_preset": "p",
            "presets": {"p": {"workflow_mode": "lean"}},
        }
        mode, origin = controller._resolve_workflow_mode(
            cli_mode=None, snapshot=snapshot, loaded_config=loaded_config
        )
        self.assertEqual(mode, "lean")
        self.assertEqual(origin, "preset")

    def test_config_workflow_mode_used_when_no_preset_mode(self) -> None:
        snapshot = {"preset": None, "workflow": {}, "codex": {}, "claude_runtime": None}
        loaded_config = {"version": 1, "workflow": {"workflow_mode": "standard"}}
        mode, origin = controller._resolve_workflow_mode(
            cli_mode=None, snapshot=snapshot, loaded_config=loaded_config
        )
        self.assertEqual(mode, "standard")
        self.assertEqual(origin, "config")

    def test_cli_mode_wins_over_preset(self) -> None:
        snapshot = {"preset": "p", "workflow": {}, "codex": {}, "claude_runtime": None}
        loaded_config = {
            "version": 1,
            "active_preset": "p",
            "presets": {"p": {"workflow_mode": "lean"}},
        }
        mode, origin = controller._resolve_workflow_mode(
            cli_mode="rigorous", snapshot=snapshot, loaded_config=loaded_config
        )
        self.assertEqual(mode, "rigorous")
        self.assertEqual(origin, "cli")

    def test_default_auto_when_nothing_set(self) -> None:
        mode, origin = controller._resolve_workflow_mode(
            cli_mode=None, snapshot=None, loaded_config=None
        )
        self.assertEqual(mode, "auto")
        self.assertEqual(origin, "default")


class ActivePresetProfileValidationTests(_TempMixin):
    """Missing referenced Codex profiles must fail closed regardless of source."""

    def _repo(self) -> Path:
        temp = self.make_tmp()
        subprocess.run(["git", "init", "-q", str(temp)], check=True)
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.email", "t@e.com"], check=True
        )
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.name", "t"], check=True
        )
        (temp / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(temp), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(temp), "commit", "-qm", "i"], check=True)
        return temp

    def test_missing_profile_via_active_preset_hard_fails(self) -> None:
        repo = self._repo()
        state_home = self.make_tmp()
        state_home.mkdir(parents=True, exist_ok=True)
        (state_home / "config.toml").write_text(
            'version = 1\n'
            'active_preset = "p"\n'
            '[presets.p]\n'
            'workflow_mode = "standard"\n'
            '[presets.p.codex.plan]\n'
            'profile = "does-not-exist"\n'
            'reasoning_effort = "high"\n',
            encoding="utf-8",
        )
        env = {**os.environ, "CODEX_HOME": str(self.make_tmp())}
        for k in list(env):
            if k.startswith("CLAUDE_AUTONOMOUS_CODEX_MODEL_"):
                del env[k]
        env.pop("CLAUDE_AUTONOMOUS_PHASE_PROFILES", None)
        r = subprocess.run(
            [
                sys.executable,
                str(CONTROLLER),
                "--project-root",
                str(repo),
                "--state-dir",
                str(state_home),
                "init",
                "--feature",
                "test",
                "--mode",
                "standard",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does-not-exist", r.stderr)

    def test_malformed_referenced_profile_hard_fails(self) -> None:
        repo = self._repo()
        state_home = self.make_tmp()
        codex_home = self.make_tmp()
        (codex_home / "azure-x.config.toml").write_text("bogus = = =\n")
        state_home.mkdir(parents=True, exist_ok=True)
        (state_home / "config.toml").write_text(
            'version = 1\n'
            'active_preset = "p"\n'
            '[presets.p.codex.plan]\n'
            'profile = "azure-x"\n',
            encoding="utf-8",
        )
        env = {**os.environ, "CODEX_HOME": str(codex_home)}
        env.pop("CLAUDE_AUTONOMOUS_PHASE_PROFILES", None)
        for k in list(env):
            if k.startswith("CLAUDE_AUTONOMOUS_CODEX_MODEL_"):
                del env[k]
        r = subprocess.run(
            [
                sys.executable,
                str(CONTROLLER),
                "--project-root",
                str(repo),
                "--state-dir",
                str(state_home),
                "init",
                "--feature",
                "test",
                "--mode",
                "standard",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("malformed", r.stderr)

    def test_omitted_profile_remains_valid(self) -> None:
        # A preset with a phase-level effort but NO profile is valid — it
        # means "use whatever Codex would normally resolve for the model".
        repo = self._repo()
        state_home = self.make_tmp()
        state_home.mkdir(parents=True, exist_ok=True)
        (state_home / "config.toml").write_text(
            'version = 1\n'
            'active_preset = "p"\n'
            '[presets.p.codex.plan]\n'
            'reasoning_effort = "high"\n',
            encoding="utf-8",
        )
        env = {**os.environ, "CODEX_HOME": str(self.make_tmp())}
        env.pop("CLAUDE_AUTONOMOUS_PHASE_PROFILES", None)
        for k in list(env):
            if k.startswith("CLAUDE_AUTONOMOUS_CODEX_MODEL_"):
                del env[k]
        r = subprocess.run(
            [
                sys.executable,
                str(CONTROLLER),
                "--project-root",
                str(repo),
                "--state-dir",
                str(state_home),
                "init",
                "--feature",
                "test",
                "--mode",
                "standard",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)


class SnapshotStabilityTests(_TempMixin):
    """Env-resolved values are frozen at init; later env changes have no effect."""

    def _repo(self) -> Path:
        temp = self.make_tmp()
        subprocess.run(["git", "init", "-q", str(temp)], check=True)
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.email", "t@e.com"], check=True
        )
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.name", "t"], check=True
        )
        (temp / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(temp), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(temp), "commit", "-qm", "i"], check=True)
        return temp

    def _clean_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {**os.environ}
        env.pop("CLAUDE_AUTONOMOUS_PHASE_PROFILES", None)
        for k in list(env):
            if k.startswith("CLAUDE_AUTONOMOUS_CODEX_MODEL_"):
                del env[k]
        if extra:
            env.update(extra)
        return env

    def test_env_captured_into_snapshot_at_init(self) -> None:
        repo = self._repo()
        state_home = self.make_tmp()
        # A config file must exist for snapshotting to engage (otherwise the
        # controller preserves legacy env-driven behavior).
        state_home.mkdir(parents=True, exist_ok=True)
        (state_home / "config.toml").write_text("version = 1\n", encoding="utf-8")
        codex_home = self.make_tmp()
        env = self._clean_env(
            {
                "CODEX_HOME": str(codex_home),
                "CLAUDE_AUTONOMOUS_PHASE_PROFILES": json.dumps(
                    {"plan": {"reasoning": "low"}}
                ),
                "CLAUDE_AUTONOMOUS_CODEX_MODEL_PLAN": "gpt-init",
            }
        )
        r = subprocess.run(
            [
                sys.executable,
                str(CONTROLLER),
                "--project-root",
                str(repo),
                "--state-dir",
                str(state_home),
                "init",
                "--feature",
                "test",
                "--mode",
                "standard",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        state_path = Path(r.stdout.strip())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        snap = state["config_snapshot"]
        self.assertEqual(snap["codex"]["plan"]["reasoning_effort"], "low")
        self.assertEqual(snap["codex"]["plan"]["model"], "gpt-init")

    def test_snapshot_stable_when_env_changes_later(self) -> None:
        snapshot = {
            "preset": "p",
            "workflow": {},
            "codex": {
                "plan": {
                    "profile": "init-profile",
                    "model": "init-model",
                    "reasoning_effort": "high",
                }
            },
            "claude_runtime": None,
        }
        # Simulate the user changing env vars AFTER init. cmd_codex must
        # ignore them and use only the snapshot.
        os.environ["CLAUDE_AUTONOMOUS_PHASE_PROFILES"] = json.dumps(
            {"plan": {"reasoning": "minimal", "profile": "later-profile"}}
        )
        os.environ["CLAUDE_AUTONOMOUS_CODEX_MODEL_PLAN"] = "later-model"
        try:
            profile, pid = controller.resolve_phase_execution("plan", snapshot=snapshot)
        finally:
            del os.environ["CLAUDE_AUTONOMOUS_PHASE_PROFILES"]
            del os.environ["CLAUDE_AUTONOMOUS_CODEX_MODEL_PLAN"]
        self.assertEqual(pid, "init-profile")
        self.assertEqual(profile["model"], "init-model")
        self.assertEqual(profile["reasoning"], "high")

        argv = controller.codex_profile_args(profile, profile_id=pid)
        # The argv must reflect the snapshot verbatim, not the later env.
        self.assertIn("--profile", argv)
        self.assertEqual(argv[argv.index("--profile") + 1], "init-profile")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "init-model")
        self.assertIn("-c", argv)
        # Contains the pinned effort, not the later one.
        self.assertIn("model_reasoning_effort=high", argv)
        self.assertNotIn("model_reasoning_effort=minimal", argv)


class PresetWorkflowModeCliTests(_TempMixin):
    def _repo(self) -> Path:
        temp = self.make_tmp()
        subprocess.run(["git", "init", "-q", str(temp)], check=True)
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.email", "t@e.com"], check=True
        )
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.name", "t"], check=True
        )
        (temp / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(temp), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(temp), "commit", "-qm", "i"], check=True)
        return temp

    def _clean_env(self, codex_home: Path) -> dict[str, str]:
        env = {**os.environ, "CODEX_HOME": str(codex_home)}
        env.pop("CLAUDE_AUTONOMOUS_PHASE_PROFILES", None)
        for k in list(env):
            if k.startswith("CLAUDE_AUTONOMOUS_CODEX_MODEL_"):
                del env[k]
        return env

    def test_init_uses_preset_workflow_mode_when_no_cli_mode(self) -> None:
        repo = self._repo()
        state_home = self.make_tmp()
        state_home.mkdir(parents=True, exist_ok=True)
        (state_home / "config.toml").write_text(
            'version = 1\n'
            'active_preset = "p"\n'
            '[presets.p]\n'
            'workflow_mode = "lean"\n',
            encoding="utf-8",
        )
        codex_home = self.make_tmp()
        env = self._clean_env(codex_home)
        r = subprocess.run(
            [
                sys.executable,
                str(CONTROLLER),
                "--project-root",
                str(repo),
                "--state-dir",
                str(state_home),
                "init",
                "--feature",
                "test",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        state_path = Path(r.stdout.strip())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["requested_mode"], "lean")
        self.assertEqual(state["effective_mode"], "lean")
        self.assertEqual(state["mode_origin"], "preset")

    def test_cli_mode_overrides_preset(self) -> None:
        repo = self._repo()
        state_home = self.make_tmp()
        state_home.mkdir(parents=True, exist_ok=True)
        (state_home / "config.toml").write_text(
            'version = 1\n'
            'active_preset = "p"\n'
            '[presets.p]\n'
            'workflow_mode = "lean"\n',
            encoding="utf-8",
        )
        codex_home = self.make_tmp()
        env = self._clean_env(codex_home)
        r = subprocess.run(
            [
                sys.executable,
                str(CONTROLLER),
                "--project-root",
                str(repo),
                "--state-dir",
                str(state_home),
                "init",
                "--feature",
                "test",
                "--mode",
                "rigorous",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        state_path = Path(r.stdout.strip())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["requested_mode"], "rigorous")
        self.assertEqual(state["effective_mode"], "rigorous")
        self.assertEqual(state["mode_origin"], "cli")

    def test_no_config_no_cli_defaults_to_auto(self) -> None:
        repo = self._repo()
        state_home = self.make_tmp()
        env = self._clean_env(self.make_tmp())
        r = subprocess.run(
            [
                sys.executable,
                str(CONTROLLER),
                "--project-root",
                str(repo),
                "--state-dir",
                str(state_home),
                "init",
                "--feature",
                "test",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        state_path = Path(r.stdout.strip())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["requested_mode"], "auto")
        self.assertEqual(state["mode_origin"], "default")


if __name__ == "__main__":
    unittest.main()
