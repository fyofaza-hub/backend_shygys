from alembic import op

revision = "0004_ai_search_consent"
down_revision = "0003_staff_versions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE consents DROP CONSTRAINT consents_type_check;
        ALTER TABLE consents ADD CONSTRAINT consents_type_check CHECK
          (type IN ('data_processing','public_contacts','portfolio','marketing','gis_export','ai_translation','ai_search'));
        ALTER TABLE opportunities ADD COLUMN external_ai_search boolean NOT NULL DEFAULT false;
    """)


def downgrade():
    op.execute("""
        ALTER TABLE opportunities DROP COLUMN external_ai_search;
        ALTER TABLE consents DROP CONSTRAINT consents_type_check;
        DELETE FROM consents WHERE type='ai_search';
        ALTER TABLE consents ADD CONSTRAINT consents_type_check CHECK
          (type IN ('data_processing','public_contacts','portfolio','marketing','gis_export','ai_translation'));
    """)
