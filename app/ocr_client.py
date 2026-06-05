import base64
import os
import re
import time
import logging
import httpx

logger = logging.getLogger(__name__)

OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b")
_TIMEOUT       = 180.0

# 文件類型關鍵字（依優先順序排列，越具體的關鍵字放越前面）
_DOC_TYPE_KEYWORDS = {
    "死亡證明書": [
        "死亡證明書", "衛生福利部", "檢察官相驗", "死亡原因", "解剖", "相驗屍體", "死亡證明",
    ],
    "火化許可證": [
        "火化許可證", "埋葬火化許可", "殯葬管理所", "准予火化", "火化許可",
    ],
    "遷出證明書": [
        "遷出證明書", "骨灰", "骨骸", "進塔證明", "遷出原因", "遷葬", "起掘遷葬",
    ],
    "起掘許可證": [
        "起掘許可證", "墳墓起掘", "起掘地點", "公墓", "撿骨", "起掘許可", "起掘",
    ],
    "國民身分證": [
        "中華民國國民身分證", "國民身分證", "統一編號", "役別", "配偶", "身分證",
        "出生年月日", "發證日期", "換發",    # 正面特有欄位
    ],
}

# OCR 提示詞（同時處理圖片分類與文件辨識）
_OCR_PROMPT = (
    "請判斷這張圖片的類型，並依照以下規則回應：\n\n"
    "★ 若圖片為人臉特寫、人像照，僅回覆：【大頭照】\n"
    "★ 若圖片為風景、食物、物品、日常生活等非文件照片，僅回覆：【生活照】\n"
    "★ 若圖片為正式文件（公文、證明書、身分證等），請仔細辨識所有文字，以繁體中文依序輸出：\n\n"
    "【文件類型】死亡證明書／火化許可證／遷出證明書／起掘許可證／國民身分證／其他\n"
    "【亡者姓名】\n"
    "【身分證字號】（1碼大寫英文+9碼數字，無則填「無」）\n"
    "【出生日期】（YYYY-MM-DD，民國年請換算：西元=民國+1911，無則填「無」）\n"
    "【死亡日期】（YYYY-MM-DD，民國年請換算，無則填「無」）\n"
    "【申請人姓名】（無則填「無」）\n"
    "【完整辨識文字】（條列文件上的所有其他可見文字）\n\n"
    "注意：遷出證明書的亡者姓名需自動去除「故」與「君」字。\n"
    "若圖片模糊無法辨識文字，僅回覆：【無法辨識】"
)

# Mock — 僅在所有 OCR 引擎均不可用時使用
_MOCK_RESULT = (
    "【文件類型】死亡證明書\n"
    "【亡者姓名】王〇〇\n"
    "【身分證字號】無\n"
    "【出生日期】無\n"
    "【死亡日期】民國 115 年 5 月 24 日\n"
    "【申請人姓名】無\n"
    "【完整辨識文字】\n"
    "【測試模式 - OCR 引擎未啟動】\n"
    "死亡地點：台中市〇〇醫院\n"
    "開立機關：台中市〇〇區戶政事務所"
)

# OCR 成功但圖片無文字（風景照等）
NO_TEXT_RESULT = (
    "【未找到文字】\n\n"
    "📋 未在圖片中找到可辨識的文字內容。\n\n"
    "若您要上傳文件，請確認圖片清晰且文字可見，\n"
    "或重新拍攝後上傳。"
)

# 模型明確回應「這不是文件」
NON_DOCUMENT_RESULT = "【非文件圖片】"
LIFE_PHOTO_RESULT   = "【生活照】"
ID_PHOTO_RESULT     = "【大頭照】"

# ── 文件類型驗證：檢查 OCR 原文是否真的含有對應關鍵字 ──────────────
# 文件類型驗證規則：
# 每種文件需要「必要關鍵字組合」同時出現，防止一般文字誤判
# rules: list of (required_keywords, min_matches)
#   - required_keywords: 這組裡至少需要符合 min_matches 個
#   - 所有 rule 都必須通過
_DOC_VALIDATION_RULES = {
    "死亡證明書": [
        (["死亡證明書", "死亡證明"], 1),       # 必須有「死亡證明」字樣
        (["死亡", "死者", "亡者", "往生"], 1),  # 且有死亡相關詞
    ],
    "火化許可證": [
        (["火化許可證", "火化許可", "准予火化"], 1),
        (["火化", "殯葬", "遺體", "骨灰"], 1),
    ],
    "遷出證明書": [
        (["遷出證明書", "遷出證明", "進塔證明"], 1),
        (["遷出", "骨灰", "骨骸", "遷葬"], 1),
    ],
    "起掘許可證": [
        (["起掘許可證", "起掘許可", "起掘"], 1),
        (["公墓", "撿骨", "墳墓", "骨骸", "起掘地點"], 1),
    ],
    "國民身分證": [
        # 條件一：正面標題、背面役別、或正面特有欄位（任一即可）
        (["中華民國國民身分證", "國民身分證", "身分證", "役別",
          "出生年月日", "發證日期", "換發"], 1),
        # 條件二：正面或背面任一欄位標籤
        (["統一編號", "役別", "配偶", "出生地", "住址",
          "出生年月日", "出生日期", "發證日期", "發證",
          "性別", "姓名", "換發", "戶籍地址", "父", "母"], 1),
    ],
}

def _validate_doc_type(doc_type: str, ocr_text: str) -> bool:
    """
    多重關鍵字驗證。
    重要：先移除模型自己產生的【標籤】行，只對實際文件內容做驗證，
    避免模型寫了「【文件類型】死亡證明書」後自我驗證通過的問題。
    """
    if doc_type not in _DOC_VALIDATION_RULES:
        return True  # 「其他」或「未分類」不需驗證

    # 移除所有 【標籤】內容行（如「【文件類型】死亡證明書」）
    clean_text = re.sub(r'【[^】]+】[^\n]*', '', ocr_text)
    logger.debug("驗證用文字（去除標籤後）前80字：%s", clean_text[:80])

    for required_keywords, min_matches in _DOC_VALIDATION_RULES[doc_type]:
        matched = sum(1 for kw in required_keywords if kw in clean_text)
        if matched < min_matches:
            logger.warning(
                "驗證失敗：%s 需要 %s 中至少 %d 個，原始文字中只符合 %d 個",
                doc_type, required_keywords, min_matches, matched
            )
            return False
    return True

# ── 日期 / 姓名 / 身分證 正規表示式 ──────────────────────────────────
_ROC_DATE_RE = re.compile(r'民國\s*(\d{1,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_CE_DATE_RE  = re.compile(r'(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})')
_ID_RE       = re.compile(r'[A-Z][0-9]{9}')
_NAME_RE     = re.compile(r'[一-鿿]{2,4}')


def _roc_to_ce(y: str, m: str, d: str) -> str:
    return f"{int(y)+1911}-{int(m):02d}-{int(d):02d}"


def _find_date_near(text: str, anchors: list) -> str | None:
    for anchor in anchors:
        idx = text.find(anchor)
        if idx == -1:
            continue
        snippet = text[idx: idx + 60]
        m = _ROC_DATE_RE.search(snippet)
        if m:
            return _roc_to_ce(*m.groups())
        m = _CE_DATE_RE.search(snippet)
        if m:
            y, mo, d = m.groups()
            return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


def _find_name_near(text: str, anchors: list) -> str | None:
    for anchor in anchors:
        idx = text.find(anchor)
        if idx == -1:
            continue
        # 取錨點後的文字，跳過冒號/空白
        snippet = text[idx + len(anchor): idx + len(anchor) + 20]
        snippet = re.sub(r'^[：:：\s　]+', '', snippet)
        m = _NAME_RE.search(snippet)
        if m:
            name = m.group().lstrip('故').rstrip('君')
            return name if name else None
    return None


def _detect_doc_type(ocr_text: str) -> str:
    for doc_type, keywords in _DOC_TYPE_KEYWORDS.items():
        if any(kw in ocr_text for kw in keywords):
            return doc_type
    return "未分類"


def _parse_structured(text: str) -> tuple[str | None, dict]:
    """解析 Ollama 結構化標籤輸出；若無標籤則回傳 (None, {})。
    支援兩種格式：
      格式 A：【文件類型】火化許可證  （OCR prompt 預期格式）
      格式 B：【火化許可證】          （模型直接用文件名稱當標籤）
    """
    # 格式 B：模型直接輸出 【已知文件類型】 作為首行
    doc_type_from_tag = next(
        (t for t in _DOC_TYPE_KEYWORDS if f"【{t}】" in text), None
    )
    # 格式 A 需要 【文件類型】 標籤
    if "【文件類型】" not in text and not doc_type_from_tag:
        return None, {}

    def _tag(name: str) -> str | None:
        m = re.search(rf'【{name}】\s*([^\n【]+)', text)
        v = m.group(1).strip() if m else None
        if v:
            v = re.sub(r'^[：:\-\s]+', '', v).strip()  # 去掉 LLM 可能加的前導冒號/破折號
        return None if v in (None, "無", "null", "—", "N/A", "") else v

    raw_type = _tag("文件類型") or ""
    doc_type = next((t for t in _DOC_TYPE_KEYWORDS if t in raw_type), None) or doc_type_from_tag
    if not doc_type:
        doc_type = _detect_doc_type(text)

    fields = {
        "亡者姓名":   _tag("亡者姓名"),
        "身分證字號": _tag("身分證字號"),
        "出生日期":   _tag("出生日期"),
        "死亡日期":   _tag("死亡日期"),
        "申請人姓名": _tag("申請人姓名"),
    }
    return doc_type, fields


def _extract_fields_regex(text: str, doc_type: str) -> dict:
    """EasyOCR 路徑：以正規表示式從原始文字擷取欄位。"""
    id_m = _ID_RE.search(text)

    deceased_anchors = {
        "死亡證明書": ["死者姓名", "姓名"],
        "火化許可證": ["死者姓名", "姓名", "茲准予"],
        "遷出證明書": ["亡者姓名", "先人姓名", "死者"],
        "起掘許可證": ["亡者姓名", "墓主姓名"],
        "國民身分證": ["姓名"],
    }
    applicant_anchors = {
        "死亡證明書": ["聲請人", "通知人", "親屬"],
        "火化許可證": ["申請人", "領屍人", "申辦人"],
        "遷出證明書": ["申請人", "原寄存人", "關係人"],
        "起掘許可證": ["申請人", "起掘人", "關係人"],
        "國民身分證": ["姓名"],
    }

    return {
        "亡者姓名":   _find_name_near(text, deceased_anchors.get(doc_type, ["姓名", "亡者姓名"])),
        "身分證字號": id_m.group() if id_m else None,
        "出生日期":   _find_date_near(text, ["出生年月日", "出生日期"]),
        "死亡日期":   _find_date_near(text, ["死亡年月日", "死亡日期", "死亡時間"]),
        "申請人姓名": _find_name_near(text, applicant_anchors.get(doc_type, ["申請人"])),
    }


def _detect_id_card_side(ocr_text: str) -> str:
    """判斷國民身分證是正面或背面。

    正面欄位：中華民國國民身分證、姓名、出生年月日、性別、發證日期、換發
    背面欄位：役別、出生地、住址、配偶、父、母

    優先判斷正面：有明確的「中華民國國民身分證」標題，或正面欄位 ≥ 2 個
    其次判斷背面：背面欄位 ≥ 2 個
    """
    front_keywords = [
        "中華民國國民身分證", "國民身分證",    # 正面標題
        "出生年月日",                          # 正面欄位標籤
        "性別",                               # 正面欄位標籤
        "發證日期", "換發",                    # 正面發證資訊
    ]
    back_keywords = [
        "役別",     # 背面特有（軍人役別）
        "出生地",   # 背面欄位
        "住址",     # 背面欄位
        "配偶",     # 背面欄位
        "父",       # 背面欄位
        "母",       # 背面欄位（注意：會誤匹配含「母」的文字，依靠 count 過濾）
    ]
    front_count = sum(1 for kw in front_keywords if kw in ocr_text)
    back_count  = sum(1 for kw in back_keywords  if kw in ocr_text)

    logger.debug("身分證正/背面判斷：正面符合 %d 個，背面符合 %d 個", front_count, back_count)

    # 背面特有欄位 ≥ 2 且多於正面 → 背面
    if back_count >= 2 and back_count >= front_count:
        return "背面"
    # 否則預設正面
    return "正面"


def format_confirm_message(doc_type: str, fields: dict, ocr_text: str) -> str:
    """產生給用戶的確認訊息：收到您提供的 XXX 的 XXXX 文件，是否正確？"""
    # 優先用亡者姓名，其次申請人姓名
    name = fields.get("亡者姓名") or fields.get("申請人姓名")

    # 國民身分證加註正面/背面
    if doc_type == "國民身分證":
        side = _detect_id_card_side(ocr_text)
        label = f"國民身分證（{side}）"
    else:
        label = doc_type if doc_type not in ("未分類", "其他") else "文件"

    if name:
        header = f"📄 收到您提供的 {name} 的{label}"
    else:
        header = f"📄 收到您提供的{label}"

    return f"{header}\n\n以上資訊是否正確？"


# ── EasyOCR 單例 ──────────────────────────────────────────────────────
_easyocr_reader = None


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        try:
            import easyocr
            logger.info("初始化 EasyOCR（繁體中文 + 英文），首次載入需要約 30 秒...")
            _easyocr_reader = easyocr.Reader(
                ["ch_tra", "en"], gpu=False, verbose=False,
                model_storage_directory="/opt/models/easyocr",
            )
            logger.info("EasyOCR 初始化完成")
        except ImportError:
            logger.warning("easyocr 未安裝")
        except Exception as e:
            logger.error("EasyOCR 初始化失敗：%s", e)
    return _easyocr_reader


# ── OCR 引擎呼叫 ──────────────────────────────────────────────────────

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
        logger.warning("Ollama 未啟動或無法連線（%s），改用 EasyOCR", OLLAMA_API_URL)
        return None
    except httpx.TimeoutException:
        logger.error("Ollama OCR 逾時（%.0fs）", _TIMEOUT)
        return None
    except Exception as e:
        logger.error("Ollama OCR 失敗：%s", e)
        return None


_PDF_TEXT_PROMPT = (
    "以下是從 PDF 文件萃取的文字，請仔細閱讀並提取欄位值。\n\n"
    "---文件內容開始---\n"
    "{text}\n"
    "---文件內容結束---\n\n"
    "請依以下格式輸出，每行一個欄位，【標籤】後直接接文件中的實際值（不加冒號或破折號）：\n"
    "【文件類型】（死亡證明書／火化許可證／遷出證明書／起掘許可證／國民身分證／其他，擇一填入）\n"
    "【亡者姓名】（填入文件中亡者的真實姓名，去除「故」與「君」字，無則填「無」）\n"
    "【身分證字號】（1碼大寫英文+9碼數字格式，無則填「無」）\n"
    "【出生日期】（YYYY-MM-DD格式，民國年換算：西元=民國年+1911，無則填「無」）\n"
    "【死亡日期】（YYYY-MM-DD格式，民國年換算，無則填「無」）\n"
    "【申請人姓名】（填入申請人的真實姓名，無則填「無」）\n"
)


def _extract_fields_via_llm(embedded_text: str) -> tuple[str, dict]:
    """將 PDF 嵌入文字送 Ollama（文字模式，不需圖片）提取結構化欄位。
    回傳 (doc_type, fields)；Ollama 不可用時回傳 ('', {})。
    """
    if not OLLAMA_API_URL:
        return "", {}

    prompt = _PDF_TEXT_PROMPT.format(text=embedded_text[:3000])
    try:
        resp = httpx.post(
            f"{OLLAMA_API_URL.rstrip('/')}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=60.0,
        )
        resp.raise_for_status()
        result = resp.json().get("response", "").strip()
        logger.debug("PDF 文字 LLM 提取結果：%s", result[:200])
    except Exception as e:
        logger.warning("PDF 文字 LLM 提取失敗，fallback 至 regex：%s", e)
        return "", {}

    doc_type, fields = _parse_structured(result)
    if not doc_type:
        doc_type = _detect_doc_type(embedded_text)
        fields = _extract_fields_regex(embedded_text, doc_type)
    return doc_type, fields


def _ocr_via_easyocr(image_bytes: bytes) -> str | None:
    reader = _get_easyocr_reader()
    if reader is None:
        return None  # 引擎不可用
    try:
        results = reader.readtext(image_bytes, detail=0, paragraph=True)
        return "\n".join(results).strip()  # 空字串表示正常執行但無文字
    except Exception as e:
        logger.error("EasyOCR 辨識失敗：%s", e)
        return None


def _run_ocr(image_bytes: bytes) -> str:
    """依序嘗試：Ollama → EasyOCR → Mock（僅在引擎不可用時）"""
    result = _ocr_via_ollama(image_bytes)
    if result:
        r = result.strip()
        # 模型回應分類標籤 → 直接回傳，不走 EasyOCR
        if "【生活照】" in r:
            return LIFE_PHOTO_RESULT
        if "【大頭照】" in r:
            return ID_PHOTO_RESULT
        if "【非文件圖片】" in r:
            return NON_DOCUMENT_RESULT
        return result

    logger.info("使用 EasyOCR 進行辨識")
    result = _ocr_via_easyocr(image_bytes)
    if result is None:
        logger.warning("所有 OCR 引擎不可用，使用 Mock 模式")
        time.sleep(1)
        return _MOCK_RESULT
    if result:
        return result
    logger.info("圖片中未找到文字內容")
    return NO_TEXT_RESULT


# ── 公開介面 ──────────────────────────────────────────────────────────

def extract_from_image(file_path: str) -> tuple[str, str, dict]:
    """回傳 (ocr_text, doc_type, fields)"""
    try:
        with open(file_path, "rb") as f:
            image_bytes = f.read()
    except OSError as e:
        logger.error("讀取圖片失敗：%s", e)
        return "【讀取檔案失敗】", "未分類", {}

    ocr_text = _run_ocr(image_bytes)
    doc_type, fields = _parse_structured(ocr_text)
    if not doc_type:
        doc_type = _detect_doc_type(ocr_text)
        fields = _extract_fields_regex(ocr_text, doc_type)

    # 驗證文件類型是否與 OCR 原文相符，防止模型亂猜
    if doc_type and not _validate_doc_type(doc_type, ocr_text):
        logger.warning("文件類型驗證失敗：模型猜測 %s，用實際文件內容重新偵測", doc_type)
        # 移除模型自己產生的標籤行，只保留實際文件內容
        clean_text = re.sub(r'【[^】]+】[^\n]*', '', ocr_text)
        # 用關鍵字重新偵測
        doc_type = _detect_doc_type(clean_text)
        if doc_type != "未分類":
            fields = _extract_fields_regex(clean_text, doc_type)
            logger.info("重新偵測結果：%s", doc_type)
        else:
            fields = {}
            logger.warning("重新偵測仍無法辨識，設為未分類")

    return ocr_text, doc_type, fields


def _is_good_pdf_text(text: str) -> bool:
    """判斷 PDF 嵌入文字是否可用。
    表單類 PDF 常因欄位排列導致逐字換行（「號\\n證\\n明\\n書」），
    短行比例過高代表版面亂序，應改走圖片 OCR。
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 5:
        return False
    short = sum(1 for l in lines if len(l) <= 2)
    return (short / len(lines)) < 0.4  # 超過 40% 單/雙字行 → 視為亂序


def _pdf_to_image_bytes(file_path: str, dpi: int = 200) -> bytes | None:
    """將 PDF 第一頁轉為 JPEG bytes；失敗回傳 None。"""
    try:
        import fitz
        doc = fitz.open(file_path)
        pix = doc[0].get_pixmap(dpi=dpi)
        data = pix.tobytes("jpeg")
        doc.close()
        return data
    except Exception as e:
        logger.error("PDF 轉圖片失敗：%s", e)
        return None


def extract_from_pdf(file_path: str) -> tuple[str, str, dict]:
    """回傳 (ocr_text, doc_type, fields)"""
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

    # 嵌入文字夠長且版面不亂序 → Ollama 文字模式提取欄位（比 regex 準確）
    if len(embedded_text) > 100 and _is_good_pdf_text(embedded_text):
        logger.info("使用 PDF 嵌入文字（%d 字元），送 LLM 提取欄位", len(embedded_text))
        doc_type, fields = _extract_fields_via_llm(embedded_text)
        if not doc_type:
            doc_type = _detect_doc_type(embedded_text)
            fields = _extract_fields_regex(embedded_text, doc_type)
        return embedded_text, doc_type, fields

    # 否則轉圖片走 OCR（涵蓋：無嵌入文字、表單亂序、掃描 PDF）
    if embedded_text:
        logger.info("PDF 嵌入文字品質不佳（短行比例過高），改走圖片 OCR")
    image_bytes = _pdf_to_image_bytes(file_path, dpi=200)
    if image_bytes is None:
        return "【PDF 轉換失敗】", "未分類", {}

    ocr_text = _run_ocr(image_bytes)
    doc_type, fields = _parse_structured(ocr_text)
    if not doc_type:
        doc_type = _detect_doc_type(ocr_text)
        fields = _extract_fields_regex(ocr_text, doc_type)
    return ocr_text, doc_type, fields


def extract(file_path: str) -> tuple[str, str, dict]:
    """統一入口：自動判斷圖片或 PDF，回傳 (ocr_text, doc_type, fields)"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        return extract_from_pdf(file_path)
    return extract_from_image(file_path)
