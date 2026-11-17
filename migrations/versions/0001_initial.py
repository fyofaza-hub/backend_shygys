from pathlib import Path

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    schema = Path(__file__).with_name("0001_schema.sql").read_text(encoding="utf-8")
    op.execute(schema)


def downgrade():
    op.execute("""
        DROP TRIGGER audit_no_update ON audit_events;
        DROP FUNCTION reject_audit_mutation();
        DROP TABLE profile_requests, email_tokens, translation_jobs, translations, analytics_events,
          audit_events, notifications, consents, moderation_cases, opportunities, events, articles,
          creator_versions, project_creators, projects, portfolio_items, media_assets, creator_oked,
          creator_categories, residencies, organization_members, organizations, creators, places,
          localities, districts, oked, creative_categories, user_roles, users;
        DROP TYPE role, legal_status, residency_status, moderation_status, publication_status;
    """)
