BEGIN;

ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT;

CREATE TABLE auth_sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    token_hash BYTEA NOT NULL UNIQUE,
    oidc_subject TEXT NOT NULL,
    user_agent_hash TEXT,
    ip_prefix INET,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    CHECK (expires_at > created_at)
);
CREATE INDEX idx_auth_sessions_active
ON auth_sessions (token_hash, expires_at) WHERE revoked_at IS NULL;

CREATE TABLE device_pairing_requests (
    id UUID PRIMARY KEY,
    device_code_hash BYTEA NOT NULL UNIQUE,
    user_code_hash BYTEA NOT NULL UNIQUE,
    device_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    public_key TEXT NOT NULL,
    requested_capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    approved_user_id UUID REFERENCES users(id),
    approved_tenant_id UUID REFERENCES tenants(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    approved_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    CHECK (expires_at > created_at),
    CHECK ((approved_user_id IS NULL) = (approved_tenant_id IS NULL))
);
CREATE INDEX idx_device_pairing_expiry
ON device_pairing_requests (expires_at) WHERE consumed_at IS NULL;

CREATE TABLE device_credentials (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    device_id UUID NOT NULL REFERENCES devices(id),
    credential_hash BYTEA NOT NULL UNIQUE,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    CHECK (expires_at > issued_at)
);
CREATE INDEX idx_device_credentials_active
ON device_credentials (device_id, expires_at) WHERE revoked_at IS NULL;

CREATE TABLE package_objects (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    object_key TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    byte_size BIGINT NOT NULL CHECK (byte_size >= 0),
    media_type TEXT NOT NULL,
    quarantine_status TEXT NOT NULL CHECK (
        quarantine_status IN ('pending', 'scanning', 'approved', 'rejected', 'deleted')
    ),
    retention_until TIMESTAMPTZ,
    created_by UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (tenant_id, sha256)
);
CREATE INDEX idx_package_objects_quarantine
ON package_objects (quarantine_status, created_at)
WHERE quarantine_status IN ('pending', 'scanning');

CREATE TABLE outbox_events (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    topic TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id UUID,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT
);
CREATE INDEX idx_outbox_pending
ON outbox_events (available_at, created_at) WHERE published_at IS NULL;

CREATE OR REPLACE FUNCTION gpa_current_tenant_id() RETURNS UUID
LANGUAGE sql STABLE PARALLEL SAFE
AS $$ SELECT nullif(current_setting('gpa.tenant_id', true), '')::uuid $$;

CREATE OR REPLACE FUNCTION gpa_current_user_id() RETURNS UUID
LANGUAGE sql STABLE PARALLEL SAFE
AS $$ SELECT nullif(current_setting('gpa.user_id', true), '')::uuid $$;

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenants
USING (id = gpa_current_tenant_id()) WITH CHECK (id = gpa_current_tenant_id());

ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY membership_isolation ON memberships
USING (tenant_id = gpa_current_tenant_id()) WITH CHECK (tenant_id = gpa_current_tenant_id());

ALTER TABLE devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE devices FORCE ROW LEVEL SECURITY;
CREATE POLICY device_isolation ON devices
USING (tenant_id = gpa_current_tenant_id()) WITH CHECK (tenant_id = gpa_current_tenant_id());

ALTER TABLE replays ENABLE ROW LEVEL SECURITY;
ALTER TABLE replays FORCE ROW LEVEL SECURITY;
CREATE POLICY replay_tenant_isolation ON replays
USING (tenant_id = gpa_current_tenant_id()) WITH CHECK (tenant_id = gpa_current_tenant_id());
CREATE POLICY replay_public_read ON replays FOR SELECT
USING (visibility = 'public' AND deleted_at IS NULL);

ALTER TABLE replay_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE replay_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY replay_version_isolation ON replay_versions
USING (tenant_id = gpa_current_tenant_id()) WITH CHECK (tenant_id = gpa_current_tenant_id());

ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY job_isolation ON jobs
USING (tenant_id = gpa_current_tenant_id()) WITH CHECK (tenant_id = gpa_current_tenant_id());

ALTER TABLE job_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_events FORCE ROW LEVEL SECURITY;
CREATE POLICY job_event_isolation ON job_events
USING (tenant_id = gpa_current_tenant_id()) WITH CHECK (tenant_id = gpa_current_tenant_id());

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_event_isolation ON audit_events
USING (tenant_id = gpa_current_tenant_id()) WITH CHECK (tenant_id = gpa_current_tenant_id());

ALTER TABLE auth_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth_sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY auth_session_isolation ON auth_sessions
USING (
    tenant_id = gpa_current_tenant_id()
    AND user_id = gpa_current_user_id()
)
WITH CHECK (
    tenant_id = gpa_current_tenant_id()
    AND user_id = gpa_current_user_id()
);

ALTER TABLE device_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_credentials FORCE ROW LEVEL SECURITY;
CREATE POLICY device_credential_isolation ON device_credentials
USING (tenant_id = gpa_current_tenant_id()) WITH CHECK (tenant_id = gpa_current_tenant_id());

ALTER TABLE package_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE package_objects FORCE ROW LEVEL SECURITY;
CREATE POLICY package_object_isolation ON package_objects
USING (tenant_id = gpa_current_tenant_id()) WITH CHECK (tenant_id = gpa_current_tenant_id());

ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY;
CREATE POLICY outbox_isolation ON outbox_events
USING (tenant_id = gpa_current_tenant_id()) WITH CHECK (tenant_id = gpa_current_tenant_id());

INSERT INTO schema_migrations(version) VALUES ('0002_security_and_operations')
ON CONFLICT (version) DO NOTHING;

COMMIT;
