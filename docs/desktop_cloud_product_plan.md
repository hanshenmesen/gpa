# GPA Desktop + Cloud Product Plan

Status: active product direction

## Implemented connected preview

The current public preview uses the hosted GPA website and its managed SQLite
database as the first control-plane slice. It implements ChatGPT account sign-in,
hash-only one-time pairing, revocable 90-day device credentials, Agent heartbeat,
private library items, short-lived `replay.prepare` proposals, local environment
preflight, and a local confirmation inbox.

The local Agent polls outbound over HTTPS. Pairing claim secrets remain in the
URL fragment and are never sent in referrers or request paths; the database only
stores their SHA-256 hashes. Device credentials are stored in a mode-0600 local
file in this unsigned preview. Moving those credentials into the OS keychain,
adding signed rotation, and replacing polling with an outbound event channel are
required before calling the desktop build production-ready.

This slice deliberately does not upload screenshots, API keys, login sessions,
or executable desktop actions. Accepting a cloud Replay creates a local
`manual_review` draft with `execution_ready: false`.

## Product decision

GPA ships as an installable desktop application with a Web-style interface and
an independently operated cloud control plane.

- The desktop application is the primary product. It owns the native window,
  operating-system permissions, recording, environment inspection, local
  approvals, Replay execution, emergency stop, offline cache, and updates.
- The cloud owns identity, subscriptions, teams, community, Replay metadata,
  encrypted synchronization, model entitlements, job coordination, moderation,
  audit indexes, and product operations.
- Public web pages remain useful for marketing, documentation, shared Replay
  links, account recovery, and downloads. They are not allowed to call the
  privileged loopback API directly.
- A cloud job is a proposal. Only the installed application can grant desktop
  authority, and that authority is short-lived, device-bound, and visible.

This gives the user one coherent application without pretending that a remote
server can bypass macOS or Windows security boundaries.

## Target architecture

```text
GPA Desktop (.app / .exe)
  Native WebView shell
    -> loopback UI + application API
       -> recorder worker
       -> intent / workflow builder
       -> compatibility + safety gate
       -> isolated Replay worker
       -> local encrypted cache / OS keychain
    -> outbound HTTPS/WSS only
                |
                v
GPA Cloud
  Edge / reverse proxy
    -> identity + session API
    -> Replay/library/community API
    -> device pairing + agent gateway
    -> jobs, events, moderation, billing
    -> model gateway
  PostgreSQL + object storage + queue/cache
  background scanner / transcode / notification workers
```

The first desktop shell uses Python plus pywebview. On macOS it renders with
the operating system's WKWebView; on Windows it can use WebView2. This reuses
the existing Python engine and Web UI while keeping the installer materially
smaller than Electron. A later native rewrite is unnecessary unless profiling
shows a real limitation.

## Desktop process boundaries

### Native shell

- Starts the local service on an available loopback port.
- Opens exactly one application window and stores only WebView session state.
- Disables file URLs, remote debugging, and implicit downloads in production.
- Exposes no general JavaScript-to-Python bridge.
- Opens public external links outside the privileged local application origin.
- Stops workers and revokes desktop authority when the application exits.

### Local application service

- Serves packaged first-party assets only on loopback.
- Keeps the current client lease, heartbeats, arm tokens, and Replay gates.
- Separates read-only catalog operations from recording and execution APIs.
- Uses random loopback ports in packaged applications to avoid collisions.
- Remains usable offline for previously installed and locally created Replays.

### Worker processes

- Recorder and Replay execution remain isolated from the UI process.
- Emergency stop must work without cloud connectivity.
- A worker cannot receive arbitrary shell commands or remote executables.
- Detailed screenshots and application sessions stay local unless the user
  chooses an explicit artifact upload.

## Cloud services

### API service

Recommended initial implementation: Python FastAPI with PostgreSQL and a
transactional outbox. It can reuse GPA's existing Python domain contracts while
the local `demo_web/server.py` is gradually split into testable application
services. The public server must never import desktop drivers.

Initial API groups:

| API | Responsibility |
| --- | --- |
| `/v1/auth/*` | browser/device sessions, passkeys or OIDC callbacks |
| `/v1/me/*` | profile, security, export, deletion |
| `/v1/library/*` | private Replay metadata and immutable versions |
| `/v1/community/*` | publications, feedback, reports and collections |
| `/v1/devices/*` | pairing, revoke, capability and permission status |
| `/v1/jobs/*` | prepare, approve reference, cancel and event timeline |
| `/v1/uploads/*` | size-limited signed object upload grants |
| `/v1/models/*` | entitlement and redacted model request routing |
| `/v1/ops/*` | operator-only moderation and support tooling |

### Persistent systems

- PostgreSQL: users, organizations, memberships, devices, Replay versions,
  jobs, feedback, moderation, entitlements, audit indexes and deletion state.
- S3-compatible object storage: immutable packages, opt-in evidence, avatars,
  exports and signed installers.
- Redis or equivalent: short-lived pairing codes, rate limits, queues,
  presence and idempotency locks. Redis is never the source of truth.
- Background workers: package quarantine and scanning, media processing,
  compatibility jobs, notifications, retention and deletion.

For the first closed beta these components can run as containers on one cloud
environment, with PostgreSQL and object storage backed up independently. Before
public launch, database, object storage and backups should be separate failure
domains; the application server must remain replaceable and stateless.

## Identity and device pairing

1. The desktop application opens the system browser for account sign-in.
2. The cloud returns a one-time loopback callback or short pairing code.
3. The application generates a device key; the private key is stored in the OS
   keychain and is never uploaded.
4. The cloud binds the public key to the account and issues a rotating,
   revocable device credential.
5. The desktop application maintains an outbound authenticated WSS connection.

Account login does not grant recording or Replay execution. Those operations
still require a fresh approval in the desktop window.

## Data ownership

| Data | Canonical location | Cloud upload |
| --- | --- | --- |
| Account, team, subscription | Cloud PostgreSQL | required |
| Replay metadata and published versions | Cloud | required for sync/publish |
| Draft Replay and offline cache | Desktop | optional synchronization |
| Device private key and API provider keys | OS keychain | never |
| Raw screenshots and local app sessions | Desktop | explicit opt-in only |
| Redacted run events and compatibility result | Cloud | default when signed in |
| Full run evidence | Desktop/object storage | per-run consent and retention |

## Security invariants

- The cloud may request `prepare`, but it cannot manufacture local approval.
- Every mutating command is authenticated, short-lived, idempotent, bound to a
  device and immutable Replay version, and checked again locally.
- The public website and remote community content never execute inside the
  privileged local application origin.
- Packages are quarantined and scanned before parsing; importing never runs.
- Credentials use the OS keychain. `.env` remains a development-only fallback.
- Production cloud endpoints require HTTPS/WSS; plaintext HTTP is loopback-only.
- All tenant-owned database queries are tenant-scoped and authorization-tested.
- The signed application, helper processes and updater all have reproducible
  version metadata, notarization, rollback and revocation paths.

## Delivery plan

### Milestone 1 — desktop foundation (current)

- Native WebView window around the loopback application.
- Ephemeral port allocation and clean lifecycle shutdown.
- Desktop console entry point and optional dependency group.
- Hardened WebView defaults and persistent first-party session storage.
- Validated cloud endpoint configuration with offline-safe defaults.

Exit criteria: the application opens as a desktop window, existing Replay
tests pass, closing the window stops the local server, and desktop automation
remains disabled until explicitly enabled.

### Milestone 2 — own-server alpha (foundation implemented)

- Containerized API, checksummed PostgreSQL migration pipeline and release
  preflight command.
- Account sessions, private library and device pairing.
- Device public-key registration and outbound Agent gateway.
- Package object storage, quarantine, scanning and signed downloads.
- Admin health, backup/restore drill scripts and forced tenant RLS.

Exit criteria: two accounts cannot read each other's records; a paired device
can synchronize metadata; revoked devices lose access; restore drills pass.

### Milestone 3 — signed closed beta

- Signed and notarized macOS installer.
- System permission onboarding, menu-bar status and emergency stop.
- Prepare/compatibility/job event flow through the cloud.
- Explicit evidence upload and retention controls.
- Crash reporting, update channels and rollback.

Exit criteria: real users install without developer tooling and can record,
publish, install, preflight, approve, run and stop a Replay safely.

### Milestone 4 — public product

- Custom domain, email/passkey identity, billing and quotas.
- Community moderation, reports, appeals and creator trust levels.
- Export/deletion, legal policies, status page and support operations.
- Independent security review and incident exercises.
- Windows release after its native permission and execution suite reaches the
  same safety bar as macOS.

## Immediate engineering sequence

1. Build the unsigned local `.app`/`.dmg` and exercise it on a clean macOS user.
2. Create the production OIDC, PostgreSQL and object-storage accounts selected
   by the product owner, then run `gpa-release-preflight all`.
3. Implement the provider-specific browser callback and pair devices through
   the token-hash tables already present in migration 0002.
4. Move library/community metadata to the cloud while retaining offline cache.
5. Add package scanning, structured monitoring and signed update manifests.
6. Sign and notarize the macOS beta before enabling remote jobs.
