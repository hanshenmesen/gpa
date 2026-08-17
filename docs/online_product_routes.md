# GPA Online Product Routes

The installed desktop application is the primary product surface. The hosted
website provides public discovery, shared Replay links, accounts and downloads;
the same visual system is reused across both surfaces.

## Public website

| Route | Purpose |
| --- | --- |
| `/` | Product promise, real use cases, trust summary and Agent download |
| `/explore` | Public Replay search, collections and community contributions |
| `/replays/:slug` | Intent, permissions, compatibility, evidence, versions and reproductions |
| `/creators/:handle` | Author profile and published Replay versions |
| `/collections/:slug` | Tutorials, verified examples and maintained benchmarks |
| `/learn` | Setup, recording, publishing and safe Replay guides |
| `/trust` | Safety model, community rules, transparency and reporting |
| `/pricing` | Individual and team entitlements |
| `/download` | Signed Host Agent downloads and system requirements |

Community is a collaboration layer on Replay pages, not a separate ambiguous
product. Operator controls belong under an authenticated `/ops` surface.

## Signed-in application

| Route | Purpose |
| --- | --- |
| `/app` | Recent work, failures needing attention and device readiness |
| `/app/library` | Created, imported, forked, private and saved Replays |
| `/app/library/:id` | Draft editor, immutable versions and publication state |
| `/app/publish/:id` | Rights, privacy, artifact and release review flow |
| `/app/import` | Quarantined package upload and inspection |
| `/app/runs` | Cross-device run history |
| `/app/runs/:id` | Timeline, decisions, evidence, recovery and feedback |
| `/app/agents` | Paired devices, connection state and versions |
| `/app/agents/:id` | Capabilities, OS permissions, diagnostics and revoke |
| `/app/settings/profile` | Identity and public creator profile |
| `/app/settings/security` | Sessions, devices, export and deletion |
| `/app/settings/models` | Hosted model entitlement or local BYOK policy |
| `/app/settings/billing` | Plan, usage and invoices |

## Core user states

### Anonymous

May explore public Replays, read evidence and download approved packages.
Saving, publishing, feedback and device coordination require authentication.

### Signed in, no Agent

May use the cloud library, import and publish. Desktop actions show a clear
"connect a device" path. Safe Web tasks may run in isolated cloud workers.

### Paired Agent

The website requests a device preflight, displays the compatibility result and
creates a run proposal. The device shows the final local approval. Progress is
then streamed back to the website with explicit artifact-upload consent.

## Migration from the local prototype

- `/store` becomes `/explore` plus `/replays/:slug`.
- `/community` contributes featured content to Explore; rules move to `/trust`
  and moderation moves to `/ops/moderation`.
- `/setup` splits into `/app/agents/:id` and `/app/settings/models`.
- `/control` splits into `/app/runs` and device diagnostics.
- The current workbench remains available from the local Host Agent during the
  transition; cloud editing and execution coordination are introduced in
  separate routes rather than expanding the existing single HTML file.
