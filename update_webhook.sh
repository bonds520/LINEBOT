#!/bin/bash
# 啟動 Cloudflare Tunnel 並顯示最新 Webhook URL

ENV_FILE="/opt/linebot/.env"
LOG_FILE="/tmp/cloudflared_tunnel.log"

# 載入環境變數
set -a
source "$ENV_FILE"
set +a

echo "============================================"
echo "  LINE Bot - Webhook URL 更新工具"
echo "============================================"
echo ""

# 檢查是否已有執行中的 Tunnel
EXISTING_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_FILE" 2>/dev/null | head -1)
TUNNEL_RUNNING=$(pgrep -f "cloudflared tunnel" 2>/dev/null)

if [ -n "$TUNNEL_RUNNING" ] && [ -n "$EXISTING_URL" ]; then
    echo "  ℹ️  偵測到已執行中的 Tunnel，使用現有 URL"
    TUNNEL_URL="$EXISTING_URL"
else
    echo "[1/2] 啟動 Cloudflare Quick Tunnel..."
    pkill -f "cloudflared tunnel" 2>/dev/null || true
    sleep 8
    rm -f "$LOG_FILE"
    cloudflared tunnel --url http://localhost:8000 > "$LOG_FILE" 2>&1 &

    echo "      等待 Tunnel 建立中..."
    TUNNEL_URL=""
    for i in $(seq 1 60); do
        TUNNEL_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_FILE" 2>/dev/null | head -1)
        if [ -n "$TUNNEL_URL" ]; then
            break
        fi
        sleep 1
    done

    if [ -z "$TUNNEL_URL" ]; then
        echo ""
        echo "❌ 無法取得 Tunnel URL（Cloudflare 可能暫時限速）"
        echo "   請等待 1-2 分鐘後重試：bash /opt/linebot/update_webhook.sh"
        exit 1
    fi
fi

WEBHOOK_URL="${TUNNEL_URL}/webhook"

# 嘗試透過 LINE API 自動更新
echo "[2/2] 嘗試自動更新 LINE Webhook..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT \
    https://api.line.me/v2/bot/channel/webhook/endpoint \
    -H "Authorization: Bearer ${LINE_CHANNEL_ACCESS_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"endpoint\": \"${WEBHOOK_URL}\"}" 2>/dev/null)

# 儲存目前 Tunnel URL 記錄
if grep -q "^TUNNEL_URL=" "$ENV_FILE"; then
    sed -i "s|^TUNNEL_URL=.*|TUNNEL_URL=${TUNNEL_URL}|" "$ENV_FILE"
else
    echo "TUNNEL_URL=${TUNNEL_URL}" >> "$ENV_FILE"
fi

echo ""
echo "============================================"
if [ "$HTTP_CODE" = "200" ]; then
    echo "  ✅ Webhook URL 已自動更新完成！"
else
    echo "  📋 請手動更新 LINE Webhook URL"
fi
echo "============================================"
echo ""
echo "  新的 Webhook URL："
echo ""
echo "  >>> ${WEBHOOK_URL} <<<"
echo ""
if [ "$HTTP_CODE" != "200" ]; then
    echo "  手動更新步驟："
    echo "  1. 開啟 LINE Developers Console"
    echo "     https://developers.line.biz/console/"
    echo "  2. 選擇你的 Channel → Messaging API"
    echo "  3. 找到「Webhook URL」欄位"
    echo "  4. 貼上上方 Webhook URL 並儲存"
    echo "  5. 按下「Verify」確認連線"
    echo ""
fi
echo "============================================"
