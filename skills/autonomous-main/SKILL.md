---
name: autonomous-main
description: Autonomously develop a repository feature directly in the current main/master checkout. Use only when the user has explicitly authorized direct edits on main/master. Same workflow as autonomous-current but passes --allow-main to bypass the main/master refusal. Still requires a clean working tree and never commits.
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
  - Bash(git *)
  - Bash(python3 *)
  - Bash(codex *)
disallowed-tools:
  - AskUserQuestion
  - EnterWorktree
  - ExitWorktree
hooks:
  Stop:
    - hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/stop_gate.py"'
          timeout: 10
---

# Autonomous feature development — current checkout on main

Implement this feature idea directly in the user's current checkout, including when the
current branch is `main` or `master`:

> $ARGUMENTS

This is the explicit-opt-in variant of `autonomous-current`. By invoking this skill the user has
acknowledged that the agent's edits will land directly in `main`/`master`. Prefer
`autonomous-feature` (isolated worktree) or `autonomous-current` (clean feature branch) when
either is appropriate; this skill exists for cases where the user really does want changes in
the current `main`/`master` checkout.

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
  human decision (see below). Do not otherwise decide the workflow is finished.
- **Genuine human decisions must be recorded before stopping.** If you actually need the user to
  resolve an ambiguity or authorize a step that only they can decide, first mark the run as
  awaiting that decision:

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" await-decision \
    --reason "specific question the user must answer"
  ```

  The Stop hook respects this state and will not force the next controller action. When the user
  answers, the workflow calls `controller.py resume` and continues from `next-action`.

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
- This skill must NOT call `EnterWorktree` / `ExitWorktree`. All edits land in the current
  checkout. Do not create or enter `.claude/worktrees/*`.
- Require a clean working tree. `git status --porcelain` must be empty before initializing. If
  it is not, stop and report which entries are dirty.
- Do not create commits. The user will review and commit with their normal `git diff`/commit
  flow.
- Preserve unrelated user changes.
- Never push, merge, publish, deploy, rotate credentials, or modify remote infrastructure.
- Never use `danger-full-access`, `--yolo`, `bypassPermissions`, or equivalent unrestricted modes.
- Never apply an irreversible database migration or delete user data.
- Do not weaken authorization, validation, tests, or static checks to make the workflow pass.
- Codex planning and review executions must remain read-only.
- Use no more than the configured review-round budget.
- Treat every Codex finding as a proposal requiring evidence-based triage.

## Required driver loop

Every invocation of this skill runs the controller loop below. There is no fast path, no
"documentation-only" shortcut, no "this task is too small" bypass. The controller and its
configured mode decide which phases apply.

1. Confirm this is a Git repository. Inspect `CLAUDE.md`, repository instructions, architecture,
   status, and tests. Verify the working tree is clean before initializing.

2. Initialize in current-checkout mode on main/master:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" doctor
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" init \
     --feature "$ARGUMENTS" \
     --mode standard \
     --worktree-mode current \
     --allow-main
   ```

   `init` prints the `run-state.json` path and run ID. With multiple concurrent runs, pass
   `--run-id <run-id>` to all subsequent commands. If `doctor` reports a missing prerequisite,
   mark the run blocked and report it rather than bypassing it.

3. Repeatedly ask the controller for the next phase, then execute it:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" next-action --json
   ```

   The response gives `phase`, `required_action`, `completion_condition`, and `references`.
   Read the referenced file under `${CLAUDE_PLUGIN_ROOT}/skills/autonomous-feature/references/`
   for that phase and follow it until the completion condition holds.

4. Do not declare success until `evaluate` succeeds:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" evaluate
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" status
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" usage-report
   ```

## Final report

- the implemented behavior;
- principal files changed (visible to the user via plain `git diff`);
- verification commands and results;
- Codex review rounds and disposition of findings;
- adversarial review result when one was required;
- per-phase usage table from `usage-report`;
- remaining risks or explicit blocked reason;
- a suggested conventional commit message (the user commits manually — this skill must not
  commit).
