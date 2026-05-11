from sqlalchemy import Column, String, Text, DateTime, Integer, Enum, Float, Boolean
from sqlalchemy.sql import func
from app.database import Base


class LineUser(Base):
    __tablename__ = "line_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    line_user_id = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    picture_url = Column(String(512), nullable=True)
    status = Column(Enum("active", "blocked"), default="active")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class MessageLog(Base):
    __tablename__ = "message_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    line_user_id = Column(String(64), nullable=False, index=True)
    direction = Column(Enum("incoming", "outgoing"), nullable=False)
    message_type = Column(String(32), nullable=False)
    content = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class QAPair(Base):
    __tablename__ = "qa_pairs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    keywords = Column(String(512), nullable=True)
    category = Column(String(64), nullable=True, default="一般")
    is_active = Column(Boolean, default=True)
    is_trained = Column(Boolean, default=False)
    trained_at = Column(DateTime, nullable=True)
    hit_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SystemUser(Base):
    __tablename__ = "system_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(128), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(Enum("admin", "user"), default="user", nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class PendingQuestion(Base):
    __tablename__ = "pending_questions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    line_user_id = Column(String(64), nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    question = Column(Text, nullable=False)
    status = Column(Enum("pending", "handled"), default="pending", index=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
