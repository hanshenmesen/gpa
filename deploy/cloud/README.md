# GPA Cloud deployment skeleton

This directory runs the independent GPA API and PostgreSQL schema. It is safe
for a private alpha behind TLS after every release preflight check passes; it
is not a substitute for managed backups, monitoring or an identity provider.

1. Copy `.env.example` to `.env` and replace every placeholder.
2. Put a TLS reverse proxy or cloud load balancer in front of `127.0.0.1:8080`.
3. Run `gpa-release-preflight cloud` and resolve every blocker.
4. Start with `docker compose up --build`. The API container applies immutable,
   checksummed migrations before it accepts traffic.
5. Check `/health/live`; use `/health/ready` for traffic readiness.

The API port intentionally binds only to loopback on the host. Do not expose it
directly to the Internet, and do not weaken staging/production TLS validation.
Tenant-owned tables have forced PostgreSQL row-level-security policies and the
application helper sets tenant and user context per transaction. Run database
access through that helper; a missing tenant context intentionally returns no
tenant data. Pairing and session token lookup tables contain hashes rather than
raw credentials and must only be granted to the API role.

Backups and restore drills:

```bash
GPA_BACKUP_DATABASE_URL='postgresql://...' ./scripts/backup_cloud_database.sh
GPA_RESTORE_ADMIN_URL='postgresql://...' ./scripts/verify_cloud_backup.sh artifacts/backups/gpa-....dump
```

Store backups in a separate provider/account or region with encryption and
retention lock. A backup is not considered valid until the restore verifier has
successfully loaded it into a temporary database.

Identity deliberately uses an external OIDC provider. GPA does not store user
passwords. Configure issuer and audience after creating that provider; the web
login callback and API token verifier are the next integration boundary.
