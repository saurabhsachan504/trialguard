from __future__ import annotations

import os
import uuid

os.environ.setdefault("ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DEVICE_HASH_SECRET", "test-pepper")
os.environ.setdefault("PAYMENT_PROVIDER", "mock")
os.environ.setdefault("MOCK_BILLING_SECRET", "test-mock-secret")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("EMAIL_BACKEND", "console")
# Password hashing is deliberately slow in production; at the default cost it
# would dominate the test run (every signup hashes). 4 rounds keeps the code
# path identical and the suite fast.
os.environ.setdefault("BCRYPT_ROUNDS", "4")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

API = settings.API_PREFIX


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def make_device(**overrides) -> dict:
    device = {
        "installation_id": str(uuid.uuid4()),
        "platform": "Win32",
        "screen": "1920x1080x24",
        "timezone": "Asia/Calcutta",
        "language": "en-IN",
        "hardware_concurrency": 8,
        "gpu": "NVIDIA GeForce RTX 3060",
    }
    device.update(overrides)
    return device


@pytest.fixture
def device() -> dict:
    return make_device()


def register(client, email="user@example.com", password="Str0ngPass1", device=None):
    device = device or make_device()
    res = client.post(
        f"{API}/auth/signup",
        json={"email": email, "password": password, "device": device},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    return body, {"Authorization": f"Bearer {body['tokens']['access_token']}"}, device


def activate_subscription(client, headers: dict):
    """Simulate a completed payment for the authenticated caller."""
    res = client.post(
        f"{API}/billing/mock/confirm", json={"session_id": "test"}, headers=headers
    )
    assert res.status_code == 200, res.text
    return res.json()
