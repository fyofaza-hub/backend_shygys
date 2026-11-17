from alembic import op

revision = "0014_notification_topics"
down_revision = "0013_delivery_subscriptions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE events ADD COLUMN category_id uuid REFERENCES creative_categories;
      ALTER TABLE notifications ADD COLUMN dedup_key text;
      CREATE UNIQUE INDEX notification_delivery_dedup ON notifications(user_id,dedup_key)
        WHERE dedup_key IS NOT NULL;
    """)


def downgrade():
    op.execute("ALTER TABLE notifications DROP COLUMN dedup_key; ALTER TABLE events DROP COLUMN category_id")
