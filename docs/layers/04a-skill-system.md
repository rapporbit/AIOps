# Layer 4a: Skill 系统

> 目录: `app/agents/skill_router.py`, `app/agents/planner.py`, `app/skills/`
>
> 上游: Layer 4 诊断图的第一个节点调用 Skill Router → 下游: 选中 Skill 后进入 Planner，再交回 Layer 4 的 Executor

Skill 系统决定"这个故障应该按什么剧本排查"。它包括三级路由选择、基于 Playbook 的种子规划、以及 Skill 注册表的自动发现。

## Skill Router：三级路由

### Level 1: 规则预检

```python
# app/agents/skill_router.py
_OUT_OF_SCOPE_KEYWORDS = ["天气", "电影", "动漫", ...]
_ONCALL_KEYWORDS = ["告警", "报错", "超时", "宕机", ...]

def _looks_like_oncall_input(query):
    if any(kw in query for kw in _OUT_OF_SCOPE_KEYWORDS):
        return False  # 明显不是 OnCall
    if any(kw in query for kw in _ONCALL_KEYWORDS):
        return True   # 明显是 OnCall
    return None       # 需要 LLM 判断
```

明确非 OnCall 的输入（问天气、聊电影）在规则层直接拦截，不消耗 LLM 调用。

### Level 2: LLM 分类 + Wiki 经验注入

```python
# 从 LLM Wiki 召回相关经验
recall_block = await wiki_store.recall_block(query, service=service)

# 构建 Router 提示，注入历史经验
messages = harness.build_skill_router_messages(query, skill_menu, recall_block)

# LLM 结构化输出
choice: SkillChoice = await llm.with_structured_output(SkillChoice).ainvoke(messages)
# SkillChoice: {skill_name: str, confidence: float, reason: str}
```

### Level 3: 降级兜底

- LLM 返回不存在的 Skill 名 → 降级到 `generic_oncall`
- LLM 调用失败 → 规则判断，OnCall 类走 `generic_oncall`，非 OnCall 类直接拒绝

### 路由转移记录

每次路由决策都记录 `StateTransition`：

```python
{"node": "skill_router", "reason": "ROUTER_OK",
 "detail": "host_resource_diagnosis (confidence=0.92)",
 "ts": "2024-01-15T10:30:00.123"}
```

| 事件 | 含义 |
|------|------|
| `ROUTER_OK` | 正常选中 |
| `ROUTER_OUT_OF_SCOPE` | 非 OnCall，直接拒绝 |
| `ROUTER_LLM_FAILED` | LLM 异常，走规则降级 |
| `ROUTER_FALLBACK_GENERIC` | LLM 返回不存在的 Skill |
| `ROUTER_RECALL_APPLIED` | Wiki 经验已注入 (附注入页数) |

## Planner：Skill Playbook 种子规划

Planner 不是从零生成计划，而是基于 Skill Playbook 做"种子 + 特化"：

```
Skill Playbook (Markdown):
    host_resource_diagnosis:
    - 场景1: CPU 持续高 → 查 top 进程, 查知识库
    - 场景2: 内存告警 → 查内存分布, 查 OOM 日志
    - 场景3: 磁盘满 → 查磁盘使用, 查大文件

LLM Planner 输入:
    "你是运维诊断助手, 以下是排障剧本: {playbook}
     当前故障: {query}
     请生成 2-3 步诊断计划"

输出: ["查询系统 CPU 和进程信息", "搜索知识库相关 SOP", "汇总诊断结论"]
```

Playbook 约束了计划的方向和工具范围，LLM 只在框架内做特化，减少不着边际的诊断计划。

## Skill 注册表

### Skill 定义 (SKILL.md)

```yaml
---
name: host_resource_diagnosis
display_name: 主机资源诊断
description: CPU、内存、磁盘、OOM、本机卡顿
triggers: [cpu, 内存, 磁盘, oom, 卡顿]
allowed_tools: [get_local_cpu_memory, get_local_disk_usage, ...]
risk_level: low
platforms: [macos, linux]
---

## 排障流程
### 场景1: CPU 持续高
1. 查看 top 进程
2. 搜索知识库 SOP
...
```

### 内置 Skill 清单

| Skill | 适用场景 | 工具边界 |
|-------|---------|---------|
| `host_resource_diagnosis` | CPU、内存、磁盘、OOM、本机卡顿 | 本机系统快照、CPU/内存、磁盘、Top 进程、知识库 |
| `network_diagnosis` | 网站打不开、接口超时、DNS 异常、端口不通 | DNS、HTTP、端口、Ping、知识库和有限联网搜索 |
| `container_diagnosis` | Docker 容器挂掉、重启循环、资源占用高 | Docker 状态、资源、日志、inspect；写操作需审批 |
| `generic_oncall` | 现象不明确或多组件复合故障 | 通用只读工具集合，兜底排障剧本 |

### 注册表特性

- **自动发现**：扫描 `app/skills/definitions/*/SKILL.md`
- **平台过滤**：`platforms: [macos, linux]` → Windows 上自动跳过
- **外部扩展**：`skills_external_dirs` 配置可加载外部 Skill 目录
- **兜底保证**：`generic_oncall` 必须存在，否则启动报错

## 模拟面试问答

### 🔥 热点拷问

**面试官：你的知识库里到底存了啥？这个项目看着不需要什么特别的知识吧。**

知识库里存了三类内容。一是 Prometheus 告警规则语料——从 awesome-prometheus-alerts 项目转换来的几百条告警的触发条件、含义和处理建议。二是 Redis/MySQL/通用 OnCall 的 SOP 文档——标准化的排障流程。三是公开的运维知识语料。这些不是"特别的知识"，但对 Agent 来说很必要——没有 SOP 的 Agent 只能靠 LLM 训练数据猜，有 SOP 的 Agent 可以按标准流程取证。RAG 的价值不是提供"LLM 不知道的知识"，而是提供"这个组织的标准操作流程"。

**追问：那 RAG 在诊断过程中实际被调用了多少次？占诊断总时间的比例？**

典型 fast 诊断中，`search_knowledge_base` 通常被调用 1-2 次——Planner 规划步骤里有"搜索知识库相关 SOP"。每次调用耗时 1-3 秒（向量检索 + BM25 + rerank），占诊断总时间（30-60 秒）的 5-10%。不是每次诊断都会调 RAG——如果 Planner 判断工具取证已经足够（比如 CPU 问题直接查 top 进程就能定位），可能跳过知识库搜索。deep 模式的 RunbookAgent 几乎必调 RAG，因为它的职责就是匹配标准 SOP。

---

**面试官：你的 Skill 是写死 4 个的，有没有想过渐进式披露——系统自动发现和生成 Skill？**

当前确实是静态注册的 4 个 Skill，先把已知高频场景做稳。渐进式披露是有价值的方向：比如 generic_oncall 作为兜底处理了一批告警后，分析这些历史诊断的工具调用模式和 Evidence 分布，发现"有一类故障总是用 dns_lookup + http_check + 知识库搜 SOP"，然后提示运维团队"是否要创建一个 DNS/HTTP 故障的专门 Skill"。需要两个支撑：一是诊断历史的结构化分析（ToolCall 表已经有数据），二是 Skill 模板生成（Playbook 可以从历史步骤中提炼）。数据基础有了但功能没做。

**追问：Skill Router 的分类准确率多少？有测过吗？**

没有系统性的分类准确率评测，该补。定性观察：规则预检层对明显场景基本 100% 准确（关键词硬匹配）；LLM 分类层在语义明确场景下也比较稳。不确定的场景（"服务响应慢"可能是网络也可能是资源）是分类错误高发区，但 Replanner 重路由可以纠正。应该建一组 100+ 的分类评测集，覆盖明确场景、模糊场景和边界 case，量化 precision/recall。

### 深度追问链

**面试官：（接知识库内容问题）你的 SOP 是公开的 awesome-prometheus-alerts，不是真正的企业 SOP。怎么证明对真实企业也有效？**

公开语料确实不等于企业 SOP。但 RAG 管道本身是通用的——Parent-Child 分块、混合检索、rerank 不依赖内容类型。换成企业 SOP 只是替换语料然后重新 ingest。真正要证明的是管道对不同类型文档的适应性——企业 SOP 可能有更多表格、流程图、内部术语。如果有机会接入真实 SOP，应该跑一轮检索评测对比公开语料的指标。

**继续追问：Skill Router 分类准确率 80%，重路由成功率 50%，端到端正确率是多少？你算过吗？**

没有精确算过，但可以推算。80% 第一次分类正确，剩下 20% 需要重路由。假设 Replanner 有 70% 概率发现错误并触发重路由，重路由后选对概率 50%。端到端 ≈ 80% + 20% x 70% x 50% = 87%。但这些数字都是估算，没有实测。要真正量化需要标注过的分类评测集 + 端到端跑一遍。如果低于 85%，Skill-first 的设计就需要反思。

**再追问：SOP 本身写得不好或者过时了呢？知识库质量谁来保证？**

当前没有知识库质量保证机制。RAG 的原则是"garbage in, garbage out"——如果 SOP 写着"Redis 内存高先重启"，Agent 就会建议重启。改进方向：一是上传时做基本质量检查。二是给文档加版本和过期时间，过期文档降权。三是引入使用反馈——某个 SOP 被多次检索但诊断结果都是 failed，标记为待审核。这些是生产化的必要能力。

### 常规问题

**面试官：Skill-first 路由和直接暴露所有工具给 LLM，区别是什么？有量化数据吗？**

40+ 工具全暴露会导致 token 浪费、工具幻觉（LLM 在不相关工具间漫游）和安全风险。Skill-first 先分类故障类型，再只暴露 5-8 个相关工具，Playbook 给排障方向。实测 tool_call 从平均 8-12 次降到 4-6 次，token 消耗减少约 30%。详见 [development-stories.md](../development-stories.md) Story 5。

### 反思与改进

**面试官：4 个 Skill 够吗？扩展的阻力在哪？**

当前场景够用。扩展的阻力不在代码——新增 Skill 只需写一个 SKILL.md 放到 `app/skills/definitions/`，注册表自动发现。真正的阻力在内容：每个 Skill 需要完整的 Playbook、工具白名单和知识库语料，写一个高质量 Skill 需要运维专家的深度参与。

**面试官：Skill 设计最成功和最后悔的决策？**

最成功："只读工具自动豁免 Skill 白名单"——一条规则让所有 Skill 都能用 `search_knowledge_base`，不需要重复列出。最后悔：没有早点做 Skill Router 分类评测——很多设计决策（关键词列表、Prompt 措辞）都凭直觉调，没有数据支撑。如果一开始就建了评测集，迭代效率会高很多。
