#!/usr/bin/env bash
# ============================================================================
# 一键启动 Multi-Agent AIOps 平台 (macOS / 本机 venv 模式)
#
#   ./start.sh              # 启动: OrbStack -> 基础设施容器 -> MCP/API/Worker
#   ./start.sh --restart    # 先停掉已在跑的应用层, 再重新启动
#
# 做的事:
#   1) 确保 Docker daemon (OrbStack) 在跑, 没跑就尝试拉起并等待就绪
#   2) docker compose 起基础设施 (redis/postgres/etcd/minio/milvus/open-websearch)
#   3) scripts/run_all.sh 起 MCP 工具 + 多-worker API + N 个诊断 Worker
#   4) 轮询 /health/ready 确认全部依赖就绪
#
# 前提: 已经跑过一次 ./rebuild.sh (创建 .venv、装依赖、导入知识库)。
#       首次部署或环境损坏请用 ./rebuild.sh。
# 停止: bash scripts/stop_all.sh  (加 --infra 连容器一起停)
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

APP_PORT="${APP_PORT:-9900}"
RESTART=0
[[ "${1:-}" == "--restart" ]] && RESTART=1

# ---- 0) 解释器: 必须用 .venv (系统 python3 是不兼容的 3.14) ----
if [[ ! -x ".venv/bin/python" ]]; then
  echo "[start] 未找到 .venv/bin/python。请先运行 ./rebuild.sh 创建环境。" >&2
  exit 1
fi
PYBIN="$ROOT/.venv/bin/python"

# ---- 幂等保护: 应用已就绪且非 --restart, 直接返回 ----
if [[ "$RESTART" == "0" ]] && curl -sf --max-time 5 "http://localhost:$APP_PORT/api/v1/health/ready" >/dev/null 2>&1; then
  echo "[start] 应用已在运行且就绪: http://localhost:$APP_PORT"
  echo "[start] 如需重启请: ./start.sh --restart"
  exit 0
fi

if [[ "$RESTART" == "1" ]]; then
  echo "[start] --restart: 先停掉已在跑的应用层..."
  bash scripts/stop_all.sh || true
  sleep 2
fi

# ---- 1) Docker daemon ----
if ! docker info >/dev/null 2>&1; then
  echo "[start] Docker daemon 未运行, 尝试启动 OrbStack..."
  open -a OrbStack 2>/dev/null || open -a Docker 2>/dev/null || true
  echo "[start] 等待 Docker daemon 就绪..."
  for _ in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then break; fi
    sleep 2
  done
  if ! docker info >/dev/null 2>&1; then
    echo "[start] Docker 仍未就绪, 请手动启动 OrbStack / Docker Desktop 后重试。" >&2
    exit 1
  fi
fi
echo "[start] Docker daemon OK"

# ---- 2) 基础设施 ----
echo "[start] 启动基础设施容器..."
docker compose up -d redis postgres etcd minio standalone open-websearch

# 等容器真正 healthy 再起应用。OrbStack 冷启动时会陆续恢复/重启容器,
# 只 ping 一次就起 Worker 容易撞上 "Redis 刚被重启" 的竞态导致 Worker 崩溃。
echo "[start] 等待 Redis / Milvus 容器 healthy..."
wait_healthy() {
  local cname="$1" tries="${2:-60}"
  for _ in $(seq 1 "$tries"); do
    local st
    st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cname" 2>/dev/null || echo missing)"
    [[ "$st" == "healthy" ]] && return 0
    sleep 2
  done
  return 1
}
wait_healthy multi-agent-redis  || echo "[start] ⚠️ Redis 未在预期时间内 healthy, 继续尝试..."
wait_healthy multi-agent-milvus || echo "[start] ⚠️ Milvus 未在预期时间内 healthy, 继续尝试..."
# 额外静置 2s, 让冷启动阶段的容器彻底稳定
sleep 2

# ---- 3) 应用层 (MCP + API + Worker) ----
echo "[start] 启动应用层 (MCP + API + Worker)..."
SKIP_INFRA=1 PYTHON="$PYBIN" bash scripts/run_all.sh

# ---- 4) 就绪检查: API 依赖 + Worker 存活 ----
echo "[start] 等待 API 就绪 (/health/ready)..."
ready=0
for _ in $(seq 1 30); do
  if curl -sf --max-time 5 "http://localhost:$APP_PORT/api/v1/health/ready" >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 2
done

if [[ "$ready" != "1" ]]; then
  echo "[start] ⚠️  API 未在预期时间内就绪, 请查看日志: tail -f logs/api.log" >&2
  exit 1
fi

# 确认 Worker 真的活着 (alive_workers > 0)。/health/ready 不检查 Worker,
# 历史上出现过 "API 就绪但 Worker 全崩" 的半死状态, 这里补一道闸。
echo "[start] 等待 Worker 注册到队列..."
alive=0
for _ in $(seq 1 20); do
  alive="$(curl -sf --max-time 5 "http://localhost:$APP_PORT/api/v1/queue/status" 2>/dev/null \
    | "$PYBIN" -c 'import sys,json; print(json.load(sys.stdin).get("alive_workers",0))' 2>/dev/null || echo 0)"
  [[ "${alive:-0}" -ge 1 ]] && break
  sleep 2
done

echo ""
if [[ "${alive:-0}" -ge 1 ]]; then
  echo "[start] ✅ 启动完成: 依赖就绪, 存活 Worker=$alive。"
  echo "  Web UI:   http://localhost:$APP_PORT"
  echo "  Swagger:  http://localhost:$APP_PORT/docs"
  echo "  就绪检查: curl http://localhost:$APP_PORT/api/v1/health/ready"
  echo "  队列状态: curl http://localhost:$APP_PORT/api/v1/queue/status"
  echo "  停止:     bash scripts/stop_all.sh"
else
  echo "[start] ⚠️  API 就绪但没有存活 Worker, 诊断任务不会被消费。" >&2
  echo "         查看: tail -f logs/worker-1.log ; 重试: ./start.sh --restart" >&2
  exit 1
fi
