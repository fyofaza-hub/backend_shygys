from alembic import op

revision = "0007_geo_content"
down_revision = "0006_editorial_collections"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE events ADD COLUMN organizer jsonb NOT NULL DEFAULT '{"kk":"","ru":"","en":""}';
        ALTER TABLE opportunities ADD COLUMN kind text NOT NULL DEFAULT 'program'
          CHECK (kind IN ('grant','contest','accelerator','program'));
        ALTER TABLE opportunities ADD COLUMN source_url text;
        ALTER TABLE opportunities ADD COLUMN district_id uuid REFERENCES districts;
        ALTER TABLE opportunities ADD COLUMN category_id uuid REFERENCES creative_categories;
        ALTER TABLE opportunities ADD COLUMN legal_status legal_status;
        CREATE INDEX creators_exact_geography ON creators USING gist ((public_point::geography))
          WHERE publication_status='published' AND accuracy='exact';
    """)


def downgrade():
    op.execute("""
        DROP INDEX creators_exact_geography;
        ALTER TABLE opportunities DROP COLUMN legal_status,DROP COLUMN category_id,DROP COLUMN district_id,
          DROP COLUMN source_url,DROP COLUMN kind;
        ALTER TABLE events DROP COLUMN organizer;
    """)
