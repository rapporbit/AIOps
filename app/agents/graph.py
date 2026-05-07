"""LangGraph 图编排.

图结构 (Skill Router + Plan-Execute-Replan):

    [START]
       │
       ▼
   ┌──────────────┐
   │ SkillRouter  │  (在 Skill 列表中选一个, 写 state.selected_skill)
   └──────┬───────┘
          ▼
   ┌──────────┐
   │ Planner  │  (基于选定 Skill 的 Playbook, 制定 4-6 步计划)
   └─────┬────┘
         ▼
   ┌──────────┐
   │ Executor │  (执行 plan[0], 调用工具)
   └─────┬────┘
         ▼
   ┌──────────┐
   │Replanner │  (评估进度)
   └─────┬────┘
         │
    ┌────┴────┐
    │ should  │
    │  end?   │   ── yes ──► [END]
    └────┬────┘
        no
         ▼
       (loop back to Executor)

设计要点:
  - 起点是 SkillRouter, 跑一次, 然后交给 Planner
  - Executor 和 Replanner 之间通过 conditional edge 形成循环
  - should_end() 通过检查 state["response"] 是否非空来决定终止
  - 不在 graph 里加 checkpoint (短任务, 不需要持久化)
"""

from typing import Literal

from langgraph.graph import END, START, StateGraph
from loguru import logger

from app.agents.executor import execute_node
from app.agents.fork_runner import fork_skill_node
from app.agents.planner import plan_node
from app.agents.replanner import replan_node
from app.agents.skill_router import skill_router_node
from app.agents.state import PlanExecuteState
from app.skills import get_skill_registry


def should_end(state: PlanExecuteState) -> Literal["executor", "planner", "__end__"]:
    """Replanner 后的条件边: 三向路由.

    优先级:
      1. 已生成 response → END
      2. pending_reroute=True → 回 planner 重新规划 (Supervisor + Handoff 保守版)
      3. plan 为空 + 无 response → 强制 END (防死循环)
      4. 默认 → 回 executor 继续跳下一步
    """
    response = state.get("response", "")
    if response:
        logger.info("[Graph] 收到 response, 流程结束")
        return END
    if state.get("pending_reroute"):
        logger.info(
            f"[Graph] 检测到 pending_reroute, 路由回 Planner 重新规划 "
            f"(new selected_skill={state.get('selected_skill', '?')})"
        )
        return "planner"
    if not state.get("plan"):
        logger.warning("[Graph] plan 为空且无 response, 强制终止")
        return END
    return "executor"


def route_after_skill(state: PlanExecuteState) -> Literal["planner", "fork_skill", "__end__"]:
    """§4 cc-haha: skill_router 之后的三向路由.

    优先级:
      1. Router 已生成 response (非 OnCall 输入 / 兜底场景) → END
      2. 当前在 fork 子图内 (state.inside_fork=True) → 退化为 inline (planner)
         避免无限递归 fork
      3. Skill 标记 context=fork → fork_skill_node (独立子图)
      4. 默认 → planner (inline 模式)
    """
    response = state.get("response", "")
    if response:
        logger.info("[Graph] Router 已生成 response, 跳过 Planner/Executor")
        return END

    if state.get("inside_fork"):
        # 子图内: 不再 fork, 走 inline. 保护性, 避免无限递归
        return "planner"

    skill_name = state.get("selected_skill", "")
    skill = get_skill_registry().get_or_generic(skill_name)
    if skill.context == "fork":
        logger.info(f"[Graph] Skill {skill.name} context=fork, 走独立子图")
        return "fork_skill"
    return "planner"


def build_aiops_graph():
    """构建 AIOps 多智能体 graph.

    Returns:
        编译后的 CompiledStateGraph, 可以 .ainvoke() / .astream() 调用
    """
    workflow = StateGraph(PlanExecuteState)

    # 节点
    workflow.add_node("skill_router", skill_router_node)
    workflow.add_node("planner", plan_node)
    workflow.add_node("executor", execute_node)
    workflow.add_node("replanner", replan_node)
    # §4: fork 模式独立子图入口节点 (内部会再 build_aiops_graph 跑子图)
    workflow.add_node("fork_skill", fork_skill_node)

    # 边
    workflow.add_edge(START, "skill_router")
    workflow.add_conditional_edges(
        "skill_router",
        route_after_skill,
        {
            "planner": "planner",
            "fork_skill": "fork_skill",
            END: END,
        },
    )
    # fork_skill 完成后直接 END (它内部已跑完整套子图)
    workflow.add_edge("fork_skill", END)
    workflow.add_edge("planner", "executor")
    workflow.add_edge("executor", "replanner")

    # 条件边: replanner -> executor (继续) / planner (Skill reroute) / END (终止)
    workflow.add_conditional_edges(
        "replanner",
        should_end,
        {
            "executor": "executor",
            "planner": "planner",
            END: END,
        },
    )

    compiled = workflow.compile()
    logger.info("[Graph] AIOps graph 已编译完成 (skill_router + plan-execute-replan)")
    return compiled
