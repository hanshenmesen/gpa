# GPA Online Release Architecture

Status: implementation direction for public beta

## Product decision

GPA should ship as an installable desktop application with a Web-style UI plus
an independently operated cloud service. The public website remains a companion
surface for discovery, shared links, account flows and downloads.

- The cloud owns accounts, teams, discovery, community, publishing,
  moderation, Replay metadata, cloud audit history, and device coordination.
- The desktop application owns operating-system permissions, recording, screenshots,
  local secrets, environment inspection, desktop execution, emergency stop,
  and final local approval.
- Safe Web Replays that operate entirely inside a controlled browser sandbox
  may run in cloud workers without a Host Agent.

A pure website cannot provide GPA's full desktop value: browsers cannot obtain
durable global keyboard/mouse capture or control other desktop applications.
The first native product is a lightweight system WebView around the existing
local application service, with background workers and menu-bar controls added
as the signed beta matures. See `desktop_cloud_product_plan.md`.

## Trust boundary

```text
Hosted website and API             User device

Account / team / billing           Signed Host Agent
Community catalog                  OS permission broker
Replay metadata                    Recorder and Replay engine
Object storage                     Local approval + emergency stop
Moderation                         Local secrets and application sessions
       |                                      |
       +---- authenticated outbound link -----+
             short-lived, device-bound jobs
```

The cloud never receives general mouse/keyboard authority. Every recording or
desktop-mutating job requires a fresh approval created on the target device.
Stopping a job is always allowed; starting one is never inferred from account
login, device online state, or a saved website preference.

## Service boundaries

### Web application

- Public landing, documentation, tutorials, pricing, download.
- Authenticated dashboard, library, community, profile and team management.
- Device page showing Agent version, capabilities, permissions and last seen.
- Replay detail, compatibility result, publication versions and reproductions.

### Cloud control API

- OIDC/passkey authentication and session management.
- Tenant authorization for every object.
- Device registration and one-time pairing codes.
- Replay metadata/version APIs and signed object-upload URLs.
- Job creation, cancellation, event ingestion and immutable audit records.
- Community feedback, reports, appeals and operator-only moderation.

### Host Agent gateway

- Authenticated outbound WebSocket or long-poll connection.
- Rotating device credentials stored in the OS keychain.
- At-least-once command delivery with idempotent command IDs.
- Short expiry, target-device binding, replay-version binding.
- No arbitrary shell command or cloud-supplied executable actions.

### Object and data storage

- PostgreSQL: users, tenants, memberships, devices, Replay metadata, jobs,
  feedback, moderation and audit indexes.
- Object storage: immutable Replay packages, recordings and run artifacts.
- Queue: scan, transcode, compatibility, publication and notification work.
- Cache/rate limit store: pairing attempts, sessions and abuse protection.

## Core hosted data model

All tenant-owned rows require `tenant_id`; public records also retain their
owner and an immutable published version.

- `users`, `tenants`, `memberships`
- `devices`, `device_credentials`, `pairing_sessions`
- `replays`, `replay_versions`, `replay_artifacts`
- `jobs`, `job_events`, `local_approvals`
- `reproductions`, `compatibility_reports`
- `community_publications`, `feedback`, `reports`, `appeals`
- `moderation_actions`, `audit_events`

Object keys must be server-generated, content-addressed where practical, and
never taken directly from uploaded filenames.

## Main data flows

### Pair a device

1. Signed Agent creates a device key and requests a short-lived pairing code.
2. Signed-in user enters the code on the website.
3. Cloud binds the public key to the user's tenant and returns a rotating
   credential to the Agent.
4. Agent opens an outbound authenticated connection and sends capabilities.

### Run a desktop Replay

1. Website creates a pending job for an immutable Replay version and device.
2. Agent receives a prepare command and computes compatibility locally.
3. Website displays the result; user reviews the plan.
4. User approves on the device. Agent mints a single-use local approval ID.
5. Cloud sends the device-bound run command containing that approval ID.
6. Agent verifies expiry, command ID, version, device and local approval before
   entering the existing local Replay safety gates.
7. Agent streams redacted events and stores detailed evidence locally unless
   the user explicitly chooses to upload it.

### Publish a Replay

1. Host Agent builds and locally scans the package.
2. Cloud issues a limited signed upload URL.
3. Scanner validates archive safety, secrets, media and package schema in an
   isolated worker.
4. Publisher completes rights/privacy declarations for an immutable version.
5. Approved metadata becomes discoverable; importing never triggers a run.

## Reuse and replacement

Reuse as shared domain packages:

- `gpa/community/package.py`, `gpa/community/safety.py`
- `gpa/replay/environment.py`, `gpa/replay/gate.py`
- `gpa/replay/request.py`, `gpa/replay/worker_protocol.py`
- `gpa/recording/*`, `gpa/execution/*`, `gpa/storage/workflow.py`

Keep local-only:

- Desktop settings and OS permissions.
- Recording and Replay process workers.
- Local API keys and application sessions.
- Detailed screenshots until explicit upload consent.

Replace for cloud use:

- `demo_web/server.py` must not be exposed publicly. Split its HTTP handlers
  into a hosted API and a small loopback Agent API.
- `gpa/community/repository.py` remains useful for offline mode and fixtures,
  but hosted community state moves to transactional database repositories.
- `gpa/replay/client_lease.py` is a per-page localhost lease; hosted devices
  require authenticated durable identities and connection leases.

## Delivery phases

### Phase 0: architecture and protocol boundary

- Freeze the Host Agent command allowlist and local-approval invariant.
- Version packages, Agent protocol and cloud API independently.
- Define tenant IDs, immutable Replay versions and audit-event schemas.

### Phase 1: website beta foundation

- Hosted public Store/community with read-only anonymous browsing.
- Accounts, profiles, private cloud library and signed upload pipeline.
- Signed macOS Host Agent, pairing, device status and manual update flow.
- Website can prepare a Replay; run approval remains on the device.

### Phase 2: closed execution beta

- Live job events, cancellation, reproduction feedback and artifact consent.
- Team roles, invitations, private sharing and operator moderation console.
- Automatic Agent update with signature verification and rollback.

### Phase 3: public launch

- Billing/quotas, deletion/export, legal flows and support operations.
- Multi-region backup/restore, incident response and service-level monitoring.
- Independent security review and signed release provenance.
- Windows Agent only after native runner and permission behavior pass the same
  end-to-end safety suite as macOS.

## Public beta release gates

- No public route reaches the existing loopback-only settings or execution API.
- Every database query is tenant-scoped and covered by authorization tests.
- Device pairing is one-time, short-lived, rate-limited and revocable.
- Desktop jobs require fresh on-device approval and support emergency stop.
- Uploaded archives are size-limited, quarantined and scanned before parsing.
- Secrets and recordings are opt-in uploads with retention and deletion rules.
- Agent binaries are signed, notarized, versioned and safely updateable.
- Restore tests, incident runbooks, status page and support contact exist.

The concrete product routes are in `docs/online_product_routes.md`. The release
security checklist is maintained in `docs/public_beta_security_gates.md`.
