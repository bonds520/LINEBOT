import csv
import io
import os
import secrets
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import QAPair, PendingQuestion, MessageLog, LineUser, SystemUser
from app.auth import hash_password

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="templates")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1234")
SESSION_TOKEN = secrets.token_hex(32)
_sessions: set = set()


def check_auth(request: Request):
    token = request.cookies.get("admin_token")
    if token not in _sessions:
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})
    return True


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")


@router.post("/login")
def do_login(request: Request, password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        token = secrets.token_hex(32)
        _sessions.add(token)
        resp = RedirectResponse(url="/admin", status_code=302)
        resp.set_cookie("admin_token", token, httponly=True, max_age=86400)
        return resp
    return templates.TemplateResponse(request=request, name="login.html", context={"error": "密碼錯誤"})


@router.get("/logout")
def logout(request: Request):
    token = request.cookies.get("admin_token")
    _sessions.discard(token)
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie("admin_token")
    return resp


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), _=Depends(check_auth)):
    qa_count = db.query(func.count(QAPair.id)).filter(QAPair.is_active == True).scalar()
    pending_count = db.query(func.count(PendingQuestion.id)).filter(PendingQuestion.status == "pending").scalar()
    user_count = db.query(func.count(LineUser.id)).scalar()
    msg_count = db.query(func.count(MessageLog.id)).scalar()
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "qa_count": qa_count,
        "pending_count": pending_count,
        "user_count": user_count,
        "msg_count": msg_count,
    })


@router.get("/qa", response_class=HTMLResponse)
def qa_list(request: Request, category: str = "", db: Session = Depends(get_db), _=Depends(check_auth)):
    query = db.query(QAPair)
    if category:
        query = query.filter(QAPair.category == category)
    qa_list = query.order_by(QAPair.id.desc()).all()
    categories = db.query(QAPair.category).distinct().all()
    return templates.TemplateResponse(request=request, name="qa_list.html", context={
        "qa_list": qa_list,
        "categories": [c[0] for c in categories],
        "current_category": category,
    })


@router.post("/qa/create")
def qa_create(
    question: str = Form(...),
    answer: str = Form(...),
    keywords: str = Form(""),
    category: str = Form("一般"),
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    qa = QAPair(question=question, answer=answer, keywords=keywords, category=category)
    db.add(qa)
    db.commit()
    return RedirectResponse(url="/admin/qa", status_code=302)


@router.post("/qa/{qa_id}/update")
def qa_update(
    qa_id: int,
    question: str = Form(...),
    answer: str = Form(...),
    keywords: str = Form(""),
    category: str = Form("一般"),
    is_active: bool = Form(True),
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    qa = db.query(QAPair).filter(QAPair.id == qa_id).first()
    if qa:
        qa.question = question
        qa.answer = answer
        qa.keywords = keywords
        qa.category = category
        qa.is_active = is_active
        db.commit()
    return RedirectResponse(url="/admin/qa", status_code=302)


@router.post("/qa/{qa_id}/delete")
def qa_delete(qa_id: int, db: Session = Depends(get_db), _=Depends(check_auth)):
    qa = db.query(QAPair).filter(QAPair.id == qa_id).first()
    if qa:
        db.delete(qa)
        db.commit()
    return RedirectResponse(url="/admin/qa", status_code=302)


@router.post("/qa/import")
async def qa_import(file: UploadFile = File(...), db: Session = Depends(get_db), _=Depends(check_auth)):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    count = 0
    for row in reader:
        if "question" in row and "answer" in row:
            qa = QAPair(
                question=row["question"].strip(),
                answer=row["answer"].strip(),
                keywords=row.get("keywords", "").strip(),
                category=row.get("category", "一般").strip(),
            )
            db.add(qa)
            count += 1
    db.commit()
    return RedirectResponse(url=f"/admin/qa?imported={count}", status_code=302)


@router.get("/pending", response_class=HTMLResponse)
def pending_list(request: Request, db: Session = Depends(get_db), _=Depends(check_auth)):
    pending = db.query(PendingQuestion).order_by(PendingQuestion.created_at.desc()).limit(100).all()
    return templates.TemplateResponse(request=request, name="pending.html", context={"pending": pending})


@router.post("/pending/{pid}/handle")
def pending_handle(pid: int, note: str = Form(""), db: Session = Depends(get_db), _=Depends(check_auth)):
    p = db.query(PendingQuestion).filter(PendingQuestion.id == pid).first()
    if p:
        p.status = "handled"
        p.note = note
        db.commit()
    return RedirectResponse(url="/admin/pending", status_code=302)


@router.get("/users", response_class=HTMLResponse)
def user_list(request: Request, db: Session = Depends(get_db), _=Depends(check_auth)):
    users = db.query(SystemUser).order_by(SystemUser.id.desc()).all()
    return templates.TemplateResponse(request=request, name="user_mgmt.html", context={"users": users})


@router.post("/users/create")
def user_create(
    username: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    existing = db.query(SystemUser).filter(SystemUser.username == username).first()
    if existing:
        return RedirectResponse(url="/admin/users?error=帳號已存在", status_code=302)
    user = SystemUser(
        username=username,
        display_name=display_name,
        hashed_password=hash_password(password),
        role=role,
    )
    db.add(user)
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{uid}/update")
def user_update(
    uid: int,
    display_name: str = Form(...),
    role: str = Form("user"),
    is_active: bool = Form(True),
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    user = db.query(SystemUser).filter(SystemUser.id == uid).first()
    if user:
        user.display_name = display_name
        user.role = role
        user.is_active = is_active
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{uid}/reset-password")
def user_reset_password(
    uid: int,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    user = db.query(SystemUser).filter(SystemUser.id == uid).first()
    if user:
        user.hashed_password = hash_password(new_password)
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{uid}/delete")
def user_delete(uid: int, db: Session = Depends(get_db), _=Depends(check_auth)):
    user = db.query(SystemUser).filter(SystemUser.id == uid).first()
    if user:
        db.delete(user)
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)
