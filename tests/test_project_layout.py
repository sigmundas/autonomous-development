from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProjectLayoutTests(unittest.TestCase):
    def test_manifest_and_components(self) -> None:
        manifest = json.loads(
            (ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], "autonomous-development")
        expected = {
            "autonomous-feature",
            "autonomous-current",
            "autonomous-main",
            "autonomous-resume",
            "enhance-idea",
            "implementation-plan",
            "implement-plan",
            "verify-feature",
            "codex-review",
            "adversarial-review",
            "fix-findings",
            "autonomous-status",
        }
        actual = {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")}
        self.assertEqual(actual, expected)

    def test_json_files_parse(self) -> None:
        for path in ROOT.rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))

    def test_output_schemas_are_strict_required(self) -> None:
        """Codex --output-schema objects must list every property in `required`
        (OpenAI strict structured outputs reject any omission)."""

        def violations(node: object, path: str) -> list[tuple[str, list[str]]]:
            found: list[tuple[str, list[str]]] = []
            if isinstance(node, dict):
                props = node.get("properties")
                if node.get("type") == "object" and isinstance(props, dict):
                    missing = sorted(set(props) - set(node.get("required", [])))
                    if missing:
                        found.append((path, missing))
                for key, value in node.items():
                    found += violations(value, f"{path}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    found += violations(value, f"{path}[{index}]")
            return found

        for name in (
            "enhanced-idea",
            "implementation-plan",
            "review",
            "review-delta",
            "adversarial-review",
        ):
            path = ROOT / "schemas" / f"{name}.schema.json"
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(violations(schema, "(root)"), [], f"{name}: {path}")

    def test_prompt_placeholders_are_known(self) -> None:
        known = {
            "FEATURE",
            "BASELINE",
            "REPOSITORY_CONTEXT",
            "CODEX_SPEC",
            "ACCEPTED_SPEC",
            "ACCEPTED_PLAN",
            "VERIFICATION",
            "PREVIOUS_REVIEW",
            "LATEST_REVIEW",
            "FINDING_LEDGER",
            "OPEN_FINDINGS",
            "ACCEPTANCE_CRITERIA",
            "CHANGED_SINCE_LAST_REVIEW",
        }
        import re

        for path in (ROOT / "prompts").glob("*.md"):
            placeholders = set(
                re.findall(r"\{\{([A-Z0-9_]+)\}\}", path.read_text(encoding="utf-8"))
            )
            self.assertTrue(placeholders <= known, f"{path}: {placeholders - known}")

    def test_autonomous_feature_skill_mentions_worktree_modes(self) -> None:
        text = (ROOT / "skills" / "autonomous-feature" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("--worktree-mode isolated", text)
        self.assertIn("--worktree-mode current", text)
        self.assertIn("EnterWorktree", text)
        self.assertIn("current-checkout mode", text)

    def test_autonomous_current_skill_uses_current_mode_without_worktree(self) -> None:
        text = (ROOT / "skills" / "autonomous-current" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("--mode standard", text)
        self.assertIn("--worktree-mode current", text)
        self.assertNotIn("--allow-main", text)
        # Must NOT enter a disposable worktree.
        self.assertIn("EnterWorktree", text)  # mentioned only to disallow it
        # The frontmatter explicitly disallows the worktree tools.
        head = text.split("---", 2)[1]
        self.assertIn("EnterWorktree", head)
        self.assertIn("ExitWorktree", head)
        # Disallows main/master.
        self.assertIn("main", text)
        self.assertIn("master", text)
        # Never commits.
        self.assertIn("Do not create commits", text)
        # Does not create .claude/worktrees/*.
        self.assertIn(".claude/worktrees", text)

    def test_autonomous_main_skill_passes_allow_main(self) -> None:
        text = (ROOT / "skills" / "autonomous-main" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("--mode standard", text)
        self.assertIn("--worktree-mode current", text)
        self.assertIn("--allow-main", text)
        # Must NOT enter a disposable worktree.
        head = text.split("---", 2)[1]
        self.assertIn("EnterWorktree", head)
        self.assertIn("ExitWorktree", head)
        # Never commits.
        self.assertIn("Do not create commits", text)
        # Still requires a clean tree.
        self.assertIn("clean working tree", text)
        # Does not create .claude/worktrees/*.
        self.assertIn(".claude/worktrees", text)

    def test_autonomous_feature_skill_remains_isolated_default(self) -> None:
        text = (ROOT / "skills" / "autonomous-feature" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        # The default invocation must keep the isolated worktree default.
        self.assertIn("--worktree-mode isolated", text)
        # And the frontmatter must still allow EnterWorktree/ExitWorktree so the
        # default workflow can enter a disposable worktree.
        head = text.split("---", 2)[1]
        self.assertIn("EnterWorktree", head)
        self.assertIn("ExitWorktree", head)

    def test_autonomous_resume_requires_explicit_run_and_never_initializes(self) -> None:
        text = (ROOT / "skills" / "autonomous-resume" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        head = text.split("---", 2)[1]
        self.assertIn('argument-hint: "<run-id>"', head)
        self.assertIn("disable-model-invocation: true", head)
        self.assertIn("exactly one non-empty run ID", text)
        self.assertIn("This is Resume, never Start", text)
        self.assertIn("Never call `controller.py init`", text)
        self.assertNotIn("/autonomous-development:autonomous-main", text)
        self.assertNotIn("/autonomous-development:autonomous-current", text)
        self.assertNotIn("/autonomous-development:autonomous-feature", text)

        # Commands recover from controller state with the selected run rather
        # than relying on conversational or process-environment context.
        prefix = (
            'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" '
            '--run-id "$ARGUMENTS" '
        )
        self.assertIn(prefix + "status --json", text)
        self.assertIn(prefix + "next-action --json", text)
        self.assertIn("not prior conversation", text)
        self.assertIn("Do not discover it", text)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/<reference>", text)

        # No executable example may contain an init subcommand.
        import re

        bash_blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
        self.assertTrue(bash_blocks)
        for block in bash_blocks:
            self.assertNotRegex(block, r"controller\.py[^\n]*\sinit(?:\s|$)")
            for line in block.splitlines():
                if "scripts/controller.py" in line:
                    self.assertIn('--run-id "$ARGUMENTS"', line)

    def test_start_skills_keep_their_existing_init_contract(self) -> None:
        for name in ("autonomous-feature", "autonomous-current", "autonomous-main"):
            text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("controller.py init", text)
            self.assertNotIn("name: autonomous-resume", text)


if __name__ == "__main__":
    unittest.main()
