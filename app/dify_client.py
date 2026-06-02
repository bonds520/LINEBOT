"""
自建語意搜尋 + Ollama 直接對話
完全繞開 Dify worker 干擾問題
"""
import os
import json
import httpx
import logging

logger = logging.getLogger(__name__)

OLLAMA_API_URL  = os.getenv("OLLAMA_API_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen2.5:32b")
EMBED_MODEL     = "bge-m3"
WEAVIATE_URL    = "http://172.18.0.3:8080"
WEAVIATE_KEY    = "WVF5YThaHlkYwhGUSmCRgsX3"
COLLECTION      = "Vector_index_2bc23da3_1d54_4e80_925c_2ee9b7d9fb66_Node"
_TIMEOUT        = 60.0
TOP_K           = 3

SYSTEM_PROMPT = """你是五湖園生命智慧園區的 AI 客服助理。
請根據以下知識庫資料回答訪客的問題，回答語言請使用繁體中文。
回答時要將答案完整回覆，不要省略重要訊息。
若知識庫中找不到相關資料，請只回覆：【轉人工客服】"""


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


def _search_weaviate(vector: list[float]) -> list[str]:
    """GraphQL 向量搜尋，回傳最相近的 text 段落列表"""
    if not vector:
        return []
    gql = {
        "query": f"""
        {{
          Get {{
            {COLLECTION}(
              nearVector: {{vector: {json.dumps(vector)}, certainty: 0.6}}
              limit: {TOP_K}
            ) {{
              text
              _additional {{ certainty }}
            }}
          }}
        }}
        """
    }
    try:
        resp = httpx.post(
            f"{WEAVIATE_URL}/v1/graphql",
            headers={"Authorization": f"Bearer {WEAVIATE_KEY}", "Content-Type": "application/json"},
            json=gql,
            timeout=10.0,
        )
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("Get", {}).get(COLLECTION, [])
        return [item["text"] for item in items if item.get("text")]
    except Exception as e:
        logger.error("Weaviate 搜尋失敗：%s", e)
        return []


def _ollama_chat(context: str, question: str) -> str | None:
    system = SYSTEM_PROMPT
    if context:
        system += f"\n\n知識庫資料：\n{context}"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        "stream": False,
    }
    try:
        resp = httpx.post(
            f"{OLLAMA_API_URL}/api/chat",
            json=payload,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "").strip() or None
    except httpx.TimeoutException:
        logger.error("Ollama chat 逾時（%.0fs）", _TIMEOUT)
        return None
    except Exception as e:
        logger.error("Ollama chat 失敗：%s", e)
        return None


def chat(user_id: str, message: str) -> str | None:
    """
    1. bge-m3 嵌入查詢
    2. Weaviate 語意搜尋
    3. Ollama qwen2.5:32b 生成回覆
    失敗時回傳 None，由呼叫端決定 fallback 行為。
    """
    # 生成查詢向量
    vector = _embed(message)
    if not vector:
        logger.warning("embedding 失敗，跳過語意搜尋")
        context = ""
    else:
        segments = _search_weaviate(vector)
        context = "\n\n---\n\n".join(segments)
        if segments:
            logger.info("語意搜尋找到 %d 段，certainty 最高段落前40字：%s",
                        len(segments), segments[0][:40])
        else:
            logger.info("語意搜尋無結果")

    return _ollama_chat(context, message)
