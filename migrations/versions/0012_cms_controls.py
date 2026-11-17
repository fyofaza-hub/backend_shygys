from alembic import op

revision = "0012_cms_controls"
down_revision = "0011_stage4"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE articles ADD COLUMN gallery jsonb NOT NULL DEFAULT '[]';
      CREATE TABLE cms_presets (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL REFERENCES users,
        kind text NOT NULL CHECK(kind IN ('articles','events','opportunities')),
        name text NOT NULL,
        filters jsonb NOT NULL,
        UNIQUE(user_id,kind,name)
      );
      CREATE TABLE cms_settings (
        key text PRIMARY KEY,
        value jsonb NOT NULL
      );
    """)


def downgrade():
    op.execute("DROP TABLE cms_settings,cms_presets; ALTER TABLE articles DROP COLUMN gallery;")
