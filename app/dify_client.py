"""
三層式 RAG 架構：
  Layer 1 - MySQL：原文唯一來源（question / answer / keywords）
  Layer 2 - Weaviate：向量索引（只存 qa_id，不存原文）
  Layer 3 - Ollama：語意生成回覆

查詢流程：query → bge-m3 embed → Weaviate 取 qa_id → MySQL 回查原文 → Ollama 生成
"""
import os
import json
import httpx
import logging
import pymysql

logger = logging.getLogger(__name__)

OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b")
EMBED_MODEL    = "bge-m3"
WEAVIATE_URL   = "http://172.18.0.3:8080"
WEAVIATE_KEY   = "WVF5YThaHlkYwhGUSmCRgsX3"
COLLECTION     = "Vector_index_2bc23da3_1d54_4e80_925c_2ee9b7d9fb66_Node"
_TIMEOUT       = 60.0
TOP_K          = 3
CERTAINTY      = 0.6

SYSTEM_PROMPT = """你是五湖園生命智慧園區的 AI 客服助理。
請根據以下知識庫資料回答訪客的問題，回答語言請使用繁體中文。
回答時要將答案完整回覆，不要省略重要訊息。
若知識庫中找不到相關資料，請只回覆：【轉人工客服】"""


# ── Layer 1：MySQL 原文查詢 ───────────────────────────────────────────────

def _get_mysql_conn():
    db_url = os.getenv("DATABASE_URL", "")
    url = db_url.replace("mysql+pymysql://", "")
    user_pass, rest = url.split("@", 1)
    user, password = user_pass.split(":", 1)
    host = rest.split("/")[0]
    db   = rest.split("/")[1] if "/" in rest else "linebot"
    return pymysql.connect(host=host, user=user, password=password,
                           db=db, charset="utf8mb4")


def _fetch_qa_by_ids(qa_ids: list[int]) -> list[dict]:
    """Layer 1：用 qa_id 回查 MySQL，取得 question + answer"""
    if not qa_ids:
        return []
    try:
        conn = _get_mysql_conn()
        placeholders = ",".join(["%s"] * len(qa_ids))
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT id, question, answer, keywords FROM qa_pairs "
                f"WHERE id IN ({placeholders}) AND is_active = 1",
                qa_ids
            )
            rows = cur.fetchall()
        conn.close()
        # 保持 Weaviate 回傳的相似度排序
        id_order = {qid: i for i, qid in enumerate(qa_ids)}
        return sorted(rows, key=lambda r: id_order.get(r["id"], 99))
    except Exception as e:
        logger.error("MySQL 回查失敗：%s", e)
        return []


# ── Layer 2：Weaviate 向量搜尋 ────────────────────────────────────────────

def _embed(text: str) -> list[float]:
    try:
        resp = httpx.post(
            f"{OLLAMA_API_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": text},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("embeddings", [[]])[0]
    except Exception as e:
        logger.error("embedding 失敗：%s", e)
        return []


def _search_weaviate(vector: list[float]) -> list[int]:
    """Layer 2：向量搜尋，回傳 qa_id 列表（不含原文）"""
    if not vector:
        return []
    gql = {
        "query": f"""
        {{
          Get {{
            {COLLECTION}(
              nearVector: {{vector: {json.dumps(vector)}, certainty: {CERTAINTY}}}
              limit: {TOP_K}
            ) {{
              qa_id
              _additional {{ certainty }}
            }}
          }}
        }}
        """
    }
    try:
        resp = httpx.post(
            f"{WEAVIATE_URL}/v1/graphql",
            headers={"Authorization": f"Bearer {WEAVIATE_KEY}",
                     "Content-Type": "application/json"},
            json=gql,
            timeout=10.0,
        )
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("Get", {}).get(COLLECTION, [])
        ids = [int(item["qa_id"]) for item in items if item.get("qa_id") is not None]
        if ids:
            certainties = [f"{item.get('_additional',{}).get('certainty',0):.2f}"
                           for item in items]
            logger.info("Weaviate 找到 %d 筆 qa_id=%s certainty=%s",
                        len(ids), ids, certainties)
        else:
            logger.info("Weaviate 無結果（certainty < %.1f）", CERTAINTY)
        return ids
    except Exception as e:
        logger.error("Weaviate 搜尋失敗：%s", e)
        return []


# ── Layer 3：Ollama 語意生成 ──────────────────────────────────────────────

def _build_context(qa_rows: list[dict]) -> str:
    """將 MySQL Q&A 整理成 LLM context"""
    parts = []
    for qa in qa_rows:
        parts.append(f"Q: {qa['question']}\nA: {qa['answer']}")
    return "\n\n---\n\n".join(parts)


def _ollama_chat(context: str, question: str) -> str | None:
    system = SYSTEM_PROMPT
    if context:
        system += f"\n\n知識庫資料：\n{context}"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": question},
        ],
        "stream": False,
    }
    try:
        resp = httpx.post(f"{OLLAMA_API_URL}/api/chat",
                          json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "").strip() or None
    except httpx.TimeoutException:
        logger.error("Ollama chat 逾時（%.0fs）", _TIMEOUT)
        return None
    except Exception as e:
        logger.error("Ollama chat 失敗：%s", e)
        return None


# ── 公開介面 ──────────────────────────────────────────────────────────────

def chat(user_id: str, message: str) -> str | None:
    """
    三層查詢：
      1. bge-m3 embed query
      2. Weaviate 取 qa_id（不含原文）
      3. MySQL 回查原文 → Ollama 生成回覆
    """
    # Layer 2：向量搜尋取 qa_id
    vector = _embed(message)
    qa_ids = _search_weaviate(vector) if vector else []

    # Layer 1：回查 MySQL 原文
    qa_rows = _fetch_qa_by_ids(qa_ids)
    context = _build_context(qa_rows)

    if qa_rows:
        logger.info("MySQL 回查 %d 筆 Q&A → 送入 Ollama", len(qa_rows))
    else:
        logger.info("無相關 Q&A，送入 Ollama 判斷")

    # Layer 3：生成回覆
    return _ollama_chat(context, message)
