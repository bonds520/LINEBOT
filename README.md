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
│   ├── main.py              # FastAPI 主程式、Webhook 端點、路由註冊
│   ├── database.py          # SQLAlchemy 資料庫連線設定
│   ├── models.py            # 資料庫模型定義
│   ├── handlers.py          # LINE 事件處理（訊息、加好友、封鎖）
│   ├── matcher.py           # 關鍵字模糊比對邏輯
│   ├── auth.py              # 使用者認證、Session 管理、密碼雜湊
│   ├── admin.py             # 管理員後台路由
│   └── user_panel.py        # 小編使用者後台路由
├── templates/
│   ├── base.html            # 管理員後台基礎模板
│   ├── dashboard.html       # 管理員儀表板
│   ├── login.html           # 管理員登入頁
│   ├── qa_list.html         # 管理員 Q&A 管理
│   ├── pending.html         # 管理員待處理問題
│   ├── user_mgmt.html       # 使用者帳號管理
│   ├── settings.html        # Webhook 設定 & LINE Channel 憑證切換
│   ├── backup.html          # 資料庫備份管理
│   ├── user_base.html       # 小編後台基礎模板
│   ├── user_login.html      # 小編登入頁
│   ├── user_dashboard.html  # 小編儀表板
│   ├── user_qa.html         # Q&A 訓練列表
│   ├── user_create_qa.html  # 建立 Q&A
│   └── user_trained.html    # 已訓練 Q&A（可匯出）
├── backups/                 # 資料庫備份存放目錄
├── .env                     # 環境變數（不納入版控）
├── .env.example             # 環境變數範本
├── requirements.txt         # Python 套件清單
├── start_tunnel.sh          # Cloudflare Tunnel 啟動 + 自動更新 Webhook
├── update_webhook.sh        # Webhook URL 手動更新腳本
└── qa_template.csv          # Q&A 批次匯入範本
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
模糊比對 Q&A（rapidfuzz，相似度 ≥ 60%，僅比對已訓練建檔的 Q&A）
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
| Webhook 設定 | `/admin/settings` | Tunnel 管理、Webhook 更新、Channel 憑證切換 |
| 資料庫備份 | `/admin/backup` | 一鍵備份、下載、刪除備份檔 |

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
| 首頁（自動導向登入） | `http://192.168.31.89` |
| 小編登入 | `http://192.168.31.89/login` |
| 管理員後台 | `http://192.168.31.89/admin` |
| LINE Webhook | `https://<cloudflare-tunnel-url>/webhook` |
| 健康檢查 | `http://192.168.31.89/health` |

---

## 系統服務

四個服務均設定為**開機自動啟動**：

| 服務 | 說明 |
|------|------|
| `linebot` | FastAPI LINE Bot 主程式 |
| `nginx` | 反向代理（Port 80 → 8000） |
| `mysql` | 資料庫 |
| `cloudflared` | Cloudflare Tunnel + 自動更新 Webhook |

```bash
# 查看所有服務狀態
sudo systemctl status linebot nginx mysql cloudflared

# 重啟個別服務
sudo systemctl restart linebot.service
sudo systemctl restart cloudflared.service

# 查看服務即時日誌
journalctl -u linebot.service -f
journalctl -u cloudflared.service -f
```

---

## Webhook URL 更新

### 自動更新（預設行為）

`cloudflared` 服務啟動後會自動執行以下流程，**無需手動操作**：

```
cloudflared 啟動
    │
    ▼
取得新的 Tunnel URL（trycloudflare.com）
    │
    ▼
等待 Tunnel 連線確認完成
    │
    ▼
呼叫 LINE API 更新 Webhook endpoint
    │
    ▼
更新 .env 的 TUNNEL_URL 紀錄
```

日誌確認（成功時顯示）：
```bash
journalctl -u cloudflared.service | grep "\[OK\]"
# [OK] LINE Webhook 已自動更新: https://xxxx.trycloudflare.com/webhook
```

### 手動更新（Tunnel 異常時）

**方法一：管理後台**
1. 登入 `http://192.168.31.89/admin`
2. 點左側 **Webhook 設定**
3. 點「重啟 Tunnel 取得新 URL」→ 「更新至 LINE」

**方法二：指令**
```bash
bash /opt/linebot/update_webhook.sh
```

---

## LINE Channel 憑證切換（移轉正式 Channel）

從訓練用 Channel 移轉至正式 Channel，**Q&A 訓練資料完全保留，無需重新訓練**。

### 操作步驟

1. 登入管理後台 → **Webhook 設定**
2. 在「LINE Channel 憑證切換」區塊輸入正式 Channel 的：
   - **Channel Secret**
   - **Channel Access Token**
3. 點「驗證並切換」→ 系統自動驗證 Token 有效性並顯示 Bot 資訊
4. 確認正確後輸入 `CONFIRM` → 點「確認切換」
5. 服務自動重啟（約 10 秒）
6. 回到 **Webhook 設定** → 更新 Webhook URL 至新 Channel

### 切換後保留的資料

| 資料 | 是否保留 |
|------|---------|
| 所有 Q&A 問答內容 | ✅ 完整保留 |
| 訓練建檔狀態 | ✅ 完整保留 |
| 分類與關鍵字 | ✅ 完整保留 |
| 命中次數統計 | ✅ 完整保留 |
| 系統使用者帳號 | ✅ 完整保留 |
| LINE 用戶資料 | ⚠️ 保留舊資料，正式用戶加入後自動新增 |

---

## 資料庫備份與還原

### 管理後台備份（建議）

1. 登入管理後台 → **資料庫備份**
2. 點「立即備份」→ 自動產生 `.sql.gz` 壓縮備份
3. 點「下載」將備份檔儲存至本機

### 指令備份

```bash
# 建立備份（壓縮）
mysqldump -u linebot -p'your_password' linebot | gzip > backup_$(date +%Y%m%d_%H%M%S).sql.gz

# 列出備份檔
ls -lh /opt/linebot/backups/
```

### 還原備份

```bash
# 解壓縮並還原
gunzip backup_20260511_181040.sql.gz
mysql -u linebot -p'your_password' linebot < backup_20260511_181040.sql
```

---

## 整機移轉至新主機

### 第一步：備份舊主機

```bash
# 1. 備份資料庫（管理後台操作，或指令）
mysqldump -u linebot -p'your_password' linebot | gzip > /opt/linebot/backups/migration_$(date +%Y%m%d).sql.gz

# 2. 備份環境變數（含所有憑證）
cp /opt/linebot/.env ~/backup.env

# 3. 將兩個檔案複製到新主機
scp /opt/linebot/backups/migration_*.sql.gz user@new-host:~/
scp ~/backup.env user@new-host:~/
```

### 第二步：新主機環境安裝

```bash
# 安裝系統套件
sudo apt-get update
sudo apt-get install -y mysql-server nginx python3-venv python3-pip git

# 安裝 cloudflared
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb

# 從 GitHub 拉取程式碼
sudo mkdir -p /opt/linebot
sudo chown $USER:$USER /opt/linebot
git clone https://github.com/bonds520/LINEBOT.git /opt/linebot

# 建立虛擬環境並安裝套件
python3 -m venv /opt/linebot/venv
/opt/linebot/venv/bin/pip install -r /opt/linebot/requirements.txt
/opt/linebot/venv/bin/pip install 'bcrypt==4.0.1'
```

### 第三步：還原資料庫

```bash
# 建立資料庫與使用者
sudo mysql -e "
CREATE DATABASE linebot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'linebot'@'localhost' IDENTIFIED BY 'your_password';
GRANT ALL PRIVILEGES ON linebot.* TO 'linebot'@'localhost';
FLUSH PRIVILEGES;"

# 還原備份
gunzip ~/migration_*.sql.gz
mysql -u linebot -p'your_password' linebot < ~/migration_*.sql
```

### 第四步：還原設定並啟動服務

```bash
# 還原環境變數
cp ~/backup.env /opt/linebot/.env

# 建立 backups 目錄
mkdir -p /opt/linebot/backups

# 設定 Nginx
sudo tee /etc/nginx/sites-available/linebot > /dev/null <<EOF
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/linebot /etc/nginx/sites-enabled/linebot
sudo rm -f /etc/nginx/sites-enabled/default
sudo systemctl restart nginx

# 設定 systemd 服務
sudo tee /etc/systemd/system/linebot.service > /dev/null <<EOF
[Unit]
Description=LINE Bot Service
After=network.target mysql.service
Requires=mysql.service

[Service]
Type=simple
User=$USER
WorkingDirectory=/opt/linebot
EnvironmentFile=/opt/linebot/.env
ExecStart=/opt/linebot/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable linebot
sudo systemctl start linebot

# 允許無密碼重啟服務
echo "$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart linebot.service" | sudo tee /etc/sudoers.d/linebot-restart
sudo chmod 440 /etc/sudoers.d/linebot-restart
```

### 第五步：設定並啟動 Cloudflare Tunnel 服務

```bash
# 複製 cloudflared 服務設定
sudo tee /etc/systemd/system/cloudflared.service > /dev/null <<EOF
[Unit]
Description=Cloudflare Tunnel
After=network-online.target linebot.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
ExecStart=/opt/linebot/start_tunnel.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable cloudflared
sudo systemctl start cloudflared

# 確認 Webhook 自動更新結果（約 15 秒後）
journalctl -u cloudflared.service | grep "\[OK\]\|\[FAIL\]"
```

> Tunnel 啟動後會**自動**偵測新 URL 並更新 LINE Webhook，無需手動操作。

---

## 環境變數設定（`.env`）

```env
# LINE Bot 設定
LINE_CHANNEL_SECRET=your_channel_secret
LINE_CHANNEL_ACCESS_TOKEN=your_channel_access_token

# 資料庫設定
DATABASE_URL=mysql+pymysql://linebot:your_password@localhost/linebot

# 管理後台密碼
ADMIN_PASSWORD=your_admin_password

# 應用程式設定
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=false

# Cloudflare Tunnel URL（自動更新）
TUNNEL_URL=https://xxx.trycloudflare.com
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

---

*文件最後更新：2026-05-11（新增 Cloudflare Tunnel 自動更新 Webhook 機制）*
