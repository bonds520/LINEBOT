#!/bin/bash
LOG=/tmp/cloudflared_tunnel.log
ENV_FILE=/opt/linebot/.env

# 清空舊 log
> "$LOG"

# 背景啟動 cloudflared，輸出同時寫入 log 和 journald
/usr/bin/cloudflared tunnel --url http://localhost:8000 2>&1 | tee "$LOG" &
PIPE_PID=$!

# 等待 URL 出現（最多 60 秒）
URL=""
for i in $(seq 1 60); do
    URL=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1)
    [ -n "$URL" ] && break
    sleep 1
done

if [ -n "$URL" ]; then
    # 等待 tunnel 實際連線完成（Registered tunnel connection）
    for i in $(seq 1 30); do
        grep -q "Registered tunnel connection" "$LOG" 2>/dev/null && break
        sleep 1
    done
    # 多等 3 秒讓邊緣節點就緒
    sleep 3

    # 載入 LINE 憑證
    set -a; source "$ENV_FILE"; set +a

    # 呼叫 LINE API 更新 Webhook
    HTTP=$(curl -s -w "%{http_code}" -o /tmp/lw_resp.json \
        -X PUT https://api.line.me/v2/bot/channel/webhook/endpoint \
        -H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"endpoint\": \"${URL}/webhook\"}")

    if [ "$HTTP" = "200" ]; then
        echo "[OK] LINE Webhook 已自動更新: ${URL}/webhook"
        # 更新 .env 的 TUNNEL_URL
        if grep -q "^TUNNEL_URL=" "$ENV_FILE"; then
            sed -i "s|^TUNNEL_URL=.*|TUNNEL_URL=${URL}|" "$ENV_FILE"
        else
            echo "TUNNEL_URL=${URL}" >> "$ENV_FILE"
        fi
    else
        echo "[FAIL] LINE Webhook 更新失敗 (HTTP ${HTTP}): $(cat /tmp/lw_resp.json)"
    fi
else
    echo "[WARN] 無法取得 Tunnel URL，跳過 Webhook 自動更新"
fi

# 保持前景等待 cloudflared 結束
wait $PIPE_PID
