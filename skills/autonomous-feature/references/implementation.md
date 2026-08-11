# Implementation phase

Implement the accepted plan incrementally:

- follow repository conventions;
- add or update tests alongside behavior;
- keep public interfaces backward compatible unless the accepted specification says otherwise;
- include migration and rollback support when applicable;
- update user-facing and operator documentation affected by the change;
- do not create commits unless the user explicitly requested them.

Record phase progress when useful:

```bash
controller.py set-phase --phase implementing
```

Boundaries: preserve unrelated user changes; never weaken authorization, validation, tests, or
static checks to make the workflow pass.

## Structured work result and UI review selection

Before marking implementation complete, write a small JSON work result and record it:

```json
{
  "summary": "Implemented the accepted plan and focused regression coverage.",
  "ui_review": {
    "scenarios": ["reference.add-range", "reference.dark"],
    "groups": []
  }
}
```

```bash
controller.py record-work-result --kind implementation --file <work-result.json>
```

`ui_review` is optional. If the repository exposes a scenario-based UI renderer and the change
materially affects rendered UI, discover registered IDs using repository guidance (normally the
renderer's `--list`) and choose the smallest useful evidence set. Include directly affected states
and important adjacent regression risks. Do not select every scenario merely because UI code
changed, and never invent IDs. If an important changed screen lacks coverage, add or update the
repository-owned scenario where appropriate, then select it. Non-visual changes should normally
omit `ui_review` or leave it empty.
