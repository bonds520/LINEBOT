#!/bin/bash
# Dify 安裝腳本（在 feature/dify-integration 分支上執行）
# 用法：bash dify/setup.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINEBOT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$SCRIPT_DIR/.env"

echo "======================================"
echo "  Dify 安裝腳本"
echo "======================================"

# ── 1. 確認 Docker ─────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "[1/5] 安裝 Docker..."
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    echo "[OK] Docker 安裝完成（請重新登入後再執行本腳本以套用群組）"
    exit 0
else
    echo "[1/5] Docker 已安裝：$(docker --version)"
fi

# ── 2. 確認 Docker Compose ─────────────────────────────────────
if ! docker compose version &>/dev/null; then
    echo "[2/5] 安裝 Docker Compose plugin..."
    sudo apt-get install -y docker-compose-plugin
else
    echo "[2/5] Docker Compose 已安裝：$(docker compose version)"
fi

# ── 3. 建立 dify/.env ──────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    echo "[3/5] 建立 dify/.env..."
    cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
    # 自動產生隨機 SECRET_KEY
    SECRET=$(openssl rand -hex 32 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s/your-secret-key-change-this/$SECRET/" "$ENV_FILE"
    echo "[OK] dify/.env 已建立（SECRET_KEY 已自動產生）"
    echo "     請編輯 $ENV_FILE 確認設定後再繼續"
    echo ""
    echo "     重要設定："
    echo "       EXPOSE_DIFY_PORT=8080   (Dify 對外 port)"
    echo "       DIFY_SECRET_KEY=已自動產生"
    echo ""
    echo "     設定完成後再次執行本腳本"
    exit 0
else
    echo "[3/5] dify/.env 已存在，跳過"
fi

# ── 4. 啟動 Dify ───────────────────────────────────────────────
echo "[4/5] 啟動 Dify Docker 容器..."
cd "$SCRIPT_DIR"
docker compose up -d

echo ""
echo "[5/5] 等待服務就緒（約 30 秒）..."
sleep 30

# ── 5. 確認狀態 ────────────────────────────────────────────────
docker compose ps

DIFY_PORT=$(grep "^EXPOSE_DIFY_PORT" "$ENV_FILE" | cut -d= -f2 | tr -d ' ')
DIFY_PORT=${DIFY_PORT:-8080}

echo ""
echo "======================================"
echo "  Dify 啟動完成！"
echo "======================================"
echo ""
echo "  管理介面：http://192.168.31.89:${DIFY_PORT}"
echo "  首次使用請前往管理介面完成初始化設定"
echo ""
echo "  下一步："
echo "  1. 開啟 http://192.168.31.89:${DIFY_PORT} 建立管理員帳號"
echo "  2. 建立一個 App（聊天助手）"
echo "  3. 設定 LLM（OpenAI / Anthropic / Gemini）"
echo "  4. 從 App → API Access 取得 API Key"
echo "  5. 將 API Key 填入 /opt/linebot/.env："
echo "       DIFY_API_URL=http://localhost:${DIFY_PORT}/v1"
echo "       DIFY_API_KEY=<你的 App API Key>"
echo "       USE_DIFY=true"
echo "  6. 重啟 LINE Bot：sudo systemctl restart linebot"
echo ""
