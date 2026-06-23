# Step 8：MCP 工具接入 — Agent 的"手"和"眼睛"

---

## 这一步要解决的问题

前面几步我们让 Agent 能规划、能检索知识库。但 Agent 要真正诊断故障，还需要**看到系统的真实状态**：CPU 多少、磁盘满没满、Docker 容器是不是在重启、网络通不通。

这些能力通过 **MCP（Model Context Protocol）** 工具提供。MCP 是 Anthropic 提出的一个开放协议，让 LLM 通过标准接口调用外部工具。我们的 AIOps 平台把各种运维探测能力封装成独立的 MCP Server，主应用通过 MCP Client 远程调用。

---

## 整体架构

```
                          主应用 (FastAPI)
                               │
                    ┌──────────┤──────────┐
                    │          │          │
                MCPClientManager         │
                    │          │          │
          ┌────────┼──────────┼──────────┼────────┐
          │        │          │          │        │
          ▼        ▼          ▼          ▼        ▼
     system    network    docker    websearch  winlog
     :8005     :8009      :8011     :8006      :8008
      │         │          │          │         │
      ▼         ▼          ▼          ▼         ▼
    psutil   ping/dns   docker    DuckDuckGo  Windows
   本机状态  HTTP/端口   容器管理   联网搜索   事件日志
```

每个 MCP Server 是一个独立 Python 进程，通过 `streamable-http` 协议在自己的端口上暴露工具。主应用通过 `langchain_mcp_adapters` 统一调用。

---

## 1. MCP Server 一览

### 五个 MCP Server

| Server | 端口 | 工具 | 能力 |
|---|---|---|---|
| **system** | 8005 | `get_local_system_overview`、`get_local_cpu_memory`、`get_local_disk_usage`、`list_top_processes` | 本机 psutil 指标采集 |
| **network** | 8009 | `ping_host`、`http_check`、`dns_lookup`、`check_port` | 网络连通性探测 |
| **docker** | 8011 | `docker_ps`、`docker_stats`、`docker_logs`、`docker_inspect`、`docker_restart` | Docker 容器管理 |
| **websearch** | 8006 | `web_search` | 受限联网搜索（三层硬约束） |
| **winlog** | 8008 | `query_windows_event` | Windows 蓝屏/崩溃/服务异常日志 |

另外还有内置的本地工具（不走 MCP）：`search_knowledge_base`（RAG）、`get_current_time`、Prometheus 四件套（`prom_query`/`prom_query_range`/`prom_active_alerts`/`prom_label_values`）。

### 面试话术

> "MCP Server 是独立进程，和主应用解耦。好处有三个：第一，Server 挂了只影响对应的工具，不会拖垮主应用；第二，每个 Server 可以独立部署、独立升级；第三，安全边界清晰——Docker Server 只能操作 Docker，系统 Server 只能读 psutil，权限不会交叉。"

---

## 2. MCPClientManager — 连接管理

### 设计要点

`MCPClientManager` 是全局单例，在 `main.py` 的 lifespan 钩子里初始化。核心设计：

**逐 Server 加载 + 单点失败隔离**

这是一个重要的工程决策。`langchain_mcp_adapters` 的 `get_tools()` 内部用 `asyncio.gather(*tasks)` 无 `return_exceptions`——任意一个 Server 失败都会把整批拒绝。

我们改成逐 Server 加载：

```
for name in servers:
    tools = await self._load_one(name, retries=1, retry_delay=0.5)
    if tools is None:
        failed.append(name)   # 这个 server 失败
    else:
        all_tools.extend(tools)  # 其它 server 的工具照样用
```

**带重试的加载**

每个 Server 有一次重试机会，间隔 0.5 秒。为什么要重试？因为启动脚本用 TCP 端口判断 Server 是否 ready，但 uvicorn 先 bind 端口再初始化 FastMCP 路由，偶尔首次 handshake 会撞上 warmup 阶段。一次短暂的 sleep 就能恢复。

**`fail_silently` 设计**

```python
await mcp_client_manager.connect(fail_silently=True)
```

`fail_silently=True`（默认）：MCP 全挂了只打 WARNING，应用继续启动。RAG 知识库、手动诊断这些不依赖 MCP 的功能照常可用。

`fail_silently=False`：生产环境推荐。MCP 是诊断核心依赖，挂了就该阻止启动。

### ExceptionGroup 展开

Python 3.11+ 的 `ExceptionGroup` 默认 `str()` 只显示 "unhandled errors in a TaskGroup (1 sub-exception)"——完全看不到内层异常。我们写了一个 `_format_exc()` 递归展开所有叶子异常，方便排错。

### 面试追问

**Q：MCP 是 stateless 的吗？每次调工具都要建连接？**

> 是的。`langchain_mcp_adapters` 是无状态的——每次工具调用时才建立短连接，调完即释放。`close()` 方法只是置空引用，不需要断长连接。这简化了生命周期管理，代价是每次调用多一次 HTTP 握手（本地 localhost 通常 <1ms）。

**Q：为什么用 `streamable-http` 而不是 `stdio`？**

> `stdio` 要求 Server 和 Client 在同一台机器上，通过 stdin/stdout 通信。`streamable-http` 是 HTTP 协议，Server 可以部署在远端。虽然我们现在 Server 都跑在本机，但用 HTTP 为将来分布式部署留了口子。

---

## 3. ToolMeta 注册中心 — 工具的"身份证"

### 为什么需要 ToolMeta

MCP Server 暴露的工具只有 name 和 description，没有安全语义。但编排层需要知道：

- 这个工具是不是只读的？（决定能不能并行）
- 并发调用安全吗？（决定 gather 分批策略）
- 有没有副作用？（决定权限检查等级）
- 输出最大多少字符？（决定截断策略）

`ToolMeta` 就是每个工具的"身份证"，集中声明这些语义。

### 核心字段

| 字段 | 含义 | 影响 |
|---|---|---|
| `read_only` | 是否只读 | 豁免 Skill 白名单、READ_ONLY 模式放行 |
| `concurrency_safe` | 并发调用安全 | Executor 的 gather 分批 |
| `destructive` | 不可逆操作 | ASK_DESTRUCTIVE 模式需审批 |
| `side_effect` | 副作用类别 | none/external/filesystem/network |
| `risk_level` | 风险等级 | low/medium/high，影响 Guardrails |
| `max_result_chars` | 输出上限 | 编排层截断，防止 20KB 日志直接喂 LLM |
| `search_hint` | 搜索关键词 | Lazy MCP 工具发现用 |

### fail-closed 原则

**未在 `TOOL_META` 登记的工具拿到保守默认**：`read_only=False`、`concurrency_safe=False`。这意味着未登记工具不能并行、不能豁免白名单、在 ASK_DESTRUCTIVE 模式下会被拦截。

> **面试话术**："ToolMeta 的设计原则是 fail-closed——宁可误拦不可误放。新加了一个 MCP 工具但忘了登记 ToolMeta？它会被当作'有写操作风险、不能并行'的工具，最安全的处理方式。开发者看到 WARNING 日志就知道该去补登记了。"

---

## 4. 并行执行：concurrency_safe 分批

这是 Step 7 Executor 提到的并行模式的具体实现。

### 分批策略

Executor 一步里可能要调多个工具（比如同时查 CPU、内存、磁盘）。`tool_runner` 根据 `concurrency_safe` 分批：

```
LLM 决定调用: [get_local_cpu_memory, get_local_disk_usage, web_search]

分批:
  Batch 1 (可并行): get_local_cpu_memory + get_local_disk_usage
    → asyncio.gather(tool1.ainvoke(), tool2.ainvoke())
  Batch 2 (必须串行): web_search
    → await tool3.ainvoke()
```

规则是：连续的 `concurrency_safe=True` 工具合并为一批并行，遇到 `concurrency_safe=False` 的工具就切批串行。

### 面试追问

**Q：为什么 `web_search` 不能并行？**

> `web_search` 的 `concurrency_safe=False` 是因为它调的是外部搜索引擎 / 本地 DuckDuckGo daemon。被 LLM 批量打爆搜索引擎会触发限频或封 IP。而且搜索结果本身有顺序依赖——LLM 通常先搜一个关键词，看了结果再决定搜不搜第二个。

---

## 5. Lazy MCP 工具（两阶段发现/执行）

### 问题

当 MCP 工具数量很大时（比如 30+ 个），全部暴露给 LLM 会导致 tool description 太长，占大量 token，LLM 选择效率也下降。

### 解决方案

Lazy MCP 把所有 MCP 工具替换成两个"元工具"：

1. **`mcp_search_tools`**：LLM 先调这个搜索可用工具。输入关键词（如 "docker"），返回匹配的工具列表（名字 + 描述）
2. **`mcp_execute_tool`**：LLM 找到想用的工具后，通过这个元工具调用。传入 tool_name + arguments_json

这样 LLM 只看到 2 个工具定义，而不是 30 个。需要时再按需发现。

### 安全边界

`mcp_execute_tool` 执行前会检查 tool_name 是否在当前 Skill 的 `allowed_tool_names` 里——Lazy 模式不绕过 Skill 白名单。

### 当前状态

默认关闭（`mcp_lazy_tools_enabled=False`）。当前工具数量不大（~20 个），直接 bind 更简单、少一次 LLM round-trip。工具数量大到 token 预算吃紧时再开。

---

## 6. MCP Server 内部的安全设计

每个 MCP Server 内部都有自己的安全约束，不完全依赖上层。以 network_server 为例：

### 内网 IP 拦截

`http_check` 和 `check_port` 会拒绝探测内网/回环地址：

```python
def _is_blocked_ip(host: str) -> bool:
    # 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, ::1
    ...
```

这样即使 LLM 被 prompt injection 诱导去探测内网资产，Server 层也会拦住。

### WebSearch Server 的三层硬约束（Step 5 已讲）

黑名单关键词 + 脱敏正则 + 限频（60s 内最多 20 次）。

### Docker Server 的写操作

`docker_restart` 的 ToolMeta 是 `read_only=False, destructive=True, risk_level="high"`。在默认的 NORMAL 模式下，Guardrails 会直接拦截它（`guardrails_block_high_risk_tools=True`）。想让 Agent 能重启容器，必须在 `.env` 里显式关闭高危拦截——这是一个**有意设计的摩擦**，防止误操作。

---

## 7. 工具结果截断

LLM 的 context window 有限。如果 `docker_logs` 返回 50KB 的容器日志，直接喂给 LLM 会挤掉其它有用信息。

`ToolMeta` 的 `max_result_chars` 字段定义了每个工具的输出上限。编排层（`tool_runner`）在拿到工具返回后，会按这个阈值截断。不同工具阈值不同——`docker_logs` 给 20000 字符（日志需要看多），`check_port` 只给 1000 字符（结果很短）。

---

## 8. 遇到的难点总结

### 难点 1：启动时偶发"全军覆没"

**现象**：5 个 MCP Server 里有 1 个还没 ready，`get_tools()` 整批失败，0 个工具加载成功。

**根因**：`langchain_mcp_adapters` 的 `get_tools()` 内部 `asyncio.gather` 无 `return_exceptions`，再叠加 anyio TaskGroup，一个异常就全军覆没。

**解决方案**：改为逐 Server 加载 + 单次重试。失败的 Server 只打 WARNING，成功的 Server 照常加载。这样 4/5 Server 成功就有 4 个 Server 的工具可用。

### 难点 2：`streamable-http` vs `streamable_http` 的命名冲突

**现象**：配置文件里写 `streamable-http`（短横线，符合 HTTP 命名惯例），但 `langchain_mcp_adapters` 内部期望 `streamable_http`（下划线，Python 命名惯例）。

**解决方案**：`_build_connections()` 里自动做 `.replace("-", "_")` 转换。用户配置侧保持友好的短横线写法。

### 难点 3：ExceptionGroup 黑盒

**现象**：MCP 加载失败时日志只显示 "unhandled errors in a TaskGroup (1 sub-exception)"，完全看不到真正的错误。

**解决方案**：写了 `_format_exc()` 递归展开 `ExceptionGroup`，把所有叶子异常拼成一行可读字符串。

---

## 快速回顾清单

| 模块 | 核心设计 | 面试关键词 |
|---|---|---|
| MCP Server | 独立进程 + streamable-http | 故障隔离、可独立部署 |
| MCPClientManager | 逐 Server 加载 + 重试 + fail_silently | 单点不拖整批、graceful degradation |
| ToolMeta | 工具安全/性能语义声明 | fail-closed、concurrency_safe 分批 |
| 并行执行 | concurrency_safe 分批 gather | 只读并行、写操作切批串行 |
| Lazy MCP | search → execute 两阶段 | token 预算优化、不绕过白名单 |
| Server 安全 | 内网拦截 / 黑名单 / 限频 / 截断 | 纵深防御、不依赖上层 |

---

*准备好了就说"开始 Step 9"，我们进入 Deep 诊断图——多 Agent 并行取证与证据归并。*
