from linebot.v3.messaging import (
    ApiClient, MessagingApi, MessagingApiBlob, Configuration,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, ImageMessage, VideoMessage,
    FlexMessage, FlexBubble, FlexBox, FlexText, FlexButton, FlexImage,
    URIAction, QuickReply, QuickReplyItem, MessageAction,
)
from sqlalchemy.orm import Session
from app.models import LineUser, MessageLog, QAPair, PendingQuestion, MessageQuote, OcrPendingConfirm, ArchivedDocument
from app.matcher import find_best_match
from app.database import SessionLocal
import os
import re
import uuid
import shutil
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

OCR_CONFIRM_TEXT  = "✅ 正確，請歸檔"
OCR_REJECT_TEXT   = "❌ 辨識有誤，重新上傳"


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

    # ── OCR 確認狀態優先處理 ──────────────────────────────────────
    if text in (OCR_CONFIRM_TEXT, OCR_REJECT_TEXT):
        pending_ocr = db.query(OcrPendingConfirm).filter(
            OcrPendingConfirm.line_user_id == user_id,
            OcrPendingConfirm.status == "waiting",
            OcrPendingConfirm.expires_at > datetime.utcnow(),
        ).order_by(OcrPendingConfirm.created_at.desc()).first()

        if pending_ocr:
            messaging_api = get_messaging_api()
            if text == OCR_CONFIRM_TEXT:
                ocr_text = pending_ocr.ocr_result or ""
                archived_path = _archive_file(pending_ocr.file_path, ocr_text)
                from app.ocr_client import _parse_structured, _detect_doc_type
                doc_type, _ = _parse_structured(ocr_text)
                if not doc_type:
                    doc_type = _detect_doc_type(ocr_text)
                display_name, _ = fetch_profile(messaging_api, user_id)
                db.add(ArchivedDocument(
                    line_user_id=user_id,
                    display_name=display_name,
                    original_file_path=pending_ocr.file_path,
                    archived_file_path=archived_path,
                    ocr_result=ocr_text,
                    document_type=doc_type,
                    confirmed_at=datetime.utcnow(),
                ))
                pending_ocr.status = "confirmed"
                db.commit()
                messaging_api.push_message(PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text="✅ 文件已歸檔完成，感謝您的確認！")],
                ))
            else:
                pending_ocr.status = "rejected"
                db.commit()
                messaging_api.push_message(PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text="了解，請重新上傳文件，或回覆問題由客服協助。")],
                ))
            log_message(db, user_id, "incoming", "text", text)
            return
    # ─────────────────────────────────────────────────────────────

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

    # ── 回覆邏輯：USE_DIFY=true 走 Dify AI，否則走現有 Q&A 比對 ──
    use_dify = os.getenv("USE_DIFY", "false").lower() == "true"

    if use_dify:
        from app.dify_client import chat as dify_chat
        reply_text = dify_chat(user_id, text)
        if not reply_text or "【轉人工客服】" in reply_text:
            # Dify 無法回答或明確要求轉人工 → 進入待回覆流程
            reply_text = "您的問題已收到，將由客服人員儘快為您回覆，感謝您的耐心等候！"
            pending = PendingQuestion(
                line_user_id=user_id,
                display_name=user.display_name if user else None,
                question=text,
            )
            db.add(pending)
            db.commit()
    else:
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
    abs_path   = os.path.abspath(save_path)

    messaging_api = get_messaging_api()
    display_name, picture_url = fetch_profile(messaging_api, user_id)
    upsert_user(db, user_id, display_name, picture_url)
    msg_log = log_message(db, user_id, "incoming", "image", image_url)

    # 立即回覆「辨識中」，背景執行 OCR
    messaging_api.reply_message(ReplyMessageRequest(
        reply_token=event.reply_token,
        messages=[TextMessage(text="📄 收到您的文件，正在辨識中，請稍候...")],
    ))
    return abs_path, user_id, msg_log.id if msg_log else None


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
    abs_path = os.path.abspath(os.path.join(save_dir, safe_name))
    with open(abs_path, "wb") as f:
        f.write(file_bytes)

    file_url = f"/files/download/{dir_id}/{safe_name}"

    messaging_api = get_messaging_api()
    display_name, picture_url = fetch_profile(messaging_api, user_id)
    upsert_user(db, user_id, display_name, picture_url)
    msg_log = log_message(db, user_id, "incoming", "file", file_url)

    # PDF 或圖片才觸發 OCR，其他檔案直接回覆
    ext = os.path.splitext(safe_name)[1].lower()
    if ext in (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp"):
        messaging_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="📄 收到您的文件，正在辨識中，請稍候...")],
        ))
        return abs_path, user_id, msg_log.id if msg_log else None
    return None, user_id, None


def _doc_type_from_ocr(ocr_text: str) -> str:
    from app.ocr_client import _detect_doc_type
    return _detect_doc_type(ocr_text)


def _archive_file(file_path: str, ocr_text: str) -> str:
    doc_type = _doc_type_from_ocr(ocr_text)
    archive_base = os.getenv("ARCHIVE_PATH", os.path.join(os.path.dirname(__file__), "..", "archived"))
    now = datetime.utcnow()
    dest_dir = os.path.join(archive_base, doc_type, str(now.year), f"{now.month:02d}")
    os.makedirs(dest_dir, exist_ok=True)
    ts = now.strftime("%Y%m%d_%H%M%S")
    basename = os.path.basename(file_path)
    dest_path = os.path.join(dest_dir, f"{ts}_{basename}")
    shutil.copy2(file_path, dest_path)
    return dest_path


def run_ocr_and_notify(user_id: str, file_path: str, message_log_id: int | None):
    """背景任務：執行 OCR 並以 push_message 回覆用戶結果。"""
    from app.ocr_client import extract, format_confirm_message
    db = SessionLocal()
    try:
        ocr_result, doc_type, fields = extract(file_path)
        messaging_api = get_messaging_api()

        # 圖片中無文字（例如風景照）→ 直接告知，不建立歸檔流程
        if ocr_result.startswith("【未找到文字】"):
            messaging_api.push_message(PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=ocr_result)],
            ))
            return

        expires_at = datetime.utcnow() + timedelta(minutes=30)
        ocr_record = OcrPendingConfirm(
            line_user_id=user_id,
            message_log_id=message_log_id,
            file_path=file_path,
            ocr_result=ocr_result,
            status="waiting",
            expires_at=expires_at,
        )
        db.add(ocr_record)
        db.commit()

        result_msg = format_confirm_message(doc_type, fields, ocr_result)
        messaging_api.push_message(PushMessageRequest(
            to=user_id,
            messages=[TextMessage(
                text=result_msg,
                quick_reply=QuickReply(items=[
                    QuickReplyItem(action=MessageAction(label="✅ 正確，請歸檔", text=OCR_CONFIRM_TEXT)),
                    QuickReplyItem(action=MessageAction(label="❌ 辨識有誤", text=OCR_REJECT_TEXT)),
                ]),
            )],
        ))
    except Exception as e:
        logger.error("OCR 背景任務失敗（user=%s）：%s", user_id, e)
        try:
            get_messaging_api().push_message(PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text="⚠️ 文件辨識失敗，請重新上傳或聯繫客服。")],
            ))
        except Exception:
            pass
    finally:
        db.close()


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
