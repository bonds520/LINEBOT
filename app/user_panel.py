import csv
import io
from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import SystemUser, QAPair, PendingQuestion, MessageLog, LineUser, TodoItem, UserTag, PresetMessage, MessageQuote
from app.auth import verify_password, create_session, destroy_session, get_current_user

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def current_user_dep(request: Request, db: Session = Depends(get_db)) -> SystemUser:
    return get_current_user(request, db)


def get_reply_count(db: Session) -> int:
    return db.query(PendingQuestion).filter(PendingQuestion.status == "pending").count()


def get_todo_count(db: Session) -> int:
    return db.query(TodoItem).filter(TodoItem.status == "pending").count()


# ── 登入 / 登出 ───────────────────────────────────────────────
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="user_login.html")


@router.post("/login")
def do_login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(SystemUser).filter(SystemUser.username == username, SystemUser.is_active == True).first()
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(request=request, name="user_login.html", context={"error": "帳號或密碼錯誤"})
    token = create_session(user.id)
    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie("user_token", token, httponly=True, max_age=86400)
    return resp


@router.get("/logout")
def logout(request: Request):
    token = request.cookies.get("user_token")
    destroy_session(token)
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("user_token")
    return resp


# ── 儀表板 ────────────────────────────────────────────────────
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    from sqlalchemy import func
    qa_count = db.query(func.count(QAPair.id)).filter(QAPair.is_active == True).scalar()
    trained_count = db.query(func.count(QAPair.id)).filter(QAPair.is_trained == True).scalar()
    pending_count = db.query(func.count(QAPair.id)).filter(QAPair.is_trained == False, QAPair.is_active == True).scalar()
    reply_count = db.query(func.count(PendingQuestion.id)).filter(PendingQuestion.status == "pending").scalar()
    return templates.TemplateResponse(request=request, name="user_dashboard.html", context={
        "user": user,
        "qa_count": qa_count,
        "trained_count": trained_count,
        "pending_count": pending_count,
        "reply_count": reply_count,
        "pending_reply_count": reply_count,
        "todo_count": get_todo_count(db),
    })


# ── Q&A 訓練列表 ──────────────────────────────────────────────
@router.get("/dashboard/qa", response_class=HTMLResponse)
def qa_list(request: Request, category: str = "", db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    query = db.query(QAPair).filter(QAPair.is_active == True)
    if category:
        query = query.filter(QAPair.category == category)
    qa_list = query.order_by(QAPair.id.desc()).all()
    categories = db.query(QAPair.category).distinct().all()
    return templates.TemplateResponse(request=request, name="user_qa.html", context={
        "user": user,
        "qa_list": qa_list,
        "categories": [c[0] for c in categories],
        "current_category": category,
        "pending_reply_count": get_reply_count(db),
        "todo_count": get_todo_count(db),
    })


@router.post("/dashboard/qa/{qa_id}/update")
def qa_update(
    qa_id: int,
    question: str = Form(...),
    answer: str = Form(...),
    keywords: str = Form(""),
    category: str = Form("一般"),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    qa = db.query(QAPair).filter(QAPair.id == qa_id).first()
    if qa:
        qa.question = question
        qa.answer = answer
        qa.keywords = keywords
        qa.category = category
        qa.is_trained = False
        qa.trained_at = None
        db.commit()
    return RedirectResponse(url="/dashboard/qa", status_code=302)


@router.post("/dashboard/qa/{qa_id}/train")
def qa_train(qa_id: int, db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    qa = db.query(QAPair).filter(QAPair.id == qa_id).first()
    if qa and not qa.is_trained:
        qa.is_trained = True
        qa.trained_at = datetime.now()
        db.commit()
    return RedirectResponse(url="/dashboard/qa", status_code=302)


@router.post("/dashboard/qa/{qa_id}/delete")
def qa_delete(qa_id: int, from_page: str = "qa", db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    qa = db.query(QAPair).filter(QAPair.id == qa_id).first()
    if qa:
        db.delete(qa)
        db.commit()
    redirect = "/dashboard/trained?deleted=1" if from_page == "trained" else "/dashboard/qa?deleted=1"
    return RedirectResponse(url=redirect, status_code=302)


# ── 建立 Q&A（手動新增訓練資料）────────────────────────────────
@router.get("/dashboard/create-qa", response_class=HTMLResponse)
def create_qa_page(request: Request, db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    categories = db.query(QAPair.category).distinct().all()
    return templates.TemplateResponse(request=request, name="user_create_qa.html", context={
        "user": user,
        "categories": [c[0] for c in categories],
        "pending_reply_count": get_reply_count(db),
        "todo_count": get_todo_count(db),
    })


@router.post("/dashboard/create-qa")
def create_qa(
    question: str = Form(...),
    answer: str = Form(...),
    keywords: str = Form(""),
    category: str = Form("一般"),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    qa = QAPair(question=question, answer=answer, keywords=keywords, category=category)
    db.add(qa)
    db.commit()
    return RedirectResponse(url="/dashboard/create-qa?success=1", status_code=302)


@router.post("/dashboard/create-qa/import")
async def create_qa_import(file: UploadFile = File(...), db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    count = 0
    for row in reader:
        if "question" in row and "answer" in row:
            is_active_val = row.get("is_active", "1").strip()
            is_trained_val = row.get("is_trained", "0").strip()
            qa = QAPair(
                question=row["question"].strip(),
                answer=row["answer"].strip(),
                keywords=row.get("keywords", "").strip(),
                category=row.get("category", "一般").strip(),
                is_active=is_active_val not in ("0", "false", "False", ""),
                is_trained=is_trained_val in ("1", "true", "True"),
            )
            db.add(qa)
            count += 1
    db.commit()
    return RedirectResponse(url=f"/dashboard/create-qa?imported={count}", status_code=302)


@router.get("/dashboard/qa-csv-template")
def qa_csv_template(user: SystemUser = Depends(current_user_dep)):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["question", "answer", "keywords", "category", "is_active", "is_trained"])
    writer.writerow(["範例問題", "範例回答", "關鍵字1,關鍵字2", "一般", "1", "0"])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=qa_template.csv"},
    )


# ── 待回覆清單 ───────────────────────────────────────────────
@router.get("/dashboard/pending", response_class=HTMLResponse)
def pending_list(request: Request, db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    items = db.query(PendingQuestion).order_by(
        PendingQuestion.status.asc(),
        PendingQuestion.created_at.desc()
    ).all()
    pending_count = sum(1 for i in items if i.status == "pending")
    user_tags_map = {}
    user_notes_map = {}
    msg_anchor_map = {}
    for item in items:
        utags = db.query(UserTag).filter(UserTag.line_user_id == item.line_user_id).all()
        user_tags_map[item.line_user_id] = utags
        lu = db.query(LineUser).filter(LineUser.line_user_id == item.line_user_id).first()
        user_notes_map[item.line_user_id] = lu.note if lu else None
        # 找對應的 MessageLog ID（最接近且不超過問題建立時間的 incoming 訊息）
        msg = db.query(MessageLog).filter(
            MessageLog.line_user_id == item.line_user_id,
            MessageLog.direction == "incoming",
            MessageLog.created_at <= item.created_at,
        ).order_by(MessageLog.created_at.desc()).first()
        if msg:
            msg_anchor_map[item.id] = msg.id
    presets = db.query(PresetMessage).order_by(PresetMessage.sort_order.asc(), PresetMessage.id.asc()).all()
    return templates.TemplateResponse(request=request, name="user_pending.html", context={
        "user": user,
        "items": items,
        "pending_count": pending_count,
        "pending_reply_count": pending_count,
        "todo_count": get_todo_count(db),
        "user_tags_map": user_tags_map,
        "user_notes_map": user_notes_map,
        "msg_anchor_map": msg_anchor_map,
        "presets": presets,
    })


@router.post("/dashboard/pending/{item_id}/handle")
def pending_handle(
    item_id: int,
    note: str = Form(""),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    item = db.query(PendingQuestion).filter(PendingQuestion.id == item_id).first()
    if item:
        item.status = "handled"
        item.note = note
        db.commit()
    return RedirectResponse(url="/dashboard/pending", status_code=302)


@router.post("/dashboard/pending/{item_id}/reopen")
def pending_reopen(item_id: int, db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    item = db.query(PendingQuestion).filter(PendingQuestion.id == item_id).first()
    if item:
        item.status = "pending"
        item.note = None
        db.commit()
    return RedirectResponse(url="/dashboard/pending", status_code=302)


@router.post("/dashboard/pending/{item_id}/reply")
def pending_reply(
    item_id: int,
    message: str = Form(...),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    from app.handlers import push_message
    item = db.query(PendingQuestion).filter(PendingQuestion.id == item_id).first()
    if item and message.strip():
        try:
            push_message(item.line_user_id, message.strip(), db, msg_type="staff")
            item.status = "handled"
            item.note = f"[已回覆] {message.strip()[:50]}"
            db.commit()
        except Exception as e:
            return RedirectResponse(url=f"/dashboard/pending?error={str(e)[:80]}", status_code=302)
    return RedirectResponse(url="/dashboard/pending?replied=1", status_code=302)


@router.post("/dashboard/pending/{item_id}/add-todo")
def pending_add_todo(
    item_id: int,
    family_name: str = Form(""),
    recipient_name: str = Form(""),
    tower_number: str = Form(""),
    task_content: str = Form(...),
    due_date: str = Form(""),
    due_time: str = Form(""),
    note: str = Form(""),
    mark_handled: str = Form(""),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    item = db.query(PendingQuestion).filter(PendingQuestion.id == item_id).first()
    if item:
        from datetime import date
        todo = TodoItem(
            family_name=family_name.strip() or (item.display_name or ""),
            recipient_name=recipient_name.strip() or None,
            tower_number=tower_number.strip() or None,
            line_user_id=item.line_user_id,
            created_by=user.display_name,
            task_content=task_content.strip(),
            due_date=date.fromisoformat(due_date) if due_date else None,
            due_time=due_time or None,
            note=note.strip() or None,
        )
        db.add(todo)
        if mark_handled:
            item.status = "handled"
            item.note = f"[待辦] {task_content.strip()[:50]}"
        db.commit()
    return RedirectResponse(url="/dashboard/pending?todo_added=1", status_code=302)


# ── 待辦事項 ──────────────────────────────────────────────────
@router.get("/dashboard/todo", response_class=HTMLResponse)
def todo_list(request: Request, db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    pending_todos = db.query(TodoItem).filter(TodoItem.status == "pending").order_by(
        TodoItem.due_date.asc(), TodoItem.due_time.asc(), TodoItem.created_at.asc()
    ).all()
    done_todos = db.query(TodoItem).filter(TodoItem.status == "done").order_by(
        TodoItem.done_at.desc()
    ).all()
    from datetime import date, timedelta
    today = date.today()
    return templates.TemplateResponse(request=request, name="user_todo.html", context={
        "user": user,
        "pending_todos": pending_todos,
        "done_todos": done_todos,
        "todo_count": get_todo_count(db),
        "pending_reply_count": get_reply_count(db),
        "now_date": today.isoformat(),
        "warn_date1": (today + timedelta(days=1)).isoformat(),
        "warn_date2": (today + timedelta(days=2)).isoformat(),
    })


@router.post("/dashboard/todo/{todo_id}/done")
def todo_done(todo_id: int, db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    todo = db.query(TodoItem).filter(TodoItem.id == todo_id).first()
    if todo:
        todo.status = "done"
        todo.done_at = datetime.now()
        db.commit()
    return RedirectResponse(url="/dashboard/todo", status_code=302)


@router.post("/dashboard/todo/{todo_id}/reopen")
def todo_reopen(todo_id: int, db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    todo = db.query(TodoItem).filter(TodoItem.id == todo_id).first()
    if todo:
        todo.status = "pending"
        todo.done_at = None
        db.commit()
    return RedirectResponse(url="/dashboard/todo", status_code=302)


@router.get("/dashboard/todo/create", response_class=HTMLResponse)
def todo_create_page(request: Request, db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    return templates.TemplateResponse(request=request, name="user_todo_create.html", context={
        "user": user,
        "todo_count": get_todo_count(db),
        "pending_reply_count": get_reply_count(db),
        "todo_count": get_todo_count(db),
    })


@router.post("/dashboard/todo/create")
def todo_create(
    family_name: str = Form(""),
    recipient_name: str = Form(""),
    tower_number: str = Form(""),
    task_content: str = Form(...),
    due_date: str = Form(""),
    due_time: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    from datetime import date
    todo = TodoItem(
        family_name=family_name.strip() or None,
        recipient_name=recipient_name.strip() or None,
        tower_number=tower_number.strip() or None,
        created_by=user.display_name,
        task_content=task_content.strip(),
        due_date=date.fromisoformat(due_date) if due_date else None,
        due_time=due_time or None,
        note=note.strip() or None,
    )
    db.add(todo)
    db.commit()
    return RedirectResponse(url="/dashboard/todo?created=1", status_code=302)


# ── 聊天記錄 ─────────────────────────────────────────────────
@router.get("/dashboard/history", response_class=HTMLResponse)
def chat_history(
    request: Request,
    line_user_id: str = "",
    anchor_id: int = 0,
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    users = db.query(LineUser).order_by(LineUser.display_name).all()
    messages = []
    selected_user = None
    search_results = []
    is_search = bool(q or date_from or date_to)

    if is_search:
        # 全域搜尋模式：跨用戶搜尋
        mq = db.query(MessageLog)
        if q:
            mq = mq.filter(MessageLog.content.like(f"%{q}%"))
        if date_from:
            mq = mq.filter(MessageLog.created_at >= date_from)
        if date_to:
            mq = mq.filter(MessageLog.created_at <= f"{date_to} 23:59:59")
        if line_user_id:
            mq = mq.filter(MessageLog.line_user_id == line_user_id)
        search_results = mq.order_by(MessageLog.created_at.desc()).limit(200).all()
        # 組建用戶顯示名稱對照
        uid_set = {m.line_user_id for m in search_results}
        uid_name_map = {}
        for uid in uid_set:
            lu = db.query(LineUser).filter(LineUser.line_user_id == uid).first()
            uid_name_map[uid] = lu.display_name if lu else uid[:8] + "…"
        # 選中用戶（若有指定）
        if line_user_id:
            selected_user = db.query(LineUser).filter(LineUser.line_user_id == line_user_id).first()
    else:
        # 一般模式：顯示單一用戶對話
        uid_name_map = {}
        if line_user_id:
            selected_user = db.query(LineUser).filter(LineUser.line_user_id == line_user_id).first()
            messages = db.query(MessageLog).filter(
                MessageLog.line_user_id == line_user_id
            ).order_by(MessageLog.created_at.asc()).all()
        elif users:
            selected_user = users[0]
            line_user_id = selected_user.line_user_id
            messages = db.query(MessageLog).filter(
                MessageLog.line_user_id == line_user_id
            ).order_by(MessageLog.created_at.asc()).all()

    # anchor_id 直接使用，不再用時間戳推算（避免秒精度碰撞）
    anchor_msg_id = anchor_id if anchor_id else None

    tags = db.query(UserTag).filter(UserTag.line_user_id == line_user_id).all() if line_user_id else []
    all_user_tags = {}
    for u in users:
        utags = db.query(UserTag).filter(UserTag.line_user_id == u.line_user_id).all()
        all_user_tags[u.line_user_id] = utags
    # 建立引用資料對照（message_log_id → MessageQuote）
    quote_map = {}
    if messages:
        msg_ids = [m.id for m in messages]
        quotes = db.query(MessageQuote).filter(MessageQuote.message_log_id.in_(msg_ids)).all()
        quote_map = {q.message_log_id: q for q in quotes}
    presets = db.query(PresetMessage).order_by(PresetMessage.sort_order.asc(), PresetMessage.id.asc()).all()
    return templates.TemplateResponse(request=request, name="user_history.html", context={
        "user": user,
        "users": users,
        "messages": messages,
        "selected_user": selected_user,
        "selected_uid": line_user_id,
        "anchor_msg_id": anchor_msg_id,
        "tags": tags,
        "all_user_tags": all_user_tags,
        "tag_colors": TAG_COLORS,
        "tag_color_labels": TAG_COLOR_LABELS,
        "is_search": is_search,
        "search_results": search_results,
        "uid_name_map": uid_name_map if is_search else {},
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
        "presets": presets,
        "quote_map": quote_map,
        "pending_reply_count": get_reply_count(db),
        "todo_count": get_todo_count(db),
    })


# ── 聊天記錄輪詢（自動更新用） ────────────────────────────────────
@router.get("/dashboard/history/poll")
def history_poll(
    line_user_id: str,
    after_id: int = 0,
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    messages = db.query(MessageLog).filter(
        MessageLog.line_user_id == line_user_id,
        MessageLog.id > after_id,
    ).order_by(MessageLog.created_at.asc()).all()

    if not messages:
        return JSONResponse({"messages": []})

    staff_ids = [m.id for m in messages if m.message_type == "staff"]
    quote_map = {}
    if staff_ids:
        quotes = db.query(MessageQuote).filter(MessageQuote.message_log_id.in_(staff_ids)).all()
        quote_map = {q.message_log_id: q for q in quotes}

    result = []
    for msg in messages:
        item = {
            "id": msg.id,
            "direction": msg.direction,
            "message_type": msg.message_type,
            "content": msg.content,
            "created_at": msg.created_at.strftime("%H:%M"),
            "date": msg.created_at.strftime("%Y/%m/%d"),
        }
        if msg.id in quote_map:
            q = quote_map[msg.id]
            item["quote"] = {"sender": q.quote_sender, "preview": q.quote_preview}
        result.append(item)

    return JSONResponse({"messages": result})


# ── 用戶標籤與備註 ───────────────────────────────────────────────
TAG_COLORS = ["primary", "danger", "warning", "success", "info", "secondary", "dark"]
TAG_COLOR_LABELS = {"primary": "藍", "danger": "紅", "warning": "橘", "success": "綠", "info": "青", "secondary": "灰", "dark": "黑"}

@router.post("/dashboard/user/{line_user_id}/tag/add")
def user_tag_add(
    line_user_id: str,
    tag_name: str = Form(...),
    color: str = Form("secondary"),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    tag_name = tag_name.strip()
    if tag_name and color in TAG_COLORS:
        exists = db.query(UserTag).filter(
            UserTag.line_user_id == line_user_id,
            UserTag.tag_name == tag_name,
        ).first()
        if not exists:
            db.add(UserTag(line_user_id=line_user_id, tag_name=tag_name, color=color, created_by=user.display_name))
            db.commit()
    from_page = db.query(UserTag).filter(UserTag.line_user_id == line_user_id).first()
    return RedirectResponse(url=f"/dashboard/history?line_user_id={line_user_id}&tag_updated=1", status_code=302)


@router.post("/dashboard/user/{line_user_id}/tag/remove/{tag_id}")
def user_tag_remove(
    line_user_id: str,
    tag_id: int,
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    tag = db.query(UserTag).filter(UserTag.id == tag_id, UserTag.line_user_id == line_user_id).first()
    if tag:
        db.delete(tag)
        db.commit()
    return RedirectResponse(url=f"/dashboard/history?line_user_id={line_user_id}&tag_updated=1", status_code=302)


@router.post("/dashboard/user/{line_user_id}/note")
def user_note_update(
    line_user_id: str,
    note: str = Form(""),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    lu = db.query(LineUser).filter(LineUser.line_user_id == line_user_id).first()
    if lu:
        lu.note = note.strip() or None
        db.commit()
    return RedirectResponse(url=f"/dashboard/history?line_user_id={line_user_id}&note_saved=1", status_code=302)


# ── 從聊天記錄直接回覆 ──────────────────────────────────────────
@router.post("/dashboard/history/reply")
def history_reply(
    line_user_id: str = Form(...),
    message: str = Form(...),
    quote_name: str = Form(""),
    quote_text: str = Form(""),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    from app.handlers import push_message
    reply = message.strip()
    if not reply:
        return RedirectResponse(url=f"/dashboard/history?line_user_id={line_user_id}", status_code=302)

    has_quote = bool(quote_name.strip() and quote_text.strip())
    if has_quote:
        preview = quote_text.strip()
        if len(preview) > 50:
            preview = preview[:50] + "…"
        # 傳送給 LINE 的文字加上引用提示；DB 僅存純回覆文字
        line_text = f"「↩ {quote_name.strip()}：{preview}」\n{reply}"
    else:
        line_text = reply

    try:
        msg_log = push_message(line_user_id, line_text, db, msg_type="staff", log_content=reply)
        if has_quote and msg_log:
            db.add(MessageQuote(
                message_log_id=msg_log.id,
                quote_sender=quote_name.strip(),
                quote_preview=quote_text.strip()[:200],
            ))
            db.commit()
    except Exception as e:
        return RedirectResponse(
            url=f"/dashboard/history?line_user_id={line_user_id}&error={str(e)[:80]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"/dashboard/history?line_user_id={line_user_id}&replied=1",
        status_code=302,
    )


# ── 預設訊息管理 ──────────────────────────────────────────────
@router.get("/dashboard/presets", response_class=HTMLResponse)
def preset_list(request: Request, db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    presets = db.query(PresetMessage).order_by(PresetMessage.sort_order.asc(), PresetMessage.id.asc()).all()
    categories = [c[0] for c in db.query(PresetMessage.category).distinct().all() if c[0]]
    return templates.TemplateResponse(request=request, name="user_presets.html", context={
        "user": user,
        "presets": presets,
        "categories": categories,
        "pending_reply_count": get_reply_count(db),
        "todo_count": get_todo_count(db),
    })


@router.post("/dashboard/presets/create")
def preset_create(
    title: str = Form(...),
    content: str = Form(...),
    category: str = Form("一般"),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    db.add(PresetMessage(
        title=title.strip(),
        content=content.strip(),
        category=category.strip() or "一般",
        sort_order=sort_order,
        created_by=user.display_name,
    ))
    db.commit()
    return RedirectResponse(url="/dashboard/presets?created=1", status_code=302)


@router.post("/dashboard/presets/{preset_id}/update")
def preset_update(
    preset_id: int,
    title: str = Form(...),
    content: str = Form(...),
    category: str = Form("一般"),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    p = db.query(PresetMessage).filter(PresetMessage.id == preset_id).first()
    if p:
        p.title = title.strip()
        p.content = content.strip()
        p.category = category.strip() or "一般"
        p.sort_order = sort_order
        db.commit()
    return RedirectResponse(url="/dashboard/presets?updated=1", status_code=302)


@router.post("/dashboard/presets/{preset_id}/delete")
def preset_delete(
    preset_id: int,
    db: Session = Depends(get_db),
    user: SystemUser = Depends(current_user_dep),
):
    p = db.query(PresetMessage).filter(PresetMessage.id == preset_id).first()
    if p:
        db.delete(p)
        db.commit()
    return RedirectResponse(url="/dashboard/presets?deleted=1", status_code=302)


# ── 已訓練 Q&A ────────────────────────────────────────────────
@router.get("/dashboard/trained", response_class=HTMLResponse)
def trained_list(request: Request, category: str = "", db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    query = db.query(QAPair).filter(QAPair.is_trained == True)
    if category:
        query = query.filter(QAPair.category == category)
    qa_list = query.order_by(QAPair.trained_at.desc()).all()
    categories = db.query(QAPair.category).filter(QAPair.is_trained == True).distinct().all()
    return templates.TemplateResponse(request=request, name="user_trained.html", context={
        "user": user,
        "qa_list": qa_list,
        "categories": [c[0] for c in categories],
        "current_category": category,
        "pending_reply_count": get_reply_count(db),
        "todo_count": get_todo_count(db),
    })


@router.get("/dashboard/trained/export")
def trained_export(fmt: str = "csv", category: str = "", db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    query = db.query(QAPair).filter(QAPair.is_trained == True)
    if category:
        query = query.filter(QAPair.category == category)
    qa_list = query.order_by(QAPair.trained_at.desc()).all()

    if fmt == "xls":
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "已訓練Q&A"
        ws.append(["#", "問題", "回答", "關鍵字", "分類", "命中次數", "訓練時間"])
        for qa in qa_list:
            ws.append([
                qa.id, qa.question, qa.answer,
                qa.keywords or "",
                qa.category or "",
                qa.hit_count,
                qa.trained_at.strftime("%Y-%m-%d %H:%M") if qa.trained_at else "",
            ])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=trained_qa.xlsx"},
        )
    else:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["#", "問題", "回答", "關鍵字", "分類", "命中次數", "訓練時間"])
        for qa in qa_list:
            writer.writerow([
                qa.id, qa.question, qa.answer,
                qa.keywords or "",
                qa.category or "",
                qa.hit_count,
                qa.trained_at.strftime("%Y-%m-%d %H:%M") if qa.trained_at else "",
            ])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue().encode("utf-8-sig")]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=trained_qa.csv"},
        )
