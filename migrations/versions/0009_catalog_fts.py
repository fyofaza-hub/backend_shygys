from alembic import op

revision = "0009_catalog_fts"
down_revision = "0008_dashboard_presets"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE INDEX creators_catalog_fts ON creators USING gin (to_tsvector('simple',search_text))
          WHERE publication_status='published' AND moderation_status='approved';
        CREATE INDEX projects_title_fts ON projects USING gin (to_tsvector('simple',
          coalesce(approved->'title'->>'kk','') || ' ' || coalesce(approved->'title'->>'ru','') || ' ' ||
          coalesce(approved->'title'->>'en','')))
          WHERE publication_status='published' AND moderation_status='approved';
    """)


def downgrade():
    op.execute("DROP INDEX projects_title_fts,creators_catalog_fts")
