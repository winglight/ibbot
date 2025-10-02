from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import os
from typing import Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from jose import JWTError, jwt
from pydantic import BaseModel

from src.config import load_project_env
from src.utils.audit import log_audit_event

# Ensure environment configuration from `.env` is available before reading
# credentials or JWT settings at import time.  When running the application via
# ``python main.py`` the environment would previously be populated only after
# this module was imported, causing the default ``admin/change_me`` credentials
# to be used instead of values from `.env`.  Loading here guarantees that the
# configured credentials are available consistently across entrypoints.
load_project_env()

router = APIRouter()

JWT_SECRET = os.getenv('JWT_SECRET', 'please_change_me')
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM', 'HS256')
JWT_EXPIRE_DAYS = int(os.getenv('JWT_EXPIRE_DAYS', '7'))
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change_me')
OPERATOR_USERNAME = os.getenv('OPERATOR_USERNAME')
OPERATOR_PASSWORD = os.getenv('OPERATOR_PASSWORD')
AUDITOR_USERNAME = os.getenv('AUDITOR_USERNAME')
AUDITOR_PASSWORD = os.getenv('AUDITOR_PASSWORD')
VISITOR_USERNAME = os.getenv('VISITOR_USERNAME')
VISITOR_PASSWORD = os.getenv('VISITOR_PASSWORD')
READ_ONLY_SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
READ_ONLY_ALLOWED_PATHS = {
    '/api/historical_data',
    '/api/chart_data',
    '/api/current_price',
}

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = 'bearer'
    expires_in: int
    role: str = 'admin'
    is_read_only: bool = False

READ_ONLY_ROLES = {'visitor', 'auditor'}


@dataclass(frozen=True)
class UserAccount:
    username: str
    password: str
    role: str
    read_only: bool = False


def _build_user_accounts() -> list[UserAccount]:
    users: list[UserAccount] = [
        UserAccount(ADMIN_USERNAME, ADMIN_PASSWORD, 'admin', False),
    ]
    if OPERATOR_USERNAME and OPERATOR_PASSWORD:
        users.append(UserAccount(OPERATOR_USERNAME, OPERATOR_PASSWORD, 'operator', False))
    if AUDITOR_USERNAME and AUDITOR_PASSWORD:
        users.append(UserAccount(AUDITOR_USERNAME, AUDITOR_PASSWORD, 'auditor', True))
    if VISITOR_USERNAME and VISITOR_PASSWORD:
        users.append(UserAccount(VISITOR_USERNAME, VISITOR_PASSWORD, 'visitor', True))
    return users


def get_user_accounts() -> list[UserAccount]:
    """Return the configured application user accounts."""

    return _build_user_accounts()


EXEMPT_PATHS = {
    '/',
    '/static/style.css',
    '/static/js/app.js',
    '/static/js/login.js',
    '/static/js/ibkr.js',
    '/static/js/ibkr_data.js',
    '/static/js/pnlcalendar.js',
    '/static/js/notifications.js',
    '/static',
    '/api/login',
    '/api/strategy',
}

def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(days=JWT_EXPIRE_DAYS))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt, int(expires_delta.total_seconds() if expires_delta else timedelta(days=JWT_EXPIRE_DAYS).total_seconds())

async def get_current_user(request: Request):
    if request.url.path in EXEMPT_PATHS:
        return None
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Unauthorized')
    token = auth_header.split(' ', 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username = payload.get('sub')
        role = payload.get('role', 'admin')
        read_only = bool(payload.get('read_only', False) or role in READ_ONLY_ROLES)

        # Enforce read-only restrictions based on HTTP method/path
        method = request.method.upper()
        path = request.url.path
        if read_only and method not in READ_ONLY_SAFE_METHODS:
            # Allow explicit safe POST-style endpoints used purely for data fetches
            if not any(path.startswith(allowed) for allowed in READ_ONLY_ALLOWED_PATHS):
                raise HTTPException(status_code=403, detail='Read-only access')

        # Attach context for downstream handlers
        request.state.user_role = role
        request.state.is_read_only = read_only
        request.state.username = username
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail='Unauthorized')

@router.post('/api/login', response_model=TokenResponse)
async def login(req: LoginRequest):
    matched: Optional[UserAccount] = None
    for account in get_user_accounts():
        if req.username == account.username and req.password == account.password:
            matched = account
            break

    if matched is None:
        raise HTTPException(status_code=401, detail='Invalid credentials')

    token_payload = {"sub": req.username, "role": matched.role, "read_only": matched.read_only}
    token, expires_in = create_access_token(token_payload)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "role": matched.role,
        "is_read_only": matched.read_only,
    }


def require_roles(*roles: str, action: str | None = None):
    allowed_roles = {role for role in roles if role}

    async def dependency(request: Request, user: Optional[str] = Depends(get_current_user)):
        role = getattr(request.state, 'user_role', None) or ('visitor' if getattr(request.state, 'is_read_only', False) else 'admin')
        username = getattr(request.state, 'username', None) or user or 'anonymous'
        effective_action = action or f"{request.method.upper()} {request.url.path}"
        request.state.audit_action = effective_action
        request.state.audit_username = username
        request.state.audit_role = role
        request.state.audit_method = request.method.upper()
        request.state.audit_path = request.url.path

        if allowed_roles and role not in allowed_roles:
            await log_audit_event(
                request,
                action=effective_action,
                success=False,
                detail=f"forbidden: requires {'/'.join(sorted(allowed_roles))}",
            )
            if getattr(request.state, 'is_read_only', False):
                raise HTTPException(
                    status_code=403,
                    detail="Read-only accounts cannot perform this action",
                )

            detail_payload: Mapping[str, object] = {
                "error": "RBAC_FORBIDDEN",
                "message": "Insufficient permissions",
                "required_roles": sorted(allowed_roles),
                "role": role,
            }
            raise HTTPException(status_code=403, detail=detail_payload)

        return user

    return dependency


async def audit_log_success(
    request: Request,
    *,
    action: str | None = None,
    detail: str | None = None,
    extra: Mapping[str, object] | None = None,
) -> None:
    effective_action = action or getattr(request.state, 'audit_action', f"{request.method.upper()} {request.url.path}")
    await log_audit_event(request, action=effective_action, success=True, detail=detail, extra=extra)


async def audit_log_failure(
    request: Request,
    *,
    action: str | None = None,
    detail: str | None = None,
    extra: Mapping[str, object] | None = None,
) -> None:
    effective_action = action or getattr(request.state, 'audit_action', f"{request.method.upper()} {request.url.path}")
    await log_audit_event(request, action=effective_action, success=False, detail=detail, extra=extra)
