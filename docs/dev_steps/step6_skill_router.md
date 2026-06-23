# Step 6：Skill Registry & Router — 故障剧本、分类路由、工具白名单

---

## 这一步要解决的问题

Agent 拿到一个告警："Redis 连接超时"。如果直接把告警文本丢给 LLM 让它自己决定用什么工具排查，会出现两个问题：

1. **LLM 在大量工具之间漫游**：系统里可能有几十个工具（磁盘检查、网络探测、Docker 状态、知识库搜索、联网搜索……），LLM 可能随机挑几个试试，效率低，甚至调到不相关的工具
2. **没有领域经验指导**：LLM 不知道"Redis 连接超时应该先查什么、再查什么"，它只能靠通用推理能力摸索

Skill 层就是为了解决这两个问题：**给 Agent 一个"故障排查剧本"，告诉它该用什么思路排查，允许调哪些工具。**

---

## 核心概念辨析

这是面试时最先要讲清楚的：

| 概念 | 职责 | 例子 |
|---|---|---|
| **Tool** | 单个具体能力 | `get_local_cpu_memory`（查本机 CPU 内存） |
| **RAG** | 知识检索，Tool 的一种 | `search_knowledge_base`（查知识库） |
| **Skill** | 面向某类故障的排查方法论 | `host_resource_diagnosis`（CPU/内存/磁盘类排查） |
| **LangGraph** | 流程编排器 | Router → Planner → Executor → Replanner |

> **面试话术**："Skill 不是 Tool，不是 Prompt，也不是 Workflow。它是'针对某类故障，用什么思路、调什么工具、按什么顺序、输出什么格式'的剧本。可以理解为运维专家把他的排障经验结构化地写成了一份 SKILL.md。"

---

## 1. Skill 定义（SKILL.md）

### 文件格式

每个 Skill 是一个目录，核心文件是 `SKILL.md`——YAML frontmatter + Markdown body：

```
app/skills/definitions/
├── host_resource_diagnosis/SKILL.md
├── network_diagnosis/SKILL.md
├── container_diagnosis/SKILL.md
└── generic_oncall/SKILL.md          ← 兜底剧本，必须存在
```

frontmatter 定义元信息：

```yaml
name: host_resource_diagnosis
display_name: 主机资源类故障排查
description: 主机/容器 CPU 持续高负载、内存 OOM、磁盘空间满...
category: oncall
platforms: [windows, linux]
tags: [cpu, host, memory, disk]
triggers:
  - cpu 高
  - 内存占用
  - 磁盘满
allowed_tools:             # 工具白名单（关键）
  - search_knowledge_base
  - get_local_cpu_memory
  - get_local_disk_usage
  - list_top_processes
risk_level: low            # low / medium / high
```

Markdown body 是排查 Playbook，给 Planner 看的。Planner 拿到这个 Playbook 后，按里面的步骤拆任务。

### 当前内置的 4 个 Skill

| Skill | 适用场景 | 工具边界 |
|---|---|---|
| `host_resource_diagnosis` | CPU 高、内存 OOM、磁盘满、本机卡顿 | 本机系统工具 + 知识库 |
| `network_diagnosis` | 网站打不开、接口超时、DNS 异常、端口不通 | DNS/HTTP/端口/Ping + 知识库 + 受限联网 |
| `container_diagnosis` | Docker 容器挂掉、重启循环、启动失败 | Docker 状态/日志/inspect |
| `generic_oncall` | 现象不明确或多组件复合故障 | 通用只读工具集合，兜底 |

### 面试追问：为什么不直接写在 Prompt 里？

> "写在 Prompt 里有三个问题：一是改了排查方法就要改代码重新部署；二是不同故障类型的 Prompt 混在一起，维护困难；三是无法做版本管理和 A/B 对比。SKILL.md 是独立文件，可以热加载（`POST /api/v1/skills/reload`），可以 Git 管理，可以外部目录挂载。运维专家不用碰代码就能优化排障方法。"

---

## 2. SkillRegistry（注册中心）

### 加载机制

启动时扫描两类目录：

1. 内置目录：`app/skills/definitions/`
2. 外部目录（可选）：通过 `SKILLS_EXTERNAL_DIRS` 配置，支持 Hermes 风格的 Skill 包

扫描逻辑：遍历每个目录下的 `*/SKILL.md` → 解析 frontmatter → 检查平台兼容性（Windows/Linux）→ 检查是否被禁用 → 注册到内存字典。

用 `lru_cache(maxsize=1)` 做进程级单例，启动时加载一次，后续从内存取。

### 关键设计：兜底 Skill 强制存在

`generic_oncall` 是兜底剧本。如果它缺失，`get_or_generic()` 会抛 `RuntimeError`。这是一个**强约束**——系统必须有一个兜底方案，不能出现"Router 选不出 Skill → Planner 没有 Playbook → Agent 瞎跑"的情况。

### Router 菜单生成

`to_router_menu()` 把所有 Skill 的 `name + description + triggers` 拼成一段 Markdown 文本，作为 Router LLM 的输入。LLM 看到这个"菜单"后选择最匹配的 Skill。

---

## 3. Skill Router（故障分类）

### 整体流程

```
用户输入 "Redis 连接超时"
    │
    ▼
① 预判：是不是 OnCall 输入？
   ├── 命中 OUT_OF_SCOPE 关键词（动漫、游戏、天气...）→ 直接拒绝
   └── 通过
    │
    ▼
② LLM Wiki 召回：从经验库读取相关历史诊断经验，注入 Router prompt
    │
    ▼
③ LLM 路由：把 Skill 菜单 + 用户输入发给 LLM
   LLM 返回 SkillChoice { is_oncall, skill_name, confidence, reason }
    │
    ▼
④ 校验 + 兜底：
   ├── is_oncall=false → 返回"非运维问题"提示，直接结束
   ├── skill_name 不存在 → 回退到 generic_oncall
   └── LLM 调用失败 → 规则兜底（关键词匹配判断是否 OnCall → generic_oncall）
```

### LLM 路由的输入输出

输入是一组 messages，由 agent_harness 构建：system prompt + Skill 菜单（每个 Skill 一张卡片：name、description、triggers）+ 用户输入。

输出是一个 Pydantic 对象 `SkillChoice`：

```python
class SkillChoice(BaseModel):
    is_oncall: bool          # 是否属于运维范畴
    skill_name: str          # 选中的 Skill
    confidence: float        # 置信度 0-1
    reason: str              # 选择理由（可观测）
```

通过 `ainvoke_structured()` 调用（Step 2 讲过的 JSON 模式 + Pydantic 校验）。

### 多层兜底（面试重点）

这个兜底设计体现了工程稳健性思维：

| 场景 | 兜底行为 |
|---|---|
| LLM 返回了不存在的 Skill 名 | 回退到 `generic_oncall`，日志 WARNING |
| LLM 调用超时/报错 | 用关键词规则判断是否 OnCall：是 → `generic_oncall`；否 → 拒绝 |
| LLM 说 `is_oncall=false` | 返回结构化提示告诉用户怎么补充信息 |
| SkillRegistry 为空 | 跳过路由，空 Skill |

> **面试话术**："Router 的设计原则是'宁可选错也不能不选'。LLM 路由失败时，我们有一个规则兜底——用关键词列表判断输入是不是运维问题。如果是，就放行到 generic_oncall 这个通用兜底剧本。generic_oncall 的工具集是通用只读集合，虽然不如专业 Skill 精准，但至少能跑完整个诊断流程。这样保证了即使 LLM 挂了，系统也不会卡在路由这一步。"

### LLM Wiki 经验回灌

Router 在调 LLM 之前，会从 LLM Wiki（经验库）召回与当前告警相关的历史诊断经验，注入到 Router 的 prompt 里。这样 Router 可以参考过去类似告警的处理经验来选择 Skill。

回灌是 best-effort 的——召回失败不影响路由，只是少了一些参考信息。命中时会记一条 transition 事件，方便追溯"这次路由有没有用到经验回灌"。

---

## 4. 工具白名单 & 三层权限防御

### 设计动机

Agent 系统里最危险的是"LLM 自己决定调什么工具"。如果不限制，LLM 在排查 CPU 问题时可能去重启 Docker 容器——这显然不对。

我们的方案是**三层防御**（借鉴 Claude Code 的 PermissionResult 设计）：

```
Layer 0: Skill 白名单（硬墙）
    └── 工具不在当前 Skill 的 allowed_tools 里 → deny，任何模式都绕不过
         例外：只读工具豁免白名单（下面会讲为什么）

Layer 1: PermissionMode 模式限制
    ├── READ_ONLY：只允许 read_only=true 的工具
    ├── NORMAL：默认，Skill 白名单 + 黑名单过滤
    ├── ASK_DESTRUCTIVE：写工具走人工审批
    └── BYPASS：开发模式，跳过所有检查

Layer 2: 静态 Guardrails（高危/通知黑名单）
    ├── 高危工具（容器重启、文件删除等）→ NORMAL 模式默认拦截
    └── 通知工具（发告警、发消息等）→ 默认不允许
```

### 决策结果

每个工具调用都会产生一个 `PermissionDecision`：

| 行为 | 含义 | LLM 可见性 |
|---|---|---|
| `allow` | 正常调用 | 暴露给 LLM |
| `ask` | 需要人工审批（MVP 阶段直接转 deny） | 暴露给 LLM |
| `deny` | 拒绝，LLM 看不到 | 不暴露 |

关键点：**deny 的工具直接不给 LLM 看到**。LLM 不知道这个工具存在，就不会尝试去调。这比"给 LLM 看到但调用时拦截"更安全——避免 LLM 反复尝试被拦截的工具浪费轮次。

### 只读工具豁免白名单——一个实践中的调整

最初设计是严格白名单：工具不在 `allowed_tools` 里就 deny。但实际运行中发现：

> Skill 作者经常漏写某个只读查询工具（比如 `list_top_processes`），导致 Agent 诊断时被硬墙挡住，报告里只能写"未提供工具"。

调整后的策略：**只读工具（`read_only=true`）自动放入候选，不需要在 `allowed_tools` 里显式声明。** 写/通知/高危工具仍然必须显式声明。

理由是：只读工具无副作用，跨 Skill 使用是安全的。写操作才需要严格管控。

### 面试怎么讲

> "我们做了三层权限防御，借鉴的是 Claude Code 的设计。第一层是 Skill 白名单硬墙——不在白名单里的写工具直接看不到；第二层是运行时 Mode——READ_ONLY 模式只暴露只读工具，ASK_DESTRUCTIVE 模式写操作需要人工审批；第三层是静态 Guardrails——高危操作（容器重启、文件删除）默认拦截。每个决策都有 `reason_type` 字段，审计和排错都能直接 grep。"

---

## 5. 从 Router 到 Planner 的衔接

Router 选完 Skill 后，Planner 拿到两样东西：

1. **Skill 的 Playbook**（Markdown body）：告诉 Planner "这类故障应该按什么步骤排查"
2. **工具白名单**（经 `filter_tools_for_skill` 过滤）：Executor 只能看到这些工具

Planner 会用 Playbook 来拆步骤。比如 `host_resource_diagnosis` 的 Playbook 写了：

```
1. 先查看 CPU 和内存使用率
2. 如果 CPU 高，查 top 进程
3. 去知识库检索类似故障的处理方案
4. 综合生成诊断报告
```

Planner 就会把这些拆成一个 `Plan { steps: ["查看 CPU 和内存", "查 top 进程", ...] }`。

如果 Planner 的 LLM 调用失败，也有 fallback plan——harness 会返回一个预定义的最小步骤列表，保证 Executor 至少能跑一些基础检查。

---

## 6. 遇到的难点总结

### 难点 1：Skill 粒度怎么选

最初想做很细的 Skill：`cpu_high_usage`、`memory_oom`、`disk_full` 各一个。但实际运行发现：

- 告警描述往往模糊，比如"机器很卡"同时涉及 CPU、内存、磁盘
- Router 在细粒度 Skill 之间的选择准确率下降
- Skill 太多时，Router 的菜单文本太长，LLM 反而选不好

调整后合并为粗粒度：`host_resource_diagnosis` 覆盖 CPU/内存/磁盘/OOM 整一类。Router 只需要判断"这是资源问题还是网络问题还是容器问题"，选择准确率明显提高。

### 难点 2：Router LLM 返回幻觉 Skill 名

LLM 有时候会"发明"一个不存在的 Skill 名，比如返回 `redis_diagnosis`（实际没有这个 Skill）。

解决方案：校验返回的 `skill_name` 是否在 registry 里，不在就回退 `generic_oncall`。同时在 Router prompt 里强调"你**必须**从给定菜单中选择，不能自己发明"。

### 难点 3：只读工具豁免 vs 安全性平衡

放开只读工具看似降低了安全性（Agent 可以查看更多系统信息）。但权衡后认为：

- 只读工具不会改变系统状态，最差情况是查了不相关的信息
- 相比之下，Agent 因为缺工具而写出"无法获取数据"的空报告，对用户价值更低
- 写操作仍然严格管控，不受影响

---

## 快速回顾清单

| 模块 | 核心设计 | 面试关键词 |
|---|---|---|
| SKILL.md | YAML frontmatter + Markdown Playbook | 解耦排障方法论、可热加载、可 Git 管理 |
| SkillRegistry | lru_cache 单例 + 外部目录挂载 | generic_oncall 强制存在、平台过滤 |
| Skill Router | LLM structured output + 多层兜底 | 宁可选错不能不选、经验回灌 |
| 工具白名单 | 三层防御：Skill 硬墙 → Mode → Guardrails | deny 不暴露给 LLM、只读豁免 |
| PermissionDecision | allow/ask/deny 三态 + reason_type | 可审计、可 grep、借鉴 Claude Code |

---

*准备好了就说"开始 Step 7"，我们进入 Fast 诊断图——Plan-Execute-Replan 的 LangGraph 状态机。*
