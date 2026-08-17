BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tenants (
    id UUID PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE users (
    id UUID PRIMARY KEY,
    email TEXT NOT NULL,
    display_name TEXT,
    identity_subject TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX idx_users_email_active
ON users (lower(email)) WHERE deleted_at IS NULL;

CREATE TABLE memberships (
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    user_id UUID NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX idx_memberships_user ON memberships (user_id, tenant_id);

CREATE TABLE devices (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    user_id UUID NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    public_key TEXT NOT NULL,
    credential_fingerprint TEXT NOT NULL,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    permissions JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    UNIQUE (tenant_id, credential_fingerprint)
);
CREATE INDEX idx_devices_tenant_active
ON devices (tenant_id, last_seen_at DESC) WHERE revoked_at IS NULL;

CREATE TABLE replays (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    creator_user_id UUID NOT NULL REFERENCES users(id),
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL CHECK (visibility IN ('private', 'tenant', 'public')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (tenant_id, slug)
);
CREATE INDEX idx_replays_tenant_updated
ON replays (tenant_id, updated_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX idx_replays_public_updated
ON replays (updated_at DESC) WHERE visibility = 'public' AND deleted_at IS NULL;

CREATE TABLE replay_versions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    replay_id UUID NOT NULL REFERENCES replays(id),
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    package_object_key TEXT NOT NULL,
    package_sha256 TEXT NOT NULL CHECK (package_sha256 ~ '^[a-f0-9]{64}$'),
    manifest JSONB NOT NULL,
    scan_status TEXT NOT NULL CHECK (
        scan_status IN ('quarantined', 'scanning', 'approved', 'rejected')
    ),
    created_by UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    UNIQUE (replay_id, version_number),
    UNIQUE (tenant_id, package_sha256)
);
CREATE INDEX idx_replay_versions_replay
ON replay_versions (tenant_id, replay_id, version_number DESC);

CREATE TABLE jobs (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    requested_by UUID NOT NULL REFERENCES users(id),
    device_id UUID NOT NULL REFERENCES devices(id),
    replay_version_id UUID NOT NULL REFERENCES replay_versions(id),
    command_type TEXT NOT NULL CHECK (command_type IN ('prepare', 'run', 'cancel')),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'dispatched', 'awaiting_local_approval', 'running',
                   'succeeded', 'failed', 'cancelled', 'expired')
    ),
    idempotency_key TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX idx_jobs_device_pending
ON jobs (tenant_id, device_id, created_at)
WHERE status IN ('pending', 'dispatched', 'awaiting_local_approval', 'running');

CREATE TABLE job_events (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    job_id UUID NOT NULL REFERENCES jobs(id),
    sequence_number BIGINT NOT NULL CHECK (sequence_number >= 0),
    event_type TEXT NOT NULL,
    redacted_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, sequence_number)
);
CREATE INDEX idx_job_events_timeline
ON job_events (tenant_id, job_id, sequence_number);

CREATE TABLE audit_events (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    actor_user_id UUID REFERENCES users(id),
    actor_device_id UUID REFERENCES devices(id),
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id UUID,
    request_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_events_tenant_time
ON audit_events (tenant_id, occurred_at DESC);

INSERT INTO schema_migrations(version) VALUES ('0001_initial')
ON CONFLICT (version) DO NOTHING;

COMMIT;
