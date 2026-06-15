# Layer 4: 智能体层

> 目录: `app/agents/`, `app/diagnosis_graphs/`, `app/skills/`, `app/runtime/agent_harness.py`
>
> 上游: Layer 3 (DiagnosisRunner 构建并执行 LangGraph 图) → 下游: Layer 5 (RAG 检索) / Layer 6 (MCP 工具调用) / Layer 7 (Evidence 写入)

智能体层是系统的大脑，负责故障分类、诊断规划、工具调用和报告生成。核心设计：**Skill-first 路由 + 双模式 LangGraph 图 + AgentHarness 统一管控**。

本层内容较多，拆分为三个文档：

- **本文**：双模式 LangGraph 图、Executor、Replanner（执行核心）
- [04a-skill-system.md](04a-skill-system.md)：Skill Router 三级路由、Planner、Skill 注册表
- [04b-agent-harness.md](04b-agent-harness.md)：AgentHarness 统一管控、状态管理、Transition History

## 4.1 双模式诊断图

### fast 模式：Plan-Execute-Replan

```
START → SkillRouter ──→ Planner → Executor → Replanner ──→ END
                 │                              │    │
                 │ (非 OnCall → 直接拒绝)        │    └─→ Executor (继续)
                 └─→ END                        └─→ Planner (重路由)
```

LangGraph 条件路由实现：

```python
# app/agents/graph.py
graph.add_conditional_edges("skill_router", route_after_skill,
    {"plan": "planner", END: END})

graph.add_conditional_edges("replanner", should_end,
    {"agent": "executor", "plan": "planner", END: END})
```

### deep 模式：多 Agent 证据图

```
START → IncidentManager → CorrelationContext → EvidencePlan
                                                    │ (fan-out)
                    ┌───────────┬───────────┬───────┴───────┐
                    ▼           ▼           ▼               ▼
               LogAgent    MetricAgent   InfraAgent    RunbookAgent
                    └───────────┴───────────┴───────┬───────┘
                                                    │ (fan-in barrier)
                                                    ▼
                          EvidenceReducer → RCAJudge → RemediationPlanner → Report → END
```

**Dispatch Guard**：每个专业 Agent 被 `_dispatch_guard()` 包裹，如果 EvidencePlan 没有选中该 Agent，直接跳过，零 LLM 调用。

**并行取证**：LangGraph 的多输入边到 EvidenceReducer 作为隐式 join barrier，四个 Agent 全部完成后才进入归并。

**隔离上下文**：Agent 之间不互聊，只把压缩后的 Evidence 写入共享 `state.evidences`，通过 `operator.add` reducer 安全归并。

## 4.2 Executor：并行工具调用

### 工具分组策略

```python
# app/runtime/tool_runner.py
def partition_tool_calls(calls, max_parallel):
    batches = []
    for call in calls:
        if call.concurrency_safe:
            # 并发安全的工具合批
            if batches and batches[-1].safe:
                batches[-1].append(call)
            else:
                batches.append(SafeBatch([call]))
        else:
            # 非安全工具单独执行
            batches.append(UnsafeBatch([call]))
    return batches
```

并发安全的只读工具（查 CPU、查知识库）合批执行；非安全工具（需要审批的写操作）串行执行。

### 结果截断

```python
if len(result) > tool_meta.max_result_chars:
    result = result[:max_chars] + f"\n... (截断, 原始 {len(result)} 字符)"
```

防止 20KB 的 Docker 日志塞满上下文窗口。

### 流式输出

Executor 不用 `ainvoke()`，而是用 `astream()` 推送 token 到前端，用户可以实时看到 Agent 的思考过程。

## 4.3 Replanner：三路决策 + 重路由

### 预判快速路径（免 LLM）

```python
# app/runtime/agent_harness.py
def evaluate_replanner_pre_llm(state):
    if iteration >= max_steps:
        return "force_report"           # 步数上限, 直接生成报告
    if last_3_steps_identical():
        return "force_report"           # 重复检测, 陷入循环
    if plan_remaining and no_last_failure:
        return "continue_next_step"     # 计划还有步骤, 上一步没失败, 跳过 LLM
    return None                         # 需要 LLM 决策
```

常见情况（计划还有剩余步骤、上一步成功）直接跳过 Replanner LLM 调用，节省 token 和延迟。

### LLM 三路决策

```python
class Act(BaseModel):
    is_finished: bool       # True → 生成报告
    response: str           # 报告内容 (is_finished=True 时)
    plan: list[str]         # 新计划 (is_finished=False 时)
    should_reroute: bool    # True → 切换 Skill
    new_skill: str          # 目标 Skill (should_reroute=True 时)
```

### 重路由机制（Supervisor + Handoff）

当 Replanner 发现当前 Skill 不匹配（比如网络问题被误分到主机资源 Skill），可以请求切换：

```
重路由校验 (代码层, 不信任 LLM 的所有建议):
    ✓ past_steps >= min_reroute_past_steps    ← 必须有足够证据
    ✓ reroute_count < max_reroutes            ← 防止无限切换
    ✓ new_skill ≠ selected_skill              ← 不能自环
    ✓ new_skill ∉ tried_skills                ← 不能回退已失败的 Skill
    ✓ new_skill 存在于注册表

    通过 → state["selected_skill"] = new_skill
           state["pending_reroute"] = True → 路由回 Planner
    阻止 → 记录 REPLANNER_REROUTE_BLOCKED, 继续当前 Skill
```

`tried_skills` 列表作为**失败记忆**，防止 A→B→A 的死循环。

详见 [04a-skill-system.md](04a-skill-system.md)（Skill Router、Planner、注册表）和 [04b-agent-harness.md](04b-agent-harness.md)（AgentHarness、状态管理、Transition History）。

## 模拟面试问答

### 🔥 热点拷问

**面试官：你这个 deep 模式，说起来很好听——多 Agent 并行取证、证据归并、RCA 裁决。但你实际跑过吗？deep 跑出来的结果真的比 fast 好吗？有数据吗？**

说实话，当前没有系统性的 fast vs deep 对比评测数据。deep 模式的设计目标是处理多信号交叉的复杂故障——比如"服务间歇性超时，可能是网络、资源或依赖中任一原因"。fast 只走一条 Skill 链路，可能漏掉跨域证据；deep 并行派多个 Agent 取证再归并，覆盖面更广。但这需要评测数据支撑——应该建一组复杂 RCA 场景的 benchmark，对比两种模式的根因命中率和证据覆盖率。这是当前的一个工程短板。

**追问：deep 的 Agent 之间不互相通信，只通过 Evidence 黑板交互。但如果 MetricAgent 发现 CPU 100%，LogAgent 发现 OOM 日志，这两个信息的关联怎么做？**

关联在 EvidenceReducer 做，不在 Agent 之间做。每个 Agent 独立产出结构化的 Evidence（比如 `{type: "metric_snapshot", summary: "CPU 100%", score: 0.9}`），EvidenceReducer 收集所有 Evidence 后做归并和去重，然后 RCAJudge 看候选根因和 evidence summary 做关联判断。代价是 RCAJudge 的 Prompt 必须设计好，让它能从结构化 Evidence 中发现跨域关联。

---

**面试官：考虑实际工程效果——如果电脑上有一个名为 svchost.exe 的恶意挖矿程序，你的系统光扫进程名能判断出来吗？**

判断不了，这是当前系统的能力边界。MCP 的 system_server 可以拿到进程列表（进程名、PID、CPU/内存占用），但光看进程名无法区分正常 svchost.exe 和冒名的恶意程序。要做到这一步需要：拿到进程完整路径和签名验证、检查进程的网络连接（挖矿有矿池连接）、知识库里要有恶意进程伪装的排查 SOP。当前系统是运维诊断定位，不是安全分析工具——但这恰恰说明 Skill 可以扩展，比如新增 `security_diagnosis` Skill，接入更专业的安全检测工具。

**追问：那你怎么保证不同告警、多次分析的路径和结果是准确一致的？LLM 本身就是非确定性的。**

完全一致做不到，这是 LLM Agent 系统的固有限制。但可以从几个层面提高一致性：一是 Skill Router 的规则预检层是确定性的——同样的关键词必定命中同样的规则。二是 Planner 基于 Playbook 框架内特化，变化范围受限。三是 Replanner 用 `temp=0.0` 降低随机性。四是最终影响结果一致性的主要是工具调用的返回——同样的系统状态下工具返回相同数据，Agent 应该得出相似结论。如果需要强一致性，可以把关键决策路径固化为 Playbook 的确定性脚本，减少 LLM 的决策空间——但这会牺牲 Agent 的灵活性。

---

**面试官：你说排查故障，能做到什么程度？能定位根因吗？能自动修复吗？**

当前定位是"诊断定位"而不是"自动修复"。能做到：收集系统状态（CPU/内存/磁盘/进程/网络/容器）、匹配知识库 SOP、给出根因分析和处置建议。根因定位的准确度取决于工具采集的信息量和知识库的覆盖度——简单场景（CPU 高 → top 进程是 XX）定位准确率较高，复杂场景（间歇性超时、多服务联动）需要 deep 模式但目前缺少系统性评测。自动修复方面，默认只读模式不执行任何写操作，`ASK_DESTRUCTIVE` 模式下写操作需人工审批——报告会列出修复命令但执行权交给人。

### 深度追问链

**面试官：（接 deep 模式问题）如果 fast 和 deep 都跑了但结论不一样，你怎么办？**

这是一个好问题，目前没有自动仲裁机制。fast 和 deep 是独立链路，不会同时跑同一个任务——要么选 fast 要么选 deep。但假设未来做了"先 fast 快速出结论、再 deep 验证"的模式，结论冲突时应该以 deep 为准（因为证据更充分），但需要标注分歧点让运维人员自己判断。

**继续追问：你怎么定义"正确的诊断"？有 ground truth 吗？**

这是 AIOps 评测的核心难题。诊断的"正确"是多层次的：根因定位是否正确、处置建议是否可操作、证据链是否完整。当前没有 ground truth 数据集——应该建一组标注过的故障场景，每个场景标注期望根因、期望工具调用序列和期望处置步骤，然后对比 Agent 的实际输出。本质上是人工标注 + 专家评审，成本不低但是量化诊断质量的唯一可靠方式。

**再追问：完全相同的输入，两次路径不同但结论相同，算一致吗？**

算。路径不一致是 LLM 的固有特性——可能先查 CPU 再查内存，也可能反过来。关键是结论一致：根因相同、处置建议相同、关键证据都被采集到。量化一致性应该看结论维度而不是路径维度。如果需要路径也一致（审计合规场景），那就不应该用 LLM Agent，而应该用确定性的规则引擎 + 脚本。

---

**面试官：（接诊断能力问题）知识库没覆盖、工具采集不到的场景，Agent 会不会胡编一个根因？**

会，这是 LLM 幻觉风险。缓解措施有三个：一是报告模板要求引用 evidence_id，没有证据支撑的结论会被标注为"推测"。二是 Replanner 的步骤上限防止 Agent 无限探索——到达上限后强制生成报告，标注"信息不足，以下结论基于有限证据"。三是 deep 模式的 RCAJudge 如果 evidence 为空或全是 error_type，应该输出"无法确定根因"而不是编一个。但这需要 Prompt 设计得好，当前没有系统性测试过幻觉率。

### 常规问题

**面试官：Executor 为什么用 astream 而不是 ainvoke？**

ainvoke 要等 Agent 完整执行完才返回，对于跑几十秒的诊断用户面对的是长时间白屏。astream 逐 token 推送到前端，用户可以实时看到 Agent 在想什么、调了什么工具，既减少等待焦虑，也方便在 Agent 跑偏时及时终止。

### 反思与改进

**面试官：如果重来，你还会选 LangGraph 吗？**

会，但用法可能不同。LangGraph 的条件边和 fan-out/fan-in 对图定义很自然，手写 asyncio 状态机代码量翻 3-5 倍且难维护。但调试体验不太好——graph 内部状态变化不容易 trace。如果重来，我会加一层更完善的执行追踪（每个节点的输入/输出/耗时自动记录到 Postgres），而不是只在 Replanner 里手动维护 Transition History。

**面试官：Agent 开发过程中最难的 bug？**

Replanner 重路由的死循环。早期没有 `tried_skills` 列表，出现过 A→B→A→B 的无限循环——Replanner 觉得当前 Skill 不对就切换，切换后又觉得不对再切回来。表现是诊断一直跑不完，token 飙升。一开始以为是 Prompt 问题调了很久，后来加了 `tried_skills` 失败记忆和 `reroute_count` 上限，两行代码就解决了。教训是 Agent 的 bug 往往不在单步推理上，而在状态管理和循环终止条件上。
