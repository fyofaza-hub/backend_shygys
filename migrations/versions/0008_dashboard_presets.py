from alembic import op

revision = "0008_dashboard_presets"
down_revision = "0007_geo_content"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE dashboard_presets (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id uuid NOT NULL REFERENCES users,
          role role NOT NULL CHECK (role IN ('analyst','admin','superadmin')),
          name text NOT NULL,
          filters jsonb NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (user_id,role,name)
        );
    """)


def downgrade():
    op.execute("DROP TABLE dashboard_presets")
