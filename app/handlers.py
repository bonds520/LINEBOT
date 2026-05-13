from linebot.v3.messaging import (
    ApiClient, MessagingApi, MessagingApiBlob, Configuration,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, ImageMessage, VideoMessage,
    FlexMessage, FlexBubble, FlexBox, FlexText, FlexButton, FlexImage,
    URIAction,
)
from sqlalchemy.orm import Session
from app.models import LineUser, MessageLog, QAPair, PendingQuestion, MessageQuote
from app.matcher import find_best_match
import os
import re
import uuid


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


def log_message(db: Session, line_user_id: str, direction: str, message_type: str, content: str = None) -> MessageLog:
    log = MessageLog(
        line_user_id=line_user_id,
        direction=direction,
        message_type=message_type,
        content=content,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def fetch_profile(messaging_api: MessagingApi, user_id: str):
    try:
        profile = messaging_api.get_profile(user_id)
        return profile.display_name, profile.picture_url
    except Exception:
        return None, None


def handle_text_message(event, db: Session):
    user_id = event.source.user_id
    text = event.message.text
    quoted_line_id = getattr(event.message, "quoted_message_id", None)

    messaging_api = get_messaging_api()
    display_name, picture_url = fetch_profile(messaging_api, user_id)
    user = upsert_user(db, user_id, display_name, picture_url)
    incoming_log = log_message(db, user_id, "incoming", "text", text)

    # If user quoted one of our messages, attach a MessageQuote to the incoming log
    if quoted_line_id and incoming_log:
        quoted_log = db.query(MessageLog).filter(
            MessageLog.line_message_id == quoted_line_id
        ).first()
        if quoted_log:
            quote_sender = "小編"
            if quoted_log.message_type in ("staff_image",) or (
                quoted_log.content and quoted_log.content.startswith("/static/images/")
            ):
                preview = quoted_log.content or ""
            elif quoted_log.message_type == "staff_video":
                preview = "[影片]"
            elif quoted_log.message_type == "staff_file":
                preview = "[檔案]"
            else:
                preview = (quoted_log.content or "")[:200]
            db.add(MessageQuote(
                message_log_id=incoming_log.id,
                quote_sender=quote_sender,
                quote_preview=preview,
            ))
            db.commit()

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

    resp = messaging_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)],
        )
    )
    reply_line_id = None
    if resp and getattr(resp, "sent_messages", None):
        reply_line_id = resp.sent_messages[0].id

    outgoing_log = MessageLog(
        line_user_id=user_id,
        direction="outgoing",
        message_type="text",
        content=reply_text,
        line_message_id=reply_line_id,
    )
    db.add(outgoing_log)
    db.commit()


def handle_image_message(event, db: Session):
    user_id = event.source.user_id
    message_id = event.message.id

    configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(message_id)

    filename = f"{uuid.uuid4().hex}.jpg"
    save_dir = os.path.join(os.path.dirname(__file__), "..", "static", "images")
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)
    with open(save_path, "wb") as f:
        f.write(image_bytes)

    image_url = f"/static/images/{filename}"

    messaging_api = get_messaging_api()
    display_name, picture_url = fetch_profile(messaging_api, user_id)
    upsert_user(db, user_id, display_name, picture_url)
    log_message(db, user_id, "incoming", "image", image_url)


def handle_video_message(event, db: Session):
    user_id = event.source.user_id
    message_id = event.message.id

    configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        video_bytes = blob_api.get_message_content(message_id)

    filename = f"{uuid.uuid4().hex}.mp4"
    save_dir = os.path.join(os.path.dirname(__file__), "..", "static", "images")
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)
    with open(save_path, "wb") as f:
        f.write(video_bytes)

    video_url = f"/static/images/{filename}"

    messaging_api = get_messaging_api()
    display_name, picture_url = fetch_profile(messaging_api, user_id)
    upsert_user(db, user_id, display_name, picture_url)
    log_message(db, user_id, "incoming", "video", video_url)


def handle_file_message(event, db: Session):
    user_id = event.source.user_id
    message_id = event.message.id
    original_name = getattr(event.message, "file_name", None) or f"{message_id}.bin"

    safe_name = re.sub(r'[^\w.\-]', '_', original_name)
    safe_name = safe_name.lstrip('.') or "file"
    safe_name = safe_name[:200]

    configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        file_bytes = blob_api.get_message_content(message_id)

    dir_id = uuid.uuid4().hex
    save_dir = os.path.join(os.path.dirname(__file__), "..", "static", "files", dir_id)
    os.makedirs(save_dir, exist_ok=True)
    with open(os.path.join(save_dir, safe_name), "wb") as f:
        f.write(file_bytes)

    file_url = f"/files/download/{dir_id}/{safe_name}"

    messaging_api = get_messaging_api()
    display_name, picture_url = fetch_profile(messaging_api, user_id)
    upsert_user(db, user_id, display_name, picture_url)
    log_message(db, user_id, "incoming", "file", file_url)


def push_message(line_user_id: str, text: str, db: Session, msg_type: str = "text", log_content: str = None) -> MessageLog:
    messaging_api = get_messaging_api()
    resp = messaging_api.push_message(
        PushMessageRequest(to=line_user_id, messages=[TextMessage(text=text)])
    )
    line_msg_id = resp.sent_messages[0].id if resp and resp.sent_messages else None
    log = MessageLog(
        line_user_id=line_user_id,
        direction="outgoing",
        message_type=msg_type,
        content=log_content if log_content is not None else text,
        line_message_id=line_msg_id,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def _file_flex(filename: str, public_url: str, file_size: int) -> FlexMessage:
    size_str = (
        f"{file_size:,} Bytes" if file_size < 1024 else
        f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024 else
        f"{file_size / 1024 / 1024:.1f} MB"
    )
    ext = filename.rsplit('.', 1)[-1].upper() if '.' in filename else 'FILE'
    icon_map = {
        'PDF': '🔴', 'DOC': '🔵', 'DOCX': '🔵',
        'XLS': '🟢', 'XLSX': '🟢', 'CSV': '🟢',
        'PPT': '🟠', 'PPTX': '🟠',
        'ZIP': '⬛', 'RAR': '⬛', '7Z': '⬛',
        'TXT': '📝',
    }
    icon = icon_map.get(ext, '📄')
    return FlexMessage(
        alt_text=f"📎 {filename}",
        contents=FlexBubble(
            size="kilo",
            body=FlexBox(
                layout="horizontal",
                spacing="md",
                align_items="center",
                contents=[
                    FlexText(text=icon, flex=0, size="xxl", gravity="center"),
                    FlexBox(
                        layout="vertical",
                        flex=1,
                        contents=[
                            FlexText(text=filename, weight="bold", size="sm", wrap=True),
                            FlexText(
                                text=f"{ext}  ·  {size_str}",
                                size="xxs",
                                color="#aaaaaa",
                                margin="sm",
                            ),
                        ],
                    ),
                ],
            ),
            footer=FlexBox(
                layout="horizontal",
                contents=[
                    FlexButton(
                        action=URIAction(label="下載", uri=public_url),
                        style="link",
                        color="#0d6efd",
                        flex=1,
                    ),
                ],
            ),
        ),
    )


def push_file_message(
    line_user_id: str,
    filename: str,
    file_url: str,
    public_url: str,
    file_size: int,
    db: Session,
) -> MessageLog:
    messaging_api = get_messaging_api()
    if public_url.startswith("http"):
        msg = _file_flex(filename, public_url, file_size)
    else:
        msg = TextMessage(text=f"📎 {filename}\n點此下載：{public_url}")
    resp = messaging_api.push_message(PushMessageRequest(to=line_user_id, messages=[msg]))
    line_msg_id = resp.sent_messages[0].id if resp and resp.sent_messages else None
    log = MessageLog(
        line_user_id=line_user_id,
        direction="outgoing",
        message_type="staff_file",
        content=file_url,
        line_message_id=line_msg_id,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def _ensure_video_placeholder() -> str:
    """建立影片縮圖佔位 PNG（深灰 64×64），回傳路徑。"""
    import struct, zlib as _zlib
    path = os.path.join(os.path.dirname(__file__), "..", "static", "images", "_video_thumb.png")
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        W = H = 64
        def _chunk(name, data):
            return struct.pack('>I', len(data)) + name + data + struct.pack('>I', _zlib.crc32(name + data) & 0xffffffff)
        raw = b''.join(b'\x00' + bytes([30, 30, 30] * W) for _ in range(H))
        png = (b'\x89PNG\r\n\x1a\n'
               + _chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
               + _chunk(b'IDAT', _zlib.compress(raw, 9))
               + _chunk(b'IEND', b''))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(png)
    return "/static/images/_video_thumb.png"


def push_image_message(line_user_id: str, local_url: str, public_url: str, db: Session) -> MessageLog:
    messaging_api = get_messaging_api()
    resp = messaging_api.push_message(
        PushMessageRequest(
            to=line_user_id,
            messages=[ImageMessage(
                original_content_url=public_url,
                preview_image_url=public_url,
            )],
        )
    )
    line_msg_id = resp.sent_messages[0].id if resp and resp.sent_messages else None
    log = MessageLog(line_user_id=line_user_id, direction="outgoing",
                     message_type="staff_image", content=local_url,
                     line_message_id=line_msg_id)
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def push_video_message(line_user_id: str, local_url: str, public_url: str,
                       thumb_public_url: str, db: Session) -> MessageLog:
    _ensure_video_placeholder()
    messaging_api = get_messaging_api()
    resp = messaging_api.push_message(
        PushMessageRequest(
            to=line_user_id,
            messages=[VideoMessage(
                original_content_url=public_url,
                preview_image_url=thumb_public_url,
            )],
        )
    )
    line_msg_id = resp.sent_messages[0].id if resp and resp.sent_messages else None
    log = MessageLog(line_user_id=line_user_id, direction="outgoing",
                     message_type="staff_video", content=local_url,
                     line_message_id=line_msg_id)
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def push_reply_with_image_quote(
    line_user_id: str,
    reply_text: str,
    quote_sender: str,
    local_img_url: str,
    db: Session,
) -> MessageLog:
    tunnel = os.getenv("TUNNEL_URL", "").rstrip("/")
    messaging_api = get_messaging_api()

    if tunnel:
        public_img_url = tunnel + local_img_url
        msg = FlexMessage(
            alt_text=f"↩ {quote_sender}：[圖片]\n{reply_text}",
            contents=FlexBubble(
                body=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexBox(
                            layout="horizontal",
                            background_color="#f0f0f0",
                            corner_radius="6px",
                            padding_all="8px",
                            spacing="md",
                            align_items="center",
                            contents=[
                                FlexImage(
                                    url=public_img_url,
                                    size="80px",
                                    aspect_ratio="1:1",
                                    aspect_mode="cover",
                                    flex=0,
                                    gravity="center",
                                ),
                                FlexBox(
                                    layout="vertical",
                                    flex=1,
                                    contents=[
                                        FlexText(
                                            text=f"↩ {quote_sender}",
                                            size="xs",
                                            color="#555555",
                                            weight="bold",
                                        ),
                                        FlexText(
                                            text="[圖片]",
                                            size="xs",
                                            color="#888888",
                                            margin="xs",
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        FlexText(
                            text=reply_text,
                            wrap=True,
                            size="sm",
                            margin="md",
                        ),
                    ],
                ),
            ),
        )
    else:
        msg = TextMessage(text=f"「↩ {quote_sender}：[圖片]」\n{reply_text}")

    resp = messaging_api.push_message(PushMessageRequest(to=line_user_id, messages=[msg]))
    line_msg_id = resp.sent_messages[0].id if resp and resp.sent_messages else None
    log = MessageLog(
        line_user_id=line_user_id,
        direction="outgoing",
        message_type="staff",
        content=reply_text,
        line_message_id=line_msg_id,
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
