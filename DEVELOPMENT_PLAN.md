# 開發規劃文件：OCR 歸檔工作流 + 架構升級

> 建立日期：2026-05-24  
> 最後更新：2026-05-31  
> 分支：feature/dify-integration  

---

## 一、需求背景

五湖園生命智慧園區 LINE Bot 新增以下需求：

1. **文件 OCR 辨識**：用戶上傳圖片或 PDF（死亡證明書、火化許可證、遷出證明書、起掘許可證、國民身分證等），系統自動辨識文字並回覆結果
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
ASUS Ascent GX10（ARM64，128GB RAM）        NAS 主機（待掛載）
├── LINE Bot FastAPI                    ├── /archive/死亡證明書/{YYYY}/{MM}/
├── MySQL                               ├── /archive/火化許可證/{YYYY}/{MM}/
├── Nginx + cloudflared                 ├── /archive/遷出證明書/{YYYY}/{MM}/
├── Dify + 知識庫（待遷移）             ├── /archive/起掘許可證/{YYYY}/{MM}/
├── Ollama + Qwen2.5VL（待安裝）        ├── /archive/國民身分證/{YYYY}/{MM}/
└── /opt/linebot/archived ──NFS 掛載───►└── /archive/未分類/{YYYY}/{MM}/

VMware VM（192.168.31.89）
└── cloudflared 已停止，linebot 服務仍在（備援保留中）
```

### ASUS Ascent GX10 規格
| 項目 | 規格 |
|------|------|
| 主機名稱 | gx10-linebot |
| IP | 192.168.31.103 |
| CPU | NVIDIA Grace（ARM v9.2，20 核）|
| GPU/AI | NVIDIA Blackwell（GB10 整合，共用記憶體）|
| RAM | 128GB LPDDR5x 統一記憶體 |
| 儲存 | PCIe 5.0 NVMe（~916GB，已用 94GB）|
| 網路 | 10GbE ConnectX-7 |
| OS | Ubuntu 24.04.4 LTS（kernel 6.17.0-nvidia）|
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
        ├─ reply_message：「收到您的文件，辨識中請稍候...」  ← 立即回應
        ├─ BackgroundTask：run_ocr_and_notify(user_id, file_path)
        └─ return 200

        背景執行（與 Webhook 脫鉤）
        │
        ├─ 圖片/PDF → OCR（Ollama → EasyOCR → Mock 降級鏈）
        ├─ PDF：先檢測嵌入文字品質（短行比例 > 40% 改走圖片 OCR）
        ├─ 語意解析：_parse_structured（Ollama 輸出）或 _extract_fields_regex（EasyOCR）
        ├─ 建立 ocr_pending_confirms 記錄（status='waiting'，30 分鐘逾時）
        └─ push_message：「收到您提供的 XXX 的 OOO 文件」+ Quick Reply
                   [✅ 正確，請歸檔] [❌ 辨識有誤，重新上傳]

用戶點「✅ 正確，請歸檔」
        │
        ├─ 移動檔案至 /archived/{document_type}/{YYYY}/{MM}/
        ├─ 寫入 archived_documents DB
        ├─ 更新 ocr_pending_confirms.status = 'confirmed'
        └─ push_message：「✅ 文件已歸檔完成」

用戶點「❌ 辨識有誤，重新上傳」
        └─ 更新 status = 'rejected'
           push_message：「請重新上傳文件」
```

### 3-2. OCR 引擎降級鏈

| 優先順序 | 引擎 | 狀態 | 備註 |
|---------|------|------|------|
| 1 | Ollama + Qwen2.5VL | ⏳ 待安裝 | GX10 GPU 加速，2-5 秒/頁，輸出結構化標籤 |
| 2 | EasyOCR（繁體中文）| ✅ 已安裝 | 現用中繼方案，模型 295MB 已部署 |
| 3 | Mock 測試資料 | ✅ | 引擎不可用時的最後備援 |

---

## 四、OCR 語意解析規格（5 種殯葬文件）

| 文件類型 | 分類關鍵字（部分） | 擷取欄位 |
|---------|-----------------|---------|
| 死亡證明書 | 死亡證明書、衛生福利部、死亡原因 | 亡者姓名、身分證字號、死亡日期、出生日期、申請人 |
| 火化許可證 | 火化許可證、殯葬管理所、准予火化 | 亡者姓名、身分證字號、死亡日期、申請人 |
| 遷出證明書 | 遷出證明書、骨灰、骨骸、進塔證明 | 亡者姓名（過濾故/君）、申請人 |
| 起掘許可證 | 起掘許可證、公墓、撿骨 | 亡者姓名、死亡日期（僅年份時填 YYYY-00-00）|
| 國民身分證 | 中華民國國民身分證、統一編號、役別 | 申請人、身分證字號、出生日期、除戶備註 |

- 民國年自動換算：西元年 = 民國年 + 1911
- Ollama 輸出帶 `【標籤】` 格式 → `_parse_structured()` 解析
- EasyOCR 原始文字 → `_extract_fields_regex()` 正規表示式擷取

---

## 五、資料庫新增資料表

### `system_sessions` — DB 化 Session（取代記憶體 dict）✅

| 欄位 | 類型 | 說明 |
|------|------|------|
| token | VARCHAR(64) PK | Session Token |
| user_id | INT | 關聯 system_users.id |
| role | ENUM('admin','user') | 角色 |
| expires_at | DATETIME | 逾期時間 |
| created_at | DATETIME | 建立時間 |

### `ocr_pending_confirms` — OCR 等待確認狀態 ✅

| 欄位 | 類型 | 說明 |
|------|------|------|
| id | INT PK | |
| line_user_id | VARCHAR(64) | LINE 用戶 ID |
| message_log_id | INT | 關聯 MessageLog.id |
| file_path | VARCHAR(512) | 本地檔案路徑 |
| ocr_result | TEXT | OCR 辨識結果（原始文字） |
| status | ENUM | waiting / confirmed / rejected |
| expires_at | DATETIME | 30 分鐘逾時 |
| created_at | DATETIME | |

### `archived_documents` — 已歸檔文件記錄 ✅

| 欄位 | 類型 | 說明 |
|------|------|------|
| id | INT PK | |
| line_user_id | VARCHAR(64) | 上傳者 LINE 用戶 ID |
| display_name | VARCHAR(255) | 上傳者名稱 |
| original_file_path | VARCHAR(512) | 原始路徑 |
| archived_file_path | VARCHAR(512) | 歸檔後路徑（本機或 NAS）|
| ocr_result | TEXT | OCR 完整內容 |
| document_type | VARCHAR(64) | 文件類型（自動辨識）|
| confirmed_at | DATETIME | 用戶確認時間 |
| created_at | DATETIME | |

---

## 六、歸檔資料夾結構

```
/opt/linebot/archived/          ← 本機路徑（NAS 就緒後改為 NFS 掛載點）
  ├── 死亡證明書/2026/05/
  ├── 火化許可證/2026/05/
  ├── 遷出證明書/2026/05/
  ├── 起掘許可證/2026/05/
  ├── 國民身分證/2026/05/
  └── 未分類/2026/05/
```

**命名規則**：`{YYYYMMDD_HHMMSS}_{original_filename}`

---

## 七、現有效能缺口修正（已完成）

### 7-1. Session DB 化 ✅
`app/auth.py` 與 `app/admin.py` 的 Session 改存 `system_sessions` 資料表，重啟服務後小編不需重新登入。

### 7-2. LINE Webhook 5 秒逾時 ✅
`BackgroundTask` + `reply_message`（即時 ACK）+ `push_message`（OCR 完成後推播）。

### 7-3. 非文件圖片處理 ✅
EasyOCR 正常執行但無文字（如風景照）→ 回覆「未找到文字」，不觸發歸檔流程。

---

## 八、VM → GX10 遷移（已完成 2026-05-31）✅

| 項目 | 結果 |
|------|------|
| 程式碼 | git clone feature/dify-integration |
| MySQL 資料 | mysqldump pipe 直傳，13 張資料表完整還原 |
| 靜態檔案 | rsync 17MB 完成 |
| 歸檔文件 | rsync 4.1MB 完成 |
| EasyOCR 模型 | rsync 295MB 完成 |
| Nginx | Port 80 → 8000 proxy 設定完成 |
| systemd 服務 | linebot / nginx / mysql / cloudflared 全部 active |
| Webhook 切換 | 自動更新至新 Tunnel URL，停機 < 10 秒 |

---

## 九、開發時程

| 階段 | 工作項目 | 狀態 |
|------|---------|------|
| **已完成** | Session DB 化 | ✅ |
| **已完成** | BackgroundTask OCR 架構 | ✅ |
| **已完成** | EasyOCR 中繼方案（繁體中文）| ✅ |
| **已完成** | OCR 語意解析 + 結構化欄位擷取 | ✅ |
| **已完成** | PDF 品質檢測（改走圖片 OCR）| ✅ |
| **已完成** | 確認歸檔流程（Quick Reply）| ✅ |
| **已完成** | VM → GX10 遷移 | ✅ |
| **下一步** | 安裝 Ollama + Qwen2.5VL | ⏳ 待辦 |
| **下一步** | 掛載 NAS，更新 ARCHIVE_PATH | ⏳ 待辦 |
| **下一步** | Dify 遷移至 GX10 | ⏳ 待辦 |
| **下一步** | VM 退役（建議觀察 2 週後）| ⏳ 待辦 |

---

## 十、下一步詳細說明

### 10-1. 安裝 Ollama + Qwen2.5VL（GX10 上）

```bash
# 在 GX10（192.168.31.103）上執行
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5vl:7b   # ~5GB，首次下載需時

# 設定 .env
OLLAMA_API_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5vl:7b

# 重啟服務
sudo systemctl restart linebot
```

安裝完成後 OCR 精準度從 EasyOCR 的 ~60% 提升至 Qwen2.5VL 的 95%+。

### 10-2. 掛載 NAS

```bash
# 安裝 NFS client
sudo apt install -y nfs-common

# 掛載（替換 NAS_IP）
echo "<NAS_IP>:/volume1/archive /opt/linebot/archived nfs defaults,_netdev 0 0" \
  | sudo tee -a /etc/fstab
sudo mount -a

# 更新 .env
ARCHIVE_PATH=/opt/linebot/archived
```

### 10-3. Dify 遷移至 GX10

需從舊 VM 匯出 Docker volumes（~366MB），在 GX10 還原後重啟 Dify：

| Volume | 大小 | 說明 |
|--------|------|------|
| dify_dify_pg_data | 86.5MB | PostgreSQL（知識庫、App 設定）|
| dify_dify_plugin_storage | 266MB | Plugin 二進位 |
| dify_dify_api_storage | 12.5MB | 上傳文件 |
| dify_dify_weaviate_data | 1.1MB | 向量資料 |

---

*文件最後更新：2026-05-31*
