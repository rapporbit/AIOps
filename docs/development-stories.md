# 开发优化故事

这些故事记录了项目开发过程中遇到的真实问题、分析过程和解决方案。每个故事都包含**量化指标**，适合面试时用 STAR 法则展开讲述。

---

## Story 1: RAG 检索从 Hit@3=0.94 到 1.0 的混合召回优化

### 问题

纯向量检索在运维场景下有盲区。50 题检索评测中，3 道题始终 miss，Hit@3 停在 0.94。分析 miss 的 case：

```
查询: "Redis 客户端连接被 ERR_CONN_REFUSED 拒绝"
期望命中: redis-connection-refused.md
实际结果: 语义相近但不相关的 redis-memory.md
```

**根因**：错误码 `ERR_CONN_REFUSED`、组件名 `redis-master-01`、状态码 `98%` 这类 token 在语义嵌入空间中没有有效表征。embedding 模型把它们当作普通英文单词处理，丢失了"精确匹配"的信息。

### 方案

引入 BM25 关键词检索作为补充，用 RRF (Reciprocal Rank Fusion) 融合两路结果。

**为什么不用加权分数融合？** BM25 分数无界（一篇文档可能得 5 分，另一篇得 50 分），向量余弦相似度在 [-1, 1]，直接加权会被 BM25 主导。RRF 只用排名，量纲无关：

```
score(d) = w_v / (60 + rank_v) + w_b / (60 + rank_b)
```

**分词设计**：运维查询混合中英文和数字。没有引入 jieba（增加依赖），而是用字级切分中文 + 空格分词英文：

```python
"Redis 内存 98% OOM" → ["redis", "内", "存", "98", "oom"]
```

简单但对运维短查询效果足够。

### 权重调优

跑了 0.0~0.5 六个 BM25 权重的对比实验：

| BM25 Weight | Hit@3 | MRR@3 |
|:-----------:|:-----:|:-----:|
| 0.0 | 0.94 | 0.89 |
| 0.1~0.3 | 0.94 | 0.89 |
| **0.4** | **1.00** | **0.93** |
| 0.5 | 1.00 | 0.93 |

0.4 是收益拐点：之前纯向量 miss 的 3 道题全部被 BM25 路补回，向量路的优势没有被稀释。

### 结果

- Hit@3: 0.94 → **1.00** (+6.4%)
- MRR@3: 0.89 → **0.93** (+4.5%)
- 端到端 Faithfulness: **0.913**, Answer Relevancy: **0.936**

### 面试要点

- 讲清 **"为什么纯向量不够"**（错误码/组件名的语义缺失）
- 讲清 **"为什么 RRF 而不是加权"**（量纲问题）
- 用数据说话，不是 "加了 BM25 就好了"

---

## Story 2: Parent-Child Chunking 解决上下文碎片化

### 问题

RAG 经典矛盾：chunk 太小（300 字），检索精准但上下文断裂——LLM 看到一句 "执行 `redis-cli info memory`"，但不知道它属于哪个 SOP 的哪一步；chunk 太大（2000 字），上下文完整但嵌入噪音大，不相关内容拉低检索精度。

### 方案

两级分块 + 链接：

```
Child (300字): 写入 Milvus, 用于向量检索 ← 小而精准
Parent (≤2400字): 存在 Child 的 metadata 中 ← 大而完整

检索时: 搜 Child → 按 parent_id 去重 → 返回 Parent 给 LLM
```

Child 负责"找得准"，Parent 负责"看得全"。

**标题路径增强**：Child 的 `page_content` 前缀注入标题路径 `[Redis / 内存管理 / OOM 处理]`，给 embedding 模型提供结构化语义锚点。

**结构保护分块**：运维文档中大量代码块和 Markdown 表格，普通分块器会从中间切断。用 6 种正则（代码块、表格、图片链接、超链接、块级 LaTeX、行内 LaTeX）做占位符保护 → 分块 → 还原。

### 结果

- Context Precision: **0.997** — 几乎每一段返回的上下文都包含相关信息
- Context Recall: **0.871** — 大部分相关信息都被检索到
- 标题路径增强后 R@1 提升约 **10%**

### 面试要点

- **不是"大小二选一"**，而是"两级各取所长"
- **结构保护**是实际工程细节，说明你真正处理过非干净文本

---

## Story 3: 从 asyncio.Semaphore 到 Redis ZSET 分布式并发控制

### 问题

早期用 `asyncio.Semaphore(2)` 限制同时诊断数。Uvicorn `--workers 4` 启动 4 个进程后，每个进程各有一个 Semaphore，实际并发变成 8 个诊断同时跑。LLM API 配额被打满，Milvus 和 MCP 工具延迟飙升。

### 方案：Redis ZSET + Lua

用 Redis 有序集合实现跨进程全局执行槽：

```
ZSET Key: aiops:limiter:worker_diagnosis
Member: {hostname}:{pid}:{uuid}
Score: 过期时间戳 (now_ms + ttl_ms)
```

**Lua 原子获取**：

```lua
-- 1. 清理过期 token (Score < 当前时间)
ZREMRANGEBYSCORE key '-inf' now_ms
-- 2. 计数当前持有者
local count = ZCARD key
-- 3. 如果有空位, 原子占位
if count < limit then ZADD key (now+ttl) token; return 1 end
return 0
```

**心跳续约**：长任务每 30s 刷新 Score，防止被清理。

**Pause/Resume**：人工审批场景下释放槽位，审批完成后重新获取，避免审批等待期间（可能几分钟）一直占着并发名额。

**Fail-Open**：Redis 不可用时返回特殊 token，允许诊断继续。限流组件故障不应导致所有诊断停摆。

### 结果

压测验证：3 Worker 同时存活，`running=3`（都在执行），但真实执行槽始终 `2/2`，第 3 个 Worker 排队等待。

```
提交 8 个任务 → 3 Worker 领取 → 最多 2 个同时执行 → 全部成功
队列最终: depth=0 pending=0 dlq=0
```

### 面试要点

- 讲清**为什么 Semaphore 不够**（多进程多 Worker 场景）
- Lua 脚本保证原子性的必要性（不是"锁住然后计数"，而是"一步完成"）
- **Fail-Open 设计哲学**是加分点

---

## Story 4: Redis Streams 优先级队列的两阶段消费

### 问题

Redis Streams 的 `XREADGROUP` 不原生支持跨流优先级。如果简单地用 `BLOCK` 监听多条流，Redis 返回"任一有数据"，不保证返回最高优先级的。

### 方案：两阶段消费

```
Phase 1: 非阻塞优先级扫描 (严格抢占)
    逐级扫描 critical → high → normal → low
    任一级有数据 → 立即返回, 不看后续
    全空 → 进入 Phase 2

Phase 2: 阻塞等待 (零 CPU 空转)
    XREADGROUP BLOCK 5000ms 同时监听所有流
    有数据 → 回到 Phase 1 重新按优先级取
    超时 → 回到 Phase 1 检查 stale 任务
```

**为什么不只用 Phase 1？** 全空时 busy-wait 浪费 CPU。**为什么不只用 Phase 2？** BLOCK 模式不保证优先级。两阶段组合：有任务时严格按优先级取，没任务时零 CPU 等待。

### 结果

- critical 告警在 normal 任务堆积时仍然优先处理
- 空闲时 Worker CPU 接近 0%（BLOCK 由 Redis 内核挂起）
- Phase 2 唤醒后立即 Phase 1 扫描，优先级保证不被打破

### 面试要点

- 这是**系统设计题的经典模式**：严格优先级 + 无锁 + 零空转
- 说明你理解 Redis Streams XREADGROUP 的语义限制

---

## Story 5: Skill-first 路由收敛 LLM 工具调用

### 问题

早期 Agent 一次能看到 40+ 工具。LLM 经常"东查一下西查一下"，比如主机 CPU 高的问题去调 Docker 工具、DNS 工具，浪费 token 和时间。更严重的是产生**工具幻觉**——调用不存在的工具组合。

### 方案

Skill-first 路由 + 工具白名单：

```
1. Skill Router 先分类 (host_resource / network / container / generic_oncall)
2. 加载对应 SKILL.md Playbook (排障剧本)
3. 只暴露该 Skill 白名单内的工具给 LLM
4. 只读工具自动豁免 (跨 Skill 安全)
```

```yaml
# host_resource_diagnosis 的 SKILL.md
allowed_tools:
  - get_local_cpu_memory
  - get_local_disk_usage
  - list_top_processes
  - search_knowledge_base    # RAG 工具
  # 看不到: docker_inspect, dns_lookup, http_check, ...
```

LLM 从 40+ 工具收敛到 5-8 个相关工具，Playbook 给出排障方向，减少无目的探索。

### 三级路由保证

1. **规则预检**：明显非 OnCall 输入（天气、电影）在关键词层直接拦截
2. **LLM + Wiki 经验**：相似历史经验注入 Router Prompt，帮助分类
3. **降级兜底**：LLM 异常 → `generic_oncall`，不让分类失败阻断诊断

### 结果

- 单次诊断 tool_call 数从平均 8-12 次降到 4-6 次
- token 消耗减少约 30%（工具描述占 token 大头）
- 工具幻觉基本消失

### 面试要点

- **不是限制 Agent 能力，而是约束搜索空间**
- Playbook 机制的关键洞察：LLM 在框架内做特化比从零规划更稳定

---

## Story 6: Replanner 重路由与 Agent 失败记忆

### 问题

Skill Router 不是 100% 准确。比如"Docker 容器里的进程 CPU 100%"可能被路由到 `container_diagnosis`，但实际根因是宿主机资源不足，需要切到 `host_resource_diagnosis`。如果路由错误不可纠正，就要从头再来。

### 方案：Replanner 重路由 + 失败记忆

在 Replanner 的三路决策中加入第三条路径——**Skill 切换**：

```python
class Act(BaseModel):
    is_finished: bool       # → 生成报告
    plan: list[str]         # → 继续执行
    should_reroute: bool    # → 切换 Skill ← 新增
    new_skill: str
```

**代码层校验（不信任 LLM）**：

```
✓ 已执行步骤 ≥ 2 (需要足够证据支持切换决策)
✓ 重路由次数 < 上限 (防止无限切换)
✓ 新 Skill ≠ 当前 Skill (不能自环)
✓ 新 Skill ∉ tried_skills (不能回退已失败的)
✓ 新 Skill 存在于注册表
```

`tried_skills` 作为**失败记忆**，记录每次切换的原因。如果 A→B 发现 B 也不对，不能回到 A（因为 A 已经在 tried_skills 里了），只能尝试 C 或强制用当前 Skill 收敛。

### 状态转移

```
Executor → Replanner → should_reroute=True
    → 校验通过:
        state["selected_skill"] = new_skill
        state["tried_skills"].append(old_skill)
        state["pending_reroute"] = True → 路由回 Planner
    → 校验不通过:
        记录 REPLANNER_REROUTE_BLOCKED
        继续当前 Skill
```

### 面试要点

- 类比 **Supervisor + Handoff 模式**
- 关键是"代码层校验"——LLM 提出建议，代码做门控
- `tried_skills` 防止 A→B→A 死循环是实际工程陷阱

---

## Story 7: Fail-Open 设计哲学

### 问题

系统中多个组件依赖 Redis（限流器、分布式执行槽、队列状态查询）。Redis 临时不可用时（网络抖动、重启），这些组件如果直接抛异常，整个系统的所有诊断请求都会失败。

### 方案：一致性 Fail-Open

在每个 Redis 依赖点实施相同的策略：

| 组件 | Fail-Open 行为 |
|------|----------------|
| 限流器 | Redis 不可用 → 放行请求, 记录 warning |
| 分布式执行槽 | Redis 不可用 → 返回特殊 token `"__fail_open__"`, 允许执行 |
| 队列状态 | Redis 不可用 → 返回 partial data, 不影响其他接口 |
| 健康检查 | Redis 不可用 → readiness probe 返回 503, 但 liveness 不受影响 |

### 设计原则

```
临时过量放行 (可能同时跑 3 个诊断而不是限制的 2 个)
    vs.
完全拒绝服务 (所有诊断请求都返回 500)
```

对于 AIOps 场景，前者的代价（多花点 LLM token）远小于后者（故障诊断完全不可用）。

### Readiness vs. Liveness 区分

Redis 不可用时 liveness 仍然 200（进程没挂），readiness 返回 503（依赖不健康）。K8s 只会停止发送新请求，不会杀掉进程，给 Redis 恢复的时间。

### 面试要点

- 说明你理解 **"限流不应该成为新的单点故障"**
- Fail-Open vs. Fail-Closed 的权衡是系统设计面试常见话题
- K8s 探针设计的分层思考

---

## Story 8: 结构保护分块——保证代码块和表格完整性

### 问题

运维知识库中大量 Markdown 文档包含代码块、表格、LaTeX 公式。`RecursiveCharacterTextSplitter` 按字符长度切分，可能从代码块中间切断：

```markdown
## 修复步骤
\```bash
redis-cli info memory          ← chunk 1 结尾
used_memory_peak: 1073741824   ← chunk 2 开头 (孤立的数字，缺少上下文)
\```
```

切断的 chunk 对 embedding 和 LLM 都是噪音。

### 方案

分块前用正则识别保护区域，替换为占位符，分块后还原：

```python
PROTECTED = [
    (r'```[\s\S]*?```',         "CODE_BLOCK"),
    (r'\|[^\n]+\|[\n][\|:\-]+', "TABLE"),
    (r'!\[[^\]]*\]\([^\)]+\)',  "IMAGE"),
    (r'\[[^\]]*\]\([^\)]+\)',   "HYPERLINK"),
    (r'\$\$[\s\S]*?\$\$',      "BLOCK_LATEX"),
    (r'\$[^\$]+\$',            "INLINE_LATEX"),
]

流程: 原文 → 占位符替换 → RecursiveCharacterTextSplitter → 占位符还原
```

代码块整体被当作一个"大 token"，不会被分块器从中间切开。

### 面试要点

- 小优化但体现**对数据质量的关注**
- 说明你的 RAG 不是"调个 API 就完事"，而是处理过真实脏数据

---

## 面试场景速查

| 面试问题 | 推荐故事 | 关键数据 |
|---------|---------|---------|
| "RAG 优化做过什么？" | Story 1 + 2 | Hit@3: 0.94→1.0, Faithfulness: 0.913 |
| "分布式并发怎么做的？" | Story 3 | Redis ZSET + Lua, 3 Worker 共享 2 槽 |
| "消息队列用过什么？" | Story 4 | Redis Streams 四级优先级, 两阶段消费 |
| "Agent 怎么避免幻觉？" | Story 5 | 40+ 工具收敛到 5-8 个, token 降 30% |
| "Agent 出错怎么办？" | Story 6 | Replanner 重路由 + tried_skills 失败记忆 |
| "怎么处理依赖故障？" | Story 7 | Fail-Open 一致性策略 |
| "文档处理踩过什么坑？" | Story 8 | 6 种正则保护代码块/表格/公式 |
