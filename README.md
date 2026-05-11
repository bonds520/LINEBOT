# LINEBOT 系統架構文件

> LINE Bot 自動回覆系統，提供 Q&A 知識庫管理、小編訓練後台與 Cloudflare Tunnel HTTPS 接入。

---

## 系統架構

```
LINE Platform
    │
    │  HTTPS Webhook
    ▼
Cloudflare Tunnel (cloudflared)
    │
    │  HTTP 內部轉發
    ▼
FastAPI + Uvicorn (0.0.0.0:8000)
    ├── /webhook          ← LINE Webhook 端點
    ├── /admin/*          ← 管理員後台
    ├── /dashboard/*      ← 小編使用者後台
    └── /login            ← 使用者登入
    │
    ▼
MySQL 8.0 (localhost:3306)
Database: linebot
```

---

## 技術堆疊

| 層級 | 技術 | 版本 |
|------|------|------|
| 程式語言 | Python | 3.12.3 |
| Web 框架 | FastAPI | 0.136.1 |
| ASGI 伺服器 | Uvicorn | 0.46.0 |
| LINE SDK | line-bot-sdk | 3.23.0 |
| 資料庫 | MySQL | 8.0.45 |
| ORM | SQLAlchemy | 2.0.49 |
| 模糊比對 | rapidfuzz | 3.14.5 |
| 模板引擎 | Jinja2 | 3.1.6 |
| 密碼加密 | passlib + bcrypt | 1.7.4 / 4.0.1 |
| HTTPS 通道 | Cloudflare Tunnel | 2026.3.0 |
| 反向代理 | Nginx | — |
| 作業系統 | Ubuntu 24.04.4 LTS | — |
| 虛擬化 | VMware | — |

---

## 專案結構

```
/opt/linebot/
├── app/
│   ├── main.py           # FastAPI 主程式、Webhook 端點、路由註冊
│   ├── database.py       # SQLAlchemy 資料庫連線設定
│   ├── models.py         # 資料庫模型定義
│   ├── handlers.py       # LINE 事件處理（訊息、加好友、封鎖）
│   ├── matcher.py        # 關鍵字模糊比對邏輯
│   ├── auth.py           # 使用者認證、Session 管理、密碼雜湊
│   ├── admin.py          # 管理員後台路由
│   └── user_panel.py     # 小編使用者後台路由
├── templates/
│   ├── base.html         # 管理員後台基礎模板
│   ├── dashboard.html    # 管理員儀表板
│   ├── login.html        # 管理員登入頁
│   ├── qa_list.html      # 管理員 Q&A 管理
│   ├── pending.html      # 管理員待處理問題
│   ├── user_mgmt.html    # 使用者帳號管理
│   ├── user_base.html    # 小編後台基礎模板
│   ├── user_login.html   # 小編登入頁
│   ├── user_dashboard.html  # 小編儀表板
│   ├── user_qa.html      # Q&A 訓練列表
│   ├── user_create_qa.html  # 建立 Q&A
│   └── user_trained.html    # 已訓練 Q&A（可匯出）
├── .env                  # 環境變數（不納入版控）
├── .env.example          # 環境變數範本
├── requirements.txt      # Python 套件清單
└── qa_template.csv       # Q&A 批次匯入範本
```

---

## 資料庫設計

### `line_users` — LINE 用戶資料

| 欄位 | 類型 | 說明 |
|------|------|------|
| id | INT | 主鍵 |
| line_user_id | VARCHAR(64) | LINE 用戶 ID（唯一） |
| display_name | VARCHAR(255) | 顯示名稱 |
| picture_url | VARCHAR(512) | 頭像網址 |
| status | ENUM | active / blocked |
| created_at | DATETIME | 建立時間 |

### `message_logs` — 訊息紀錄

| 欄位 | 類型 | 說明 |
|------|------|------|
| id | INT | 主鍵 |
| line_user_id | VARCHAR(64) | LINE 用戶 ID |
| direction | ENUM | incoming / outgoing |
| message_type | VARCHAR(32) | 訊息類型 |
| content | TEXT | 訊息內容 |
| created_at | DATETIME | 建立時間 |

### `qa_pairs` — Q&A 問答對

| 欄位 | 類型 | 說明 |
|------|------|------|
| id | INT | 主鍵 |
| question | TEXT | 標準問題 |
| answer | TEXT | 對應回答 |
| keywords | VARCHAR(512) | 比對關鍵字（逗號分隔） |
| category | VARCHAR(64) | 分類標籤 |
| is_active | BOOLEAN | 是否啟用 |
| is_trained | BOOLEAN | 是否已完成訓練建檔 |
| trained_at | DATETIME | 訓練建檔時間 |
| hit_count | INT | 命中次數 |

### `pending_questions` — 待處理問題

| 欄位 | 類型 | 說明 |
|------|------|------|
| id | INT | 主鍵 |
| line_user_id | VARCHAR(64) | LINE 用戶 ID |
| display_name | VARCHAR(255) | 用戶顯示名稱 |
| question | TEXT | 問題內容 |
| status | ENUM | pending / handled |
| note | TEXT | 處理備註 |

### `system_users` — 系統使用者

| 欄位 | 類型 | 說明 |
|------|------|------|
| id | INT | 主鍵 |
| username | VARCHAR(64) | 登入帳號（唯一） |
| display_name | VARCHAR(128) | 顯示名稱 |
| hashed_password | VARCHAR(255) | bcrypt 雜湊密碼 |
| role | ENUM | admin / user |
| is_active | BOOLEAN | 帳號是否啟用 |

---

## 功能說明

### LINE Bot 自動回覆流程

```
用戶傳訊息
    │
    ▼
模糊比對 Q&A（rapidfuzz，相似度 ≥ 60%）
    │
    ├── 找到匹配 ──► 回傳對應答案 + 更新命中數
    │
    └── 未找到 ───► 回覆「轉交客服」通知 + 記錄至待處理問題
```

### 管理員後台（`/admin`）

| 頁面 | 路徑 | 功能 |
|------|------|------|
| 儀表板 | `/admin` | 統計概覽 |
| Q&A 管理 | `/admin/qa` | 新增/編輯/刪除、CSV 匯入 |
| 待處理問題 | `/admin/pending` | 查看並標記處理 |
| 使用者管理 | `/admin/users` | 建立帳號、重設密碼、停用帳號 |

### 小編後台（`/dashboard`）

| 頁面 | 路徑 | 功能 |
|------|------|------|
| 儀表板 | `/dashboard` | 訓練進度概覽 |
| Q&A 訓練 | `/dashboard/qa` | 編輯 Q&A、點擊「訓練建檔」完成訓練 |
| 已訓練 Q&A | `/dashboard/trained` | 查看已建檔內容、匯出 CSV / XLS |
| 建立 Q&A | `/dashboard/create-qa` | 手動新增或批次 CSV 匯入 |

---

## 存取路徑

| 用途 | 網址 |
|------|------|
| 內網存取（小編） | `http://192.168.31.89/login` |
| 內網存取（管理員） | `http://192.168.31.89/admin` |
| LINE Webhook | `https://<cloudflare-tunnel-url>/webhook` |
| 健康檢查 | `http://192.168.31.89/health` |

---

## 系統服務

```bash
# LINE Bot 服務
sudo systemctl status linebot.service

# Nginx 反向代理
sudo systemctl status nginx

# MySQL 資料庫
sudo systemctl status mysql
```

---

## 環境變數設定（`.env`）

```env
# LINE Bot 設定
LINE_CHANNEL_SECRET=your_channel_secret
LINE_CHANNEL_ACCESS_TOKEN=your_access_token

# 資料庫設定
DATABASE_URL=mysql+pymysql://linebot:password@localhost/linebot

# 管理後台密碼
ADMIN_PASSWORD=your_admin_password

# 應用程式設定
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=false
```

---

## 安裝部署步驟

```bash
# 1. 建立虛擬環境
python3 -m venv /opt/linebot/venv

# 2. 安裝套件
/opt/linebot/venv/bin/pip install -r requirements.txt

# 3. 設定環境變數
cp .env.example .env
# 編輯 .env 填入實際設定值

# 4. 啟動服務
sudo systemctl start linebot.service
sudo systemctl start nginx

# 5. 啟動 Cloudflare Tunnel
cloudflared tunnel --url http://localhost:8000
```

---

## 主機資訊

| 項目 | 值 |
|------|-----|
| 主機名稱 | LINEBOT |
| IP 位址 | 192.168.31.89 |
| 作業系統 | Ubuntu 24.04.4 LTS |
| 虛擬化平台 | VMware |
| Python 版本 | 3.12.3 |
| Node.js 版本 | 18.19.1 |

---

*文件最後更新：2026-05-11*
