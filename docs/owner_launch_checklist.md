# GPA owner launch checklist

Engineering can prepare and verify the application, but the following items
must be owned by the person or company publishing GPA. Do not send passwords,
private keys or recovery codes to a developer or place them in this repository.

## Accounts and ownership

- Register the company/product domain and keep registrar recovery access.
- Create a cloud account with billing and MFA; add a second owner/recovery
  contact before production data is stored.
- Create managed PostgreSQL and S3-compatible object storage in the intended
  data region. Enable point-in-time recovery and object versioning/retention.
- Choose an OIDC identity provider. Configure verified email, MFA/passkeys,
  account recovery, abuse limits and the production callback URLs.
- Enrol in the Apple Developer Program as the legal publisher. Create a
  Developer ID Application certificate and a `notarytool` keychain profile.
- Create transactional email, error monitoring and uptime/status services.

## Recommended closed-beta stack

To minimize the number of moving parts, start with this managed combination:

- Cloudflare Registrar/DNS for the product domain and DNS ownership.
- Supabase for PostgreSQL, Auth/JWT signing keys and S3-compatible object
  storage. Keep server-side S3 access keys in the deployment secret manager.
- Render Web Service for the existing Dockerfile, HTTPS, custom API domain,
  health checks and rollback. Run `gpa-cloud-migrate` as the pre-deploy step.
- Apple Developer Program for the Developer ID certificate and notarization.

This is an operational recommendation, not a hard code dependency. GPA uses
standard PostgreSQL, OIDC/JWKS and S3 boundaries, so each service can be moved
later without rewriting the desktop execution engine.

## Values engineering needs (never raw account passwords)

- Public website and API domains.
- OIDC issuer, client/audience, callback URL and provider-side client secret
  supplied through the production secret manager.
- PostgreSQL application and migration-role connection strings.
- Object-storage endpoint, bucket, access role and retention region.
- Apple signing identity name and locally stored notary profile name.
- A separate 24+ character metrics-scrape token kept in the deployment secret
  manager; never reuse the session signing key.
- Legal publisher name, support address, privacy contact and data region.

## Commands to run with those values

```bash
# Reports every missing production prerequisite without printing secrets.
gpa-release-preflight all

# Applies immutable/checksummed database migrations.
gpa-cloud-migrate

# Creates the signed app/DMG when the certificate environment is configured.
./scripts/build_macos_app.sh

# Proves that a database backup actually restores.
./scripts/backup_cloud_database.sh
./scripts/verify_cloud_backup.sh /path/to/backup.dump
```

## Release approval gates

- A clean Mac installs, opens, records, preflights, replays and emergency-stops.
- Apple notarization succeeds and `spctl --assess` accepts the artifact.
- Two test tenants cannot read, mutate or enumerate each other's records.
- Revoked sessions and devices lose access immediately.
- Restore drill, key rotation and rollback are completed and timed.
- Provider-level WAF/rate limits complement the in-process limiter, and
  structured logs/aggregate metrics are connected to alerts without raw bodies.
- Privacy policy, terms, acceptable-use rules, security contact, export and
  deletion flows are published before inviting public community uploads.
- Community reports, quarantine, moderation queues and appeals have an owner.
- No remote command can create desktop authority without fresh local approval.
