from alembic import op

revision = "0002_analytics"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE analytics_daily (
          day date NOT NULL,
          event_type text NOT NULL,
          count bigint NOT NULL,
          refreshed_at timestamptz NOT NULL,
          PRIMARY KEY (day,event_type)
        );
        CREATE INDEX analytics_event_objects ON analytics_events (created_at,event_type,object_id);
        CREATE INDEX moderation_decided ON moderation_cases (decided_at) WHERE decided_at IS NOT NULL;
    """)


def downgrade():
    op.execute("DROP INDEX moderation_decided,analytics_event_objects; DROP TABLE analytics_daily;")
