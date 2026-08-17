# Design

## Source of truth
- Status: Active
- Last refreshed: 2026-08-10
- Primary product surfaces: GPA Desktop, Replay Store (`/store`), Product Control Center (`/control`), and Replay Studio (`/`)
- Evidence reviewed: `demo_web/index.html`, `demo_web/store.html`, `demo_web/control.html`, `demo_web/server.py`, persisted Run History, `docs/optimization_roadmap.md`, and the public GPA repository.

## Brand
- Personality: capable, calm, local-first, technical without looking like an admin console.
- Trust signals: explicit source, permissions, platform compatibility, intent summary, step count, privacy review, and run preflight.
- Avoid: marketplace hype, opaque one-click execution, generic SaaS gradients, and mixing remote discovery with live desktop control.

## Product goals
- Goals: record a task, turn it into a portable Replay plugin, understand its intent, upload or discover it in the Store, install it locally, and replay it safely on a compatible system.
- Non-goals: background remote control without local approval, cloud storage of local secrets, or pretending that an incompatible desktop app can be replayed safely.
- Success signals: a user can record -> review intent -> save/upload -> install -> preflight -> replay; every transition has a visible status and no Store action can emit desktop input.

## Personas and jobs
- Primary personas: operators who repeat GUI work; creators who share proven workflows; reviewers who assess safety and portability.
- User jobs: capture actions, describe the goal, inspect parsed intent, publish a package, find and install a Replay, resolve variables, verify compatibility, and run or stop it.
- Key contexts of use: native desktop WebView connected to a loopback service, authenticated cloud sessions, potentially sensitive text and irreversible GUI actions.

## Information architecture
- Primary navigation: `Replay Store`, `Control Center`, and `Replay Studio`, using one fixed-height header contract so route changes do not move the page top.
- Core routes/screens: `/store` for discovery/upload/publish, `/control` for product health and evidence, and `/` for recording, installed Replays, intent review, preflight, execution, and run history.
- Content hierarchy: Store catalog -> Replay details -> Install -> Studio detail -> Intent and compatibility -> Variables -> Arm and run.

## Design principles
- Discovery and execution are separate trust zones: Store actions never execute a Replay.
- A Replay is a portable plugin, not a recording dump: it has a manifest, intent, capabilities, permissions, compatibility, variables, and steps.
- Each run gets an isolated Replay Space with its own state, artifacts, logs, and stop signal, inspired by ego-lite Spaces.
- Prefer a compressed semantic plan over repeated UI probing; screenshots remain evidence and recovery input, not the primary package format.
- Record-first, intent-aware: deterministic recorded actions are preserved while intent guides validation and recovery.
- Portability is explicit: the system reports supported, degraded, or blocked instead of silently guessing across operating systems.
- Complexity is evidence-based: a task is only presented as verified when a persisted successful run proves its step count, failures, elapsed time, and model cost.
- Semantic assertions fail closed on wrong URL, missing browser text, or incorrect clipboard output; a long sequence of unverified waits does not count as a mature task.

## Visual language
- Color: preserve GPA teal (`#245f68` family), clay accent, warm off-white surfaces, restrained green success, amber warning, and red destructive states.
- Typography: system sans-serif; compact metadata, strong 20-36px headings, and readable 13-15px body copy.
- Spacing/layout rhythm: 8px base rhythm; 24-40px Store sections; denser 10-16px Studio controls.
- Shape/radius/elevation: 10-16px radii, subtle borders, low elevation, and tinted selected/featured cards.
- Motion: only short state transitions; honor reduced motion; no autoplay.
- Imagery/iconography: reuse the diagonal GPA mark, category initials, capability chips, and simple line icons.

## Components
- Existing components to reuse: product navigation, GPA mark, chips, badges, cards, detail panels, status boxes, buttons, workflow editor, feedback regions, and safety copy.
- New/changed components: Replay manifest summary, intent card, capability/permission chips, platform compatibility matrix, Space status, upload validation result, preflight checklist, live maturity metrics, complexity leaderboard, and persisted run evidence.
- Variants and states: local/store/installed Replay; draft/ready/blocked; compatible/degraded/unsupported; idle/recording/planning/running/stopped/failed.
- Token/component ownership: page-local CSS variables stay canonical; no new frontend framework or design-system dependency.

## Accessibility
- Target standard: WCAG 2.1 AA.
- Keyboard/focus behavior: logical tab order, visible focus, cards exposed as buttons, and no hover-only content.
- Contrast/readability: metadata is at least 12px and status never relies on color alone.
- Screen-reader semantics: landmarks, headings, labels, button names, tables/lists for compatibility data, and live status regions.
- Reduced motion and sensory considerations: honor `prefers-reduced-motion`; no flashing or automatic focus jumps.

## Responsive behavior
- Supported breakpoints/devices: desktop-first at 1280px+, usable at 768px+, one-column fallback below 768px.
- Layout adaptations: Store grid collapses from three to two to one; detail panels stack; dense Studio panels become a vertical workflow.
- Touch/hover differences: 40px minimum action targets and no hover-only commands.

## Interaction states
- Loading: keep current content visible and mark the refreshing region.
- Empty: explain how to record, upload, or install the first Replay.
- Error: preserve selection, show the failed boundary and recovery action, and retain diagnostic details.
- Success: expose the next lifecycle action without starting it automatically.
- Disabled: state the missing permission, variable, client lease, or platform adapter.
- Offline/slow network: local Replays remain usable; Store refresh failure is isolated.

## Content voice
- Tone: concise, trustworthy, action-oriented.
- Terminology: `Replay Store`, `Replay Studio`, `Replay`, `插件`, `意图`, `兼容性预检`, `Space`, `录制`, `安装`, `运行`.
- Microcopy rules: explain prerequisites before actions; distinguish 上传/发布/安装/运行; never label installation as execution.

## Implementation constraints
- Framework/styling system: static HTML/CSS/JavaScript served by the Python loopback service inside a lightweight system WebView; the public website reuses the same product language.
- Architecture: the web layer calls an application service; domain objects and platform planning cannot import HTTP or desktop drivers; legacy Workflow/Community packages are adapters.
- Design-token constraints: extend current CSS variables and components instead of replacing the visual system.
- Performance constraints: Store browsing and manifest inspection cannot initialize visual models or desktop automation.
- Privacy constraints: packages exclude screenshots by default; uploads are bounded and validated before install; publishing requires explicit privacy review.
- Compatibility constraints: existing `.gpa-record.zip`, stored workflows, and old API routes remain readable while new `/api/replays/*` routes become canonical.
- Test/screenshot expectations: domain and API regression tests, full unittest suite, server smoke tests, browser-visible route checks, and no automatic desktop input during verification.

## Open questions
- [ ] Hosted identity, synchronization, and moderation implementation / product owner / required before public multi-user launch.
- [ ] Signing and trust levels for third-party Replay plugins / security owner / required before remote plugin execution.
- [ ] First supported non-macOS end-to-end runner / product owner / current implementation provides portable planning and adapter contracts, but this machine can only verify macOS execution.
