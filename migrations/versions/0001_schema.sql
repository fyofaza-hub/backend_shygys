CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TYPE role AS ENUM ('guest','creator','org_rep','moderator','content_editor','analyst','center_curator','admin','superadmin');
CREATE TYPE legal_status AS ENUM ('individual','sole_proprietor','legal_entity');
CREATE TYPE residency_status AS ENUM ('none','pending','active','expired','revoked');
CREATE TYPE moderation_status AS ENUM ('draft','pending_review','changes_requested','approved','rejected','suspended');
CREATE TYPE publication_status AS ENUM ('unpublished','published','hidden','archived');

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email text NOT NULL UNIQUE CHECK (email = lower(email)),
    password_hash text NOT NULL,
    verified_at timestamptz,
    disabled_at timestamptz,
    auth_version int NOT NULL DEFAULT 0,
    locale text NOT NULL DEFAULT 'kk' CHECK (locale IN ('kk','ru','en')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE user_roles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users,
    role role NOT NULL,
    scope_id uuid,
    UNIQUE NULLS NOT DISTINCT (user_id, role, scope_id)
);
CREATE TABLE districts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE,
    labels jsonb NOT NULL,
    centroid geometry(Point,4326) NOT NULL,
    geom geometry(MultiPolygon,4326)
);
CREATE INDEX districts_geom ON districts USING gist (geom);
CREATE TABLE localities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE,
    district_id uuid NOT NULL REFERENCES districts,
    labels jsonb NOT NULL,
    centroid geometry(Point,4326) NOT NULL,
    geom geometry(MultiPolygon,4326),
    UNIQUE (id, district_id)
);
CREATE INDEX localities_geom ON localities USING gist (geom);
CREATE TABLE places (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    locality_id uuid NOT NULL REFERENCES localities,
    labels jsonb NOT NULL,
    kind text NOT NULL CHECK (kind IN ('studio','workshop','gallery','coworking','center')),
    geo_point geometry(Point,4326) NOT NULL,
    is_public boolean NOT NULL DEFAULT false,
    verified_at timestamptz
);
CREATE INDEX places_geo ON places USING gist (geo_point) WHERE is_public AND verified_at IS NOT NULL;
CREATE TABLE creative_categories (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE,
    labels jsonb NOT NULL
);
CREATE TABLE oked (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL,
    labels jsonb NOT NULL,
    version text NOT NULL,
    UNIQUE (code,version)
);
CREATE TABLE creators (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL UNIQUE REFERENCES users,
    slug text NOT NULL UNIQUE,
    public_name text NOT NULL DEFAULT '',
    legal_status legal_status NOT NULL DEFAULT 'individual',
    legal_verified boolean NOT NULL DEFAULT false,
    moderation_status moderation_status NOT NULL DEFAULT 'draft',
    publication_status publication_status NOT NULL DEFAULT 'unpublished',
    approved jsonb,
    district_id uuid REFERENCES districts,
    locality_id uuid,
    place_id uuid REFERENCES places,
    public_point geometry(Point,4326),
    accuracy text CHECK (accuracy IN ('exact','locality','district')),
    phone text,
    email text,
    phone_public boolean NOT NULL DEFAULT false,
    email_public boolean NOT NULL DEFAULT false,
    available_for_collaboration boolean NOT NULL DEFAULT false,
    search_text text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (locality_id,district_id) REFERENCES localities (id,district_id)
);
CREATE INDEX creators_geo ON creators USING gist (public_point) WHERE publication_status = 'published';
CREATE INDEX creators_search ON creators USING gin (search_text gin_trgm_ops) WHERE publication_status = 'published';
CREATE INDEX creators_catalog ON creators (district_id, legal_status) WHERE publication_status = 'published';
CREATE INDEX creators_phone ON creators (phone);
CREATE INDEX creators_email ON creators (email);
CREATE TABLE organizations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    legal_type legal_status NOT NULL CHECK (legal_type <> 'individual'),
    display_name jsonb NOT NULL,
    verification_state moderation_status NOT NULL DEFAULT 'pending_review',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE organization_members (
    org_id uuid NOT NULL REFERENCES organizations,
    user_id uuid NOT NULL REFERENCES users,
    role text NOT NULL CHECK (role IN ('owner','member')),
    verified_at timestamptz,
    PRIMARY KEY (org_id,user_id)
);
CREATE TABLE residencies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    creator_id uuid REFERENCES creators,
    org_id uuid REFERENCES organizations,
    center_id uuid NOT NULL REFERENCES places,
    starts_on date NOT NULL,
    ends_on date NOT NULL CHECK (ends_on >= starts_on),
    status residency_status NOT NULL DEFAULT 'pending',
    curator_id uuid REFERENCES users,
    admin_id uuid REFERENCES users,
    CHECK (num_nonnulls(creator_id,org_id)=1)
);
CREATE INDEX residencies_creator ON residencies (creator_id, status);
CREATE TABLE creator_categories (
    creator_id uuid NOT NULL REFERENCES creators,
    category_id uuid NOT NULL REFERENCES creative_categories,
    PRIMARY KEY (creator_id,category_id)
);
CREATE INDEX creator_categories_lookup ON creator_categories (category_id,creator_id);
CREATE TABLE creator_oked (
    creator_id uuid NOT NULL REFERENCES creators,
    oked_id uuid NOT NULL REFERENCES oked,
    PRIMARY KEY (creator_id,oked_id)
);
CREATE INDEX creator_oked_lookup ON creator_oked (oked_id,creator_id);
CREATE TABLE media_assets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES users,
    mime text NOT NULL,
    path text NOT NULL UNIQUE,
    bytes int NOT NULL CHECK (bytes > 0),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','ready','failed')),
    variants jsonb NOT NULL DEFAULT '{}',
    attempts int NOT NULL DEFAULT 0,
    retry_at timestamptz NOT NULL DEFAULT now(),
    rights text,
    alt_text jsonb NOT NULL DEFAULT '{"kk":"","ru":"","en":""}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE portfolio_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    creator_id uuid NOT NULL REFERENCES creators,
    category_id uuid REFERENCES creative_categories,
    cover_id uuid REFERENCES media_assets,
    approved jsonb,
    moderation_status moderation_status NOT NULL DEFAULT 'draft',
    publication_status publication_status NOT NULL DEFAULT 'unpublished',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX portfolio_items_creator ON portfolio_items (creator_id,publication_status);
CREATE TABLE projects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_creator_id uuid NOT NULL REFERENCES creators,
    organization_id uuid REFERENCES organizations,
    approved jsonb,
    moderation_status moderation_status NOT NULL DEFAULT 'draft',
    publication_status publication_status NOT NULL DEFAULT 'unpublished',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE project_creators (
    project_id uuid NOT NULL REFERENCES projects,
    creator_id uuid NOT NULL REFERENCES creators,
    confirmed_at timestamptz,
    PRIMARY KEY (project_id,creator_id)
);
CREATE TABLE creator_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    creator_id uuid NOT NULL REFERENCES creators,
    portfolio_item_id uuid REFERENCES portfolio_items,
    project_id uuid REFERENCES projects,
    payload jsonb NOT NULL,
    diff jsonb NOT NULL DEFAULT '{}',
    status moderation_status NOT NULL DEFAULT 'draft',
    reviewer_id uuid REFERENCES users,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(portfolio_item_id,project_id) <= 1)
);
CREATE INDEX creator_versions_target ON creator_versions (creator_id,portfolio_item_id,project_id,created_at DESC);
CREATE UNIQUE INDEX creator_versions_one_pending ON creator_versions (creator_id,portfolio_item_id,project_id)
    NULLS NOT DISTINCT WHERE status='pending_review';

CREATE TABLE articles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    author_id uuid NOT NULL REFERENCES users,
    slug text NOT NULL UNIQUE,
    type text NOT NULL CHECK (type IN ('news','article','story','page')),
    source_locale text NOT NULL CHECK (source_locale IN ('kk','ru','en')),
    fields jsonb NOT NULL,
    media_ids uuid[] NOT NULL DEFAULT '{}',
    status publication_status NOT NULL DEFAULT 'unpublished',
    approved jsonb,
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    author_id uuid NOT NULL REFERENCES users,
    fields jsonb NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL CHECK (ends_at >= starts_at),
    place_id uuid REFERENCES places,
    registration_url text,
    status publication_status NOT NULL DEFAULT 'unpublished',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE opportunities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    author_id uuid NOT NULL REFERENCES users,
    fields jsonb NOT NULL,
    eligibility jsonb NOT NULL,
    deadline timestamptz,
    url text,
    status publication_status NOT NULL DEFAULT 'unpublished',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE moderation_cases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id uuid NOT NULL UNIQUE REFERENCES creator_versions ON DELETE CASCADE,
    assigned_to uuid REFERENCES users,
    decision moderation_status NOT NULL DEFAULT 'pending_review',
    reviewer_id uuid REFERENCES users,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz
);
CREATE INDEX moderation_queue ON moderation_cases (decision,created_at);
CREATE TABLE consents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users,
    type text NOT NULL CHECK (type IN ('data_processing','public_contacts','portfolio','marketing','gis_export','ai_translation')),
    policy_version text NOT NULL,
    given_at timestamptz NOT NULL DEFAULT now(),
    withdrawn_at timestamptz
);
CREATE UNIQUE INDEX consent_active ON consents (user_id,type) WHERE withdrawn_at IS NULL;
CREATE TABLE notifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users,
    template_id text NOT NULL,
    locale text NOT NULL,
    context jsonb NOT NULL DEFAULT '{}',
    read_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id uuid REFERENCES users,
    action text NOT NULL,
    entity_id uuid,
    context jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION reject_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$;
CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_mutation();
CREATE TABLE analytics_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type text NOT NULL,
    object_id uuid,
    properties jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX analytics_period ON analytics_events (event_type,created_at);
CREATE TABLE translations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type text NOT NULL CHECK (entity_type IN ('article','project','creator')),
    entity_id uuid NOT NULL,
    locale text NOT NULL CHECK (locale IN ('kk','ru','en')),
    source_hash text NOT NULL,
    fields_json jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('original','ai_draft','human_reviewed','published')),
    author_id uuid REFERENCES users,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX translations_history ON translations (entity_type,entity_id,locale,created_at DESC);
CREATE TABLE translation_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type text NOT NULL CHECK (entity_type IN ('article','project','creator')),
    entity_id uuid NOT NULL,
    source_hash text NOT NULL,
    source_locale text NOT NULL CHECK (source_locale IN ('kk','ru','en')),
    target_locale text NOT NULL CHECK (target_locale IN ('kk','ru','en') AND target_locale <> source_locale),
    model text NOT NULL,
    provider text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','complete','failed','stale','protected')),
    error_code text,
    attempts int NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (entity_type,entity_id,source_hash,source_locale,target_locale,model,provider)
);
CREATE TABLE email_tokens (
    digest text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users,
    purpose text NOT NULL CHECK (purpose IN ('verify','reset','change')),
    target_email text,
    expires_at timestamptz NOT NULL,
    used_at timestamptz
);
CREATE TABLE profile_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users,
    kind text NOT NULL CHECK (kind IN ('export','close')),
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);
