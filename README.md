# Multi-Agent AIOps Platform

面向 OnCall / SRE 场景的多智能体智能运维诊断平台。

项目基于 `FastAPI`、`LangGraph`、`RAG`、`Milvus` 和 `MCP` 构建，可根据告警或故障描述自动选择诊断 Skill，调用知识库和工具服务，输出结构化诊断报告。

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-green)
![LangGraph](https://img.shields.io/badge/LangGraph-Agent-orange)
![Milvus](https://img.shields.io/badge/Milvus-VectorDB-purple)
![MCP](https://img.shields.io/badge/MCP-Tools-black)

## 功能特性

- **多智能体诊断**：基于 `Plan -> Execute -> Replan -> Report` 的诊断流程。
- **Skill 路由**：根据用户输入选择 CPU、内存、磁盘、Redis、本机诊断、通用 OnCall 等 Skill。
- **RAG 知识库**：使用 DashScope Embedding + Milvus，支持 OnCall SOP 和 Prometheus 告警语料检索。
- **MCP 工具服务**：接入系统信息、网络诊断、Windows 日志、联网搜索、Docker 等工具服务。
- **工具安全控制**：通过 Skill 白名单、工具元信息和权限模式限制高风险操作。
- **SSE 流式输出**：前端实时展示路由、计划、工具调用和最终报告。
- **告警 Webhook**：支持 Alertmanager Webhook 触发后台诊断。

## 架构概览

```text
User / Alertmanager Webhook
        |
        v
FastAPI API
        |
        +--> Skill Router
        +--> Planner
        +--> Executor + MCP Tools + RAG
        +--> Replanner
        +--> Diagnosis Report

RAG:
docs/sop + data/kb_corpus/awesome-prometheus-alerts
        |
        v
DashScope Embedding -> Milvus Vector DB
```

## 数据来源

项目保留两类 OnCall 知识库语料：

| 路径 | 说明 |
|---|---|
| `docs/sop/` | 项目内置 Redis / MySQL / 通用告警 SOP |
| `data/kb_corpus/awesome-prometheus-alerts/` | 从开源项目 `samber/awesome-prometheus-alerts` 整理的 Prometheus 告警语料 |

第三方语料来源：

- **项目**：`samber/awesome-prometheus-alerts`
- **地址**：https://github.com/samber/awesome-prometheus-alerts
- **License**：Creative Commons Attribution 4.0 International (CC BY 4.0)

如果你公开发布包含该语料的版本，请遵守原项目的 CC BY 4.0 署名要求。

## 技术栈

| 类型 | 技术 |
|---|---|
| Web 服务 | FastAPI + Uvicorn |
| Agent 编排 | LangGraph + LangChain |
| LLM | DashScope / Qwen，兼容 DeepSeek OpenAI-style API |
| Embedding | DashScope `text-embedding-v4` |
| 向量数据库 | Milvus |
| 会话记忆 | Redis，可选 |
| 工具协议 | MCP / FastMCP |
| 本机监控 | psutil |
| 前端 | HTML + TailwindCSS + Vanilla JS |
| 运行环境 | Python 3.11+ / Docker / Windows PowerShell |

## 快速开始

### 1. 克隆项目

```powershell
git clone <your-repo-url>
cd multi_agent_github
```

### 2. 创建 Python 环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. 配置环境变量

```powershell
copy .env.example .env
notepad .env
```

至少需要配置：

```env
DASHSCOPE_API_KEY=your-dashscope-api-key
KB_ADMIN_TOKEN=change-this-admin-token
```

默认联网搜索使用 `mock` 模式，不需要外部搜索 API。

如需 Tavily 搜索：

```env
WEB_SEARCH_PROVIDER=tavily
TAVILY_API_KEY=your-tavily-api-key
```

### 4. 启动 Milvus 和 Redis

```powershell
docker compose up -d
```

Milvus 用于向量检索，Redis 用于可选的 RAG Chat 会话记忆。

### 5. 导入知识库

先检查切分结果：

```powershell
python scripts\ingest_kb_corpus.py --dry-run
```

确认无误后写入 Milvus：

```powershell
python scripts\ingest_kb_corpus.py --reset
```

如需重新从上游开源项目生成语料：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\fetch_kb_corpus.ps1
python scripts\convert_prometheus_alerts.py
```

### 6. 启动应用

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

默认会启动：

```text
FastAPI       http://localhost:9900
system MCP    http://localhost:8005/mcp
websearch MCP http://localhost:8006/mcp
winlog MCP    http://localhost:8008/mcp
network MCP   http://localhost:8009/mcp
docker MCP    http://localhost:8011/mcp
```

停止服务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Stop
```

## 访问地址

| 页面 | 地址 |
|---|---|
| Web UI | http://localhost:9900 |
| Swagger | http://localhost:9900/docs |
| ReDoc | http://localhost:9900/redoc |
| 健康检查 | http://localhost:9900/api/v1/health |
| 就绪检查 | http://localhost:9900/api/v1/health/ready |
| Attu Milvus UI | http://localhost:8000 |

## 使用示例

### 本机诊断

```text
我电脑很卡，帮我看下是不是 CPU 或内存太高
```

系统会选择本机诊断 Skill，并通过 MCP 工具读取 CPU、内存、磁盘和进程信息。

### Redis 告警诊断

```text
Redis 实例 redis-master-01 内存使用率 98%，客户端连接被强制断开
```

系统会结合 Redis SOP、Prometheus 告警知识库和工具返回的信息生成诊断报告。

### Alertmanager Webhook 模拟

```powershell
python scripts\mock_alert.py --scenario redis
python scripts\mock_alert.py --list-history
```

## API 概览

| 功能 | 方法 | 路径 |
|---|---|---|
| AIOps 诊断，SSE | POST | `/api/v1/aiops/diagnose` |
| Alertmanager Webhook | POST | `/api/v1/webhook/alertmanager` |
| RAG Chat | POST | `/api/v1/chat/stream` |
| Skill 列表 | GET | `/api/v1/skills` |
| 上传文档 | POST | `/api/v1/documents/upload` |
| 文档列表 | GET | `/api/v1/documents` |
| 删除文档 | DELETE | `/api/v1/documents/{source}` |
| 健康检查 | GET | `/api/v1/health` |
| 就绪检查 | GET | `/api/v1/health/ready` |

知识库上传和删除需要请求头：

```http
X-KB-Admin-Token: your-admin-token
```

## 项目结构

```text
multi_agent_github/
├── app/                    # FastAPI / Agent / RAG / Skill 核心代码
├── mcp_servers/            # MCP 工具服务
├── frontend/               # 前端页面
├── docs/sop/               # 内置 OnCall SOP
├── data/kb_corpus/         # RAG 开源语料
├── scripts/                # 知识库和告警模拟脚本
├── docker-compose.yml      # Milvus + etcd + MinIO + Attu + Redis
├── requirements.txt
├── .env.example
├── .gitignore
└── run.ps1                 # Windows 一键启动脚本
```

## 安全说明

- `.env` 不应提交到 GitHub。
- 本仓库只保留 `.env.example` 作为配置模板。
- 日志、缓存、数据库 volume、测试产物和本地运行历史已加入 `.gitignore`。
- Tavily、DeepSeek、DashScope 等密钥请只放在本地 `.env` 或部署平台的 Secret Manager 中。

## License

项目代码可按 MIT License 使用。

项目中的部分 RAG 知识库语料整理自 `samber/awesome-prometheus-alerts`，其原始内容遵循 CC BY 4.0。公开发布时请保留来源与许可说明。
