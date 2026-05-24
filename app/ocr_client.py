import base64
import os
import time
import logging
import httpx

logger = logging.getLogger(__name__)

OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b")
_TIMEOUT       = 60.0

# 文件類型關鍵字對應
_DOC_TYPE_KEYWORDS = {
    "死亡證明書":   ["死亡證明", "死亡日期", "死亡原因"],
    "火化許可證":   ["火化許可", "火化", "許可證字號"],
    "遷葬證明":     ["遷葬", "起掘遷葬"],
    "起掘許可證":   ["起掘許可", "起掘"],
    "身份證明文件": ["身分證", "護照", "居留證", "戶籍謄本"],
}

_OCR_PROMPT = (
    "請仔細辨識這份文件的所有文字內容，以繁體中文輸出。\n"
    "先標示【文件類型】（如：死亡證明書、火化許可證、遷葬證明、起掘許可證、身份證明文件、其他），\n"
    "然後條列所有辨識到的重要資訊（姓名、日期、證件號碼、機關名稱等）。\n"
    "若圖片模糊或無法辨識，請回覆：【無法辨識】"
)

_MOCK_RESULT = (
    "【文件類型】死亡證明書\n"
    "【測試模式 - Ollama 未設定】\n"
    "姓　　名：王〇〇\n"
    "死亡日期：民國 115 年 5 月 24 日\n"
    "死亡地點：台中市〇〇醫院\n"
    "死亡原因：〇〇〇\n"
    "開立機關：台中市〇〇區戶政事務所"
)


def _detect_doc_type(ocr_text: str) -> str:
    for doc_type, keywords in _DOC_TYPE_KEYWORDS.items():
        if any(kw in ocr_text for kw in keywords):
            return doc_type
    return "未分類"


def _ocr_via_ollama(image_bytes: bytes) -> str | None:
    if not OLLAMA_API_URL:
        return None
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": _OCR_PROMPT,
        "images": [b64],
        "stream": False,
    }
    try:
        resp = httpx.post(
            f"{OLLAMA_API_URL.rstrip('/')}/api/generate",
            json=payload,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip() or None
    except httpx.ConnectError:
        logger.warning("Ollama 未啟動或無法連線（%s），使用 Mock 模式", OLLAMA_API_URL)
        return None
    except httpx.TimeoutException:
        logger.error("Ollama OCR 逾時（%.0fs）", _TIMEOUT)
        return None
    except Exception as e:
        logger.error("Ollama OCR 失敗：%s", e)
        return None


def extract_from_image(file_path: str) -> tuple[str, str]:
    """回傳 (ocr_result, document_type)"""
    try:
        with open(file_path, "rb") as f:
            image_bytes = f.read()
    except OSError as e:
        logger.error("讀取圖片失敗：%s", e)
        return "【讀取檔案失敗】", "未分類"

    result = _ocr_via_ollama(image_bytes)
    if result is None:
        # Mock 模式：Ollama 未設定或連線失敗
        time.sleep(2)  # 模擬處理延遲
        result = _MOCK_RESULT

    doc_type = _detect_doc_type(result)
    return result, doc_type


def extract_from_pdf(file_path: str) -> tuple[str, str]:
    """回傳 (ocr_result, document_type)"""
    try:
        import fitz  # pymupdf
        doc = fitz.open(file_path)
        texts = [page.get_text().strip() for page in doc]
        doc.close()
        embedded_text = "\n".join(t for t in texts if t)
    except ImportError:
        logger.warning("pymupdf 未安裝，PDF 改走圖片 OCR")
        embedded_text = ""
    except Exception as e:
        logger.error("PDF 文字萃取失敗：%s", e)
        embedded_text = ""

    if len(embedded_text) > 50:
        # 有內嵌文字，直接使用
        doc_type = _detect_doc_type(embedded_text)
        return embedded_text, doc_type

    # 無內嵌文字，轉圖片再 OCR
    try:
        import fitz
        doc = fitz.open(file_path)
        page = doc[0]  # 只取第一頁
        pix = page.get_pixmap(dpi=150)
        image_bytes = pix.tobytes("jpeg")
        doc.close()
    except Exception as e:
        logger.error("PDF 轉圖片失敗：%s", e)
        if not OLLAMA_API_URL:
            time.sleep(2)
            return _MOCK_RESULT, _detect_doc_type(_MOCK_RESULT)
        return "【PDF 轉換失敗】", "未分類"

    result = _ocr_via_ollama(image_bytes)
    if result is None:
        time.sleep(2)
        result = _MOCK_RESULT

    doc_type = _detect_doc_type(result)
    return result, doc_type


def extract(file_path: str) -> tuple[str, str]:
    """統一入口：自動判斷圖片或 PDF，回傳 (ocr_result, document_type)"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        return extract_from_pdf(file_path)
    return extract_from_image(file_path)
