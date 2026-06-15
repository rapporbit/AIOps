# Layer 6: 工具与 MCP 层

> 目录: `app/tools/`, `mcp_servers/`, `app/runtime/tool_filter.py`, `app/runtime/permissions.py`
>
> 上游: Layer 4 (Executor 发起工具调用) → 下游: MCP Servers (独立进程) / Layer 5 (RAG 检索，包装为 `@tool`)

工具层通过 MCP 协议统一接入系统诊断工具，通过三层过滤控制 LLM 可见的工具集，通过权限三态决定工具是否可执行。

## 6.1 MCP 工具接入

### MCP 服务清单

| 服务 | 端口 | 提供的工具 |
|------|------|-----------|
| `system_server` | 8005 | CPU/内存/磁盘/进程/端口 |
| `network_server` | 8009 | DNS/Ping/HTTP/端口探测 |
| `docker_server` | 8011 | 容器状态/日志/inspect/资源 |
| `websearch_server` | 8006 | 联网搜索 (open-webSearch) |
| `winlog_server` | 8008 | Windows 事件日志 |

### 工具加载优先级

```
1. 本地工具 (search_knowledge_base, get_current_time, ...)
2. MCP 工具 (从各 MCP Server 动态加载)
3. 子 Agent 工具 (delegate_to_evidence_collector, ...)

同名工具: 优先级高的覆盖低的, 防止 LangChain 同名冲突
```

### MCP 客户端连接

```python
# app/core/mcp_client.py
client = MultiServerMCPClient(servers={
    "system": {"url": "http://localhost:8005/mcp", "transport": "streamable_http"},
    "network": {"url": "http://localhost:8009/mcp", "transport": "streamable_http"},
    ...
})
```

**优雅降级**：
- `fail_silently=True`：MCP 服务不可用时 APP 仍然启动，只用本地工具
- 单个 Server 失败不影响其他 Server 的工具加载
- 每个 Server 1 次重试，0.5s 延迟

### 知识库工具

```python
# app/tools/knowledge_tool.py
@tool
async def search_knowledge_base(query: str) -> str:
    """搜索运维知识库 (SOP、On-Call 手册、故障处理流程等)."""
    context, hits, _sources, _hits_meta = await build_context(query)
    if hits <= 0:
        return "知识库中没有找到相关内容..."
    return context  # Markdown 格式的 Parent 上下文
```

RAG 检索能力包装为标准 LangChain `@tool`，对 Agent 来说和其他 MCP 工具没有区别。

## 6.2 三层工具过滤

Agent 不应该看到所有工具。40+ 工具全部暴露给 LLM 会导致：
- token 浪费（每个工具描述占几十 token）
- 工具幻觉（LLM 调用不相关的工具）
- 安全风险（主机诊断不应该看到 Docker 写操作）

### 过滤管道

```
全部工具 (40+)
    │
    ▼ Layer 1: Skill 白名单 (硬墙)
    Skill.allowed_tools 显式列出 + 所有 read_only 工具自动通过
    │
    ▼ Layer 2: 权限评估
    evaluate_permission(tool, skill_allowed, mode) → allow / ask / deny
    │
    ▼ Layer 3: 可见性控制
    allow + ask → 绑定到 LLM (bind_tools)
    deny → LLM 永远不知道它的存在
```

### 只读工具豁免

```python
if tool_meta.read_only:
    skill_allowed.add(tool.name)  # 自动通过 Skill 白名单
```

只读工具（查 CPU、查知识库、ping）跨 Skill 安全，不受白名单限制。

### Lazy MCP 替换

当 MCP 工具较多时，用两个元工具（list + call）替代直接绑定，减少 token 占用。

## 6.3 权限三态系统

```
                    ┌─ allow → 直接执行, LLM 可见
evaluate_permission ┼─ ask   → 需要人工审批, LLM 可见
                    └─ deny  → 拒绝执行, LLM 不可见
```

### 评估层级（短路逻辑）

```
0. Skill 白名单硬墙
   tool ∉ skill_allowed AND NOT read_only → deny (reason: skill_allowlist)

1. BYPASS 模式 → allow (仅开发环境)

2. READ_ONLY 模式 → 非只读工具 deny (reason: mode_read_only)

3. NORMAL 模式:
   高风险工具 (if flagged) → deny (reason: guardrail_high)
   通知类工具 (if not allowed) → deny (reason: guardrail_notify)
   其余 → allow

4. ASK_DESTRUCTIVE 模式:
   高风险/破坏性工具 → ask (reason: mode_ask)
   其余 → allow
```

### Reason Type 审计

每个权限决策都附带 `reason_type`，支持 grep 审计：

```
skill_allowlist   # 不在 Skill 白名单中
guardrail_high    # 高风险工具
mode_read_only    # 只读模式下的写操作
mode_ask          # 需要审批
ok                # 允许执行
```

## 6.4 审批流程

```
Agent 请求调用高风险工具 (如 docker restart)
    │
    ▼ 权限评估 → ask
    │
    ▼ 创建 ApprovalRequest (工具名, 参数, 影响摘要)
    │
    ▼ 推送 tool_pending_approval SSE 事件给前端
    │
    ▼ distributed_slot.pause() → 释放执行槽, 不占着坑等人
    │
    ▼ 等待人工决策 (前端 /approvals/{id}/decide)
    │
    ▼ distributed_slot.resume() → 重新获取执行槽
    │
    ├─ approved → 执行工具
    └─ denied  → 返回拒绝消息给 Agent
```

## 6.5 工具结果截断

```python
# 默认 max_result_chars ≈ 5000
if len(result) > tool_meta.max_result_chars:
    result = result[:max_chars] + f"\n... (截断, 原始 {len(result)} 字符)"
```

Docker 日志、进程列表等输出可能很长，截断后保留关键信息同时避免上下文窗口溢出。

## 6.6 工具元数据

每个工具在注册时携带 `ToolMeta`：

| 字段 | 作用 |
|------|------|
| `read_only` | 是否只读（影响 Skill 白名单豁免和权限评估） |
| `concurrency_safe` | 是否可以并行执行（影响 Executor 分组） |
| `max_result_chars` | 结果截断上限 |
| `risk_level` | 风险等级（影响权限评估） |

## 6.7 MCP 协议的好处

为什么用 MCP 而不是直接写 Python 函数？

1. **进程隔离**：MCP Server 独立进程，崩溃不影响主 API
2. **语言无关**：理论上 MCP Server 可以用任何语言写
3. **标准协议**：LangChain `langchain-mcp-adapters` 原生支持，工具发现自动化
4. **热部署**：新增 MCP Server 只需改配置重启，不改主代码
5. **测试友好**：每个 Server 可以独立测试

## 模拟面试问答

### 🔥 热点拷问

**面试官：你的 MCP 工具，实际使用中调用频率和准确度怎么样？有统计过吗？**

有部分数据但不系统。从 ToolCall 表可以统计：fast 模式一次诊断典型调用 4-6 次工具，其中 `search_knowledge_base` 1-2 次、系统工具（CPU/内存/磁盘/进程）2-3 次、偶尔联网搜索 1 次。工具本身的执行成功率接近 100%（MCP Server 是本地服务，稳定性高）。但 Agent 选择调用哪个工具的"准确度"更难量化——什么算"对的工具"取决于故障场景。应该建一组标准诊断场景的 benchmark，标注期望工具调用序列，然后对比 Agent 的实际调用。这个评测没做，是一个短板。

**追问：MCP 工具能采集到什么层面的信息？能发现隐蔽问题吗？比如进程伪装、隐藏端口？**

当前工具采集的是操作系统层面的标准信息：进程列表（名称、PID、CPU/内存）、网络连接、磁盘使用、Docker 容器状态。这些是"表面信息"——能发现 CPU 100%、磁盘满、容器 OOM 这类显性问题，但发现不了进程伪装（需要路径 + 签名验证）、隐藏端口（需要 rootkit 检测工具）、内存注入（需要进程内存扫描）。这不是 MCP 协议的限制，而是当前 MCP Server 的能力边界。可以扩展——比如写一个 security_server 集成 ClamAV 或 YARA 规则，通过 MCP 暴露出来，Agent 就能做安全层面的检测。

---

**面试官：MCP 协议听起来很好，但每个工具调用都要走一次 HTTP 请求到独立进程，延迟开销值得吗？**

单次 MCP 调用的网络开销在 5-50ms（本地 localhost），对比 LLM 调用的 2-10 秒可以忽略不计。整个诊断链路中 MCP 工具调用总耗时 1-3 秒，LLM 推理总耗时 20-40 秒，MCP 的网络开销占总时间 5% 以下。进程隔离带来的稳定性收益（MCP Server 崩溃不影响主 API）远大于这点延迟代价。

**追问：那如果工具需要的参数 Agent 没给对，或者工具返回了错误结果，Agent 怎么处理？**

两种情况。参数错误通常被 MCP Server 的输入校验拦截，返回错误消息给 Agent，Agent 在下一步可以修正参数重试。工具返回错误结果（比如查 DNS 返回超时）不是"工具错了"而是"取到了一条证据"——DNS 超时本身就是诊断信息。Replanner 的逻辑是：工具调用失败不等于诊断失败，失败的工具调用也是证据。deep 模式中单个 Agent 的工具失败会变成带 `error_type` 的 Evidence，报告会标注"以下信息未能采集"。

### 深度追问链

**面试官：（接工具输出问题）MCP 工具输出是文本，Agent 怎么理解结构化数据？有没有出现过误解工具输出的情况？**

MCP 工具返回格式化文本（通常是 JSON 或 key-value），LLM 对这类结构化文本的理解能力还可以——比如看到 `cpu_percent: 95.2` 能正确判断 CPU 高。但有误解风险：比如 `memory_available: 512MB`，LLM 可能不知道总内存多少，无法判断是否异常。缓解措施：工具输出尽量包含上下文（`512MB / 8GB total (6.4%)`）；Skill Playbook 提示 Agent 如何解读数据。系统性测试 Agent 对工具输出的理解准确率，目前没做。

**继续追问：你说可以扩展 security_server，但安全检测和运维诊断的知识体系完全不同，复用性有多高？**

复用的是架构而不是内容：MCP 协议、工具过滤、权限三态、Skill Playbook 框架、Plan-Execute-Replan 图对安全诊断同样适用。需要新建的是安全领域的 Skill、安全工具的 MCP Server（集成 ClamAV/YARA/进程签名验证）、安全知识库。但诊断图、任务队列、执行槽、审计链路都可以复用。这恰恰是分层架构的价值——换一套 Skill + 工具 + 知识库就能适应不同领域。当然安全诊断的实时性要求更高（恶意进程需要立即响应），可能需要调整队列优先级。

### 常规问题

**面试官：三层工具过滤和权限三态，会不会让系统变得太复杂？**

复杂度是有意的。不过滤的代价更大——40+ 工具全暴露导致 token 浪费、工具幻觉和安全风险。三层过滤是逐级收敛：Skill 白名单是硬墙（Layer 1），权限评估是策略层（Layer 2），可见性控制是最终执行层（Layer 3）。每一层的逻辑都很简单（集合过滤、模式匹配、bool 判断），组合起来实现了"Agent 只看到该看的工具，只执行被允许的操作"。

### 反思与改进

**面试官：MCP 协议的选择你满意吗？**

总体满意。进程隔离、标准协议和热部署在开发中确实体现了价值。不满意的是冷启动——主 APP 启动时 MCP Server 必须已就绪，否则缺失工具。应该做热加载和健康检查，让 MCP Server 可以运行时加入/退出。

**面试官：工具层最大的教训？**

工具描述对 LLM 行为影响极大。早期有个工具描述写成"获取系统信息"，LLM 几乎每步都想调它。改成"获取本机 CPU 使用率和内存使用率的快照"后调用频率立刻正常。教训是 `@tool` 的 docstring 不是给人看的，是给 LLM 看的——要像写 API 文档一样精确。
