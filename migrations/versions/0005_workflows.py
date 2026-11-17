from alembic import op

revision = "0005_workflows"
down_revision = "0004_ai_search_consent"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE creators ADD COLUMN merged_into uuid REFERENCES creators;
        ALTER TABLE creators ADD CONSTRAINT creators_not_merged_into_self CHECK (merged_into<>id);
        CREATE TABLE organization_invitations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations,
            user_id uuid NOT NULL REFERENCES users,
            invited_by uuid NOT NULL REFERENCES users,
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz,
            accepted boolean
        );
        CREATE UNIQUE INDEX organization_one_invitation ON organization_invitations(org_id,user_id)
            WHERE resolved_at IS NULL;
    """)


def downgrade():
    op.execute("DROP TABLE organization_invitations; ALTER TABLE creators DROP COLUMN merged_into;")
