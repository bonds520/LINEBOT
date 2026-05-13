from sqlalchemy import Column, String, Text, DateTime, Date, Integer, Enum, Float, Boolean
from sqlalchemy.sql import func
from app.database import Base


class LineUser(Base):
    __tablename__ = "line_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    line_user_id = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    picture_url = Column(String(512), nullable=True)
    status = Column(Enum("active", "blocked"), default="active")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class UserTag(Base):
    __tablename__ = "user_tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    line_user_id = Column(String(64), nullable=False, index=True)
    tag_name = Column(String(64), nullable=False)
    color = Column(String(16), nullable=False, default="secondary")
    created_by = Column(String(128), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class MessageLog(Base):
    __tablename__ = "message_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    line_user_id = Column(String(64), nullable=False, index=True)
    direction = Column(Enum("incoming", "outgoing"), nullable=False)
    message_type = Column(String(32), nullable=False)
    content = Column(Text, nullable=True)
    line_message_id = Column(String(64), nullable=True, index=True)
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


class TodoItem(Base):
    __tablename__ = "todo_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    family_name = Column(String(255), nullable=True)       # 家屬姓名
    recipient_name = Column(String(255), nullable=True)    # 使用者（服務接受者）
    tower_number = Column(String(64), nullable=True)       # 塔/牌位編號
    line_user_id = Column(String(64), nullable=True)       # 關聯 LINE 用戶
    created_by = Column(String(128), nullable=False)       # 建立者（小編帳號）
    task_content = Column(Text, nullable=False)            # 待辦項目
    due_date = Column(Date, nullable=True)                 # 待辦日期
    due_time = Column(String(8), nullable=True)            # 待辦時間 HH:MM
    note = Column(Text, nullable=True)                     # 備註
    status = Column(Enum("pending", "done"), default="pending", index=True)
    done_at = Column(DateTime, nullable=True)              # 處理完成時間
    created_at = Column(DateTime, server_default=func.now())


class MessageQuote(Base):
    __tablename__ = "message_quotes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_log_id = Column(Integer, nullable=False, index=True)
    quote_sender = Column(String(255), nullable=True)
    quote_preview = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class PresetMessage(Base):
    __tablename__ = "preset_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(64), nullable=False)
    content = Column(Text, nullable=False)
    category = Column(String(64), nullable=True, default="一般")
    sort_order = Column(Integer, default=0)
    created_by = Column(String(128), nullable=True)
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
