"""User registration, sign-in and session endpoints.

Credentials submitted at sign-up are Fernet-encrypted at rest (see
``services.db_handler``) and only decrypted into process memory while the
account is signed in. Binance Demo verification is best-effort: an account
whose keys cannot be verified still logs in but cannot trade until the keys
are reconnected through ``POST /api/order-session``.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.routes import order_error
from api.security import bearer_token, require_user
from services.db_handler import hash_password, verify_password
from services.order_service import BinanceOrderError

router = APIRouter()


class SignupRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


async def _activate(db: Any, service: Any, user_id: int, api_key: str, api_secret: str) -> dict[str, Any]:
    """Store encrypted keys, activate the in-memory session and verify online."""
    db.set_user_credentials(user_id, db.encrypt_secret(api_key), db.encrypt_secret(api_secret))
    status = await service.connect_user_credentials(user_id, api_key, api_secret)
    if status.get("verified") and status.get("can_trade"):
        db.mark_credentials_verified(user_id)
    return status


async def _session_status(db: Any, service: Any, user_id: int) -> dict[str, Any]:
    """Restore decrypted keys after a restart and report the account state."""
    if not service.has_active_session(user_id):
        stored = db.get_user_credentials(user_id)
        if stored is not None:
            try:
                api_key = db.decrypt_secret(stored["api_key_enc"])
                api_secret = db.decrypt_secret(stored["api_secret_enc"])
            except ValueError as error:
                return {"credentials_configured": True, "verified": False, "runtime_session_active": False, "execution_enabled": False, "error": str(error)}
            await _activate(db, service, user_id, api_key, api_secret)
    return {
        "credentials_configured": service.credentials_configured,
        "verified": service.session_verified,
        "runtime_session_active": service.runtime_session_active,
        "execution_enabled": service.execution_enabled,
    }


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {"id": user["id"], "username": user["username"], "display_name": user["display_name"]}


@router.post("/auth/signup", status_code=201)
async def signup(payload: SignupRequest, request: Request) -> dict[str, object]:
    db = request.app.state.db_handler
    service = request.app.state.order_service
    username = payload.username.strip()
    display_name = (payload.display_name or username).strip()
    api_key = payload.api_key.strip()
    api_secret = payload.api_secret.strip()
    if not username or not api_key or not api_secret:
        raise HTTPException(status_code=422, detail="username and Binance Demo keys are required")
    try:
        user_id = db.create_user(username, display_name, hash_password(payload.password))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    status = await _activate(db, service, user_id, api_key, api_secret)
    token = db.create_session(user_id)
    user = db.get_user_by_id(user_id)
    return {"token": token, "user": _public_user(user), "session": status}


@router.post("/auth/login")
async def login(payload: LoginRequest, request: Request) -> dict[str, object]:
    db = request.app.state.db_handler
    service = request.app.state.order_service
    user = db.get_user_by_username(payload.username.strip())
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid username or password")
    stored = db.get_user_credentials(user["id"])
    if stored is None:
        raise HTTPException(status_code=409, detail="no Binance Demo keys are stored for this account; sign up again")
    try:
        api_key = db.decrypt_secret(stored["api_key_enc"])
        api_secret = db.decrypt_secret(stored["api_secret_enc"])
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    status = await _activate(db, service, user["id"], api_key, api_secret)
    token = db.create_session(user["id"])
    return {"token": token, "user": _public_user(user), "session": status}


@router.get("/auth/me")
async def me(request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, object]:
    db = request.app.state.db_handler
    service = request.app.state.order_service
    return {
        "user": _public_user(user),
        "session": await _session_status(db, service, int(user["id"])),
    }


@router.post("/auth/logout")
async def logout(request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, object]:
    db = request.app.state.db_handler
    service = request.app.state.order_service
    db.delete_session(bearer_token(request))
    service.deactivate_user_session(int(user["id"]))
    return {"logged_out": True}


@router.post("/auth/verify-binance")
async def verify_binance(request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, object]:
    """Re-run the online canTrade check for the signed-in account."""
    db = request.app.state.db_handler
    service = request.app.state.order_service
    stored = db.get_user_credentials(int(user["id"]))
    if stored is None:
        raise HTTPException(status_code=409, detail="no Binance Demo keys are stored for this account")
    try:
        api_key = db.decrypt_secret(stored["api_key_enc"])
        api_secret = db.decrypt_secret(stored["api_secret_enc"])
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    status = await _activate(db, service, int(user["id"]), api_key, api_secret)
    if not status.get("verified") or not status.get("can_trade"):
        raise order_error(BinanceOrderError(status.get("error") or "Binance Demo keys could not be verified", 403))
    return {"connected": True, "verified": True, "storage": "encrypted_database"}
