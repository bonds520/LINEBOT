import csv
import io
from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import SystemUser, QAPair, PendingQuestion
from app.auth import verify_password, create_session, destroy_session, get_current_user

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def current_user_dep(request: Request, db: Session = Depends(get_db)) -> SystemUser:
    return get_current_user(request, db)


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
    return templates.TemplateResponse(request=request, name="user_dashboard.html", context={
        "user": user,
        "qa_count": qa_count,
        "trained_count": trained_count,
        "pending_count": pending_count,
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


# ── 建立 Q&A（手動新增訓練資料）────────────────────────────────
@router.get("/dashboard/create-qa", response_class=HTMLResponse)
def create_qa_page(request: Request, db: Session = Depends(get_db), user: SystemUser = Depends(current_user_dep)):
    categories = db.query(QAPair.category).distinct().all()
    return templates.TemplateResponse(request=request, name="user_create_qa.html", context={
        "user": user,
        "categories": [c[0] for c in categories],
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
            qa = QAPair(
                question=row["question"].strip(),
                answer=row["answer"].strip(),
                keywords=row.get("keywords", "").strip(),
                category=row.get("category", "一般").strip(),
            )
            db.add(qa)
            count += 1
    db.commit()
    return RedirectResponse(url=f"/dashboard/create-qa?imported={count}", status_code=302)


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
