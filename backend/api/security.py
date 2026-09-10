"""Bearer-token authentication dependency shared by the REST routers.

A validated token binds the request task to one user id via the
``current_user_id`` context variable, which makes every signed Binance call
and order-log write inside that request use that account's own credentials.
"""

from typing import Any

from fastapi import HTTPException, Request

from services.order_service import current_user_id


def bearer_token(request: Request) -> str:
    """Return the raw bearer token or raise 401 when absent."""
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_REQUIRED", "message": "authentication required"},
        )
    return token.strip()


async def require_user(request: Request) -> dict[str, Any]:
    """Resolve the bearer token to a user and bind the request to that account."""
    token = bearer_token(request)
    handler = getattr(request.app.state, "db_handler", None)
    if handler is None:
        raise HTTPException(status_code=503, detail="database handler is not available")
    user = handler.get_session_user(token)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SESSION_EXPIRED",
                "message": "session is invalid or expired",
            },
        )
    current_user_id.set(int(user["id"]))
    return user
