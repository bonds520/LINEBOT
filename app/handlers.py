from linebot.v3.messaging import (
    ApiClient, MessagingApi, MessagingApiBlob, Configuration,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, ImageMessage, VideoMessage,
    FlexMessage, FlexBubble, FlexBox, FlexText, FlexButton, FlexImage,
    URIAction,
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
from pathlib import Path

logger = logging.getLogger(__name__)


def get_messaging_api() -> MessagingApi:
    configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
    return MessagingApi(ApiClient(configuration))


def _download_content(message_id: str) -> bytes:
    configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
    with ApiClient(configuration) as api_client:
        return MessagingApiBlob(api_client).get_message_content(message_id)


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


def _resolve_qa_reply(text: str, question: str, user_id: str, display_name: str | None, db: Session) -> str:
    """Dify → rapidfuzz → 人工客服 fallback，回傳回覆文字並寫入 PendingQuestion。
    text: 送入比對的原始問題；question: 儲存到 PendingQuestion 的字串（可帶前綴）。
    """
    use_dify = os.getenv("USE_DIFY", "false").lower() == "true"
    reply_text = None

    if use_dify:
        from app.dify_client import chat as dify_chat
        reply_text = dify_chat(user_id, text)
        if not reply_text or "【轉人工客服】" in reply_text:
            reply_text = None

    if not reply_text:
        result = find_best_match(text, db)
        if result:
            qa, _ = result
            reply_text = qa.answer
            qa.hit_count += 1
            db.commit()

    if not reply_text:
        reply_text = "您的問題已收到，將由客服人員儘快為您回覆，感謝您的耐心等候！"
        db.add(PendingQuestion(line_user_id=user_id, display_name=display_name, question=question))
        db.commit()

    return reply_text


def handle_text_message(event, db: Session):
    user_id = event.source.user_id
    text = event.message.text
    quoted_line_id = getattr(event.message, "quoted_message_id", None)

    # ── 大頭照姓名輸入：2～5 個中文字 → 直接歸檔 ─────────────────
    if re.fullmatch(r'[一-鿿]{2,5}', text.strip()):
        id_photo_pending = db.query(OcrPendingConfirm).filter(
            OcrPendingConfirm.line_user_id == user_id,
            OcrPendingConfirm.doc_type == "大頭照",
            OcrPendingConfirm.detected_name == "__ASK_NAME__",
            OcrPendingConfirm.status == "waiting",
            OcrPendingConfirm.expires_at > datetime.utcnow(),
        ).order_by(OcrPendingConfirm.created_at.asc()).first()

        if id_photo_pending:
            name = text.strip()
            archived_path = _archive_file(id_photo_pending.file_path, "", doc_type="大頭照", name=name)
            messaging_api = get_messaging_api()
            display_name, _ = fetch_profile(messaging_api, user_id)
            db.add(ArchivedDocument(
                line_user_id=user_id,
                display_name=display_name,
                original_file_path=id_photo_pending.file_path,
                archived_file_path=archived_path,
                ocr_result="",
                document_type="大頭照",
                confirmed_at=datetime.utcnow(),
            ))
            id_photo_pending.detected_name = name
            id_photo_pending.status = "confirmed"
            db.commit()
            messaging_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=f"✅ 大頭照已以「{name}」為檔名完成歸檔，感謝您！")],
            ))
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

    reply_text = _resolve_qa_reply(text, text, user_id, user.display_name if user else None, db)

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

    image_bytes = _download_content(message_id)

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

    messaging_api.reply_message(ReplyMessageRequest(
        reply_token=event.reply_token,
        messages=[TextMessage(text="📎 已收到您提供的檔案，感謝您。")],
    ))
    return abs_path, user_id, msg_log.id if msg_log else None


def handle_audio_message(event, db: Session):
    """語音訊息：下載 M4A → Whisper STT → 走文字回覆流程"""
    user_id = event.source.user_id
    message_id = event.message.id

    audio_bytes = _download_content(message_id)

    messaging_api = get_messaging_api()
    display_name, picture_url = fetch_profile(messaging_api, user_id)
    upsert_user(db, user_id, display_name, picture_url)

    return audio_bytes, user_id, event.reply_token


def run_stt_and_reply(audio_bytes: bytes, user_id: str, db_factory, reply_token: str = None):
    """背景任務：STT 辨識 → 走文字回覆流程。優先用 reply_message（token 30 秒內有效），逾時 fallback push。"""
    from app.stt_client import transcribe
    db = db_factory()

    def _send(text: str):
        messaging_api = get_messaging_api()
        if reply_token:
            try:
                messaging_api.reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text)],
                ))
                return
            except Exception:
                pass
        messaging_api.push_message(PushMessageRequest(
            to=user_id, messages=[TextMessage(text=text)],
        ))

    try:
        text = transcribe(audio_bytes)

        if not text:
            _send("⚠️ 語音辨識失敗，請重新說話或改用文字輸入。")
            return

        logger.info("STT 結果：%s", text[:60])

        # 記錄語音原文
        log_message(db, user_id, "incoming", "audio", f"[語音] {text}")

        # 走和文字訊息相同的回覆邏輯
        user = db.query(LineUser).filter_by(line_user_id=user_id).first()
        reply_text = _resolve_qa_reply(text, f"[語音] {text}", user_id, user.display_name if user else None, db)

        _send(reply_text)
        log_message(db, user_id, "outgoing", "text", reply_text)

    except Exception as e:
        logger.error("STT 背景任務失敗（user=%s）：%s", user_id, e)
        try:
            _send("⚠️ 語音處理失敗，請改用文字輸入。")
        except Exception:
            pass
    finally:
        db.close()


def handle_video_message(event, db: Session):
    user_id = event.source.user_id
    message_id = event.message.id

    video_bytes = _download_content(message_id)

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

    file_bytes = _download_content(message_id)

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

    ext = os.path.splitext(safe_name)[1].lower()

    # PDF 或圖片：觸發 OCR，後台自動歸檔
    if ext in (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp"):
        messaging_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="📎 已收到您提供的檔案，感謝您。")],
        ))
        return abs_path, user_id, msg_log.id if msg_log else None

    # 非 OCR 格式 → 存至「其他檔案區」，以日期 + 原始檔名重新命名
    archive_base = os.getenv("ARCHIVE_PATH", os.path.join(os.path.dirname(__file__), "..", "archived"))
    now = datetime.utcnow()
    dest_dir = os.path.join(archive_base, "其他檔案區", str(now.year), f"{now.month:02d}")
    os.makedirs(dest_dir, exist_ok=True)

    new_name = f"{now.strftime('%Y%m%d')}_{safe_name}"
    dest_path = os.path.join(dest_dir, new_name)
    if os.path.exists(dest_path):
        stem, ext_part = os.path.splitext(new_name)
        dest_path = os.path.join(dest_dir, f"{stem}_{now.strftime('%H%M%S')}{ext_part}")
        new_name = os.path.basename(dest_path)

    shutil.copy2(abs_path, dest_path)
    logger.info("其他檔案歸檔：%s → %s（user=%s）", safe_name, new_name, user_id)

    messaging_api.reply_message(ReplyMessageRequest(
        reply_token=event.reply_token,
        messages=[TextMessage(text="📎 已收到您提供的檔案，感謝您。")],
    ))
    return None, user_id, None


def _archive_file(file_path: str, ocr_text: str, doc_type: str = None, name: str = None) -> str:
    """
    歸檔檔案。doc_type 與 name 應由呼叫端傳入（已驗證修正的值）。
    若未傳入才退而求其次重新解析（向後相容）。
    """
    from app.ocr_client import _parse_structured, _detect_doc_type, _detect_id_card_side

    # 優先使用傳入的已驗證值；未傳入才重新解析
    if not doc_type:
        doc_type, fields = _parse_structured(ocr_text)
        if not doc_type:
            doc_type = _detect_doc_type(ocr_text)
        if name is None and fields:
            name = fields.get("亡者姓名") or fields.get("申請人姓名")

    # 國民身分證加上正/背面；大頭照資料夾改為「亡者大頭照」
    label = doc_type
    if doc_type == "國民身分證":
        side = _detect_id_card_side(ocr_text)
        label = f"國民身分證（{side}）"
    folder = "亡者大頭照" if doc_type == "大頭照" else doc_type

    # 組成檔名：姓名-證件類別（無姓名時用「未知」）
    safe_name = re.sub(r'[\\/:*?"<>|]', '', name) if name else "未知"
    ext = os.path.splitext(file_path)[1].lower() or ".jpg"
    base_filename = f"{safe_name}-{label}{ext}"

    # 建立目錄（依分類）
    archive_base = os.getenv("ARCHIVE_PATH", os.path.join(os.path.dirname(__file__), "..", "archived"))
    now = datetime.utcnow()
    dest_dir = os.path.join(archive_base, folder, str(now.year), f"{now.month:02d}")
    os.makedirs(dest_dir, exist_ok=True)

    # 同名檔案加序號避免覆蓋
    dest_path = os.path.join(dest_dir, base_filename)
    if os.path.exists(dest_path):
        ts = now.strftime("%H%M%S")
        dest_path = os.path.join(dest_dir, f"{safe_name}-{label}_{ts}{ext}")

    shutil.copy2(file_path, dest_path)
    logger.info("歸檔：%s → %s（類型=%s 姓名=%s）",
                os.path.basename(file_path), os.path.basename(dest_path), doc_type, safe_name)
    return dest_path


def run_ocr_and_notify(user_id: str, file_path: str, message_log_id: int | None):
    """背景任務：OCR（含分類）→ 自動歸檔。大頭照例外：push 詢問亡者姓名。"""
    from app.ocr_client import (
        extract,
        NON_DOCUMENT_RESULT, LIFE_PHOTO_RESULT, ID_PHOTO_RESULT,
    )
    db = SessionLocal()
    try:
        def _get_user():
            return db.query(LineUser).filter_by(line_user_id=user_id).first()

        def _archive_to_manual(reason: str):
            user = _get_user()
            db.add(PendingQuestion(
                line_user_id=user_id,
                display_name=user.display_name if user else None,
                question=reason,
            ))
            archived_path = _archive_file(file_path, "", doc_type="待人工確認", name=None)
            db.add(ArchivedDocument(
                line_user_id=user_id,
                display_name=user.display_name if user else None,
                original_file_path=file_path,
                archived_file_path=archived_path,
                ocr_result="",
                document_type="待人工確認",
                confirmed_at=datetime.utcnow(),
            ))
            db.commit()
            logger.info("自動歸檔→待人工確認（user=%s reason=%s）", user_id, reason)

        # ── 單次 OCR 呼叫，3 分鐘整體 timeout ────────────────────────
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
        OCR_TIMEOUT = 180

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(extract, file_path)
            try:
                ocr_result, doc_type, fields = future.result(timeout=OCR_TIMEOUT)
            except FuturesTimeoutError:
                logger.warning("OCR 超過 %ds 未完成（user=%s）", OCR_TIMEOUT, user_id)
                _archive_to_manual("[辨識失敗] OCR 超時")
                return

        logger.info("OCR 結果（user=%s）：doc_type=%s ocr前50=%s", user_id, doc_type, ocr_result[:50])

        # ── 生活照：存入待人工確認 ────────────────────────────────────
        if ocr_result == LIFE_PHOTO_RESULT or ocr_result.startswith("【生活照】"):
            _archive_to_manual("[生活照] 用戶上傳了一張生活照片")
            return

        # ── 大頭照：以原始檔名為亡者姓名，直接歸檔 ─────────────────
        if ocr_result == ID_PHOTO_RESULT or ocr_result.startswith("【大頭照】"):
            name = Path(file_path).stem
            archived_path = _archive_file(file_path, "", doc_type="大頭照", name=name)
            user = _get_user()
            db.add(ArchivedDocument(
                line_user_id=user_id,
                display_name=user.display_name if user else None,
                original_file_path=file_path,
                archived_file_path=archived_path,
                ocr_result="",
                document_type="大頭照",
                confirmed_at=datetime.utcnow(),
            ))
            db.commit()
            logger.info("自動歸檔完成（user=%s doc_type=大頭照 name=%s）", user_id, name)
            return

        # ── 無法辨識：存入待人工確認 ─────────────────────────────────
        if ocr_result == NON_DOCUMENT_RESULT or ocr_result.startswith("【非文件圖片】"):
            _archive_to_manual("[非文件照片] 模型判斷圖片非正式文件")
            return

        if ocr_result.startswith("【未找到文字】"):
            _archive_to_manual("[非文件照片] 未含文字的照片")
            return

        if doc_type in ("未分類", "其他") and not any(fields.values()):
            _archive_to_manual("[非文件照片] 無法辨識的圖片")
            return

        # ── 正常文件：直接自動歸檔 ───────────────────────────────────
        detected_name = (fields.get("亡者姓名") or fields.get("申請人姓名")) if fields else None
        archived_path = _archive_file(file_path, ocr_result, doc_type=doc_type, name=detected_name)
        user = _get_user()
        db.add(ArchivedDocument(
            line_user_id=user_id,
            display_name=user.display_name if user else None,
            original_file_path=file_path,
            archived_file_path=archived_path,
            ocr_result=ocr_result,
            document_type=doc_type,
            confirmed_at=datetime.utcnow(),
        ))
        db.commit()
        logger.info("自動歸檔完成（user=%s doc_type=%s name=%s）", user_id, doc_type, detected_name)

    except Exception as e:
        logger.error("OCR 背景任務失敗（user=%s）：%s", user_id, e)
        try:
            db.rollback()
            _archive_to_manual("[辨識失敗] 背景任務例外")
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
