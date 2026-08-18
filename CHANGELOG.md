# Changelog

All notable public-preview changes will be documented here. The project follows
semantic versioning once the first preview tag is published.

## 0.1.0-preview.6 - 2026-08-18

### Added

- A first-party, authenticated feedback inbox on GPA Online with draft
  recovery, reference IDs, per-user history, duplicate suppression, rate
  limits and a private-security-report boundary.
- The local Agent now exposes its validated online base URL so self-hosted
  builds send product feedback to the configured control plane.

### Changed

- Website device and Replay actions share timeout-aware request handling and
  always recover their loading state after network or response failures.
- Agent commands are deduplicated and capped per user before they can fill the
  local inbox.
- Store filters use accurate labels and remain on one line on narrow screens.

### Fixed

- Seeded tutorial revisions are collapsed by stable workflow identity, fixing
  inflated Store counts and repeated cards.
- Malformed recovery configuration now fails closed instead of enabling an
  automatic Escape keypress.
- Mobile setup status badges no longer truncate short authorization states.
- Website JSON endpoints reject oversized, malformed or cross-site payloads
  with bounded reads and consistent no-store security headers.

## 0.1.0-preview.5 - 2026-08-18

### Fixed

- Diagnostic privacy flags remain explicit booleans while credential values
  are still recursively redacted.
- Invalid community-package uploads are removed before the error response is
  observable, closing a timing-sensitive temporary-file cleanup race.
- The automated PostgreSQL 17 restore drill installs matching client tools
  from the official PostgreSQL package repository.

## 0.1.0-preview.4 - 2026-08-18

### Added

- Privacy-conscious desktop update discovery with cached GitHub release
  metadata; previews remain manual-install and never change desktop authority.
- User-exportable, recursively redacted diagnostics bundles that exclude raw
  logs, recordings, screenshots, environment variables and credentials.
- Cloud request size limits, per-client rate limits, structured access events,
  protected aggregate metrics and production HSTS/CSP headers.
- Scheduled macOS compatibility smoke tests and PostgreSQL backup/restore
  drills, plus a checksummed desktop update manifest.
- Community reliability tiers based on sample size, distinct environments and
  Wilson confidence rather than raw success percentages alone.
- Public community, privacy, technical-preview terms, acceptable-use and honest
  product-status pages on GPA Online.

### Changed

- Bounded safe Replay recovery is enabled by default and can be disabled with
  `GPA_ENABLE_ERROR_RECOVERY=0`.
- The macOS release workflow can optionally import ephemeral Developer ID
  credentials, notarize and validate a signed build when publisher-owned
  secrets are configured; unsigned Apache-2.0 previews remain supported.

### Fixed

- Online-account connection errors are no longer overwritten by an incorrect
  “connected” message in Runtime Setup.
- Runtime Setup now exposes update, diagnostics and feedback actions with
  verified desktop and narrow-screen layouts.

## 0.1.0-preview.1 - 2026-08-17

### Added

- Native WebView desktop shell and unsigned macOS DMG build pipeline.
- Replay intent cleanup, evidence, compatibility, safety gates and isolated
  recorder/execution workers.
- Replay Store, Community, Setup and Control product surfaces.
- Independent GPA Cloud API foundation with PostgreSQL migrations, forced
  tenant RLS, OIDC verification and device-pairing primitives.
- Public GitHub feedback forms, governance, security policy and technical
  preview release workflow.

### Security

- Desktop automation remains disabled by default.
- Community package import and Replay execution are separated.
- Cloud commands cannot manufacture local desktop authority.
- Public preview binaries are explicitly marked unsigned and ship with
  SHA-256 checksums.
