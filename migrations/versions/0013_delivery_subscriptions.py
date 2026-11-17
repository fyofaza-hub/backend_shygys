from alembic import op

revision = "0013_delivery_subscriptions"
down_revision = "0012_cms_controls"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE email_outbox (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        notification_id uuid NOT NULL UNIQUE REFERENCES notifications,
        user_id uuid NOT NULL REFERENCES users,
        attempts int NOT NULL DEFAULT 0,
        available_at timestamptz NOT NULL DEFAULT now(),
        sent_at timestamptz,
        last_error text
      );
      CREATE TABLE notification_subscriptions (
        user_id uuid NOT NULL REFERENCES users,
        category_id uuid NOT NULL REFERENCES creative_categories,
        kind text NOT NULL CHECK(kind IN ('events','opportunities')),
        PRIMARY KEY(user_id,category_id,kind)
      );
      CREATE TABLE demo_installation (
        id boolean PRIMARY KEY DEFAULT true CHECK(id),
        created_at timestamptz NOT NULL DEFAULT now()
      );
    """)


def downgrade():
    op.execute("DROP TABLE demo_installation,notification_subscriptions,email_outbox")
