from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.config import settings

url = make_url(settings.database_url).set(drivername="postgresql+psycopg")
if context.is_offline_mode():
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url).connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
