import os

# Tests use an isolated SQLite database, not a developer's MySQL database.
os.environ['DATABASE_URL'] = 'sqlite://'
# A developer's ignored .env must never turn the test suite into paid external calls.
os.environ['QUESTION_GENERATION_PROVIDER'] = ''

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base, get_db
from app.main import app

@pytest.fixture
def db_and_client():
    """Provide an isolated database-backed API client and release resources after the test."""
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    @event.listens_for(engine, 'connect')
    def enable_foreign_keys(connection, _):
        """Configure the isolated test database without touching the developer database."""
        connection.execute('PRAGMA foreign_keys=ON')

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        """Configure the isolated test database without touching the developer database."""
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield session_factory, client
    app.dependency_overrides.clear()
    engine.dispose()


