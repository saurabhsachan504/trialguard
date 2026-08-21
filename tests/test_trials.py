"""The behaviour the whole product hinges on: exactly 5 free runs per device."""
from __future__ import annotations

from app.config import settings
from tests.conftest import activate_subscription, make_device, register

API = settings.API_PREFIX


def consume(client, headers, device, **kwargs):
    return client.post(
        f"{API}/usage/consume", json={"device": device, **kwargs}, headers=headers
    )


def test_exactly_five_free_runs_then_402(client, device):
    _, headers, _ = register(client, device=device)

    for i in range(5):
        res = consume(client, headers, device)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["consumed"] is True
        assert body["granted_by"] == "trial"
        assert body["entitlement"]["trials_remaining"] == 4 - i

    sixth = consume(client, headers, device)
    assert sixth.status_code == 402
    detail = sixth.json()["detail"]
    assert detail["entitlement"]["reason"] == "trial_exhausted"
    assert "$5/month" in detail["message"]


def test_entitlement_check_does_not_spend_a_trial(client, device):
    _, headers, _ = register(client, device=device)

    for _ in range(10):
        res = client.post(
            f"{API}/entitlement/check", json={"device": device}, headers=headers
        )
        assert res.status_code == 200
        assert res.json()["trials_remaining"] == 5


def test_second_account_on_same_device_gets_no_fresh_trials(client, device):
    """A new email on the same machine must not reset the counter."""
    _, headers_a, _ = register(client, email="first@example.com", device=device)
    for _ in range(5):
        assert consume(client, headers_a, device).status_code == 200

    body_b, headers_b, _ = register(client, email="second@example.com", device=device)

    assert body_b["entitlement"]["allowed"] is False
    assert body_b["entitlement"]["reason"] == "device_trial_exhausted"
    assert body_b["entitlement"]["trials_remaining"] == 5  # account is fresh...
    assert body_b["entitlement"]["device_trials_remaining"] == 0  # ...device is not

    res = consume(client, headers_b, device)
    assert res.status_code == 402
    assert res.json()["detail"]["entitlement"]["reason"] == "device_trial_exhausted"


def test_different_device_gets_its_own_allowance(client):
    device_a, device_b = make_device(), make_device()
    _, headers, _ = register(client, email="multi@example.com", device=device_a)

    for _ in range(5):
        assert consume(client, headers, device_a).status_code == 200

    # Same account, genuinely different machine: the ACCOUNT limit still binds.
    res = consume(client, headers, device_b)
    assert res.status_code == 402
    assert res.json()["detail"]["entitlement"]["reason"] == "trial_exhausted"


def test_device_hash_ignores_volatile_fields(client):
    """Changing the label or extension version must not mint a new device."""
    base = make_device()
    _, headers, _ = register(client, device=base)
    for _ in range(5):
        assert consume(client, headers, base).status_code == 200

    same_machine = dict(base, label="My laptop", extension_version="9.9.9")
    res = consume(client, headers, same_machine)
    assert res.status_code == 402


def test_clearing_extension_storage_does_not_reset_the_machine_ledger(client):
    """The obvious attack: new email + wiped installation_id, same laptop.

    The composite device hash changes (it includes installation_id), so the
    per-device ledger is fresh each time. The coarse hardware ledger is not,
    and caps the machine at MACHINE_TRIAL_LIMIT runs in total.
    """
    hardware = {
        "platform": "Win32",
        "screen": "1920x1080x24",
        "hardware_concurrency": 8,
        "device_memory": 16,
        "gpu": "NVIDIA GeForce RTX 3060",
    }

    granted = 0
    blocked_reason = None
    for attempt in range(6):
        wiped = make_device(**hardware)  # fresh installation_id every time
        res = client.post(
            f"{API}/auth/signup",
            json={
                "email": f"wipe{attempt}@example.com",
                "password": "Str0ngPass1",
                "device": wiped,
            },
        )
        if res.status_code != 201:
            blocked_reason = f"signup:{res.status_code}"
            break
        headers = {"Authorization": f"Bearer {res.json()['tokens']['access_token']}"}
        for _ in range(5):
            run = consume(client, headers, wiped)
            if run.status_code == 200:
                granted += 1
            else:
                blocked_reason = run.json()["detail"]["entitlement"]["reason"]
                break

    assert granted == settings.MACHINE_TRIAL_LIMIT, granted
    assert blocked_reason in {"machine_trial_exhausted", "signup:409"}


def test_machine_ledger_skipped_when_fingerprint_is_too_thin(client):
    """Too few hardware traits to identify a machine -> do not lump users together."""
    thin = {"installation_id": "abcdefgh-1234", "platform": "Win32"}
    _, headers, _ = register(client, email="thin@example.com", device=thin)
    for _ in range(5):
        assert consume(client, headers, thin).status_code == 200
    res = consume(client, headers, thin)
    assert res.json()["detail"]["entitlement"]["reason"] == "trial_exhausted"


def test_account_cap_per_device_blocks_signup_farming(client, device):
    for i in range(settings.MAX_ACCOUNTS_PER_DEVICE):
        register(client, email=f"farm{i}@example.com", device=device)

    res = client.post(
        f"{API}/auth/signup",
        json={
            "email": "farm-too-many@example.com",
            "password": "Str0ngPass1",
            "device": device,
        },
    )
    assert res.status_code == 409
    assert "Too many accounts" in res.json()["detail"]


def test_idempotency_key_prevents_double_charge(client, device):
    _, headers, _ = register(client, device=device)

    first = client.post(
        f"{API}/usage/consume",
        json={"device": device},
        headers={**headers, "Idempotency-Key": "abc-123"},
    )
    assert first.status_code == 200
    assert first.json()["consumed"] is True

    retry = client.post(
        f"{API}/usage/consume",
        json={"device": device},
        headers={**headers, "Idempotency-Key": "abc-123"},
    )
    assert retry.status_code == 200
    assert retry.json()["duplicate"] is True
    assert retry.json()["consumed"] is False
    assert retry.json()["entitlement"]["trials_used"] == 1


def test_subscription_unblocks_and_does_not_consume(client, device):
    body, headers, _ = register(client, device=device)
    for _ in range(5):
        consume(client, headers, device)
    assert consume(client, headers, device).status_code == 402

    activate_subscription(client, headers)

    res = consume(client, headers, device)
    assert res.status_code == 200
    assert res.json()["granted_by"] == "subscription"
    assert res.json()["consumed"] is False
    assert res.json()["entitlement"]["plan"] == "subscription"

    for _ in range(20):
        assert consume(client, headers, device).status_code == 200


def test_usage_history_is_recorded(client, device):
    _, headers, _ = register(client, device=device)
    for _ in range(3):
        consume(client, headers, device, action="summarise")

    history = client.get(f"{API}/usage/history", headers=headers).json()
    assert len(history) == 3
    assert all(h["action"] == "summarise" for h in history)
    assert all(h["counted_against_trial"] for h in history)


def test_admin_can_grant_extra_trials(client, device, monkeypatch):
    """A per-account override raises both the account and device ceiling."""
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "ADMIN_API_KEY", "secret-admin-key")
    _, headers, _ = register(client, email="grant@example.com", device=device)
    for _ in range(5):
        consume(client, headers, device)
    assert consume(client, headers, device).status_code == 402

    res = client.post(
        f"{API}/admin/grant-trials",
        json={"email": "grant@example.com", "trial_limit": 10},
        headers={"X-Admin-Key": "secret-admin-key"},
    )
    assert res.status_code == 200
    assert res.json()["trials_used"] == 5

    res = consume(client, headers, device)
    assert res.status_code == 200
    assert res.json()["entitlement"]["trials_remaining"] == 4


def test_admin_can_reset_a_device_for_a_genuine_new_owner(client, device, monkeypatch):
    """e.g. a resold laptop: clear the machine ledger without touching accounts."""
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "ADMIN_API_KEY", "secret-admin-key")
    admin_headers = {"X-Admin-Key": "secret-admin-key"}

    _, headers_a, _ = register(client, email="old-owner@example.com", device=device)
    for _ in range(5):
        consume(client, headers_a, device)

    _, headers_b, _ = register(client, email="new-owner@example.com", device=device)
    assert consume(client, headers_b, device).status_code == 402

    from app.schemas import DeviceFingerprint
    from app.services import devices as device_service

    device_hash = device_service.fingerprint_to_hash(DeviceFingerprint(**device))
    reset = client.post(
        f"{API}/admin/reset-device-trials",
        json={"device_hash": device_hash},
        headers=admin_headers,
    )
    assert reset.status_code == 200

    assert consume(client, headers_b, device).status_code == 200
    # The original account keeps its own exhausted counter.
    assert consume(client, headers_a, device).status_code == 402


def test_admin_can_block_an_abusive_device(client, device, monkeypatch):
    from app.config import settings as live_settings

    monkeypatch.setattr(live_settings, "ADMIN_API_KEY", "secret-admin-key")

    _, headers, _ = register(client, email="abuse@example.com", device=device)

    from app.schemas import DeviceFingerprint
    from app.services import devices as device_service

    device_hash = device_service.fingerprint_to_hash(DeviceFingerprint(**device))
    client.post(
        f"{API}/admin/block-device",
        json={"device_hash": device_hash, "blocked": True, "reason": "abuse"},
        headers={"X-Admin-Key": "secret-admin-key"},
    )

    res = consume(client, headers, device)
    assert res.status_code == 403


def test_admin_requires_key(client):
    assert client.get(f"{API}/admin/stats").status_code == 403
