from alembic import op

revision = "0010_stage3"
down_revision = "0009_catalog_fts"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE media_assets ADD COLUMN width int, ADD COLUMN height int;
        ALTER TABLE projects ADD COLUMN slug text DEFAULT gen_random_uuid()::text;
        ALTER TABLE projects ALTER COLUMN slug SET NOT NULL;
        ALTER TABLE projects ADD CONSTRAINT projects_slug_unique UNIQUE(slug);
        CREATE TABLE portfolio_pins (
          creator_id uuid NOT NULL REFERENCES creators,
          work_id uuid NOT NULL REFERENCES portfolio_items,
          slot smallint NOT NULL CHECK(slot BETWEEN 1 AND 3),
          PRIMARY KEY(creator_id,slot),
          UNIQUE(creator_id,work_id)
        );
        CREATE INDEX portfolio_view_count ON analytics_events(object_id)
          WHERE event_type='portfolio_item.view';
    """)


def downgrade():
    op.execute("""
        DROP INDEX portfolio_view_count;
        DROP TABLE portfolio_pins;
        ALTER TABLE projects DROP COLUMN slug;
        ALTER TABLE media_assets DROP COLUMN width,DROP COLUMN height;
    """)
