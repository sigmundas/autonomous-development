# Review phase

## Independent Codex review

```bash
controller.py codex --phase review
```

Round 1 is a full review; rounds 2+ automatically use a compact delta schema (resolved findings,
new findings, regressions, affected acceptance criteria). The controller merges each round into a
cumulative finding ledger that the completion gate consults.

Read the generated `review-NN.codex.json`. For each finding, decide a disposition:

- `accepted`; `rejected_with_evidence`; `already_resolved`; `out_of_scope_but_recorded`;
  `requires_human_decision`.

Record your triage as a machine-readable ledger so later rounds do not re-raise rejected findings.
Each entry has a semantic `fingerprint` (`file:symbol:issue`), a `status`, and a `resolution` or
`reason`. To release a high/critical finding you are rejecting (rather than fixing) from the
completion gate, also set `finding_id` to its review id (`F-N`) and use a non-blocking `status`
(`rejected`, `rejected_with_evidence`, `already_resolved`, or `out_of_scope_but_recorded`); a
finding left `open` or marked `requires_human_decision` continues to block completion:

```bash
controller.py triage --file <triage.json>
```

Also write a human-readable `triage-NN.md` with repository evidence for every rejection. Fix
accepted findings, add regression tests, rerun affected checks, and request a fresh review when a
MUST_FIX_NOW item changed the implementation. Do not silently extend the snapshotted review budget.
When the budget is exhausted, the controller routes to completion disposition; a human may still
grant exactly one run-local confirmation round with `controller.py authorize-review`.

After applying fixes, record a structured fix result when UI relevance changed:

```bash
controller.py record-work-result --kind fix --file <fix-result.json>
```

It accepts the same optional `ui_review.groups` and `ui_review.scenarios` as implementation.
Omitting `ui_review` preserves the previous review selection for the next fresh render. A new
non-empty selection replaces it. Use an explicitly empty `ui_review` only when UI evidence is no
longer relevant. Discover repository-owned IDs rather than inventing them, and keep the evidence
set focused on changed and adjacent-risk states.

## Adversarial review (high-risk gate)

When `risk.requires_adversarial_review` is set:

```bash
controller.py codex --phase adversarial
```

Address MUST_FIX_NOW actions and verify again. A `changes_required` verdict is not itself a command
to iterate indefinitely: when `next-action` reports `completion-evaluation`, inspect accepted scope,
required ACs, verification, and provenance, then create a schema-valid disposition artifact and run:

```bash
controller.py disposition --file <completion-disposition.json>
```

Classify every current concern exactly once as `MUST_FIX_NOW`, `FIX_LATER`,
`ACCEPTED_WITH_EVIDENCE`, or `HUMAN_DECISION_REQUIRED`. Never defer an unmet required AC, an
ordinary-use correctness failure, realistic current-path data loss/corruption, security/privacy/
authz failure, destructive behavior, required compatibility, failed required verification, or an
implementation that misses the accepted feature. Reserve human decisions for actual product,
scientific, destructive-policy, or high-impact authority boundaries. The controller persists
FIX_LATER items in `follow-ups.json` and `follow-ups.md`.

Completion condition: hard gates pass and every remaining finding has an explicit disposition;
the terminal result is `complete` or successful `complete_with_followups`.
