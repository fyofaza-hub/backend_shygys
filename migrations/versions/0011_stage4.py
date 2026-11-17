from alembic import op

revision = "0011_stage4"
down_revision = "0010_stage3"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE articles ADD COLUMN cover_id uuid REFERENCES media_assets,
        ADD COLUMN meta_tags jsonb NOT NULL DEFAULT '{"kk":"","ru":"","en":""}';
      ALTER TABLE events ADD COLUMN publish_at timestamptz;
      ALTER TABLE opportunities ADD COLUMN publish_at timestamptz;
      ALTER TABLE places ADD COLUMN archived_at timestamptz;
      ALTER TABLE districts ADD COLUMN retired_at timestamptz;
      ALTER TABLE creative_categories ADD COLUMN retired_at timestamptz;
      ALTER TABLE oked ADD COLUMN retired_at timestamptz;
      CREATE TABLE tags (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        code text NOT NULL UNIQUE,
        labels jsonb NOT NULL,
        retired_at timestamptz
      );
      CREATE TABLE translation_job_options (
        job_id uuid PRIMARY KEY REFERENCES translation_jobs ON DELETE CASCADE,
        overwrite_reviewed boolean NOT NULL DEFAULT false,
        requested_by uuid REFERENCES users,
        reviewed_id uuid REFERENCES translations,
        actual_model text
      );
      CREATE INDEX events_place_public ON events(place_id) WHERE status='published';
      CREATE INDEX audit_entity_history ON audit_events(entity_id,created_at DESC);
    """)


def downgrade():
    op.execute("""
      DROP INDEX audit_entity_history,events_place_public;
      DROP TABLE translation_job_options,tags;
      ALTER TABLE oked DROP COLUMN retired_at;
      ALTER TABLE creative_categories DROP COLUMN retired_at;
      ALTER TABLE districts DROP COLUMN retired_at;
      ALTER TABLE places DROP COLUMN archived_at;
      ALTER TABLE opportunities DROP COLUMN publish_at;
      ALTER TABLE events DROP COLUMN publish_at;
      ALTER TABLE articles DROP COLUMN meta_tags,DROP COLUMN cover_id;
    """)
