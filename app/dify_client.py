import os
import httpx
import logging

logger = logging.getLogger(__name__)

DIFY_API_URL = os.getenv("DIFY_API_URL", "http://localhost:8080/v1")
DIFY_API_KEY = os.getenv("DIFY_API_KEY", "")
_TIMEOUT = 30.0


def chat(user_id: str, message: str) -> str:
    """
    呼叫 Dify Chat API，回傳 AI 回覆文字。
    失敗時回傳 None，由呼叫端決定 fallback 行為。
    """
    if not DIFY_API_KEY:
        logger.warning("DIFY_API_KEY 未設定，無法呼叫 Dify")
        return None

    url = f"{DIFY_API_URL.rstrip('/')}/chat-messages"
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "inputs": {},
        "query": message,
        "response_mode": "blocking",
        "conversation_id": "",
        "user": user_id,
    }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("answer", "").strip() or None
    except httpx.TimeoutException:
        logger.error("Dify API 逾時（%.1fs）", _TIMEOUT)
        return None
    except httpx.HTTPStatusError as e:
        logger.error("Dify API 回傳錯誤 %s：%s", e.response.status_code, e.response.text[:200])
        return None
    except Exception as e:
        logger.error("Dify API 呼叫失敗：%s", e)
        return None
