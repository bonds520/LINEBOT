import secrets
from passlib.context import CryptContext
from fastapi import Request, HTTPException
from sqlalchemy.orm import Session
from app.models import SystemUser

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_user_sessions: dict = {}


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    _user_sessions[token] = user_id
    return token


def destroy_session(token: str):
    _user_sessions.pop(token, None)


def get_current_user(request: Request, db: Session) -> SystemUser:
    token = request.cookies.get("user_token")
    user_id = _user_sessions.get(token)
    if not user_id:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    user = db.query(SystemUser).filter(SystemUser.id == user_id, SystemUser.is_active == True).first()
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user
