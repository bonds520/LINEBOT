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
OCR_VIEW_TEXT     = "🔍 查看辨識結果"


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

    # ── OCR 確認狀態優先處理 ──────────────────────────────────────
    if text == OCR_VIEW_TEXT:
        pending_ocr = db.query(OcrPendingConfirm).filter(
            OcrPendingConfirm.line_user_id == user_id,
            OcrPendingConfirm.status == "waiting",
            OcrPendingConfirm.expires_at > datetime.utcnow(),
        ).order_by(OcrPendingConfirm.created_at.desc()).first()

        messaging_api = get_messaging_api()
        view_btn = QuickReply(items=[
            QuickReplyItem(action=MessageAction(label="🔍 查看辨識結果", text=OCR_VIEW_TEXT)),
        ])

        if not pending_ocr:
            messaging_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(
                    text="⏳ 辨識尚未完成，請稍後再試。",
                    quick_reply=view_btn,
                )],
            ))
            return

        # 大頭照：詢問被攝者姓名後再歸檔
        if pending_ocr.doc_type == "大頭照":
            pending_ocr.detected_name = "__ASK_NAME__"
            db.commit()
            messaging_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(
                    text="🙂 收到您的大頭照！\n\n請問被攝者的姓名為何？（將用於存檔檔名）",
                )],
            ))
            return

        non_doc_messages = {
            "生活照":    "📷 收到您的生活照片！\n\n如需客服協助，我們將儘快為您回覆。",
            "非文件照片": "📷 收到您的照片，未能辨識為殯葬相關文件。\n\n若您要上傳文件，請確認圖片清晰且文字完整可見，或重新拍攝後上傳。",
            "辨識失敗":  "⚠️ 文件辨識失敗，請重新上傳或聯繫客服。",
        }
        if pending_ocr.doc_type in non_doc_messages:
            reply = non_doc_messages[pending_ocr.doc_type]
            pending_ocr.status = "confirmed"
            db.commit()
            messaging_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)],
            ))
            return

        from app.ocr_client import format_confirm_message, _parse_structured
        _, fields = _parse_structured(pending_ocr.ocr_result or "")
        result_msg = format_confirm_message(pending_ocr.doc_type, fields or {}, pending_ocr.ocr_result or "")
        messaging_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(
                text=result_msg,
                quick_reply=QuickReply(items=[
                    QuickReplyItem(action=MessageAction(label="✅ 正確，請歸檔", text=OCR_CONFIRM_TEXT)),
                    QuickReplyItem(action=MessageAction(label="❌ 辨識有誤", text=OCR_REJECT_TEXT)),
                ]),
            )],
        ))
        return

    # ── 大頭照等待姓名輸入 ────────────────────────────────────────
    id_photo_asking = db.query(OcrPendingConfirm).filter(
        OcrPendingConfirm.line_user_id == user_id,
        OcrPendingConfirm.doc_type == "大頭照",
        OcrPendingConfirm.detected_name == "__ASK_NAME__",
        OcrPendingConfirm.status == "waiting",
        OcrPendingConfirm.expires_at > datetime.utcnow(),
    ).order_by(OcrPendingConfirm.created_at.desc()).first()

    if id_photo_asking:
        name = text.strip()
        id_photo_asking.detected_name = name
        db.commit()
        messaging_api = get_messaging_api()
        messaging_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(
                text=f"🙂 收到大頭照，被攝者姓名：{name}\n\n以上資訊是否正確，可以歸檔嗎？",
                quick_reply=QuickReply(items=[
                    QuickReplyItem(action=MessageAction(label="✅ 正確，請歸檔", text=OCR_CONFIRM_TEXT)),
                    QuickReplyItem(action=MessageAction(label="❌ 辨識有誤", text=OCR_REJECT_TEXT)),
                ]),
            )],
        ))
        return
    # ─────────────────────────────────────────────────────────────

    if text in (OCR_CONFIRM_TEXT, OCR_REJECT_TEXT):
        pending_ocr = db.query(OcrPendingConfirm).filter(
            OcrPendingConfirm.line_user_id == user_id,
            OcrPendingConfirm.status == "waiting",
            OcrPendingConfirm.expires_at > datetime.utcnow(),
        ).order_by(OcrPendingConfirm.created_at.desc()).first()

        if pending_ocr:
            messaging_api = get_messaging_api()
            if text == OCR_CONFIRM_TEXT:
                # 大頭照姓名尚未收集，導回詢問
                if pending_ocr.doc_type == "大頭照" and pending_ocr.detected_name in (None, "__ASK_NAME__"):
                    pending_ocr.detected_name = "__ASK_NAME__"
                    db.commit()
                    messaging_api.reply_message(ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="請先告知被攝者的姓名，再進行歸檔。")],
                    ))
                    return
                ocr_text = pending_ocr.ocr_result or ""
                # 使用辨識當下已驗證修正的類型與姓名，確保與確認訊息一致
                doc_type = pending_ocr.doc_type
                detected_name = pending_ocr.detected_name
                if not doc_type:
                    from app.ocr_client import _parse_structured, _detect_doc_type
                    doc_type, _f = _parse_structured(ocr_text)
                    if not doc_type:
                        doc_type = _detect_doc_type(ocr_text)
                archived_path = _archive_file(
                    pending_ocr.file_path, ocr_text,
                    doc_type=doc_type, name=detected_name,
                )
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
                messaging_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="✅ 文件已歸檔完成，感謝您的確認！")],
                ))
            else:
                pending_ocr.status = "rejected"
                db.commit()
                messaging_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
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

    # 立即回覆「辨識中」並附按鈕（備援：若推播失敗用戶仍可手動查詢）
    messaging_api.reply_message(ReplyMessageRequest(
        reply_token=event.reply_token,
        messages=[TextMessage(
            text="📄 收到您的文件，正在辨識中，請稍候約 30~60 秒，辨識完成後系統會通知您。",
            quick_reply=QuickReply(items=[
                QuickReplyItem(action=MessageAction(label="🔍 查看辨識結果", text=OCR_VIEW_TEXT)),
            ]),
        )],
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

    # PDF 或圖片才觸發 OCR，其他檔案直接回覆
    ext = os.path.splitext(safe_name)[1].lower()
    if ext in (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp"):
        messaging_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(
                text="📄 收到您的文件，正在辨識中，請稍候約 30~60 秒，辨識完成後系統會通知您。",
                quick_reply=QuickReply(items=[
                    QuickReplyItem(action=MessageAction(label="🔍 查看辨識結果", text=OCR_VIEW_TEXT)),
                ]),
            )],
        ))
        return abs_path, user_id, msg_log.id if msg_log else None
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

    # 國民身分證加上正/背面（依原始 OCR 文字判斷）
    label = doc_type
    if doc_type == "國民身分證":
        side = _detect_id_card_side(ocr_text)
        label = f"國民身分證（{side}）"

    # 組成檔名：姓名-證件類別（無姓名時用「未知」）
    safe_name = re.sub(r'[\\/:*?"<>|]', '', name) if name else "未知"
    ext = os.path.splitext(file_path)[1].lower() or ".jpg"
    base_filename = f"{safe_name}-{label}{ext}"

    # 建立目錄（依 doc_type 分類）
    archive_base = os.getenv("ARCHIVE_PATH", os.path.join(os.path.dirname(__file__), "..", "archived"))
    now = datetime.utcnow()
    dest_dir = os.path.join(archive_base, doc_type, str(now.year), f"{now.month:02d}")
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
    """背景任務：OCR（含分類）→ 生活照/大頭照/文件分流通知。"""
    from app.ocr_client import (
        extract, format_confirm_message,
        NON_DOCUMENT_RESULT, LIFE_PHOTO_RESULT, ID_PHOTO_RESULT,
    )
    db = SessionLocal()
    try:
        messaging_api = get_messaging_api()

        view_btn = QuickReply(items=[
            QuickReplyItem(action=MessageAction(label="🔍 查看辨識結果", text=OCR_VIEW_TEXT)),
        ])

        def _get_user():
            return db.query(LineUser)\
                     .filter_by(line_user_id=user_id).first()

        # ── 單次 OCR 呼叫，3 分鐘整體 timeout ────────────────────────
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
        OCR_TIMEOUT = 180

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(extract, file_path)
            try:
                ocr_result, doc_type, fields = future.result(timeout=OCR_TIMEOUT)
            except FuturesTimeoutError:
                logger.warning("OCR 超過 %ds 未完成，通知用戶重試（user=%s）", OCR_TIMEOUT, user_id)
                try:
                    messaging_api.push_message(PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text="⚠️ 您傳的檔案無法順利辨識，請重新拍照上傳。")],
                    ))
                except Exception:
                    pass
                return

        logger.info("OCR 結果（user=%s）：doc_type=%s ocr前50=%s", user_id, doc_type, ocr_result[:50])

        # ── 生活照 ────────────────────────────────────────────────────
        if ocr_result == LIFE_PHOTO_RESULT or ocr_result.startswith("【生活照】"):
            user = _get_user()
            db.add(PendingQuestion(
                line_user_id=user_id,
                display_name=user.display_name if user else None,
                question="[生活照] 用戶上傳了一張生活照片",
            ))
            db.commit()
            try:
                messaging_api.push_message(PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text="📷 收到您的生活照片！\n\n如需客服協助，我們將儘快為您回覆。")],
                ))
            except Exception as push_err:
                logger.warning("生活照推播失敗（user=%s）：%s", user_id, push_err)
            return

        # ── 大頭照 ────────────────────────────────────────────────────
        if ocr_result == ID_PHOTO_RESULT or ocr_result.startswith("【大頭照】"):
            user = _get_user()
            db.add(PendingQuestion(
                line_user_id=user_id,
                display_name=user.display_name if user else None,
                question="[大頭照] 用戶上傳了一張大頭照",
            ))
            db.add(OcrPendingConfirm(
                line_user_id=user_id,
                message_log_id=message_log_id,
                file_path=file_path,
                ocr_result="",
                doc_type="大頭照",
                detected_name=None,
                status="waiting",
                expires_at=datetime.utcnow() + timedelta(minutes=30),
            ))
            db.commit()
            try:
                messaging_api.push_message(PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(
                        text="🙂 收到您的大頭照！\n\n請點下方按鈕，填寫被攝者姓名後進行歸檔。",
                        quick_reply=view_btn,
                    )],
                ))
            except Exception as push_err:
                logger.warning("大頭照推播失敗（user=%s）：%s", user_id, push_err)
            return

        def _push_non_doc(question_tag: str, push_text: str):
            user = db.query(LineUser)\
                     .filter_by(line_user_id=user_id).first()
            db.add(PendingQuestion(
                line_user_id=user_id,
                display_name=user.display_name if user else None,
                question=question_tag,
            ))
            db.commit()
            try:
                messaging_api.push_message(PushMessageRequest(
                    to=user_id, messages=[TextMessage(text=push_text)],
                ))
            except Exception as push_err:
                logger.warning("非文件推播失敗（user=%s）：%s", user_id, push_err)

        # 模型明確判斷非文件
        if ocr_result == NON_DOCUMENT_RESULT or ocr_result.startswith("【非文件圖片】"):
            _push_non_doc(
                "[非文件照片] 模型判斷圖片非正式文件",
                "📷 收到您的照片！這張圖片不像是文件，如需客服協助，我們將儘快為您回覆。",
            )
            return

        # OCR 未找到文字 → 分類可能誤判，當生活照處理
        if ocr_result.startswith("【未找到文字】"):
            _push_non_doc(
                "[非文件照片] 用戶上傳了一張未含文字的照片",
                "📷 收到您的照片，未在圖片中找到文件內容。\n\n若您要上傳正式文件，請確認圖片清晰且文字可見，或重新拍攝後上傳。",
            )
            return

        # 文件類型未知且無任何有效欄位 → 很可能是非文件照片
        if doc_type in ("未分類", "其他") and not any(fields.values()):
            _push_non_doc(
                "[非文件照片] 用戶上傳無法辨識的圖片",
                "📷 收到您的照片，未能辨識為殯葬相關文件。\n\n若您要上傳文件，請確認圖片清晰且文字完整可見，或重新拍攝後上傳。",
            )
            return

        # 存下「已驗證修正」的類型與姓名，歸檔時直接使用，不再重新解析
        detected_name = (fields.get("亡者姓名") or fields.get("申請人姓名")) if fields else None

        expires_at = datetime.utcnow() + timedelta(minutes=30)
        ocr_record = OcrPendingConfirm(
            line_user_id=user_id,
            message_log_id=message_log_id,
            file_path=file_path,
            ocr_result=ocr_result,
            doc_type=doc_type,
            detected_name=detected_name,
            status="waiting",
            expires_at=expires_at,
        )
        db.add(ocr_record)
        db.commit()

        try:
            messaging_api.push_message(PushMessageRequest(
                to=user_id,
                messages=[TextMessage(
                    text="✅ 文件辨識完成！請點下方按鈕查看辨識結果。",
                    quick_reply=view_btn,
                )],
            ))
            logger.info("OCR 完成通知已推播（user=%s doc_type=%s）", user_id, doc_type)
        except Exception as push_err:
            logger.warning("OCR 完成推播失敗（user=%s）：%s，用戶可點初始回覆按鈕查看結果", user_id, push_err)
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
