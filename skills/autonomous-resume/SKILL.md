---
name: autonomous-resume
description: Resume one explicit existing autonomous-development controller run after conversational context was lost. Use only when the user supplies the exact run ID and explicitly asks to continue that run; never initialize or create a run.
argument-hint: "<run-id>"
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
  - Bash(git status:*)
  - Bash(git diff:*)
  - Bash(git log:*)
  - Bash(git show:*)
  - Bash(git rev-parse:*)
  - Bash(git ls-files:*)
  - Bash(python3:*)
  - Bash(codex:*)
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

# Resume an existing autonomous-development run

Resume exactly this controller run:

> $ARGUMENTS

The invocation must supply exactly one non-empty run ID and no other argument. If it does not,
stop and request an explicit run ID without calling the controller.

## Non-negotiable boundaries

- This is Resume, never Start. Never call `controller.py init`, use `init --reuse`, or create a
  controller run.
- Treat `$ARGUMENTS` as the authoritative run ID. Do not discover it from conversation history,
  `AUTODEV_RUN_ID`, the terminal environment, or an unscoped active-run lookup.
- Invoke the controller only through the plugin root. The target repository is not expected to
  contain `controller.py`.
- Put `--run-id "$ARGUMENTS"` before every run-scoped controller subcommand, including commands
  described by a shared phase reference. Never fall back to an unscoped controller command.
- Invoke one controller command per Bash tool call and inspect its original result. Do not pipe,
  chain, redirect, wrap, or retry commands merely to inspect output or exit status.
- Preserve the existing run's snapshotted mode, runtime configuration, review budget, risk gate,
  verification requirements, and human-decision state. Never weaken gates or bypass a phase.
- Stop only when the controller reports `complete`, `complete_with_followups`, `blocked`, `cancelled`, or a genuine recorded
  human-decision pause. Never create commits unless the user explicitly requested them. Never
  push, merge, publish, deploy, rotate credentials, or modify remote infrastructure.

`disable-model-invocation` is intentionally enabled. Resume requires explicit user authorization
and an exact run ID; callers must invoke this skill directly. The VSIX Resume action does that by
submitting this skill invocation as the first prompt.

## Recovery and driver loop

1. Recover state from the controller, not prior conversation:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" --run-id "$ARGUMENTS" status --json
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" --run-id "$ARGUMENTS" continuation-context --json
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" --run-id "$ARGUMENTS" next-action --json
   ```

   If the run does not exist for this repository, stop with that controller error. Do not choose
   another run. If it is already terminal, report its state without changing it.

2. Follow `next-action` until it reports `phase: evaluate`. Read each returned reference from
   `${CLAUDE_PLUGIN_ROOT}/<reference>` and follow the existing phase contract. The shared phase
   guidance lives under `skills/autonomous-feature/references/` and covers specification,
   planning, implementation, verification, and review.

   Every controller example in a reference is shorthand. Execute it through the plugin-root
   controller and insert the explicit selector before the subcommand. For example,
   `controller.py codex --phase plan` becomes:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" --run-id "$ARGUMENTS" codex --phase plan
   ```

   After satisfying a phase completion condition, call explicit-run `next-action --json` again.

3. Respect human decisions. If status or next-action reports an existing human-decision pause,
   surface the recorded decision and stop. Review-budget exhaustion routes to completion
   disposition and does not itself require a human; `authorize-review` remains available for one
   explicit confirmation round. Do not imply the last `changes_required` review passed. After the user supplies any other decision,
   record it on the same run and continue:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" --run-id "$ARGUMENTS" resume --note "<decision supplied by user>"
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" --run-id "$ARGUMENTS" next-action --json
   ```

   Use `await-decision` only for a concrete choice, user-only authorization, or missing fact that
   cannot be safely inferred. Apply the same restrictions documented by the Start workflows.

   A terminal `blocked` run remains immutable. If the user chooses **Continue blocked run**, use
   `continue-run --intent <allow-one-more-review|resume-adversarial|continue-blocked>` on that exact
   parent id; continue only in the linked child run returned by the controller. Reuse an existing
   active child returned by the controller rather than creating another. Cancelled, archived, and
   successful runs are not blocked-continuation sources; selected durable follow-ups from a
   `complete_with_followups` run start a semantically new run via `start-followup-run`.

4. When next-action reports `phase: evaluate`, finish through the same gates and report from the
   same run:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" --run-id "$ARGUMENTS" evaluate
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" --run-id "$ARGUMENTS" status
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" --run-id "$ARGUMENTS" usage-report
   ```

## Final report

Report the continued behavior, principal files changed, verification results, review and
adversarial-review outcomes, usage table, remaining risk or blocked reason, and a suggested
conventional commit message. State the resumed run ID explicitly.
