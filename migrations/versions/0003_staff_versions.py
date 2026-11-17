from alembic import op

revision = "0003_staff_versions"
down_revision = "0002_analytics"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE creator_versions ADD COLUMN submitted_by uuid REFERENCES users")


def downgrade():
    op.execute("ALTER TABLE creator_versions DROP COLUMN submitted_by")
