# Step 1：项目初始化 & 配置体系 — FastAPI 脚手架、配置管理、日志、中间件、异常

---

## 这一步要解决的问题

一个企业级 AIOps 平台启动时需要连接 LLM API、Milvus、Redis、Postgres 等多个外部服务，涉及几十个配置项。同时 API 层需要统一的日志格式、请求追踪、异常处理。

这一步的目标是搭出一个**干净、规范、可扩展**的骨架，让后续所有模块"直接填业务逻辑就好"。

---

## 目录结构

```
app/
├── __init__.py
├── config.py              # 配置管理（核心）
├── main.py                # FastAPI 入口 + lifespan
├── logging_config.py      # 日志系统
├── exceptions.py          # 异常体系
├── api/
│   ├── middleware.py       # 中间件（RequestID + Logging + CORS）
│   └── v1/
│       ├── health.py      # 健康检查
│       ├── aiops.py       # 诊断接口
│       └── ...            # 其它路由
└── schemas/
    └── common.py          # 统一响应模型 ApiResponse
```

---

## 1. config.py — 配置管理

### 为什么选 Pydantic Settings

项目用 `pydantic-settings` 的 `BaseSettings`，而不是 `python-dotenv` + 手动 `os.getenv()`。好处是：字段类型自动校验（`port: int`，如果 `.env` 里写了 `PORT=abc` 启动就报错）；默认值清晰；字段说明自动生成文档。

### 核心配置

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,   # DASHSCOPE_API_KEY 自动匹配 dashscope_api_key
        extra="ignore",         # .env 里多了未定义的字段不报错
    )
```

`case_sensitive=False` 让 Python 风格的字段名（小写 + 下划线）自动匹配 POSIX 风格的环境变量（大写）。`extra="ignore"` 让旧 `.env` 文件多出来的字段不会导致启动失败——向后兼容。

### 两层校验策略（面试重点）

**第一层：field_validator — 字段级格式归一化**

在对象构造时就跑，做单字段的归一化和合法性检查：

```python
@field_validator("log_level")
@classmethod
def _normalize_log_level(cls, v: str) -> str:
    return v.upper()  # 用户写 "info" 也能正常工作

@field_validator("embedding_provider")
@classmethod
def _normalize_embedding_provider(cls, v: str) -> str:
    value = (v or "dashscope").lower().strip()
    if value not in {"dashscope", "ollama"}:
        raise ValueError("embedding_provider 只能是 dashscope 或 ollama")
    return value
```

**第二层：validate_runtime() — 跨字段运行时校验**

在 lifespan 启动钩子里手动调用。为什么不合并到第一层？因为它需要**扫描所有模型配置字段**，看哪些以 "deepseek" 开头，才能决定检查哪个 API Key：

```python
def validate_runtime(self) -> None:
    configured_models = [
        self.dashscope_chat_model,
        self.dashscope_router_model,
        self.agent_planner_model,
        self.agent_executor_model,
        self.agent_report_model,
    ]
    uses_deepseek = any(
        (model or "").strip().lower().startswith("deepseek")
        for model in configured_models
    )
    if uses_deepseek and (not self.deepseek_api_key or ...):
        raise RuntimeError("DEEPSEEK_API_KEY 未配置...")
```

### lru_cache 单例

```python
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

settings = get_settings()  # 全局便捷访问
```

`Settings()` 每次构造都会读 `.env` 文件 + 做校验，有 I/O 开销。`lru_cache(maxsize=1)` 保证整个进程只构造一次。测试时 `get_settings.cache_clear()` 重置。

### 面试追问

**Q：为什么 validate_runtime() 不放在 field_validator 里？**

> 因为它是跨字段校验——需要看所有 model 字段才能决定检查哪个 API Key。Pydantic v2 的 `field_validator` 只能拿到单个字段的值。虽然可以用 `model_validator(mode='after')` 做模型级校验，但项目选择把它显式放在 lifespan 里调用，语义更清晰：「这是运行时检查，不是类型检查」。

**Q：lru_cache 做单例有什么坑？**

> 多进程场景下（`uvicorn --workers 4`），每个 worker 各自构造一份 Settings。但这恰好是对的——每个 worker 独立运行。真正的坑是：运行时改了 `.env`，`lru_cache` 不会重新加载，需要手动 `cache_clear()`。

**Q：mcp_servers 属性为什么用 `@property` 而不是 Field？**

> 因为它是从多个扁平字段组装出来的嵌套字典，不是直接从 `.env` 读的。`@property` 每次调用时动态组装，保证和底层字段同步。如果用 Field，就得在 `.env` 里写 JSON 嵌套结构，用户体验很差。

---

## 2. logging_config.py — 日志系统

### 为什么用 Loguru 替代标准 logging

标准库 logging 需要配置 Handler/Formatter/Logger 三件套，样板代码多。Loguru 开箱即用，而且原生支持 `contextualize()`（用于注入 request_id）。

### 关键设计

**控制台 + 文件双输出**：开发模式彩色便于 debug，生产模式纯文本便于日志聚合。文件按天滚动 + 自动压缩 + 自动清理。

**`enqueue=True` 异步写文件**：文件 I/O 是阻塞操作，`enqueue=True` 让 Loguru 把日志写入放到后台线程的队列里异步执行。对高吞吐的 async FastAPI 应用很关键——不让日志 I/O 阻塞事件循环。

**拦截标准 logging**：uvicorn、pymilvus、langchain 等依赖用的是标准库 `logging`。通过 `_InterceptHandler` 把它们的输出统一转发到 Loguru，保证所有日志格式一致、都带 request_id。

**默认 extra 防 KeyError**：format 字符串里有 `{extra[request_id]}`，但非请求上下文（比如启动日志）没有 request_id。`logger.configure(extra={"request_id": "-"})` 设默认值，避免 KeyError。

### 面试追问

**Q：为什么 `diagnose=False` 在生产环境？**

> `diagnose=True` 会在异常日志里打印出每个栈帧的局部变量值。开发很方便，但生产环境可能泄露 API Key、用户数据等敏感信息。

---

## 3. middleware.py — 中间件洋葱模型

### 三层中间件

注册顺序和执行顺序相反（洋葱模型）：

```python
def setup_middlewares(app: FastAPI) -> None:
    app.add_middleware(LoggingMiddleware)      # 最后加 → 最内层
    app.add_middleware(RequestIDMiddleware)    # 中间
    app.add_middleware(CORSMiddleware, ...)    # 最先加 → 最外层
```

请求经过：`CORS → RequestID → Logging → 业务handler → Logging → RequestID → CORS`

### RequestIDMiddleware

每个请求分配一个唯一 ID（优先复用客户端传来的 `X-Request-ID`，否则生成新 UUID）。通过 `logger.contextualize(request_id=...)` 注入 Loguru context，这样同一个请求内所有 `logger.info/warning/error` 调用都自动带上这个 ID。

### 面试追问

**Q：RequestID 为什么放在 Logging 外面？**

> RequestID 先执行，把 `request_id` 注入 logger context。然后 Logging 中间件才开始记日志，这样日志天然就带上了 `request_id`。如果反过来，Logging 记日志时还没有 `request_id`。

**Q：为什么跳过静态资源和健康检查的日志？**

> 静态文件每秒可能几十个请求，健康检查每 10 秒一次。如果都打日志，会刷屏淹没真正有用的业务日志。`skip_log` 判断路径前缀，这些请求静默处理。

---

## 4. exceptions.py — 异常体系

### 继承树设计

```
AppException (500)
├── BadRequestError (400)
│   └── UnsupportedFileTypeError
├── NotFoundError (404)
│   └── DocumentNotFoundError
└── ServiceError (500)
    ├── VectorStoreError
    ├── EmbeddingError
    ├── LLMError
    ├── MCPConnectionError (503)
    └── AgentExecutionError
```

每个异常自带 `status_code`（HTTP 状态码）、`code`（业务错误码）、`message`（用户可见的提示）。构造时可以覆盖任何一个。

### 三层异常处理器

在 `main.py` 里注册：

| 处理器 | 捕获什么 | 返回什么 |
|---|---|---|
| `handle_app_exception` | `AppException` 子类 | 对应的 status_code + ApiResponse.error |
| `handle_validation_error` | `RequestValidationError` | 422 + 字段级错误详情 |
| `handle_unexpected_exception` | `Exception` | 500 + 通用错误（debug 模式才暴露 detail） |

### 面试追问

**Q：为什么分三层？只用 Exception 兜底不行吗？**

> 分层是为了返回不同的 HTTP 状态码和错误结构。`AppException` 能精准返回 400/404/503，`RequestValidationError` 固定 422 带字段级错误，`Exception` 兜底返回 500。生产环境 `handle_unexpected_exception` 不暴露内部信息（`detail=str(exc) if settings.debug else None`），防止泄露。

---

## 5. main.py — Lifespan 启动顺序

Lifespan 是 FastAPI 推荐的启动/关闭钩子（替代旧的 `on_startup/on_shutdown`）。项目的启动顺序有讲究：

```
启动:
  1. validate_runtime()                    # 配置不对直接退出
  2. milvus_manager.connect()              # 必需依赖
  3. connect_postgres() + init_schema()    # 条件启用（incident_pipeline_enabled）
  4. incident_queue.connect()              # 条件启用
  5. mcp_client_manager.connect(fail_silently=True)  # 可选依赖

关闭（反向清理）:
  5. mcp_client_manager.close()
  4. incident_queue.close()
  3. close_postgres()
  2. milvus_manager.disconnect()
```

### 设计原则

**必需依赖失败直接退出，可选依赖 fail_silently**。Milvus 是 RAG 的基础，挂了整个系统没法用，应该阻止启动。MCP 挂了只影响工具调用，RAG 知识库和手动诊断还能用，应该继续启动。

**启动顺序从轻到重**：先校验配置（纯内存操作，最快）→ 再连 Milvus → 再连 Postgres → 最后连 MCP（最慢、最可能失败）。如果 Milvus 连不上，就不会浪费时间去连后面的。

**关闭顺序反向**：先关最外层（MCP），最后关最底层（Milvus）。保证正在进行的请求用到的底层连接最后释放。

### 面试追问

**Q：为什么用 lifespan 而不是 on_startup/on_shutdown？**

> `on_startup/on_shutdown` 是旧 API，FastAPI 官方推荐 lifespan。lifespan 用 async context manager，`yield` 前后分别是启动和关闭逻辑，代码结构更清晰。而且 lifespan 和 app 实例绑定，不是全局注册，测试时更容易 mock。

---

## 6. schemas/common.py — 统一响应模型

```python
class ApiResponse(BaseModel, Generic[T]):
    code: str = "OK"
    message: str = "success"
    data: Optional[T] = None
    detail: Optional[Any] = None
```

所有接口统一返回 `ApiResponse`，保证前端只需要解析一种格式。`code` 用于程序判断（`"OK"` / `"NOT_FOUND"` / `"RATE_LIMITED"`），`message` 用于用户展示，`data` 是业务数据，`detail` 是可选的额外信息（错误时的详细原因）。

---

## 7. 遇到的难点总结

### 难点 1：env_file 在 Docker 和本地的路径差异

本地开发时 `.env` 在项目根目录，但 Docker 里工作目录是 `/app`。如果写死路径会在某个环境下找不到文件。

解决方案：`SettingsConfigDict(env_file=".env")` 用相对路径，Docker 的 `WORKDIR` 和 `COPY` 保证 `.env` 在工作目录下。同时 `docker-compose.yml` 也用 `env_file: .env` 注入环境变量作为双保险。

### 难点 2：Loguru 的 request_id 在非请求上下文下报错

启动日志、定时任务、Worker 进程的日志没有经过 RequestIDMiddleware，`extra["request_id"]` 会 KeyError。

解决方案：`logger.configure(extra={"request_id": "-"})` 设全局默认值。请求上下文里 `contextualize()` 覆盖为真实 ID，非请求上下文显示 `-`。

### 难点 3：中间件顺序导致日志缺失 request_id

最初把 LoggingMiddleware 加在 RequestIDMiddleware 前面（执行时 Logging 反而在后面），导致日志里没有 request_id。

解决方案：理解洋葱模型——**后加的先执行**。RequestID 必须比 Logging 后加（先执行），才能在 Logging 记日志时拿到 request_id。

---

## 快速回顾清单

| 模块 | 核心设计 | 面试关键词 |
|---|---|---|
| config.py | BaseSettings + 两层校验 + lru_cache | 字段级归一化 vs 跨字段运行时检查 |
| logging_config.py | Loguru + enqueue + 拦截标准 logging | 异步写文件、diagnose=False |
| middleware.py | CORS → RequestID → Logging 洋葱模型 | contextualize、skip_log |
| exceptions.py | 继承树 + 三层异常处理器 | 精准状态码、debug 才暴露 detail |
| main.py | lifespan 启动/关闭钩子 | 必需依赖阻止启动、可选依赖 fail_silently |
| common.py | 泛型 ApiResponse | 统一响应格式 |

---

*这是整个项目的地基。后续所有模块都建在这个骨架上——配置从 settings 读、日志用 logger、异常抛 AppException 子类、响应用 ApiResponse。*
