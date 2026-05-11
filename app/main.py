from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent,
    FollowEvent, UnfollowEvent
)
from app.database import get_db, engine
from app import models
from app import handlers
from app.admin import router as admin_router
from app.user_panel import router as user_router
from dotenv import load_dotenv
import os
import logging

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="LINE Bot", version="1.0.0")
app.include_router(admin_router)
app.include_router(user_router)

parser = WebhookParser(os.getenv("LINE_CHANNEL_SECRET"))


@app.get("/")
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/login", status_code=302)


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "LINE Bot"}


@app.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        events = parser.parse(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        logger.warning("Invalid signature received")
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in events:
        try:
            if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
                handlers.handle_text_message(event, db)
            elif isinstance(event, FollowEvent):
                handlers.handle_follow_event(event, db)
            elif isinstance(event, UnfollowEvent):
                handlers.handle_unfollow_event(event, db)
        except Exception as e:
            logger.error(f"Error handling event: {e}")

    return {"status": "ok"}
