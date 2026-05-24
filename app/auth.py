import secrets
from datetime import datetime, timedelta
from passlib.context import CryptContext
from fastapi import Request, HTTPException
from sqlalchemy.orm import Session
from app.models import SystemUser, SystemSession

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SESSION_TTL_HOURS = 24


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_session(user_id: int, db: Session) -> str:
    token = secrets.token_hex(32)
    expires_at = datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)
    db.add(SystemSession(token=token, user_id=user_id, role="user", expires_at=expires_at))
    # 清除同一 user 的舊過期 session
    db.query(SystemSession).filter(
        SystemSession.user_id == user_id,
        SystemSession.role == "user",
        SystemSession.expires_at <= datetime.utcnow(),
    ).delete()
    db.commit()
    return token


def destroy_session(token: str, db: Session):
    db.query(SystemSession).filter(SystemSession.token == token).delete()
    db.commit()


def get_current_user(request: Request, db: Session) -> SystemUser:
    token = request.cookies.get("user_token")
    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    session = db.query(SystemSession).filter(
        SystemSession.token == token,
        SystemSession.role == "user",
        SystemSession.expires_at > datetime.utcnow(),
    ).first()
    if not session:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    user = db.query(SystemUser).filter(
        SystemUser.id == session.user_id,
        SystemUser.is_active == True,
    ).first()
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user
