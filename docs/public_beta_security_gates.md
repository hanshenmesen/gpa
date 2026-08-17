# Public Beta Security Gates

## P0 — before any Internet exposure

- Use an authenticated hosted API; never expose `demo_web/server.py` publicly.
- Add users, tenants, memberships, roles and object-level authorization.
- Require `tenant_id` on private Replay, run, artifact and device objects.
- Separate cloud control from the local execution plane.
- Pair devices with short-lived one-time codes and device-generated keys.
- Require fresh on-device approval for recording and desktop execution.
- Sign immutable Replay versions and verify signatures in the Host Agent.
- Quarantine uploads; scan archives, secrets and media in isolated workers.
- Bind community authors and moderation actors to authenticated identities.
- Store local BYOK credentials in the OS keychain, never the hosted database by
  default; store platform credentials in managed KMS.
- Define consent, retention, export and deletion for recordings and evidence.
- Route cloud web fetching through a controlled egress proxy with DNS rebinding,
  redirect and metadata-network protections.

## P1 — before invited external Beta

- Secure cookies, CSRF protection, strict origins, HTTPS and HSTS.
- Persistent distributed rate limits and abuse controls.
- Signed and notarized macOS Agent with signed updates and rollback.
- Dependency locks, SBOM, secret scanning, SAST and vulnerability scanning.
- Monitoring for auth risk, pairing abuse, upload failures, run failures and
  moderation backlog; redacted support bundles require user consent.
- Backup and restore drills with documented RPO/RTO.
- Privacy policy, terms, community rules, copyright and security contact.
- Third-party application and Host Agent security review.

## Required adversarial tests

- Cross-tenant ID enumeration for every Replay, run, device and artifact route.
- OAuth/session fixation, CSRF, MFA recovery and operator elevation.
- Expired, replayed, revoked, wrong-device and tampered Agent commands.
- Agent disconnect, update-signature failure and offline emergency stop.
- ZIP bomb, traversal, duplicate member, malformed media and parser fuzzing.
- DNS rebinding, redirect SSRF, IPv6 and cloud metadata access.
- Recording privacy cases containing passwords, API keys, email and notifications.
- Report flooding, reviewer privilege escalation and audit-log tampering.
