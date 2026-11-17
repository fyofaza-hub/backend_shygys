from alembic import op

revision = "0006_editorial_collections"
down_revision = "0005_workflows"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE editorial_collections (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          slug text NOT NULL UNIQUE,
          kind text NOT NULL CHECK (kind IN ('exhibition','collection','route')),
          fields jsonb NOT NULL,
          work_ids uuid[] NOT NULL DEFAULT '{}',
          place_ids uuid[] NOT NULL DEFAULT '{}',
          status publication_status NOT NULL DEFAULT 'unpublished',
          author_id uuid NOT NULL REFERENCES users,
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE collaboration_requests (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          sender_id uuid NOT NULL REFERENCES users,
          creator_id uuid NOT NULL REFERENCES creators,
          message text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX collaboration_recent ON collaboration_requests(sender_id,creator_id,created_at);
    """)


def downgrade():
    op.execute("DROP TABLE collaboration_requests, editorial_collections")
