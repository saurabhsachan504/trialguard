"""Signup, login, token refresh, email verification and password reset."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_client_ip, get_current_user
from app.models import (
    OneTimeToken,
    RefreshToken,
    TokenPurpose,
    User,
)
from app.schemas import (
    AuthResponse,
    DeviceOut,
    EmailRequest,
    LoginRequest,
    LogoutRequest,
    MessageOut,
    PasswordChangeRequest,
    PasswordResetRequest,
    RefreshRequest,
    SignupRequest,
    TokenPair,
    UserOut,
    VerifyEmailRequest,
)
from app.security import (
    create_access_token,
    generate_one_time_token,
    generate_refresh_token,
    hash_password,
    sha256,
    validate_password_strength,
    verify_password,
)
from app.services import devices as device_service
from app.services import email as email_service
from app.services import entitlements, ratelimit

router = APIRouter(prefix="/auth", tags=["auth"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _issue_tokens(db: Session, user: User, device_hash: str | None) -> TokenPair:
    access, expires_in = create_access_token(
        user.id, extra_claims={"email": user.email}
    )
    raw_refresh, token_hash, expires_at = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            device_hash=device_hash,
            expires_at=expires_at,
        )
    )
    db.flush()
    return TokenPair(
        access_token=access, refresh_token=raw_refresh, expires_in=expires_in
    )


def _send_verification(db: Session, user: User) -> None:
    raw, token_hash, expires_at = generate_one_time_token(
        timedelta(hours=settings.EMAIL_VERIFY_TTL_HOURS)
    )
    db.add(
        OneTimeToken(
            user_id=user.id,
            purpose=TokenPurpose.email_verify,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    db.flush()
    email_service.send_verification_email(user.email, raw)


# ---------------------------------------------------------------------------
@router.post(
    "/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED
)
def signup(
    payload: SignupRequest,
    request: Request,
    db: Session = Depends(get_db),
    ip: str = Depends(get_client_ip),
):
    """Register a new account and bind the first device."""
    device_hash = device_service.fingerprint_to_hash(payload.device)
    machine_hash = device_service.fingerprint_to_machine_hash(payload.device)

    ratelimit.hit(
        db,
        f"signup:ip:{ip}",
        limit=settings.SIGNUP_RATE_LIMIT,
        window_seconds=settings.SIGNUP_RATE_WINDOW_SECONDS,
    )
    device_service.assert_device_may_register_account(db, device_hash, machine_hash)

    problems = validate_password_strength(payload.password)
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password " + "; ".join(problems) + ".",
        )

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        signup_ip=ip,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    device = device_service.register_device(
        db, user, payload.device, ip=ip, new_account=True
    )
    tokens = _issue_tokens(db, user, device.device_hash)
    _send_verification(db, user)
    ent = entitlements.build_entitlement(db, user, device.device_hash, machine_hash)

    db.commit()
    db.refresh(user)
    return AuthResponse(
        user=UserOut.model_validate(user),
        tokens=tokens,
        device_id=device.id,
        entitlement=ent,
    )


@router.post("/login", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    db: Session = Depends(get_db),
    ip: str = Depends(get_client_ip),
):
    ratelimit.hit(
        db,
        f"login:{payload.email}",
        limit=settings.LOGIN_RATE_LIMIT,
        window_seconds=settings.LOGIN_RATE_WINDOW_SECONDS,
    )
    ratelimit.hit(
        db,
        f"login:ip:{ip}",
        limit=settings.LOGIN_RATE_LIMIT * 5,
        window_seconds=settings.LOGIN_RATE_WINDOW_SECONDS,
    )

    user = db.execute(
        select(User).where(User.email == payload.email)
    ).scalar_one_or_none()

    # Same generic error either way, so the endpoint cannot be used to discover
    # which email addresses are registered.
    if user is None or not verify_password(payload.password, user.password_hash):
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )
    if not user.is_active:
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account is disabled."
        )

    device_id = None
    device_hash = None
    machine_hash = None
    if payload.device is not None:
        device = device_service.register_device(db, user, payload.device, ip=ip)
        device_id, device_hash = device.id, device.device_hash
        machine_hash = device_service.fingerprint_to_machine_hash(payload.device)

    user.last_login_at = _now()
    tokens = _issue_tokens(db, user, device_hash)
    ent = entitlements.build_entitlement(db, user, device_hash, machine_hash)

    db.commit()
    db.refresh(user)
    return AuthResponse(
        user=UserOut.model_validate(user),
        tokens=tokens,
        device_id=device_id or "",
        entitlement=ent,
    )


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    """Rotate a refresh token. Reuse of a rotated token revokes the whole family."""
    token_hash = sha256(payload.refresh_token)
    record = db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    ).scalar_one_or_none()

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token."
        )

    if not record.is_valid:
        # Replay of an already-rotated token => assume theft, kill all sessions.
        if record.replaced_by is not None:
            db.query(RefreshToken).filter(
                RefreshToken.user_id == record.user_id,
                RefreshToken.revoked_at.is_(None),
            ).update({"revoked_at": _now()}, synchronize_session=False)
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired or revoked. Please sign in again.",
        )

    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled"
        )

    new_tokens = _issue_tokens(db, user, record.device_hash)
    record.revoked_at = _now()
    record.replaced_by = sha256(new_tokens.refresh_token)
    db.commit()
    return new_tokens


@router.post("/logout", response_model=MessageOut)
def logout(
    payload: LogoutRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.all_devices:
        db.query(RefreshToken).filter(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        ).update({"revoked_at": _now()}, synchronize_session=False)
    elif payload.refresh_token:
        record = db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == sha256(payload.refresh_token),
                RefreshToken.user_id == user.id,
            )
        ).scalar_one_or_none()
        if record is not None:
            record.revoked_at = _now()
    db.commit()
    return MessageOut(detail="Signed out.")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(user: User = Depends(get_current_user)):
    return [DeviceOut.model_validate(d) for d in user.devices if not d.revoked]


@router.delete("/devices/{device_id}", response_model=MessageOut)
def revoke_device(
    device_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    device = next((d for d in user.devices if d.id == device_id), None)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    device.revoked = True
    db.commit()
    return MessageOut(detail="Device removed.")


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------
@router.post("/verify-email", response_model=MessageOut)
def verify_email(payload: VerifyEmailRequest, db: Session = Depends(get_db)):
    record = db.execute(
        select(OneTimeToken).where(
            OneTimeToken.token_hash == sha256(payload.token),
            OneTimeToken.purpose == TokenPurpose.email_verify,
        )
    ).scalar_one_or_none()
    if record is None or not record.is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This verification link is invalid or has expired.",
        )
    user = db.get(User, record.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    user.email_verified = True
    record.used_at = _now()
    db.commit()
    return MessageOut(detail="Email verified.")


@router.post("/resend-verification", response_model=MessageOut)
def resend_verification(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    if user.email_verified:
        return MessageOut(detail="Email already verified.")
    ratelimit.hit(db, f"verify:{user.id}", limit=3, window_seconds=3600)
    _send_verification(db, user)
    db.commit()
    return MessageOut(detail="Verification email sent.")


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
@router.post("/password/forgot", response_model=MessageOut)
def forgot_password(
    payload: EmailRequest,
    db: Session = Depends(get_db),
    ip: str = Depends(get_client_ip),
):
    ratelimit.hit(db, f"forgot:ip:{ip}", limit=5, window_seconds=3600)
    user = db.execute(
        select(User).where(User.email == payload.email)
    ).scalar_one_or_none()
    if user is not None:
        raw, token_hash, expires_at = generate_one_time_token(
            timedelta(minutes=settings.PASSWORD_RESET_TTL_MINUTES)
        )
        db.add(
            OneTimeToken(
                user_id=user.id,
                purpose=TokenPurpose.password_reset,
                token_hash=token_hash,
                expires_at=expires_at,
            )
        )
        db.flush()
        email_service.send_password_reset_email(user.email, raw)
    db.commit()
    # Always the same response, so the endpoint reveals nothing about who exists.
    return MessageOut(
        detail="If an account exists for that address, a reset link has been sent."
    )


@router.post("/password/reset", response_model=MessageOut)
def reset_password(payload: PasswordResetRequest, db: Session = Depends(get_db)):
    problems = validate_password_strength(payload.new_password)
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password " + "; ".join(problems) + ".",
        )
    record = db.execute(
        select(OneTimeToken).where(
            OneTimeToken.token_hash == sha256(payload.token),
            OneTimeToken.purpose == TokenPurpose.password_reset,
        )
    ).scalar_one_or_none()
    if record is None or not record.is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid or has expired.",
        )
    user = db.get(User, record.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")

    user.password_hash = hash_password(payload.new_password)
    record.used_at = _now()
    db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
    ).update({"revoked_at": _now()}, synchronize_session=False)
    db.commit()
    return MessageOut(detail="Password updated. Please sign in again.")


@router.post("/password/change", response_model=MessageOut)
def change_password(
    payload: PasswordChangeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )
    problems = validate_password_strength(payload.new_password)
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password " + "; ".join(problems) + ".",
        )
    user.password_hash = hash_password(payload.new_password)
    db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
    ).update({"revoked_at": _now()}, synchronize_session=False)
    db.commit()
    return MessageOut(detail="Password updated.")
