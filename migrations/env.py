from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import DATABASE_URL
from app.core.database import Base
from app.models import auth, rate_limit, project, prompt, connection  # noqa: F401

config = context.config
target_metadata = Base.metadata


def run_migrations_offline():
    """Generate migration SQL using the configured database dialect."""
    context.configure(url=DATABASE_URL, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={'paramstyle': 'named'})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Apply schema migrations through a dedicated non-pooled connection."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration['sqlalchemy.url'] = DATABASE_URL
    connectable = engine_from_config(configuration, prefix='sqlalchemy.', poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
