# Step 7：Fast 诊断图 — Plan-Execute-Replan 的 LangGraph 状态机

---

## 这一步要解决的问题

Step 6 我们有了 Skill（告诉 Agent"查什么"）和工具白名单（限制 Agent"能用什么"）。但光有剧本还不够——需要一个**编排器**来驱动整个诊断流程：拆步骤、执行、评估、调整、收尾。

这就是 LangGraph 状态图的职责。Fast 模式用的是经典的 **Plan-Execute-Replan** 三段式循环。

---

## 为什么用状态图而不是链式调用

这是面试必答题。

链式调用（Chain）是线性的：A → B → C → D。但诊断过程天然不是线性的：

- 执行完第一步发现方向错了，需要**回头调整计划**
- 查完 CPU 发现正常，需要**换个方向查网络**
- 已经收集够信息了，需要**提前收尾**而不是机械跑完所有步骤

状态图（StateGraph）支持**条件边**（conditional edges）：Replanner 评估完后，可以走三个方向——继续执行、回 Planner 重新规划、或者结束。这种"有条件的循环"是链式调用做不到的。

> **面试话术**："链式调用是固定管道，状态图是有条件分支和循环的流程引擎。诊断过程需要'走两步看一步'的灵活性——Replanner 每次都会评估当前进度，决定继续、调整还是收尾。这个动态决策能力是选 LangGraph 的核心原因。"

---

## 图结构

```
[START]
    │
    ▼
SkillRouter ──(已 response?)── yes ──► [END]
    │ no
    ▼
Planner ──────────────────────────────────┐
    │                                      │
    ▼                                      │
Executor ◄─────────────────────┐           │
    │                          │           │
    ▼                          │           │
Replanner ──(should_end?)──────┤           │
    │          │               │           │
    │       executor        planner        │
    │       (继续)        (Skill reroute)──┘
    │
    ▼
  [END] (is_finished=true)
```

四个节点，三种边：

| 边 | 类型 | 条件 |
|---|---|---|
| START → SkillRouter | 固定边 | 总是 |
| SkillRouter → Planner / END | 条件边 | Router 已生成 response（非 OnCall 输入）→ END |
| Planner → Executor | 固定边 | 总是 |
| Executor → Replanner | 固定边 | 总是 |
| Replanner → Executor / Planner / END | 条件边 | 核心决策点 |

---

## 共享状态（PlanExecuteState）

所有节点共享一个 TypedDict 状态对象，关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `input` | str | 用户原始告警（不变） |
| `selected_skill` | str | Router 选定的 Skill |
| `plan` | List[str] | 待执行步骤（Replanner 会更新） |
| `past_steps` | List[(str, str)] | 已执行的 (步骤, 结果)，`operator.add` 累加 |
| `response` | str | 最终报告（非空 → 触发 END） |
| `iteration` | int | 已执行步数（防死循环硬上限） |
| `tried_skills` | List[TriedSkill] | 已试过的 Skill + 被拒原因（防 reroute 回环） |
| `transition_history` | List[StateTransition] | 结构化事件时间线，`operator.add` 累加 |

### Reducer 约定

LangGraph 的 reducer 是面试可以展开的技术点：

- **普通字段**（如 `plan`、`response`）：新值直接覆盖旧值
- **`Annotated[List, operator.add]`**（如 `past_steps`、`transition_history`）：新值追加到旧值后面

为什么 `past_steps` 用累加而不是覆盖？因为 Executor 每次只返回当前步的 `(step, result)`，需要自动追加到历史列表里。如果用覆盖，Executor 就得自己维护完整历史。

### 一个重要的坑

> **不要在 state.py 文件里加 `from __future__ import annotations`**。否则 `Annotated` 的元数据会被字符串化，LangGraph 读不到 `operator.add` reducer，所有累加字段都会变成覆盖行为。这个坑在 `state_deep.py` 的注释里也特别标注了。

---

## 四个节点详解

### ① SkillRouter（Step 6 已讲）

选 Skill → 写 `selected_skill`。非 OnCall 输入直接写 `response` 触发 END。

### ② Planner

拿到 Skill 的 Playbook → LLM 拆成 4-6 步计划 → 写入 `plan`。

关键设计：Planner 的 prompt 里注入了 Skill 的 Playbook 全文。LLM 不是从零拆步骤，而是参考运维专家写好的排查方法论来拆。这比通用 prompt "你是一个运维专家，请拆解步骤" 准确率高很多。

兜底：LLM 调用失败 → harness 返回一个预定义的最小 fallback plan（通常是"查知识库 → 检查系统状态 → 生成报告"三步）。

### ③ Executor

执行 `plan[0]`（当前步骤），调用工具收集信息。

**两种执行模式**：

- **Parallel 模式**（默认）：只读工具按 `ToolMeta.concurrency_safe` 分批 `asyncio.gather` 并行。比如同时查 CPU、内存、磁盘三个指标。遇到写工具自动切批走串行。
- **Serial 模式**（fallback）：用 LangChain 标准的 `create_agent`，所有工具串行调用。

执行完后把 `(step, result)` 追加到 `past_steps`。

**工具来自哪里**：`get_all_tools()` 加载全量 → `filter_tools_for_skill()` 按 Skill 白名单和 PermissionMode 过滤 → 只把 allow/ask 的工具暴露给 LLM。

**Executor 缓存**：同一个 (skill, tool_names, runner_mode, perm_mode) 组合的 Executor 实例会被缓存复用，避免每步都重建。

### ④ Replanner（核心决策节点）

评估目前的进度，做三选一决策：

```
看 past_steps 里已经收集到的证据
    │
    ├─ 信息够了 → is_finished=true → 生成最终报告 → 写 response → END
    │
    ├─ 还需继续 → is_finished=false → 返回剩余步骤 → 回 Executor
    │
    └─ 方向不对 → should_reroute=true → 切换 Skill → 回 Planner 重新规划
```

---

## Replanner 的三层防死循环

这是面试高频追问点。LLM 可能无限循环（"还需要继续""还需要继续"……），必须有硬性保护：

**第一层：Prompt 层**
> 在 Replanner 的 system prompt 里写"尽快收尾，控制在 6 步以内"。这是软约束，靠 LLM 自觉。

**第二层：代码层 — Harness 预判**
> `evaluate_replanner_pre_llm()` 在调 LLM 之前就检查：iteration 达到 `max_steps`（默认 6）→ 直接 `force_report`，不调 LLM。

**第三层：兜底**
> LLM 返回了空 plan 且 `is_finished=false`（矛盾状态）→ 强制用 `_force_summary()` 基于 `past_steps` 拼一份兜底报告。

> **面试话术**："防死循环有三层。Prompt 层是软提示，代码层有硬上限——超过 6 步不管 LLM 说什么都强制收尾。第三层是逻辑矛盾检测——如果 LLM 说'没完成'但又不给剩余步骤，直接兜底出报告。三层叠加确保最差情况下 Agent 也会在 6 步内停下来。"

---

## Skill Reroute（故障域切换）

这是一个亮点设计，面试值得展开讲。

### 场景

用户说"我的服务很慢"，Router 选了 `host_resource_diagnosis`（CPU/内存类）。Executor 查完发现 CPU 10%、内存 30%、磁盘 20%——全部正常。这时候继续在这个 Skill 下排查没有意义了。

Replanner 可以提议 reroute：切换到 `network_diagnosis`，让 Planner 重新生成网络方向的排查计划。

### 安全校验（代码层六道门槛）

LLM 只负责"提议"reroute，最终是否生效由代码严格校验：

| 校验 | 不通过的结果 |
|---|---|
| `new_skill` 非空 | 拒绝 |
| `past_steps` 数量 ≥ 门槛（默认 2） | 拒绝："证据不足，不能急着换方向" |
| `reroute_count` < 上限（默认 2） | 拒绝："已经切过太多次了" |
| `new_skill` ≠ 当前 `selected_skill` | 拒绝："不能自循环" |
| `new_skill` 不在 `tried_skills` 里 | 拒绝："这个方向已经试过了" |
| `new_skill` 在 SkillRegistry 里存在 | 拒绝："LLM 幻觉了一个不存在的 Skill" |

全部通过后：
- 当前 Skill 加入 `tried_skills`（带失败原因）
- `reroute_count` + 1
- `plan` 清空
- `pending_reroute = true` → 图的条件边路由回 Planner

### 面试话术

> "reroute 的设计借鉴了 LangGraph 的 Supervisor + Handoff 模式和 NeurIPS 2025 的 failure memory 思路。LLM 可以提议切换故障域，但代码层有六道校验门槛：证据够不够、次数超没超、有没有试过、Skill 存不存在。这样既保留了 Agent 灵活换方向的能力，又防止了无限切换。tried_skills 还会记录'为什么这个方向不对'，下次 Replanner 做决策时可以参考，避免重蹈覆辙。"

---

## 最终报告的双模型策略

Replanner 决定 `is_finished=true` 后，不是直接用 Replanner 的输出当最终报告。而是再调一次专门的 report_model（默认用 pro 级大模型）基于所有 `past_steps` 重写一份高质量报告。

为什么分两个模型？

- **Replanner 用 flash 模型**：跑得频繁（每步一次），需要快、便宜。它只需要做"继续/结束/reroute"的决策，不需要写长报告。
- **Report 用 pro 模型**：只跑一次（诊断结束时），用大模型写结构化的五段报告（问题概述、根因分析、关键证据、处置建议、结论）。

> **面试话术**："我们做了模型分层——Replanner 这种高频决策节点用便宜快速的 flash 模型，最终报告只跑一次用 pro 模型保证质量。这是成本和质量的平衡。"

---

## Transition History（结构化事件时间线）

每个节点在出口处都会记一条 `StateTransition`：

```python
make_transition("replanner", REPLANNER_CONTINUE, "剩余 3 步")
make_transition("replanner", REPLANNER_REROUTE, "host_resource → network_diagnosis")
make_transition("executor", EXECUTOR_OK, "tools=2 step='查看 CPU 和内存'")
```

所有 transition 通过 `operator.add` 累加到 `transition_history` 里，形成一条完整的诊断时间线。这个时间线有两个用途：

1. **前端实时展示**：`diagnosis_runner` 把每条 transition 转成 SSE 事件推给前端，用户能看到"正在路由 → 已选 CPU 诊断 → 正在查 CPU → CPU 正常 → 切换到网络诊断 → ……"
2. **事后审计**：存到 Postgres 的 `agent_runs` 表，可以回溯每次诊断的完整决策链路

---

## 遇到的难点总结

### 难点 1：Union 类型在通义千问下的兼容性

最初 Replanner 的输出用的是 `Union[Response, Plan]`（LangChain 官方教程的写法），但通义千问对 Union 类型支持不好——会返回字符串 `"Plan"` 而不是 JSON 对象。

解决方案：改用单一 schema `Act`，用 `is_finished: bool` 作为 discriminator。`is_finished=true` 时取 `response` 字段，`is_finished=false` 时取 `plan` 字段。通用性更强，不依赖 LLM 理解 Union 语义。

### 难点 2：Executor 每步创建新 Agent 的性能问题

最初每步都重新创建 Agent（构建 tools binding、prompt 模板）。诊断 6 步就创建 6 次，每次几百毫秒。

解决方案：缓存 `(skill, tool_names, runner_mode, perm_mode)` 对应的 Executor 实例。同一个 Skill 的多步执行复用同一个 Agent，只有 Skill 或权限模式变了才重建。

### 难点 3：Reroute 的回环问题

最初 reroute 没有 `tried_skills` 记录，Agent 可能在两个 Skill 之间来回切换：CPU 诊断 → 网络诊断 → CPU 诊断 → ……

解决方案：引入 `tried_skills` 黑名单。每次 reroute 把当前 Skill 加入黑名单并记录原因。下次 reroute 时检查黑名单，已试过的不允许再切回去。

---

## 快速回顾清单

| 环节 | 核心设计 | 面试关键词 |
|---|---|---|
| 图结构 | SkillRouter → Planner → Executor ⇄ Replanner | 条件边、有循环的状态图 |
| State | TypedDict + operator.add reducer | 累加 vs 覆盖、不能加 future annotations |
| Planner | Skill Playbook 注入 + LLM 拆步骤 | 有领域知识的规划、fallback plan |
| Executor | 只读工具并行 + 写工具串行 | concurrency_safe 分批、Agent 缓存 |
| Replanner | 继续/结束/reroute 三选一 | 三层防死循环、Act 单 schema |
| Reroute | LLM 提议 + 代码六道校验 | tried_skills 黑名单、failure memory |
| 报告 | flash 做决策 + pro 写报告 | 模型分层、成本质量平衡 |
| Transition | 结构化事件时间线 | 实时展示 + 事后审计 |

---

*准备好了就说"开始 Step 8"，我们进入 MCP 工具接入——Agent 的"手"和"眼睛"。*
