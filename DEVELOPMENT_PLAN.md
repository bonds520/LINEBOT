# 開發規劃文件：OCR 歸檔工作流 + 架構升級

> 建立日期：2026-05-24  
> 分支：feature/dify-integration  

---

## 一、需求背景

五湖園生命智慧園區 LINE Bot 新增以下需求：

1. **文件 OCR 辨識**：用戶上傳圖片或 PDF（死亡證明書、火化許可證、遷葬證明、起掘許可證、身份證明文件等），系統自動辨識文字並回覆結果
2. **用戶確認歸檔**：用戶確認 OCR 結果正確後，系統自動將文件歸類存入對應資料夾
3. **資安要求**：所有文件處理必須完全在本地端執行，不得傳送至外部 API

---

## 二、目標硬體架構

```
LINE Platform（雲端）
        │ HTTPS Webhook
        ▼
Cloudflare Tunnel
        │
        ▼
ASUS Ascent GX10（ARM64，128GB RAM）        NAS 主機
├── LINE Bot FastAPI                    ├── /archive/死亡證明書/{YYYY}/{MM}/
├── MySQL                               ├── /archive/火化許可證/{YYYY}/{MM}/
├── Nginx + cloudflared                 ├── /archive/遷葬證明/{YYYY}/{MM}/
├── Dify + 知識庫（知識庫 RAG）          ├── /archive/起掘許可證/{YYYY}/{MM}/
├── Ollama + Qwen2.5VL（本地 OCR）      ├── /archive/身份證明文件/{YYYY}/{MM}/
└── /opt/linebot/archived ──NFS 掛載───►└── /archive/未分類/{YYYY}/{MM}/

VMware VM（192.168.31.89，遷移完成後退役）
```

### ASUS Ascent GX10 規格
| 項目 | 規格 |
|------|------|
| CPU | NVIDIA Grace（ARM v9.2，20 核）|
| GPU/AI | NVIDIA Blackwell（GB10 整合，共用記憶體）|
| RAM | 128GB LPDDR5x 統一記憶體 |
| 儲存 | PCIe 5.0 NVMe（1-4TB）|
| 網路 | 10GbE ConnectX-7 |
| AI 算力 | 1 petaFLOP |

---

## 三、OCR 工作流程

### 3-1. 完整流程

```
用戶上傳圖片 / PDF
        │
        ▼
Webhook handler（/webhook）
        │
        ├─ 儲存檔案到本地（< 1 秒）
        ├─ reply_message：「收到您的文件，辨識中請稍候...」  ← 立即回應，不超逾 LINE 5 秒限制
        ├─ BackgroundTask：run_ocr_and_notify(user_id, file_path)
        └─ return 200

        背景執行（與 Webhook 脫鉤）
        │
        ├─ 圖片 → base64 編碼 → Ollama Qwen2.5VL API
        ├─ PDF  → pymupdf 萃取內嵌文字
        │         若無內嵌文字 → 轉圖片 → Ollama Qwen2.5VL
        │
        ├─ 建立 ocr_pending_confirms 記錄（status='waiting'，30 分鐘逾時）
        └─ push_message：OCR 辨識結果 + Quick Reply 按鈕
                   [✅ 正確，請歸檔] [❌ 辨識有誤，重新上傳]

用戶點「✅ 正確，請歸檔」
        │
        ▼
handle_text_message（優先判斷 ocr_pending_confirms）
        │
        ├─ 移動檔案至 /archived/{document_type}/{YYYY}/{MM}/
        ├─ 寫入 archived_documents DB
        ├─ 更新 ocr_pending_confirms.status = 'confirmed'
        └─ push_message：「✅ 文件已歸檔完成」

用戶點「❌ 辨識有誤，重新上傳」
        └─ 更新 status = 'rejected'
           push_message：「請重新上傳文件」
```

### 3-2. LINE Webhook 5 秒逾時解法

| 機制 | 說明 |
|------|------|
| `reply_message` | 使用 replyToken（30 秒有效），Webhook handler 內立即呼叫 |
| `BackgroundTask` | FastAPI 內建，回應送出後才執行，不佔用 Webhook 時限 |
| `push_message` | 使用 userId，背景任務完成後任意時間推播 |

---

## 四、OCR 引擎

### 選型：Ollama + Qwen2.5VL（本地視覺 LLM）

| 項目 | 說明 |
|------|------|
| 引擎 | Ollama（ARM64 原生支援）|
| 模型 | `qwen2.5vl:7b`（~5GB，繁中最佳）|
| API | `POST http://localhost:11434/api/generate` |
| 精準度 | 95%+（繁體中文官方文件）|
| 速度 | 2-5 秒/頁（Blackwell GPU）|
| 隱私 | 完全本地，零外部傳輸 |

### 安裝（GX10 到貨後）

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5vl:7b

# 開放網路監聽（若需跨機存取）
# /etc/systemd/system/ollama.service 加入：
# Environment="OLLAMA_HOST=0.0.0.0:11434"
```

### Mock 模式（GX10 到貨前測試用）

`OLLAMA_API_URL` 未設定或連線失敗時，`ocr_client.py` 自動回傳測試資料，讓完整流程可在現有 VM 上驗證。

---

## 五、資料庫新增資料表

### `system_sessions` — DB 化 Session（取代記憶體 dict）

| 欄位 | 類型 | 說明 |
|------|------|------|
| token | VARCHAR(64) PK | Session Token |
| user_id | INT | 關聯 system_users.id |
| role | ENUM('admin','user') | 角色 |
| expires_at | DATETIME | 逾期時間 |
| created_at | DATETIME | 建立時間 |

### `ocr_pending_confirms` — OCR 等待確認狀態

| 欄位 | 類型 | 說明 |
|------|------|------|
| id | INT PK | |
| line_user_id | VARCHAR(64) | LINE 用戶 ID |
| message_log_id | INT | 關聯 MessageLog.id |
| file_path | VARCHAR(512) | 本地檔案路徑 |
| ocr_result | TEXT | OCR 辨識結果 |
| status | ENUM | waiting / confirmed / rejected |
| expires_at | DATETIME | 30 分鐘逾時 |
| created_at | DATETIME | |

### `archived_documents` — 已歸檔文件記錄

| 欄位 | 類型 | 說明 |
|------|------|------|
| id | INT PK | |
| line_user_id | VARCHAR(64) | 上傳者 LINE 用戶 ID |
| display_name | VARCHAR(255) | 上傳者名稱 |
| original_file_path | VARCHAR(512) | 原始路徑 |
| archived_file_path | VARCHAR(512) | 歸檔後路徑（本機或 NAS）|
| ocr_result | TEXT | OCR 完整內容 |
| document_type | VARCHAR(64) | 文件類型（Qwen 自動辨識）|
| confirmed_at | DATETIME | 用戶確認時間 |
| created_at | DATETIME | |

---

## 六、歸檔資料夾結構

```
/opt/linebot/archived/          ← 本機測試路徑（未來掛載至 NAS）
  ├── 死亡證明書/2026/05/
  ├── 火化許可證/2026/05/
  ├── 遷葬證明/2026/05/
  ├── 起掘許可證/2026/05/
  ├── 身份證明文件/2026/05/
  └── 未分類/2026/05/
```

**命名規則**：`{timestamp}_{line_user_id}_{original_filename}`  
例：`20260524_143022_Uxxxxxxx_death_cert.pdf`

---

## 七、現有效能缺口修正

### 7-1. Session 記憶體問題

**問題**：`app/auth.py` 與 `app/admin.py` 的 Session 存在 Python dict/set，重啟即失效，且無法多 Worker。  
**解法**：Session 改存 `system_sessions` 資料表。

### 7-2. 單一 Worker 限制

目前啟動指令只用 1 個 Worker：
```
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Session DB 化後可升級為多 Worker：
```
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

---

## 八、VM → GX10 遷移計畫

### 為什麼遷移快速（預計 30 分鐘內完成切換）

| 項目 | 大小 | 方式 |
|------|------|------|
| 程式碼 | — | `git clone`（GitHub）|
| MySQL 資料 | 0.16 MB | `mysqldump` 管道直傳 |
| 靜態檔案 | 5.7 MB | `rsync` |
| Webhook 切換 | — | `start_tunnel.sh` 自動更新 |

### 遷移步驟（GX10 到貨後）

```bash
# Step 1：GX10 安裝基礎環境（VM 持續服務中）
sudo apt install -y python3.12-venv python3-pip mysql-server nginx git
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o /tmp/cf.deb && sudo dpkg -i /tmp/cf.deb
curl -fsSL https://get.docker.com | sh
curl -fsSL https://ollama.com/install.sh | sh && ollama pull qwen2.5vl:7b

# Step 2：部署程式碼
git clone https://github.com/bonds520/LINEBOT.git /opt/linebot
python3 -m venv /opt/linebot/venv
/opt/linebot/venv/bin/pip install -r /opt/linebot/requirements.txt

# Step 3：從 VM 複製資料
scp superai@192.168.31.89:/opt/linebot/.env /opt/linebot/.env
rsync -avz superai@192.168.31.89:/opt/linebot/static/ /opt/linebot/static/
ssh superai@192.168.31.89 "mysqldump -u linebot -p'Wuhu6688!' linebot" | mysql -u linebot -p linebot

# Step 4：掛載 NAS
echo "<NAS_IP>:/volume1/archive /opt/linebot/archived nfs defaults,_netdev 0 0" | sudo tee -a /etc/fstab
sudo mount -a

# Step 5：啟動服務並切換（停機 < 10 秒）
sudo systemctl start linebot nginx
sudo systemctl start cloudflared   # 自動更新 LINE Webhook

# Step 6：VM 停止服務
# ssh 至 VM：sudo systemctl stop cloudflared linebot
```

---

## 九、新增環境變數

```env
# Ollama OCR（GX10 到貨後設定）
OLLAMA_API_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5vl:7b

# 歸檔路徑（本機測試用；NAS 就緒後改為 NFS 掛載點）
ARCHIVE_PATH=/opt/linebot/archived
```

---

## 十、開發時程

| 階段 | 工作項目 | 狀態 |
|------|---------|------|
| **階段一（現在）** | Session DB 化 | 🔄 進行中 |
| **階段一（現在）** | BackgroundTask OCR 架構 + Mock 模式 | 🔄 進行中 |
| **階段一（現在）** | 確認歸檔流程（Quick Reply + 本機存檔）| 🔄 進行中 |
| **階段二（GX10 到貨）** | 安裝 Ollama + Qwen2.5VL，替換 Mock | ⏳ 待辦 |
| **階段二（GX10 到貨）** | LINE Bot 搬遷至 GX10 | ⏳ 待辦 |
| **階段二（GX10 到貨）** | 掛載 NAS，更新 ARCHIVE_PATH | ⏳ 待辦 |
| **階段三** | Dify 知識庫重建、VM 退役 | ⏳ 待辦 |

---

*文件最後更新：2026-05-24*
