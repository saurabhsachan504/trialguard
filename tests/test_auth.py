from __future__ import annotations

from app.config import settings
from tests.conftest import make_device, register

API = settings.API_PREFIX


def test_signup_returns_tokens_and_entitlement(client, device):
    body, headers, _ = register(client, device=device)

    assert body["user"]["email"] == "user@example.com"
    assert body["user"]["trials_used"] == 0
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]
    assert body["entitlement"]["allowed"] is True
    assert body["entitlement"]["trials_remaining"] == 5
    assert body["device_id"]

    me = client.get(f"{API}/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "user@example.com"


def test_password_is_never_returned_or_stored_in_clear(client, db, device):
    register(client, device=device)
    from app.models import User

    user = db.query(User).one()
    assert user.password_hash != "Str0ngPass1"
    assert user.password_hash.startswith("$2")


def test_duplicate_email_is_rejected(client):
    register(client, email="dup@example.com", device=make_device())
    res = client.post(
        f"{API}/auth/signup",
        json={
            "email": "dup@example.com",
            "password": "Str0ngPass1",
            "device": make_device(),
        },
    )
    assert res.status_code == 409


def test_email_is_case_insensitive(client):
    register(client, email="Case@Example.com", device=make_device())
    res = client.post(
        f"{API}/auth/login",
        json={"email": "case@EXAMPLE.com", "password": "Str0ngPass1"},
    )
    assert res.status_code == 200


def test_weak_password_rejected(client):
    res = client.post(
        f"{API}/auth/signup",
        json={"email": "weak@example.com", "password": "short", "device": make_device()},
    )
    assert res.status_code == 422


def test_login_wrong_password_gives_generic_error(client):
    register(client, email="a@example.com", device=make_device())
    res = client.post(
        f"{API}/auth/login", json={"email": "a@example.com", "password": "Wr0ngPass1"}
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Incorrect email or password."

    # Unknown address gives exactly the same message - no user enumeration.
    res2 = client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": "Wr0ngPass1"}
    )
    assert res2.status_code == 401
    assert res2.json()["detail"] == res.json()["detail"]


def test_protected_route_requires_token(client):
    assert client.get(f"{API}/auth/me").status_code == 401
    assert (
        client.get(f"{API}/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code
        == 401
    )


def test_refresh_rotates_and_old_token_is_dead(client, device):
    body, _, _ = register(client, device=device)
    old_refresh = body["tokens"]["refresh_token"]

    res = client.post(f"{API}/auth/refresh", json={"refresh_token": old_refresh})
    assert res.status_code == 200
    new_tokens = res.json()
    assert new_tokens["refresh_token"] != old_refresh

    # Replaying the rotated token fails and revokes the family.
    replay = client.post(f"{API}/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401

    reuse_new = client.post(
        f"{API}/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]}
    )
    assert reuse_new.status_code == 401


def test_logout_revokes_refresh_token(client, device):
    body, headers, _ = register(client, device=device)
    refresh = body["tokens"]["refresh_token"]

    assert (
        client.post(
            f"{API}/auth/logout", json={"refresh_token": refresh}, headers=headers
        ).status_code
        == 200
    )
    assert client.post(f"{API}/auth/refresh", json={"refresh_token": refresh}).status_code == 401


def test_password_reset_flow(client, db, device):
    register(client, email="reset@example.com", device=device)

    res = client.post(f"{API}/auth/password/forgot", json={"email": "reset@example.com"})
    assert res.status_code == 200

    # Unknown addresses get the identical response.
    res2 = client.post(f"{API}/auth/password/forgot", json={"email": "ghost@example.com"})
    assert res2.json() == res.json()

    from app.models import OneTimeToken

    assert db.query(OneTimeToken).count() >= 1


def test_change_password_invalidates_sessions(client, device):
    body, headers, _ = register(client, email="chg@example.com", device=device)
    res = client.post(
        f"{API}/auth/password/change",
        json={"current_password": "Str0ngPass1", "new_password": "N3wStrongPass"},
        headers=headers,
    )
    assert res.status_code == 200
    assert (
        client.post(
            f"{API}/auth/refresh", json={"refresh_token": body["tokens"]["refresh_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"{API}/auth/login",
            json={"email": "chg@example.com", "password": "N3wStrongPass"},
        ).status_code
        == 200
    )


def test_device_list_and_revoke(client, device):
    body, headers, _ = register(client, device=device)
    devices = client.get(f"{API}/auth/devices", headers=headers).json()
    assert len(devices) == 1

    res = client.delete(f"{API}/auth/devices/{devices[0]['id']}", headers=headers)
    assert res.status_code == 200
    assert client.get(f"{API}/auth/devices", headers=headers).json() == []
