# GPA framework optimization roadmap

This note turns the two product directions into implementation tracks:
speeding up visual understanding, and opening a community record platform.

## Current framework hooks

The current runtime has a clean path to optimize without redesigning the
system:

- `gpa/core/ui_parser.py`: screenshot to `UIGraph`, currently YOLO + OCR +
  IconCLIP + Sentence-E5.
- `gpa/core/smc.py`: localizes a recorded target against the runtime graph.
- `gpa/execution/executor.py`: calls the parser during replay and asks the
  configured VLM for step decisions when needed.
- `gpa/storage/workflow.py`: stores a record as `workflow.yaml`,
  `metadata.json`, and `steps_data.json`.

## Acceleration paper scan

### OmniParser

Sources: [paper](https://arxiv.org/abs/2408.00203),
[repo](https://github.com/microsoft/OmniParser).

- WHY: GUI agents need robust screen parsing before a VLM can choose actions.
- HOW: parse screenshots into structured interactable regions and semantic
  descriptions using a detection model plus an icon caption model.
- WHAT to reuse: replace or augment `_detect_icons`, OCR-derived text nodes,
  and icon semantics in `parse_screenshot`.
- Fit: highest priority because it maps directly to our `UIGraph` builder.

### GoClick

Sources: [paper](https://arxiv.org/abs/2604.23941),
[repo](https://github.com/ZJULiHongxin/GoClick).

- WHY: full GUI grounding models are often too large for low-latency local
  execution.
- HOW: uses a lightweight GUI grounding VLM, with the paper reporting a 230M
  parameter design and a device-cloud collaboration pattern.
- WHAT to reuse: add a `local_grounder` path that receives a step description
  or target hint and returns click coordinates before falling back to SMC or
  cloud VLM.
- Fit: best candidate for fast on-device target localization.

### GUI-Actor

Source: [paper](https://arxiv.org/abs/2506.03143).

- WHY: coordinate generation is brittle across screen resolutions and layouts.
- HOW: predicts action regions through an attention-based action head and uses
  a verifier to choose among candidates.
- WHAT to reuse: add a candidate verifier over SMC or parser candidates rather
  than trusting a single best coordinate.
- Fit: medium-term route for more reliable cross-machine replay.

### FastV and TokenPacker

Sources: [FastV paper](https://arxiv.org/abs/2403.06764),
[FastV repo](https://github.com/pkunlp-icler/FastV),
[TokenPacker paper](https://arxiv.org/abs/2407.02392),
[TokenPacker repo](https://github.com/CircleRadon/TokenPacker).

- WHY: VLM image tokens dominate inference cost.
- HOW: FastV prunes redundant visual tokens in later layers; TokenPacker
  compresses visual tokens through a coarse-to-fine projector.
- WHAT to reuse: only applies once we support local open VLM backends for
  `_agent_step_decision`; less useful for hosted OpenAI API calls.
- Fit: second phase after a local model backend is introduced.

## Implemented first step

- Added an in-process LRU cache around `parse_screenshot`, controlled by
  `GPA_UI_PARSE_CACHE_SIZE` and defaulting to 16 entries. This avoids repeated
  YOLO/OCR/embedding work for identical screenshots during retries or repeated
  observations.
- Made precheck skip steps without visual subgraphs.
- Added `.gpa-record.zip` export/import as a community package format with a
  manifest, checksums, platform metadata, and path-safety checks.
- Added a standalone Replay Store at `/store` for searching and inspecting
  community workflows, saving an inert local copy, and then opening that exact
  workflow in My Replays. Publishing and package upload remain secondary Store
  actions instead of being embedded in the operational Console.
- Community imports are deliberately inert: importing validates and stores the
  workflow but never starts replay. Saving the same Store record is idempotent,
  and public publishing requires an explicit privacy confirmation.
- Hardened packages against oversized archives, excessive members, duplicate
  or undeclared members, encrypted members, checksum/size mismatches, invalid
  workflow identities, and partial imports. Catalog writes and feedback
  aggregation are atomic and concurrency-safe.

## Next acceleration milestones

1. Add a parser backend interface.
   - `builtin`: current YOLO + OCR + CLIP/E5 path.
   - `omniparser`: converts OmniParser outputs to `UINode`.
   - `hybrid`: use fast OCR/parser first, then only run CLIP on ambiguous nodes.

2. Add timing and cache metrics.
   - Track `detect_icons_ms`, `ocr_ms`, `icon_embedding_ms`,
     `text_embedding_ms`, `cache_hit`.
   - Persist metrics into run records for community benchmark comparison.

3. Add local grounding before SMC.
   - Call GoClick with `step.action`, `target_hint`, and screenshot.
   - Treat the result as a high-priority candidate.
   - Use SMC and text anchors as fallback and verifier.

4. Add candidate verification.
   - Convert SMC top candidates, text-anchor candidates, and local-grounder
     candidates into one ranked set.
   - Use a lightweight verifier to reject coordinates that do not match the
     step intent.

## Community platform shape

The local MVP below is now implemented in `gpa/community/repository.py`,
`demo_web/server.py`, `demo_web/store.html`, and `demo_web/index.html`. The
Store and My Replays are separate product surfaces. The catalog is stored
under `storage/community/records/<record_id>/`, with immutable package bytes,
record metadata, saved-local mappings, and append-only feedback events. A
hosted service can retain the same API and package contract while replacing
only the repository backend.

The platform should be centered on records as portable, testable artifacts:

1. Upload
   - User exports a `.gpa-record.zip`.
   - Server validates manifest, checksums, schema, and privacy flags.

2. Inspect
   - Show task description, steps, variables, apps, OS, and risk level.
   - Let users redact variables or OCR text before public release.

3. Replay
   - A downloader imports the package locally.
   - Runtime adapts through app activation, window sizing, text anchors, SMC,
     and local grounding.

4. Feedback
   - Replay reports success, failure step, OS, app versions, screen size, and
     parser backend.
   - The platform aggregates a compatibility matrix by environment.

5. Improve
   - High-success records become templates.
   - Failed traces become benchmark cases and training data for grounding.

## Local community API

- `GET /api/community/records?q=&tag=`: search and list records.
- `POST /api/community/records`: publish a selected local workflow or a
  base64-encoded `.gpa-record.zip`; `privacy_reviewed` must be `true`.
- `GET /api/community/records/<record_id>`: inspect metadata, aggregate stats,
  compatibility results, and recent feedback.
- `GET /api/community/records/<record_id>/download`: download the validated
  package and increment its download count.
- `POST /api/community/records/<record_id>/import`: validate and import into
  local workflow storage without executing it; repeated saves return the same
  local workflow instead of creating duplicate copies.
- `POST /api/community/records/<record_id>/feedback`: record success/failure,
  failed step, environment information, and an optional note.

## Community package manifest fields to grow

- `record_license`
- `author`
- `tags`
- `app_requirements`
- `os_requirements`
- `redaction_status`
- `benchmark_results`
- `known_failures`
- `min_gpa_version`

## Guardrails

- Treat shared records as potentially sensitive. Packages can include OCR text,
  typed defaults, app names, and embeddings.
- Never import undeclared zip members.
- Never execute a freshly imported package without showing variables and
  required apps.
- Keep record replay deterministic first, then allow model-assisted recovery
  with explicit safety gates.
