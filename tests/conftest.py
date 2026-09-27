import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_LOGIN_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def database_url(tmp_path):
    """SQLite by default; set TEST_DATABASE_URL to run the same suite on PostgreSQL."""
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture()
def db(database_url, monkeypatch):
    from sqlalchemy import text

    monkeypatch.setenv("DATABASE_URL", database_url)
    from app import db as db_module

    db_module.get_engine.cache_clear()
    db_module.init_db()
    with db_module.get_db() as conn:
        for table in ("reviews", "runs", "entities", "users", "organizations"):
            conn.execute(text(f"DELETE FROM {table}"))
    yield db_module
    db_module.get_engine().dispose()
    db_module.get_engine.cache_clear()


@pytest.fixture()
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.auth import hash_password, login_throttle

    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("API_KEY", "k" * 40)
    monkeypatch.setenv("UI_LOGIN_USER", "manager")
    monkeypatch.setenv("UI_LOGIN_PASSWORD_HASH", hash_password(TEST_LOGIN_PASSWORD))
    monkeypatch.delenv("APP_ENV", raising=False)
    login_throttle._failures.clear()

    from app.main import app

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


_password_hash_cache: dict = {}


def add_user(db, username, org_id, role, must_change_password=False, password=TEST_LOGIN_PASSWORD):
    from app.auth import hash_password

    if password not in _password_hash_cache:  # bcrypt is slow on purpose; hash once per password
        _password_hash_cache[password] = hash_password(password)
    user_id = db.create_user(username, _password_hash_cache[password], org_id=org_id, role=role,
                             must_change_password=must_change_password)
    assert user_id
    return user_id


def login(client, username, password=TEST_LOGIN_PASSWORD):
    """Signs the client in as `username` (replacing any current session) and
    returns the session token so a test can switch back to it later."""
    client.cookies.clear()
    response = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return client.cookies.get("pf_session")


def use_session(client, token):
    client.cookies.clear()
    client.cookies.set("pf_session", token)


@pytest.fixture()
def org(db):
    return db.create_organization("Test Co")


@pytest.fixture()
def admin(client):
    login(client, "manager")  # the platform admin bootstrapped from UI_LOGIN_USER
    return client


@pytest.fixture()
def logged_in(client, db, org):
    """An organization owner — every permission inside their organization."""
    add_user(db, "owner1", org, "owner")
    login(client, "owner1")
    return client
