#!/bin/bash
# 一键启动:dashboard server + bot runner
# 双击此文件即可。在 Terminal 里看到 runner 输出后,浏览器会自动打开 dashboard。
# 按 Ctrl+C 停止。

set -e

# 切到项目根目录(无论你把 .command 放哪)
cd "$(dirname "$0")"

# 用今天的日期
RUN_DATE="$(date +%Y-%m-%d)"
DASH_PORT=8765
DASH_LOG="/tmp/orchestrator-dashboard.log"

echo "================================================================"
echo "  Trading Orchestrator — 一键启动"
echo "================================================================"
echo "项目目录:  $(pwd)"
echo "运行日期:  $RUN_DATE"
echo "Dashboard: http://127.0.0.1:${DASH_PORT}/dashboard-v3.html"
echo "Dashboard 日志: ${DASH_LOG}"
echo "================================================================"
echo

# 如果之前的 dashboard 还在跑就先杀掉,避免端口占用
EXISTING_PID="$(lsof -ti tcp:${DASH_PORT} 2>/dev/null || true)"
if [ -n "$EXISTING_PID" ]; then
  echo "[setup] 端口 ${DASH_PORT} 已被进程 ${EXISTING_PID} 占用,先停掉..."
  kill "$EXISTING_PID" 2>/dev/null || true
  sleep 1
fi

# 后台起 dashboard server
echo "[1/3] 启动 dashboard server (后台)..."
nohup python3 -m pipelines.dashboard_server --host 127.0.0.1 --port "${DASH_PORT}" \
  > "${DASH_LOG}" 2>&1 &
DASH_PID=$!

# 等服务起来
for i in 1 2 3 4 5 6 7 8; do
  if curl -sf "http://127.0.0.1:${DASH_PORT}/api/dashboard?date=${RUN_DATE}" > /dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! kill -0 "$DASH_PID" 2>/dev/null; then
  echo "[error] dashboard server 起不来,看日志: ${DASH_LOG}"
  tail -n 30 "${DASH_LOG}"
  exit 1
fi
echo "       OK (PID ${DASH_PID})"

# 打开浏览器
echo "[2/3] 打开浏览器..."
open "http://127.0.0.1:${DASH_PORT}/dashboard-v3.html"

# 注册 cleanup:Ctrl+C 时一起杀掉 dashboard
cleanup() {
  echo
  echo "[stop] 停止 dashboard server (PID ${DASH_PID})..."
  kill "$DASH_PID" 2>/dev/null || true
  echo "[stop] done."
}
trap cleanup EXIT INT TERM

# 前台跑 runner,每 5 分钟一圈,无限循环
echo "[3/3] 启动 bot runner (前台,Ctrl+C 停止)..."
echo "----------------------------------------------------------------"
python3 -m pipelines.runner \
  --date "${RUN_DATE}" \
  --paper-auto-approve \
  --iterations 0 \
  --interval-seconds 300
