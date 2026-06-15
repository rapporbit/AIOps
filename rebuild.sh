#!/usr/bin/env bash
# ============================================================================
# 一键重建 Multi-Agent AIOps 平台 (从干净状态重建本机环境)
#
#   ./rebuild.sh            # 重建 venv + 依赖, 重置并重导知识库, 然后启动
#   ./rebuild.sh --wipe     # 额外: docker compose down -v 清空所有数据卷
#                           #       (Postgres 事实库 / Milvus 向量库全部清掉)
#   ./rebuild.sh --skip-kb  # 跳过知识库重导 (保留 Milvus 里已有的 collection)
#
# 适用场景: 首次部署 / .venv 损坏 / 依赖错乱 / 想把知识库重新灌一遍。
# 日常只是开机启动请用 ./start.sh, 不需要重建。
#
# 关键约定 (见 memory: local-startup-setup):
#   - 用 python3.13 建 venv (系统 python3 是不兼容的 3.14)
#   - Postgres 映射到宿主 5433 (5432 被其它项目占用), 由 .env 控制
#   - 模型 deepseek-v4-* 经 DashScope 调用, embedding/rerank 也走 DashScope
#   以上都已写在 .env, 本脚本不改 .env。
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

WIPE=0
SKIP_KB=0
for arg in "$@"; do
  case "$arg" in
    --wipe) WIPE=1 ;;
    --skip-kb) SKIP_KB=1 ;;
    *) echo "[rebuild] 未知参数: $arg" >&2; exit 1 ;;
  esac
done

# ---- 0) 选择建 venv 用的解释器 (3.11~3.13, 避开 3.14) ----
PYBUILD=""
for c in python3.13 python3.12 python3.11; do
  if command -v "$c" >/dev/null 2>&1; then PYBUILD="$c"; break; fi
done
if [[ -z "$PYBUILD" ]]; then
  echo "[rebuild] 找不到 python3.11/3.12/3.13。请先安装 (推荐 brew install python@3.13)。" >&2
  exit 1
fi
echo "[rebuild] 用 $PYBUILD ($($PYBUILD --version 2>&1)) 建 venv"

if [[ ! -f ".env" ]]; then
  echo "[rebuild] 缺少 .env, 从 .env.example 复制并按 memory 配好 key/端口后再重试。" >&2
  echo "          cp .env.example .env" >&2
  exit 1
fi

# ---- 1) 停掉旧的应用层进程 ----
echo "[rebuild] 停止已在跑的应用层..."
bash scripts/stop_all.sh || true
sleep 2

# ---- 2) 确保 Docker daemon ----
if ! docker info >/dev/null 2>&1; then
  echo "[rebuild] Docker daemon 未运行, 尝试启动 OrbStack..."
  open -a OrbStack 2>/dev/null || open -a Docker 2>/dev/null || true
  for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
  docker info >/dev/null 2>&1 || { echo "[rebuild] Docker 仍未就绪, 请手动启动后重试。" >&2; exit 1; }
fi
echo "[rebuild] Docker daemon OK"

# ---- 3) (可选) 清空数据卷 ----
if [[ "$WIPE" == "1" ]]; then
  echo "[rebuild] --wipe: docker compose down -v (清空 Postgres / Milvus 等所有数据卷)..."
  docker compose down -v || true
fi

# ---- 4) 重建 venv + 依赖 ----
echo "[rebuild] 重建 .venv ..."
rm -rf .venv
"$PYBUILD" -m venv .venv
PYBIN="$ROOT/.venv/bin/python"
"$PYBIN" -m pip install --upgrade pip
echo "[rebuild] 安装依赖 (requirements.txt)... 这一步较慢"
"$PYBIN" -m pip install -r requirements.txt
"$PYBIN" -c "import fastapi, pymilvus, langgraph, langchain, redis, asyncpg, ragas; print('[rebuild] 核心依赖导入 OK')"

# ---- 5) 起基础设施 ----
echo "[rebuild] 启动基础设施容器..."
docker compose up -d redis postgres etcd minio standalone open-websearch
echo "[rebuild] 等待 Redis / Milvus 就绪..."
for _ in $(seq 1 40); do
  if docker compose exec -T redis redis-cli ping >/dev/null 2>&1; then break; fi
  sleep 1
done
# Milvus 起来要更久一点, 给它一些时间再导入
for _ in $(seq 1 40); do
  if curl -sf --max-time 3 http://localhost:9091/healthz >/dev/null 2>&1; then break; fi
  sleep 2
done

# ---- 6) 重导知识库 ----
if [[ "$SKIP_KB" == "1" ]]; then
  echo "[rebuild] --skip-kb: 跳过知识库重导。"
else
  echo "[rebuild] 重置并重导知识库 (DashScope embedding, 约 2-3 分钟)..."
  "$PYBIN" scripts/ingest_kb_corpus.py --reset
fi

# ---- 7) 启动应用层 + 就绪检查 ----
echo "[rebuild] 启动应用层..."
exec ./start.sh --restart
