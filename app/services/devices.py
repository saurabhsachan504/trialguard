"""Device registration and the per-machine trial ledger."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Device, DeviceTrialLedger, User
from app.schemas import DeviceFingerprint
from app.security import hash_device, hash_machine, hash_mac_address


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fingerprint_to_hash(fp: DeviceFingerprint) -> str:
    """Derive the stored device hash from a client fingerprint.

    If a native helper supplied a real MAC address we key off that alone, so the
    identity survives a browser profile reset or extension reinstall. Otherwise
    we use the composite browser fingerprint.
    """
    if fp.mac_address:
        return hash_mac_address(fp.mac_address)
    return hash_device(fp.model_dump(exclude_none=True))


def fingerprint_to_machine_hash(fp: DeviceFingerprint) -> str | None:
    """Coarse hardware-only key.

    The composite device hash includes ``installation_id``, so clearing
    chrome.storage.local produces a brand new device. This second key does not,
    which is what makes a storage wipe stop being a free trial reset. It is
    lower entropy, so it is enforced with a much looser cap
    (``MACHINE_TRIAL_LIMIT``) and skipped when the client reported a real MAC.
    """
    if fp.mac_address:
        return None
    return hash_machine(fp.model_dump(exclude_none=True))


def get_ledger(db: Session, device_hash: str, *, lock: bool = False) -> DeviceTrialLedger:
    stmt = select(DeviceTrialLedger).where(
        DeviceTrialLedger.device_hash == device_hash
    )
    if lock and not settings.is_sqlite:
        stmt = stmt.with_for_update()
    ledger = db.execute(stmt).scalar_one_or_none()
    if ledger is None:
        ledger = DeviceTrialLedger(device_hash=device_hash, trials_used=0)
        db.add(ledger)
        db.flush()
    return ledger


_TOO_MANY_ACCOUNTS = (
    "Too many accounts have been created from this device. "
    "Please sign in to your existing account or subscribe."
)


def assert_device_may_register_account(
    db: Session, device_hash: str, machine_hash: str | None = None
) -> None:
    """Stop one machine from farming unlimited free accounts.

    Checked against both ledgers: the precise per-installation one and the
    coarse hardware one, which a storage wipe cannot shake off.
    """
    ledger = get_ledger(db, device_hash)
    if ledger.blocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ledger.block_reason or "This device has been blocked.",
        )
    if ledger.account_count >= settings.MAX_ACCOUNTS_PER_DEVICE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=_TOO_MANY_ACCOUNTS
        )

    if machine_hash and settings.ENFORCE_MACHINE_TRIAL_LIMIT:
        machine = get_ledger(db, machine_hash)
        if machine.blocked:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=machine.block_reason or "This device has been blocked.",
            )
        # Looser, because this key has lower entropy and could in principle be
        # shared by two different people on identical hardware.
        max_machine_accounts = settings.MAX_ACCOUNTS_PER_DEVICE * 3
        if machine.account_count >= max_machine_accounts:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=_TOO_MANY_ACCOUNTS
            )
        machine.account_count += 1
        db.flush()


def register_device(
    db: Session,
    user: User,
    fp: DeviceFingerprint,
    *,
    ip: str | None = None,
    new_account: bool = False,
) -> Device:
    """Upsert the (user, device) pair and touch the global ledger."""
    device_hash = fingerprint_to_hash(fp)
    ledger = get_ledger(db, device_hash)

    if ledger.blocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ledger.block_reason or "This device has been blocked.",
        )

    device = db.execute(
        select(Device).where(
            Device.user_id == user.id, Device.device_hash == device_hash
        )
    ).scalar_one_or_none()

    if device is None:
        existing = db.execute(
            select(func.count())
            .select_from(Device)
            .where(Device.user_id == user.id, Device.revoked.is_(False))
        ).scalar_one()
        subscribed = user.active_subscription() is not None
        # Owner/team accounts are exempt: they have to be able to test from the
        # extension, the web app and a second browser without hitting a cap
        # meant for ordinary free users.
        owner = user.email.lower() in settings.unlimited_emails
        max_devices = (
            settings.MAX_DEVICES_PER_PAID_USER
            if subscribed
            else settings.MAX_DEVICES_PER_FREE_USER
        )
        if not owner and existing >= max_devices:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"This account is already active on {max_devices} devices. "
                    "Remove one from your account settings"
                    + ("" if subscribed else " or subscribe for more.")
                ),
            )
        device = Device(
            user_id=user.id,
            device_hash=device_hash,
            label=fp.label,
            platform=fp.platform,
            extension_version=fp.extension_version,
            mac_address_hash=hash_mac_address(fp.mac_address) if fp.mac_address else None,
        )
        db.add(device)
        ledger.account_count += 1

    if device.revoked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This device has been removed from your account.",
        )

    device.last_seen_at = _now()
    device.last_ip = ip
    if fp.extension_version:
        device.extension_version = fp.extension_version
    if fp.label:
        device.label = fp.label
    ledger.last_seen_at = _now()

    db.flush()
    return device


def resolve_device(db: Session, user: User, fp: DeviceFingerprint) -> Device:
    """Look up an already-registered device, registering it on first sight."""
    return register_device(db, user, fp)
