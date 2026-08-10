---
name: autonomous-feature
description: Autonomously develop a repository feature from a high-level idea. Codex independently enhances the idea, proposes a detailed plan, and reviews the implementation while Claude reconciles requirements, implements, verifies, triages findings, and fixes valid issues. Use when the user delegates an end-to-end feature change.
argument-hint: "[feature idea]"
disable-model-invocation: true
effort: high
allowed-tools:
  - Read
  - Grep
  - Glob
  - Edit
  - Write
  - LSP
  - Agent
  - EnterWorktree
  - ExitWorktree
  - Bash(git *)
  - Bash(python3 *)
  - Bash(codex *)
disallowed-tools:
  - AskUserQuestion
hooks:
  Stop:
    - hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/stop_gate.py"'
          timeout: 10
---

# Autonomous feature development

Implement this feature idea:

> $ARGUMENTS

Use ultrathink for architecture, compatibility, and review triage. Operate as a state-machine
driver: ask the controller what to do next, execute that phase, repeat. Detailed per-phase guidance
lives in `references/` and is loaded only when a phase needs it.

## Non-negotiable boundaries

- **The controller state machine is the ONLY permitted execution path for this skill.** When
  invoked, always run `controller.py init` (or `--reuse` an existing active run) and then follow
  `controller.py next-action` until it reports `phase: evaluate`, at which point run
  `controller.py evaluate`. Never bypass the controller because a task looks small, "obvious",
  documentation-only, well-scoped, or otherwise low-risk. Task complexity does NOT select the
  workflow mode — the configured/snapshotted mode does. A lean preset (or `--mode lean`) is the
  way to request lighter phases; there is no "skip controller" affordance.
- **Invoke the controller directly and inspect its original Bash result.** One `controller.py`
  invocation must be one Bash tool call containing one command. Never append `echo $?`,
  `echo "EXIT=$?"`, or another exit-code probe; redirect controller output to `/tmp` merely to
  inspect it; wrap it in `tail`, `cat`, `tee`, `grep`, a pipe, shell chaining, or command
  substitution merely to inspect output; or retry it solely to determine its exit status. Rely
  on the Bash tool result from the original controller invocation. For example, run
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase plan` directly, not a
  redirected or chained wrapper.
- **Terminate only on a controller-authorized state.** Stop when the controller reports the run
  as `complete`, `complete_with_followups`, `blocked`, `cancelled`, or when you have explicitly marked it awaiting a genuine
  human decision via `controller.py await-decision --reason "..."`. Do not otherwise decide the
  workflow is finished.

  **When `await-decision` may / may not be used.** `await-decision` is NOT a general stopping
  mechanism. It may be used ONLY when continued execution genuinely requires one of:

  - a specific human choice (e.g. "user must pick between library A and library B for the new
    dependency");
  - an authorization only the user can grant (e.g. "user must authorize dropping the legacy
    `sessions` table");
  - a missing fact that cannot be safely inferred from the codebase, spec, or standard practice
    (e.g. "the OIDC issuer URL is not in any config file and must be provided by the user").

  `await-decision` must NOT be used because:

  - the task is difficult;
  - the task is ambiguous but the correct choice can be safely inferred;
  - the task is lengthy or has many steps;
  - the task feels low priority;
  - the model would prefer to stop or take a break.

  The `--reason` string must state the CONCRETE decision required — not a general "need input"
  or "unclear next step". The controller rejects reasons shorter than 12 characters after strip
  and rejects a small denylist of exact generic placeholders (case-insensitive, whole-phrase):
  `stop`, `stopping`, `pausing`, `taking a break`, `need input`, `unclear`,
  `human decision needed`, `too hard`, `too big`, `too long`, `low priority`. A fuller sentence
  that merely contains one of these words is fine — only the whole reason is compared.
- Preserve unrelated user changes.
- Never push, merge, publish, deploy, rotate credentials, or modify remote infrastructure.
- Never use `danger-full-access`, `--yolo`, `bypassPermissions`, or equivalent unrestricted modes.
- Never apply an irreversible database migration or delete user data.
- Do not weaken authorization, validation, tests, or static checks to make the workflow pass.
- Codex planning and review executions must remain read-only.
- Use no more than the configured review-round budget.
- Treat every Codex finding as a proposal requiring evidence-based triage.
- Do not create commits unless the user explicitly requested them.

## Driver loop

1. Confirm this is a Git repository. Inspect `CLAUDE.md`, repository instructions, architecture,
   status, and tests. Use `EnterWorktree` for an isolated worktree whenever available — mandatory
   when the starting worktree has uncommitted changes. If the user explicitly asks for
   current-checkout mode, stay in the current branch instead of entering a worktree, and require a
   clean feature branch before proceeding.
2. Initialize:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" doctor
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" init --feature "$ARGUMENTS" --mode auto --worktree-mode isolated
   ```

   `init` prints the `run-state.json` path and run ID. With multiple concurrent runs, pass
   `--run-id <run-id>` to all subsequent commands. If `doctor` reports a missing prerequisite, mark
   the run blocked and report it rather than bypassing it.

   For current-checkout mode, do not call `EnterWorktree`. Instead, keep the current branch checked
   out and run:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" init --feature "$ARGUMENTS" --mode auto --worktree-mode current
   ```

   The controller refuses `main`/`master` unless the user also passes `--allow-main`, and it
   refuses a dirty tree in current-checkout mode.

3. Repeatedly ask the controller for the next phase, then execute it:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" next-action --json
   ```

   The response gives `phase`, `required_action`, `completion_condition`, and `references`. Read the
   referenced file under `references/` for that phase and follow it until the completion condition
   holds. Phase references:

   - `references/specification.md` — produce the accepted spec.
   - `references/planning.md` — produce the accepted plan and set the risk gate.
   - `references/implementation.md` — implement the plan.
   - `references/verification.md` — run and record checks.
   - `references/review.md` — Codex review, triage, and adversarial review.

4. Do not declare success until `evaluate` succeeds:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" evaluate
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" status
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" usage-report
   ```

## Final report

- the implemented behavior;
- principal files changed;
- verification commands and results;
- Codex review rounds and disposition of findings;
- adversarial review result when required;
- per-phase usage table from `usage-report`;
- remaining risks or explicit blocked reason;
- a suggested conventional commit message.
