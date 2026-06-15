# Layer 4b: AgentHarness 统一管控

> 目录: `app/runtime/agent_harness.py`
>
> 上游: Layer 4 各节点通过 AgentHarness 获取模型、Prompt 和预算控制 → 下游: AgentHarness 的状态管理贯穿整个诊断生命周期

AgentHarness 把分散在各 Agent 里的模型选择、Prompt 模板、预算控制收敛到一处，避免每个节点各自为政。

## 模型分层

| 阶段 | 模型选择 | 理由 |
|------|---------|------|
| Router | DashScope 快速模型 | 分类任务简单，追求速度 |
| Planner | Agent 规划模型 | 需要理解 Playbook |
| Executor | 流式对话模型 | 需要实时 token 输出 |
| Replanner | 同 Planner (temp=0.0) | 决策需要确定性 |
| Report | Pro 模型 | 最终报告质量优先 |

## 预算控制

```python
def evaluate_budget(stats):
    if stats.total_tokens > token_limit:
        return BudgetStatus.EXCEEDED
    if stats.total_ms > time_limit:
        return BudgetStatus.EXCEEDED
    if stats.total_tokens > token_limit * 0.8:
        return BudgetStatus.WARNING
    return BudgetStatus.OK
```

## Prompt 收敛

所有 Agent 的系统 Prompt 都在 AgentHarness 中定义，包括：
- **SRE 人格**："你是一个经验丰富的 SRE，优先用工具取证而不是猜测"
- **报告模板**：5 段式（问题概述、已收集证据、根因分析、处置建议、结论）
- **重路由规则**：切换条件、证据要求、配额限制

## 状态管理

### fast 模式状态 (TypedDict)

```python
class PlanExecuteState(TypedDict):
    # 累积字段 (operator.add)
    past_steps: List[Tuple[str, str]]           # (步骤, 结果)
    transition_history: List[StateTransition]    # 状态转移记录
    tried_skills: List[TriedSkill]              # 重路由失败记忆

    # 覆盖字段
    selected_skill: str                         # 当前 Skill
    plan: List[str]                             # 当前计划
    response: str                               # 最终报告 (触发 END)
    iteration: int                              # 步骤计数
    reroute_count: int                          # 重路由次数
    pending_reroute: bool                       # 重路由信号
    permission_mode: str                        # 权限模式
```

### deep 模式状态

```python
evidences: Annotated[List[Dict], operator.add]  # 并行安全累积
```

四个专业 Agent 并发写入 `evidences`，LangGraph 的 `operator.add` reducer 保证原子归并。

## Transition History：结构化事件线

每个节点执行后追加 `StateTransition`：

```python
{"node": "replanner", "reason": "REPLANNER_REROUTE",
 "detail": "network_diagnosis → host_resource_diagnosis, 证据不足阻止",
 "ts": "2024-01-15T10:30:00.123"}
```

这条时间线让诊断过程完全可回溯：从 Skill Router 的选择、Planner 的规划、Executor 的每一步调用到 Replanner 的决策，都有结构化记录。

## 模拟面试问答

### 🔥 热点拷问

**面试官：预算控制你设了 token 上限和时间上限，实际跑的时候 token 消耗是什么量级？一次诊断花多少钱？**

fast 模式一次诊断典型 token 消耗在 3000-8000 tokens（包含所有节点的 input + output）。以 DeepSeek 定价，一次诊断大概 0.01-0.03 元。deep 模式因为 4 个 Agent 并行 + Reducer + RCAJudge + Report，token 消耗大约是 fast 的 3-5 倍。预算控制的 80% 预警线和 100% 强制收敛主要防两种情况：Agent 陷入循环（反复调同一个工具），以及异常大的工具返回塞满上下文后每一步都消耗大量 token。

**追问：如果我要换一个 LLM Provider，改动量大吗？**

改动量很小——收敛的好处。AgentHarness 通过配置决定每个阶段用哪个模型，底层都是 OpenAI-compatible 接口，换 Provider 只需改 `.env` 里的 API Key 和模型名。最大的风险不是代码改动，而是不同模型的推理质量差异——换一个便宜模型后 Router 的分类准确率可能下降。

### 常规问题

**面试官：Transition History 有什么实际用处？不就是个日志吗？**

不只是日志，是结构化的决策链。当诊断结果不对时，回溯 Transition History 可以看到：Skill Router 为什么选了这个 Skill（`ROUTER_OK, confidence=0.92`）、Replanner 为什么没有重路由（`REPLANNER_CONTINUE, plan_remaining=2`）、哪一步工具调用返回了异常数据。普通日志需要从几百行里 grep，Transition History 是按节点、原因、时间戳组织的结构化序列，可以直接定位决策链上的哪一环出了问题。

### 反思与改进

**面试官：AgentHarness 的设计初衷是什么？之前出过什么问题？**

V2 之前每个 Agent 节点各自管理模型选择和 Prompt——Router 里写死 DeepSeek，Planner 里写死 DashScope，Report 里又是另一个。改模型要改 5 个文件，调 Prompt 要满代码库搜索。收敛到 AgentHarness 后才有了"切模型改一处配置"的能力。体现的是关注点分离和单一职责原则在 Agent 系统中的应用。
