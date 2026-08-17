# Changelog

All notable public-preview changes will be documented here. The project follows
semantic versioning once the first preview tag is published.

## Unreleased

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
