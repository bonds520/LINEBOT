import base64
import io
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
    "【測試模式 - OCR 引擎未啟動】\n"
    "姓　　名：王〇〇\n"
    "死亡日期：民國 115 年 5 月 24 日\n"
    "死亡地點：台中市〇〇醫院\n"
    "死亡原因：〇〇〇\n"
    "開立機關：台中市〇〇區戶政事務所"
)

# EasyOCR reader 單例（避免每次重新載入模型）
_easyocr_reader = None


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        try:
            import easyocr
            logger.info("初始化 EasyOCR（繁體中文 + 英文），首次載入需要約 30 秒...")
            _easyocr_reader = easyocr.Reader(["ch_tra", "en"], gpu=False, verbose=False)
            logger.info("EasyOCR 初始化完成")
        except ImportError:
            logger.warning("easyocr 未安裝")
        except Exception as e:
            logger.error("EasyOCR 初始化失敗：%s", e)
    return _easyocr_reader


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
        logger.warning("Ollama 未啟動或無法連線（%s），嘗試 EasyOCR", OLLAMA_API_URL)
        return None
    except httpx.TimeoutException:
        logger.error("Ollama OCR 逾時（%.0fs）", _TIMEOUT)
        return None
    except Exception as e:
        logger.error("Ollama OCR 失敗：%s", e)
        return None


def _ocr_via_easyocr(image_bytes: bytes) -> str | None:
    reader = _get_easyocr_reader()
    if reader is None:
        return None
    try:
        results = reader.readtext(image_bytes, detail=0, paragraph=True)
        text = "\n".join(results).strip()
        return text or None
    except Exception as e:
        logger.error("EasyOCR 辨識失敗：%s", e)
        return None


def _run_ocr(image_bytes: bytes) -> str:
    """依序嘗試：Ollama → EasyOCR → Mock"""
    result = _ocr_via_ollama(image_bytes)
    if result:
        return result

    logger.info("使用 EasyOCR 進行辨識")
    result = _ocr_via_easyocr(image_bytes)
    if result:
        return result

    logger.warning("所有 OCR 引擎失敗，使用 Mock 模式")
    time.sleep(1)
    return _MOCK_RESULT


def extract_from_image(file_path: str) -> tuple[str, str]:
    """回傳 (ocr_result, document_type)"""
    try:
        with open(file_path, "rb") as f:
            image_bytes = f.read()
    except OSError as e:
        logger.error("讀取圖片失敗：%s", e)
        return "【讀取檔案失敗】", "未分類"

    result = _run_ocr(image_bytes)
    doc_type = _detect_doc_type(result)
    return result, doc_type


def extract_from_pdf(file_path: str) -> tuple[str, str]:
    """回傳 (ocr_result, document_type)"""
    embedded_text = ""
    try:
        import fitz  # pymupdf
        doc = fitz.open(file_path)
        texts = [page.get_text().strip() for page in doc]
        doc.close()
        embedded_text = "\n".join(t for t in texts if t)
    except ImportError:
        logger.warning("pymupdf 未安裝，PDF 改走圖片 OCR")
    except Exception as e:
        logger.error("PDF 文字萃取失敗：%s", e)

    if len(embedded_text) > 50:
        doc_type = _detect_doc_type(embedded_text)
        return embedded_text, doc_type

    # 無內嵌文字，轉第一頁圖片再 OCR
    try:
        import fitz
        doc = fitz.open(file_path)
        pix = doc[0].get_pixmap(dpi=150)
        image_bytes = pix.tobytes("jpeg")
        doc.close()
    except Exception as e:
        logger.error("PDF 轉圖片失敗：%s", e)
        return "【PDF 轉換失敗】", "未分類"

    result = _run_ocr(image_bytes)
    doc_type = _detect_doc_type(result)
    return result, doc_type


def extract(file_path: str) -> tuple[str, str]:
    """統一入口：自動判斷圖片或 PDF，回傳 (ocr_result, document_type)"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        return extract_from_pdf(file_path)
    return extract_from_image(file_path)
