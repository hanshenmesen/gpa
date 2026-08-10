# Replay Platform Architecture

## Outcome

GPA becomes a local-first Replay platform: recorded workflows are packaged as installable Replay plugins, stored or shared through Replay Store, parsed into an explicit intent contract, and executed inside isolated Replay Spaces through platform adapters.

## Architecture map

```text
Replay Studio / Replay Store
            |
        HTTP API
            |
   ReplayApplicationService
     /       |        \
 Registry  Intent   SpaceManager
 Adapter   Parser       |
     |        |      ReplayEngine port
 legacy     local        |
 packages  rules     PlatformAdapter
                       /   |   \
                    macOS Win Linux
```

## Domain boundaries

1. **Replay plugin** — a versioned portable manifest plus ordered semantic steps. It declares intent, variables, capabilities, permissions, source, platform constraints, and content digest.
2. **Replay registry** — lists Store records, inspects uploads, installs packages, and maps legacy `.gpa-record.zip` archives into the Replay model. It never executes desktop actions.
3. **Intent parser** — converts the task description and recorded steps into a stable `ReplayIntent`. The default parser is deterministic and offline; an optional agent adapter may enrich it without changing the schema.
4. **Compatibility planner** — selects a platform adapter, normalizes app aliases and hotkeys, reports missing capabilities, and produces a portable execution plan. Unsupported steps block before input is armed.
5. **Replay Space** — an isolated run directory and state machine (`created -> planned -> armed -> running -> terminal`) containing the plan, artifacts, logs, and cancellation signal. Store state is never shared with a running Space.
6. **Execution engine port** — consumes a validated plan. The current GPA FSM is the macOS implementation; Windows/Linux implementations can be added behind the same port.

## ego-lite principles retained

- Isolated Spaces keep concurrent agent work and user tabs/state from colliding.
- Code-level tools compose snapshot, action, wait, navigation, and capture operations behind one boundary.
- Semantic snapshots/plans reduce repeated tool round-trips.
- Existing local sessions are reused without exporting credentials.

The implementation does not copy ego-lite branding or assume its private browser runtime is available. Its public repository is MIT-licensed; GPA reuses architectural ideas and public interfaces, while desktop Replay remains an independent implementation.

## Canonical lifecycle

```text
record -> preview -> parse intent -> save Replay -> upload/publish
  -> discover -> inspect -> install -> create Space -> preflight
  -> arm -> execute -> stop/complete -> feedback
```

Every arrow is explicit and reversible until `arm`. Installation never runs a Replay.

## API surface

- `GET /api/replays` — installed Replay summaries.
- `GET /api/replays/{id}` — manifest, intent, compatibility, and steps.
- `POST /api/replays/intent` — parse an intent from goal and optional steps.
- `POST /api/replays/{id}/plan` — create an isolated Space and compatibility plan.
- `GET /api/replay-spaces/{id}` — inspect a Space without executing it.
- Existing recording, package upload, Store publish/import, arm, stop, and panic endpoints remain compatibility adapters.

## Non-negotiable invariants

- Store and upload inspection cannot emit desktop input.
- A package is validated and size-bounded before installation.
- A Replay cannot run without a local client lease, compatibility preflight, explicit arm token, and safety gates.
- Untrusted screen/page text never changes the trusted Replay intent.
- Cross-system support is capability-based and fail-closed; no silent coordinate guessing for incompatible app-bound steps.
- Each run has an isolated Space and terminal audit record.

## Delivery slices

1. Introduce the Replay domain, intent parser, platform adapter registry, Space manager, and tests.
2. Adapt existing workflows and community packages into canonical Replay manifests.
3. Add canonical Replay/Space APIs while preserving old routes.
4. Update Store and Studio views to show intent, capability, compatibility, and lifecycle state in the existing visual language.
5. Verify domain, API, legacy compatibility, server smoke, and UI route behavior.
