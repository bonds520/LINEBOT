from linebot.v3.messaging import (
    ApiClient, MessagingApi, Configuration,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from sqlalchemy.orm import Session
from app.models import LineUser, MessageLog, QAPair, PendingQuestion
from app.matcher import find_best_match
import os


def get_messaging_api() -> MessagingApi:
    configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
    return MessagingApi(ApiClient(configuration))


def upsert_user(db: Session, line_user_id: str, display_name: str = None, picture_url: str = None):
    user = db.query(LineUser).filter(LineUser.line_user_id == line_user_id).first()
    if not user:
        user = LineUser(
            line_user_id=line_user_id,
            display_name=display_name,
            picture_url=picture_url,
        )
        db.add(user)
    else:
        if display_name:
            user.display_name = display_name
        if picture_url:
            user.picture_url = picture_url
    db.commit()
    return user


def log_message(db: Session, line_user_id: str, direction: str, message_type: str, content: str = None):
    log = MessageLog(
        line_user_id=line_user_id,
        direction=direction,
        message_type=message_type,
        content=content,
    )
    db.add(log)
    db.commit()


def fetch_profile(messaging_api: MessagingApi, user_id: str):
    try:
        profile = messaging_api.get_profile(user_id)
        return profile.display_name, profile.picture_url
    except Exception:
        return None, None


def handle_text_message(event, db: Session):
    user_id = event.source.user_id
    text = event.message.text

    messaging_api = get_messaging_api()
    display_name, picture_url = fetch_profile(messaging_api, user_id)
    user = upsert_user(db, user_id, display_name, picture_url)
    log_message(db, user_id, "incoming", "text", text)

    result = find_best_match(text, db)

    if result:
        qa, score = result
        reply_text = qa.answer
        qa.hit_count += 1
        db.commit()
    else:
        reply_text = "您的問題已收到，將由客服人員儘快為您回覆，感謝您的耐心等候！"
        pending = PendingQuestion(
            line_user_id=user_id,
            display_name=user.display_name if user else None,
            question=text,
        )
        db.add(pending)
        db.commit()

    messaging_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)],
        )
    )

    log_message(db, user_id, "outgoing", "text", reply_text)


def push_message(line_user_id: str, text: str, db: Session, msg_type: str = "text", log_content: str = None) -> MessageLog:
    messaging_api = get_messaging_api()
    messaging_api.push_message(
        PushMessageRequest(to=line_user_id, messages=[TextMessage(text=text)])
    )
    log = MessageLog(
        line_user_id=line_user_id,
        direction="outgoing",
        message_type=msg_type,
        content=log_content if log_content is not None else text,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def handle_follow_event(event, db: Session):
    user_id = event.source.user_id
    messaging_api = get_messaging_api()
    display_name, picture_url = fetch_profile(messaging_api, user_id)
    upsert_user(db, user_id, display_name, picture_url)

    messaging_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="歡迎加入！有什麼我可以幫你的嗎？")],
        )
    )


def handle_unfollow_event(event, db: Session):
    user_id = event.source.user_id
    user = db.query(LineUser).filter(LineUser.line_user_id == user_id).first()
    if user:
        user.status = "blocked"
        db.commit()
