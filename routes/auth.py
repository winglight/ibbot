from datetime import datetime, timedelta
import os
from fastapi import APIRouter, HTTPException, Request
from jose import JWTError, jwt
from pydantic import BaseModel

router = APIRouter()

JWT_SECRET = os.getenv('JWT_SECRET', 'please_change_me')
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM', 'HS256')
JWT_EXPIRE_DAYS = int(os.getenv('JWT_EXPIRE_DAYS', '7'))
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change_me')

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = 'bearer'
    expires_in: int

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
        return payload.get('sub')
    except JWTError:
        raise HTTPException(status_code=401, detail='Unauthorized')

@router.post('/api/login', response_model=TokenResponse)
async def login(req: LoginRequest):
    if req.username != ADMIN_USERNAME or req.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail='Invalid credentials')
    token, expires_in = create_access_token({"sub": req.username})
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in}
