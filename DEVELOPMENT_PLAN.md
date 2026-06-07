# 開發規劃文件：OCR 歸檔工作流 + 架構升級

> 建立日期：2026-05-24  
> 最後更新：2026-06-07  
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
ASUS Ascent GX10（ARM64，128GB RAM）        NAS（192.168.31.35，SMB 掛載）
├── LINE Bot FastAPI                    ├── /scan/LINEBOT/死亡證明書/{YYYY}/{MM}/
├── MySQL                               ├── /scan/LINEBOT/火化許可證/{YYYY}/{MM}/
├── Nginx + cloudflared                 ├── /scan/LINEBOT/遷出證明書/{YYYY}/{MM}/
├── Dify + 知識庫 ✅                    ├── /scan/LINEBOT/亡者大頭照/{YYYY}/{MM}/
├── Ollama qwen2.5vl:7b ✅              ├── /scan/LINEBOT/待人工確認/{YYYY}/{MM}/
├── Weaviate + bge-m3 ✅                └── ...
└── /mnt/nas_linebot（ARCHIVE_PATH）────►（fstab 已設定，/etc/.nas-credentials 已建立）

VMware VM（192.168.31.89）
└── 觀察中，建議 2026-06-14 退役
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
        ├─ reply_message：「📎 已收到您提供的檔案，感謝您。」＋[🔍 查看辨識結果]  ← 立即回應
        ├─ BackgroundTask：run_ocr_and_notify(user_id, file_path)
        └─ return 200

        背景執行（與 Webhook 脫鉤）
        │
        ├─ 圖片/PDF → OCR（Ollama → EasyOCR → Mock 降級鏈）
        ├─ PDF：先檢測嵌入文字品質（短行比例 > 40% 改走圖片 OCR）
        ├─ 語意解析：_parse_structured（Ollama 輸出）或 _extract_fields_regex（EasyOCR）
        │
        ├─ [生活照] 自動存入「待人工確認」
        ├─ [大頭照] 直接以原始檔名（Path.stem）歸檔至「亡者大頭照」
        │           ⚠ 使用者須以亡者姓名命名檔案後上傳；圖片訊息無法保留原始檔名
        ├─ [非文件/無法辨識/OCR 失敗] 自動存入「待人工確認」
        └─ [正常文件] 驗證 doc_type → 自動歸檔至對應資料夾，寫入 archived_documents

用戶上傳非 OCR 格式（docx、csv、xlsx…）
        └─ 直接歸檔至「其他檔案區/{YYYY}/{MM}/」（日期前綴重新命名）
           reply_message：「📎 已收到您提供的檔案，感謝您。」
```

### 3-2. OCR 引擎降級鏈

| 優先順序 | 引擎 | 狀態 | 備註 |
|---------|------|------|------|
| 1 | Ollama + Qwen2.5VL | ✅ 已安裝 | GX10 GB10 GPU，48%CPU/52%GPU 推論，結構化標籤輸出 |
| 2 | EasyOCR（繁體中文）| ✅ 已安裝 | Ollama 不可用時備援，模型 295MB 已部署 |
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
{ARCHIVE_PATH}/                 ← .env 設定（本機 /opt/linebot/archived 或 NAS 掛載點）
  ├── 死亡證明書/2026/06/
  ├── 火化許可證/2026/06/
  ├── 遷出證明書/2026/06/
  ├── 起掘許可證/2026/06/
  ├── 國民身分證/2026/06/
  └── 未分類/2026/06/
```

**目前測試掛載**：`ARCHIVE_PATH=/mnt/nas_linebot`
（SMB 掛載 `//192.168.31.35/scan/LINEBOT`，臨時手動掛載，重開機消失）

**命名規則**：`{姓名}-{證件類別}{副檔名}`
- 範例：`王大明-死亡證明書.jpg`、`陳德明-國民身分證（背面）.jpg`
- 國民身分證自動標註正面/背面
- 無姓名時填「未知」，同名檔案加 `_HHMMSS` 序號
- 姓名與類型取自 `ocr_pending_confirms` 已驗證欄位（非重新解析）
- 特殊字元 `\/:*?"<>|` 自動移除（Windows SMB 相容）

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
| **已完成** | 安裝 Ollama + Qwen2.5VL + bge-m3 | ✅ |
| **已完成** | Dify 遷移至 GX10 | ✅ |
| **已完成** | 自建語意搜尋（bge-m3 + Weaviate named vector）| ✅ |
| **已完成** | 後台「同步至 AI 知識庫」按鈕 + sync_qa_to_dify.py | ✅ |
| **已完成** | Q&A 新增/修改/刪除自動背景同步 Weaviate | ✅ |
| **已完成** | 三層式 RAG：Weaviate 只存 qa_id，搜尋後回查 MySQL | ✅ |
| **已完成** | 語音輸入 STT（Whisper medium + faster-whisper）| ✅ |
| **已完成** | 模型統一管理（/opt/models/，移除未使用大模型）| ✅ |
| **已完成** | 影像智能分類（生活照/大頭照/文件兩階段判斷）| ✅ |
| **已完成** | OCR 多重關鍵字驗證（標籤移除、驗證失敗自動重偵測）| ✅ |
| **已完成** | 國民身分證正/背面自動識別 | ✅ |
| **已完成** | OCR 確認訊息簡化（僅顯示姓名＋證件種類）| ✅ |
| **已完成** | OCR/STT 改用 reply_message 節省推播配額 | ✅ |
| **已完成** | 大頭照歸檔流程（詢問被攝者姓名後歸檔）| ✅ |
| **已完成** | OCR 結果按鈕延遲顯示（辨識完成後才出現）| ✅ |
| **已完成** | 合併圖片分類 + OCR 為單次 Ollama 呼叫（180s timeout）| ✅ |
| **已完成** | PDF 嵌入文字改用 LLM 提取欄位（取代脆弱 regex）| ✅ |
| **已完成** | _parse_structured 支援格式 B（模型直接用文件名稱當標籤）| ✅ |
| **已完成** | OCR 3 分鐘整體 timeout，超時回覆用戶重新上傳 | ✅ |
| **已完成** | 程式碼精簡：移除死碼、提取重複邏輯為 helper | ✅ |
| **已完成** | 安裝 Karpathy Coding Guidelines（CLAUDE.md）| ✅ |
| **已完成** | OCR 卡死循環修復（timeout/例外/非文件 → 存辨識失敗記錄，斷循環）| ✅ |
| **已完成** | 無法辨識照片 → 詢問用戶「重新拍照 or 仍要歸檔」| ✅ |
| **已完成** | 「仍要歸檔」存入「待人工確認」資料夾並寫入 ArchivedDocument | ✅ |
| **已完成** | 新照片上傳時作廢舊 waiting 記錄，防止多張上傳歸錯檔 | ✅ |
| **已完成** | 非 OCR 格式檔案（docx/csv/xlsx 等）自動歸檔至「其他檔案區」 | ✅ |
| **已完成** | 移除所有 OCR 推播通知（生活照/非文件/完成/逾時），改被動 reply_message | ✅ |
| **已完成** | 大頭照流程：push 詢問姓名 → 用戶直接回覆 2~5 中文字 → 立即歸檔（去除二次確認） | ✅ |
| **已完成** | 大頭照 OcrPendingConfirm 有效期延長至 48 小時（避免 OCR 排隊時記錄提前過期） | ✅ |
| **已完成** | 全自動歸檔流程：所有 OCR 結果不再要求用戶確認，直接歸檔或存入待人工確認 | ✅ |
| **已完成** | EasyOCR 姓名誤匹配修正（_find_name_near 僅取錨點後第一行，防止跨行抓到標題詞） | ✅ |
| **已完成** | PDF LLM 路徑加入 _validate_doc_type 驗證（防止發票被誤判為國民身分證） | ✅ |
| **已完成** | _validate_doc_type fallback：去標籤後文字 < 20 字，改對完整輸出驗證（解決格式 B 標籤驗證失敗） | ✅ |
| **已完成** | 國民身分證驗證獨立邏輯：正面任 1（身份證/身分證/發證日期/換證日期）OR 背面 6 取 4（父母配偶役別出生地住址） | ✅ |
| **已完成** | 大頭照改為直接以原始檔名歸檔（廢除 push 詢問姓名流程，不再消耗推播配額） | ✅ |
| **已完成** | NAS fstab 自動掛載設定（/etc/.nas-credentials + fstab _netdev,nofail,x-systemd.automount） | ✅ |
| **下一步** | 後台「已歸檔文件」管理頁面（含待人工確認篩選）| ⏳ 待辦 |
| **下一步** | 大頭照命名：使用者需自行以亡者姓名命名檔案後上傳，或後台提供重新命名介面 | ⏳ 待辦 |
| **下一步** | VM 退役（建議觀察至 2026-06-14）| ⏳ 待辦 |

---

## 十、下一步詳細說明

### 10-1. NAS fstab 永久掛載 ✅

憑證檔：`/etc/.nas-credentials`（chmod 600），fstab 已加入以下條目：

```
//192.168.31.35/scan/LINEBOT  /mnt/nas_linebot  cifs
  credentials=/etc/.nas-credentials,uid=1000,gid=1000,
  file_mode=0755,dir_mode=0755,iocharset=utf8,vers=3.0,
  soft,_netdev,nofail,x-systemd.automount  0  0
```

**注意**：重開機後若 NAS 未在線，服務仍可正常啟動（nofail），歸檔路徑會暫時寫入本機，NAS 掛載後本機舊檔案會被掛載點覆蓋（隱藏），需手動取回。

### 10-2. 後台「已歸檔文件」管理頁面 ⏳

目前 admin 後台沒有查看 `archived_documents` 的頁面，尤其「待人工確認」需要人工處理：
- 列表顯示：上傳者、文件類型、歸檔路徑、確認時間
- 可篩選「待人工確認」，點擊下載/預覽
- 確認後可手動修改 document_type 並移至正確資料夾

### 10-3. 大頭照命名問題 ⏳

目前大頭照以 `Path(file_path).stem` 作為亡者姓名：
- **文件訊息上傳**（LINE `+` → 文件）：保留原始檔名，使用者須事先以亡者姓名命名
- **圖片訊息上傳**（拍照/相簿）：無原始檔名，歸檔後得到 UUID-大頭照.jpg

後續可評估：後台提供已歸檔文件重新命名介面，人工修正 UUID 檔名。

### 10-4. VM 退役 ⏳

觀察至 2026-06-14，確認 GX10 穩定後關閉 VM（192.168.31.89）。

---

*文件最後更新：2026-06-07（全自動歸檔 + 身分證驗證重構 + 大頭照改原始檔名歸檔 + NAS fstab 設定）*
