# Repository-provided UI review evidence

Phase 1 supports one optional, repository-owned screenshot renderer. It is
review evidence plumbing, not a generic GUI renderer: the target repository is
responsible for deterministic fixtures, meaningful states, and headless rendering.

## Repository configuration

Add `.autonomous-development.toml` at the repository root:

```toml
version = 1

[ui_review]
command = ["./.venv/bin/python", "-m", "tools.render_review_screenshots"]
timeout_seconds = 120
```

`command` is an argv array and is never interpreted by a shell. The controller
appends one argument: a new, run-owned output directory. No credentials,
network access, database access, or desktop-capture permission is added by the
framework. A missing configuration leaves existing non-visual workflows unchanged.

Repositories should also describe the direct renderer command in `AGENTS.md`.
That lets ordinary agents generate the same evidence on request without running
the autonomous workflow.

## Manifest version 1

The renderer must write `manifest.json` inside the supplied output directory:

```json
{
  "version": 1,
  "screens": [
    {
      "id": "reference-add-range",
      "path": "reference-add-range.png",
      "title": "Add reference — range data",
      "description": "Publication selected, entering a normalized range",
      "viewport": "1200x850"
    }
  ]
}
```

Only `id` and `path` are required per screen. IDs must be unique stable tokens
of at most 80 letters, digits, dots, underscores, or hyphens. `title`,
`description`, and `viewport` are optional bounded strings. Paths must be
relative, confined to the output directory, exist, and end in `.png`, `.jpg`,
or `.jpeg`. The manifest is authoritative; the controller does not guess files.

Limits are 20 images, 10 MiB per image, 50 MiB total, and 256 KiB for the
manifest. At least one screen is required for successful visual evidence.

## Review timing and artifacts

The renderer runs immediately before every independent review and every
adversarial review. It always receives a fresh invocation directory. Successful
review publication moves that directory to:

```text
review-01/screenshots/
review-02/screenshots/
adversarial-01/screenshots/
```

Each directory retains the manifest, images, and renderer stdout/stderr logs.
The corresponding review ledger entry records status, artifact paths, and
whether images were attached to Codex. Invocation-unique staging plus the
existing locked round merge prevents a concurrent or retried review from
receiving a previous round's screenshots as current evidence.

Renderer configuration errors, unavailable commands, timeouts, non-zero exits,
missing or malformed manifests, unsafe paths, missing files, unsupported formats,
and limit violations degrade visual evidence instead of failing the code review.
The review prompt names the reason and explicitly says that visual evidence was
unavailable. A successful review preserves the failed renderer's logs as the
round artifact.

## Codex and Claude runtime behavior

The controller capability-checks the installed runtime with `codex exec --help`.
When it advertises `-i, --image <FILE>...`, the controller repeats `--image FILE`
for every current-round manifest image while retaining the existing stdin prompt,
`--json`, and `--output-schema` flow. Codex CLI 0.146.1 documents and advertises
this combination. The public Codex image-input documentation supports PNG/JPEG
and repeated `--image`; it does not publish numeric image-count or request-size
limits or a provider-specific restriction. The framework's stricter bounds apply.

Provider/model vision support can still differ behind a CLI profile. If an image,
vision, or multimodal error is returned, the controller retries once without
images and changes the prompt to identify degraded evidence. A CLI without the
flag never receives image arguments.

Claude Code 2.1.181 does not advertise a local-image attachment flag for its
non-interactive prompt path (`--file` refers to hosted file resources). Phase 1
therefore does not inject screenshots into Claude implementation/fix subagents.
The paths remain available as run artifacts, and repository `AGENTS.md` guidance
can tell interactive agents with an image-viewing tool how to generate and inspect
them directly.
