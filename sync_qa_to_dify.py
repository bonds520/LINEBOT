"""
MySQL Q&A → Weaviate 同步腳本（三層架構：Weaviate 只存向量 + qa_id）
執行方式：/opt/linebot/venv/bin/python sync_qa_to_dify.py
"""

import os
import re
import sys
import uuid
import logging
import pymysql
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MYSQL_URL    = os.getenv("DATABASE_URL", "")
WEAVIATE_URL = "http://172.18.0.3:8080"
WEAVIATE_KEY = "WVF5YThaHlkYwhGUSmCRgsX3"
OLLAMA_URL   = os.getenv("OLLAMA_API_URL", "http://localhost:11434")
EMBED_MODEL  = "bge-m3"
DATASET_ID   = "2bc23da3-1d54-4e80-925c-2ee9b7d9fb66"
COLLECTION   = f"Vector_index_{DATASET_ID.replace('-','_')}_Node"


def get_mysql_conn():
    url = MYSQL_URL.replace("mysql+pymysql://", "")
    user_pass, rest = url.split("@", 1)
    user, password = user_pass.split(":", 1)
    host_db = rest.split("/", 1)
    host = host_db[0]
    db = host_db[1] if len(host_db) > 1 else "linebot"
    return pymysql.connect(host=host, user=user, password=password, db=db, charset="utf8mb4")


def embed(text: str) -> list:
    resp = requests.post(f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": text}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("embeddings", [[]])[0]


def build_embed_text(qa: dict) -> str:
    """組成語意向量化文字（問題 + 關鍵字 + 答案），只用於 embedding，不存入 Weaviate"""
    kws = ""
    if qa.get("keywords"):
        kws_list = [k.strip() for k in re.sub(r'[、，；;]', ',', qa["keywords"]).split(",") if k.strip()]
        if kws_list:
            kws = " " + " ".join(kws_list)
    return f"Q: {qa['question']}{kws}\nA: {qa['answer']}"


def weaviate_rebuild(objects: list[dict]):
    """刪除舊 collection，重建並插入（只含 qa_id + 向量）"""
    headers = {"Authorization": f"Bearer {WEAVIATE_KEY}", "Content-Type": "application/json"}

    r = requests.delete(f"{WEAVIATE_URL}/v1/schema/{COLLECTION}", headers=headers)
    log.info("刪除舊 collection: %s", r.status_code)

    # Schema：只保留 qa_id（category 供未來 metadata 過濾用）
    schema = {
        "class": COLLECTION,
        "vectorConfig": {"default": {"vectorIndexType": "hnsw", "vectorizer": {"none": {}}}},
        "properties": [
            {"name": "qa_id",    "dataType": ["int"]},
            {"name": "category", "dataType": ["text"]},
        ],
    }
    r = requests.post(f"{WEAVIATE_URL}/v1/schema", headers=headers, json=schema)
    r.raise_for_status()
    log.info("建立新 collection: %s", r.status_code)

    ok = fail = 0
    for obj in objects:
        ins = requests.post(f"{WEAVIATE_URL}/v1/objects", headers=headers, json=obj)
        if ins.status_code in (200, 201):
            ok += 1
        else:
            fail += 1
            log.error("插入失敗 qa_id=%s: %s %s",
                      obj["properties"].get("qa_id"), ins.status_code, ins.text[:80])
    return ok, fail


def sync():
    log.info("=== MySQL Q&A → Weaviate 同步開始（三層架構）===")

    mysql = get_mysql_conn()
    with mysql.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("""
            SELECT id, question, answer, keywords, category
            FROM qa_pairs WHERE is_active = 1 ORDER BY id
        """)
        qa_rows = cur.fetchall()
    mysql.close()
    log.info("MySQL 讀取 %d 筆啟用 Q&A", len(qa_rows))

    if not qa_rows:
        log.warning("沒有啟用的 Q&A，中止同步")
        return 0, 0

    objects = []
    embed_fail = 0
    for qa in qa_rows:
        embed_text = build_embed_text(qa)
        try:
            vector = embed(embed_text)
            if not vector:
                raise ValueError("empty vector")
        except Exception as e:
            log.error("embedding 失敗 Q#%s: %s", qa["id"], e)
            embed_fail += 1
            continue

        objects.append({
            "class": COLLECTION,
            "id": str(uuid.uuid4()),
            "properties": {
                "qa_id":    int(qa["id"]),
                "category": qa.get("category") or "一般",
            },
            "vectors": {"default": vector},
        })
        log.info("[%d/%d] Q#%s: %s", len(objects), len(qa_rows), qa["id"], qa["question"][:30])

    if not objects:
        log.error("所有 embedding 失敗，中止同步")
        return 0, embed_fail

    ok, fail = weaviate_rebuild(objects)
    total_fail = fail + embed_fail
    log.info("=== 同步完成：%d 成功, %d 失敗 ===", ok, total_fail)
    return ok, total_fail


if __name__ == "__main__":
    try:
        ok, fail = sync()
        sys.exit(0 if fail == 0 else 1)
    except Exception as e:
        log.exception("同步失敗：%s", e)
        sys.exit(1)
