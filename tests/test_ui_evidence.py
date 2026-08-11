from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import controller  # noqa: E402
import ui_evidence  # noqa: E402
from state import find_active_runs, resolve_repository  # noqa: E402


PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff\xe0"


class UIEvidenceUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def configure(
        self,
        command: list[str] | None = None,
        *,
        selectors: bool = False,
    ) -> None:
        command = command or ["fake-renderer"]
        args = ", ".join(json.dumps(value) for value in command)
        selector_config = (
            'scenario_flag = "--scenario"\ngroup_flag = "--group"\n'
            if selectors
            else ""
        )
        (self.temp / ui_evidence.PROJECT_CONFIG_NAME).write_text(
            f'version = 1\n[ui_review]\ncommand = [{args}]\ntimeout_seconds = 5\n'
            f"{selector_config}",
            encoding="utf-8",
        )

    @staticmethod
    def success_runner(command, *, cwd, timeout=None, **kwargs):
        output = Path(command[-1])
        (output / "one.png").write_bytes(PNG)
        (output / "two.jpg").write_bytes(JPEG)
        (output / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "screens": [
                        {"id": "one", "path": "one.png", "title": "One"},
                        {"id": "two", "path": "two.jpg"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "rendered", "")

    def test_no_renderer_configured(self) -> None:
        result = ui_evidence.render_ui_evidence(
            self.temp, self.temp / "out", self.success_runner
        )
        self.assertEqual(result.status, "not_configured")
        self.assertFalse((self.temp / "out").exists())

    def test_successful_renderer_and_manifest(self) -> None:
        self.configure()
        result = ui_evidence.render_ui_evidence(
            self.temp, self.temp / "out", self.success_runner
        )
        self.assertTrue(result.available)
        self.assertEqual([path.name for path in result.image_paths or []], ["one.png", "two.jpg"])
        self.assertTrue((self.temp / "out" / "renderer.stdout.log").exists())

    def test_selector_arguments_are_appended_before_output_directory(self) -> None:
        self.configure(selectors=True)
        seen: list[str] = []

        def runner(command, **kwargs):
            seen.extend(command)
            return self.success_runner(command, **kwargs)

        result = ui_evidence.render_ui_evidence(
            self.temp,
            self.temp / "selected",
            runner,
            selection={
                "groups": ["reference-library"],
                "scenarios": ["reference.add-range", "reference.dark"],
            },
        )
        self.assertTrue(result.available)
        self.assertTrue(result.selection_applied)
        self.assertEqual(
            seen,
            [
                "fake-renderer",
                "--group",
                "reference-library",
                "--scenario",
                "reference.add-range",
                "--scenario",
                "reference.dark",
                str(self.temp / "selected"),
            ],
        )

    def test_selector_rejection_is_visible_and_never_falls_back(self) -> None:
        self.configure(selectors=True)
        calls = 0

        def rejected(command, **kwargs):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(
                command, 2, "", "unknown scenario ID(s): reference.foo"
            )

        result = ui_evidence.render_ui_evidence(
            self.temp,
            self.temp / "rejected",
            rejected,
            selection={"groups": [], "scenarios": ["reference.foo"]},
        )
        self.assertEqual(calls, 1)
        self.assertEqual(result.status, "selector_rejected")
        self.assertIn("reference.foo", result.detail)
        self.assertIn("Visual evidence is unavailable", ui_evidence.visual_prompt(result, False))

    def test_repository_without_selector_support_keeps_legacy_invocation(self) -> None:
        self.configure(selectors=False)
        seen: list[str] = []

        def runner(command, **kwargs):
            seen.extend(command)
            return self.success_runner(command, **kwargs)

        result = ui_evidence.render_ui_evidence(
            self.temp,
            self.temp / "legacy",
            runner,
            selection={"groups": [], "scenarios": ["reference.add-range"]},
        )
        self.assertTrue(result.available)
        self.assertFalse(result.selection_applied)
        self.assertEqual(seen, ["fake-renderer", str(self.temp / "legacy")])

    def test_malformed_persisted_selection_never_reaches_process_argv(self) -> None:
        self.configure(selectors=True)
        called = False

        def runner(command, **kwargs):
            nonlocal called
            called = True
            return self.success_runner(command, **kwargs)

        result = ui_evidence.render_ui_evidence(
            self.temp,
            self.temp / "invalid-selection",
            runner,
            selection={"groups": [], "scenarios": ["../escape"]},
        )
        self.assertFalse(called)
        self.assertEqual(result.status, "invalid_selection")

    def test_renderer_failure_and_timeout(self) -> None:
        self.configure()

        def failure(command, **kwargs):
            return subprocess.CompletedProcess(command, 7, "", "boom")

        failed = ui_evidence.render_ui_evidence(self.temp, self.temp / "failed", failure)
        self.assertEqual(failed.status, "nonzero_exit")

        def timeout(command, **kwargs):
            return subprocess.CompletedProcess(command, 124, "", "timed out")

        timed_out = ui_evidence.render_ui_evidence(self.temp, self.temp / "timeout", timeout)
        self.assertEqual(timed_out.status, "timeout")

    def test_command_unavailable_and_missing_manifest(self) -> None:
        self.configure()

        def unavailable(command, **kwargs):
            raise FileNotFoundError("executable not found")

        missing_command = ui_evidence.render_ui_evidence(
            self.temp, self.temp / "unavailable", unavailable
        )
        self.assertEqual(missing_command.status, "command_unavailable")

        def no_manifest(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "", "")

        missing_manifest = ui_evidence.render_ui_evidence(
            self.temp, self.temp / "missing", no_manifest
        )
        self.assertEqual(missing_manifest.status, "missing_manifest")

    def test_cli_without_image_flag_degrades_cleanly(self) -> None:
        def old_cli(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "usage: codex exec\n", "")

        self.assertFalse(ui_evidence.codex_supports_images(old_cli, self.temp))
        result = ui_evidence.UIEvidenceResult(
            "success",
            True,
            manifest={"version": 1, "screens": [{"id": "x", "path": "x.png"}]},
            image_paths=[self.temp / "x.png"],
            detail="installed CLI has no image flag",
        )
        self.assertIn("Visual evidence is unavailable", ui_evidence.visual_prompt(result, False))

    def _manifest_result(self, manifest: dict, files: dict[str, bytes] | None = None):
        self.configure()

        def runner(command, **kwargs):
            output = Path(command[-1])
            for name, data in (files or {}).items():
                path = output / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        return ui_evidence.render_ui_evidence(self.temp, self.temp / "out", runner)

    def test_malformed_and_unsupported_manifest(self) -> None:
        self.configure()

        def malformed(command, **kwargs):
            (Path(command[-1]) / "manifest.json").write_text("{", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        result = ui_evidence.render_ui_evidence(self.temp, self.temp / "bad", malformed)
        self.assertEqual(result.status, "malformed_manifest")
        unsupported = self._manifest_result({"version": 2, "screens": []})
        self.assertEqual(unsupported.status, "unsupported_manifest")

    def test_path_traversal_missing_image_and_unsupported_extension(self) -> None:
        traversal = self._manifest_result(
            {"version": 1, "screens": [{"id": "x", "path": "../x.png"}]}
        )
        self.assertEqual(traversal.status, "unsafe_path")
        shutil.rmtree(self.temp / "out")
        missing = self._manifest_result(
            {"version": 1, "screens": [{"id": "x", "path": "x.png"}]}
        )
        self.assertEqual(missing.status, "missing_image")
        shutil.rmtree(self.temp / "out")
        unsupported = self._manifest_result(
            {"version": 1, "screens": [{"id": "x", "path": "x.gif"}]},
            {"x.gif": b"GIF"},
        )
        self.assertEqual(unsupported.status, "unsupported_format")

    def test_count_and_size_limits(self) -> None:
        with mock.patch.object(ui_evidence, "MAX_SCREEN_COUNT", 1):
            count = self._manifest_result(
                {
                    "version": 1,
                    "screens": [
                        {"id": "x", "path": "x.png"},
                        {"id": "y", "path": "y.png"},
                    ],
                },
                {"x.png": PNG, "y.png": PNG},
            )
        self.assertEqual(count.status, "excessive_count")
        shutil.rmtree(self.temp / "out")
        with mock.patch.object(ui_evidence, "MAX_IMAGE_BYTES", 4):
            size = self._manifest_result(
                {"version": 1, "screens": [{"id": "x", "path": "x.png"}]},
                {"x.png": PNG},
            )
        self.assertEqual(size.status, "excessive_size")


class UIEvidenceControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp())
        self.state_home = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test"],
            check=True,
        )
        (self.repo / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        (self.repo / ui_evidence.PROJECT_CONFIG_NAME).write_text(
            'version = 1\n[ui_review]\ncommand = ["fake-renderer"]\n'
            'timeout_seconds = 5\nscenario_flag = "--scenario"\ngroup_flag = "--group"\n',
            encoding="utf-8",
        )
        args = argparse.Namespace(
            project_root=str(self.repo), state_dir=str(self.state_home), run_id=None,
            feature="F", label=None, worktree_mode="isolated", allow_main=False,
            reuse=False, force=False, max_review_rounds=3, preset=None, mode=None,
            config_path=None,
        )
        controller.cmd_init(args)
        ref = find_active_runs(self.state_home, resolve_repository(self.repo).id)[0]
        self.run_dir = ref.run_dir
        self.state_path = self.run_dir / "run-state.json"
        (self.run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (self.run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["verification"] = {
            "checks": [{"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}],
            "passed": True,
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.render_commands: list[list[str]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.state_home, ignore_errors=True)

    def _run_reviews(self, *, reject_images: bool = False, rounds: int = 1):
        render_count = 0
        codex_commands: list[list[str]] = []
        original = controller.run_process

        def runner(command, *, cwd, input_text=None, check=False, timeout=None):
            nonlocal render_count
            if command[0] == "git":
                return original(
                    command,
                    cwd=cwd,
                    input_text=input_text,
                    check=check,
                    timeout=timeout,
                )
            if command[0] == "fake-renderer":
                render_count += 1
                self.render_commands.append(list(command))
                output = Path(command[-1])
                selected = [
                    command[index + 1]
                    for index, value in enumerate(command[:-1])
                    if value == "--scenario"
                ]
                screen_ids = selected or [f"round-{render_count}"]
                for screen_id in screen_ids:
                    (output / f"{screen_id}.png").write_bytes(PNG)
                (output / "manifest.json").write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "screens": [
                                {
                                    "id": screen_id,
                                    "path": f"{screen_id}.png",
                                }
                                for screen_id in screen_ids
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:3] == ["codex", "exec", "--help"]:
                return subprocess.CompletedProcess(command, 0, "  -i, --image <FILE>...\n", "")
            codex_commands.append(list(command))
            if reject_images and "--image" in command:
                return subprocess.CompletedProcess(command, 1, "", "model rejects image input")
            out = Path(command[command.index("--output-last-message") + 1])
            delta = "review-delta.schema.json" in " ".join(command)
            payload = (
                {
                    "verdict": "pass",
                    "summary": "ok",
                    "resolved_findings": [],
                    "new_findings": [],
                    "regressions": [],
                    "affected_acceptance_criteria": [],
                    "confidence": 1.0,
                }
                if delta else
                {
                    "verdict": "pass",
                    "summary": "ok",
                    "findings": [],
                    "verification_gaps": [],
                    "acceptance_criteria_assessment": [],
                    "confidence": 1.0,
                }
            )
            out.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        controller.run_process = runner
        try:
            args = argparse.Namespace(
                project_root=str(self.repo), state_dir=str(self.state_home), run_id=None,
                phase="review", timeout=None,
            )
            for _ in range(rounds):
                controller.cmd_codex(args)
        finally:
            controller.run_process = original
        return render_count, codex_commands

    def _record_work_result(self, kind: str, payload: dict) -> None:
        input_index = len(list(self.run_dir.glob("input-*")))
        source = self.run_dir / f"input-{kind}-{input_index}.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        controller.cmd_record_work_result(
            argparse.Namespace(
                project_root=str(self.repo),
                state_dir=str(self.state_home),
                run_id=None,
                kind=kind,
                file=str(source),
            )
        )

    def test_images_are_attached_and_regenerated_per_review_round(self) -> None:
        render_count, commands = self._run_reviews(rounds=2)
        self.assertEqual(render_count, 2)
        self.assertIn("review-01", commands[0][commands[0].index("--image") + 1])
        self.assertIn(".staging-", commands[0][commands[0].index("--image") + 1])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        first = state["reviews"][0]["ui_evidence"]
        second = state["reviews"][1]["ui_evidence"]
        self.assertEqual(first["path"], "review-01/screenshots")
        self.assertEqual(second["path"], "review-02/screenshots")
        self.assertTrue((self.run_dir / first["manifest"]).exists())
        self.assertTrue((self.run_dir / second["manifest"]).exists())

    def test_provider_image_rejection_falls_back_without_images(self) -> None:
        _, commands = self._run_reviews(reject_images=True)
        self.assertEqual(len(commands), 2)
        self.assertIn("--image", commands[0])
        self.assertNotIn("--image", commands[1])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        evidence = state["reviews"][0]["ui_evidence"]
        self.assertFalse(evidence["attached_to_codex"])
        self.assertIn("rejected image input", evidence["detail"])
        prompt = (self.run_dir / "review.prompt.md").read_text(encoding="utf-8")
        self.assertIn("Visual evidence is unavailable", prompt)

    def test_work_result_absence_preserves_and_fix_updates_selection(self) -> None:
        self._record_work_result(
            "implementation",
            {"ui_review": {"scenarios": ["reference.add-range"]}},
        )
        self._record_work_result("fix", {"summary": "non-visual fix"})
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["ui_review_selection"]["scenarios"], ["reference.add-range"]
        )

        self._record_work_result(
            "fix",
            {
                "ui_review": {
                    "groups": ["reference-library"],
                    "scenarios": ["reference.dark", "reference.raw-points"],
                }
            },
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["ui_review_selection"]["groups"], ["reference-library"])
        self.assertEqual(
            state["ui_review_selection"]["scenarios"],
            ["reference.dark", "reference.raw-points"],
        )

        self._record_work_result("fix", {"ui_review": {}})
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["ui_review_selection"]["groups"], [])
        self.assertEqual(state["ui_review_selection"]["scenarios"], [])

    def test_selected_scenarios_are_fresh_and_associated_with_each_round(self) -> None:
        self._record_work_result(
            "implementation",
            {
                "ui_review": {
                    "scenarios": ["reference.add-range", "reference.dark"]
                }
            },
        )
        render_count, commands = self._run_reviews(rounds=2)
        self.assertEqual(render_count, 2)
        for command in self.render_commands:
            self.assertEqual(
                command[1:-1],
                [
                    "--scenario",
                    "reference.add-range",
                    "--scenario",
                    "reference.dark",
                ],
            )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        first, second = state["reviews"]
        self.assertEqual(
            first["ui_evidence"]["selection"]["scenarios"],
            ["reference.add-range", "reference.dark"],
        )
        self.assertEqual(
            second["ui_evidence"]["selection"], first["ui_evidence"]["selection"]
        )
        self.assertNotEqual(
            first["ui_evidence"]["path"], second["ui_evidence"]["path"]
        )
        prompt = (self.run_dir / "review.prompt.md").read_text(encoding="utf-8")
        self.assertIn("selected as relevant to the current implementation", prompt)
        self.assertIn("not necessarily exhaustive UI coverage", prompt)
        image_args = [
            commands[0][index + 1]
            for index, value in enumerate(commands[0][:-1])
            if value == "--image"
        ]
        self.assertEqual(len(image_args), 2)
        self.assertTrue(any("reference.add-range.png" in value for value in image_args))
        self.assertTrue(any("reference.dark.png" in value for value in image_args))

    def test_fix_selection_replaces_implementation_selection_for_next_round(self) -> None:
        self._record_work_result(
            "implementation",
            {"ui_review": {"scenarios": ["reference.add-range"]}},
        )
        self._run_reviews(rounds=1)
        self._record_work_result(
            "fix", {"ui_review": {"scenarios": ["reference.dark"]}}
        )
        self._run_reviews(rounds=1)
        self.assertEqual(
            self.render_commands[0][1:-1],
            ["--scenario", "reference.add-range"],
        )
        self.assertEqual(
            self.render_commands[1][1:-1],
            ["--scenario", "reference.dark"],
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["reviews"][1]["ui_evidence"]["selection"]["scenarios"],
            ["reference.dark"],
        )


if __name__ == "__main__":
    unittest.main()
