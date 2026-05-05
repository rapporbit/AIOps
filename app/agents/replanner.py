"""Replanner 节点: 评估进度, 决定继续 or 出报告.

设计要点:
  - 用 with_structured_output(Act) 强制 LLM 返回 Plan 或 Response (二选一)
  - 三层防死循环:
      1. Prompt 层: "尽快收尾, 控制在 6 步以内"
      2. 代码层: 已达 max_steps 强制生成报告
      3. 兜底: 如果 LLM 返回空 plan, 也强制生成报告
"""

import re
from datetime import datetime

from loguru import logger

from app.agents.prompts import REPLANNER_SYSTEM_PROMPT, REPLANNER_USER_TEMPLATE
from app.agents.state import Act, PlanExecuteState, TriedSkill
from app.config import settings
from app.core.llm import get_chat_llm
from app.core.structured import ainvoke_structured
from app.runtime.transitions import (
    REPLANNER_CONTINUE,
    REPLANNER_FINISHED_EMPTY,
    REPLANNER_FINISHED_OK,
    REPLANNER_LLM_FAILED,
    REPLANNER_MAX_STEPS_FORCE,
    REPLANNER_NOT_FINISHED_EMPTY,
    REPLANNER_REROUTE,
    REPLANNER_REROUTE_BLOCKED,
    make_transition,
)
from app.skills import get_skill_registry


def _format_past_steps(past_steps: list[tuple[str, str]]) -> str:
    """把已完成步骤拼成 Markdown 列表. 每条 result 截断防止 prompt 撑爆."""
    if not past_steps:
        return "(暂无已完成的步骤)"
    limit = max(200, settings.agent_replanner_past_step_chars)
    lines = []
    for i, (step, result) in enumerate(past_steps, 1):
        body = result if len(result) <= limit else result[:limit] + f"\n[... 已截断, 原长 {len(result)} 字符]"
        lines.append(f"## 步骤 {i}: {step}\n{body}")
    return "\n\n".join(lines)


def _last_step_failed(past_steps: list[tuple[str, str]]) -> bool:
    """看上一步 result 是否包含失败标记 (Executor 在异常时会 prefix '[执行失败' 或 '[超过最大步数')."""
    if not past_steps:
        return False
    _step, result = past_steps[-1]
    head = (result or "")[:50]
    return head.startswith("[执行失败") or head.startswith("[超过最大步数")


_REPORT_SYSTEM_PROMPT = """你是一名资深 SRE 工程师, 基于已收集的证据写一份运维诊断报告.

# 硬性要求
1. 必须产出完整的 5 段: ① 问题概述 ② 关键证据 ③ 根因分析 ④ 处置建议 ⑤ 结论.
2. 证据段里要用 Markdown 引用或表格展示关键指标/日志原文片段, 不要凭空编.
3. 处置建议按 "紧急止损 / 长期优化" 两小节; 紧急止损必须是可立刻执行的命令或操作.
4. 结论一段话收束, 明确告诉用户根因 + 下一步该做什么.
5. 中文输出, 语气专业但不啰嗦, 每段不超过 6 行.
6. 不要写 "我将" "我需要" 这类过程性语言, 直接给结论.
"""

_REPORT_USER_TEMPLATE = """# 用户原始问题
{user_input}

# 诊断流程中收集的证据 (按时间顺序)
{past_steps_text}

# Replanner 初版结论 (flash 写的草稿, 可以参考但不要照抄)
{draft}

# 报告生成时间
{current_time}

请严格按 5 段结构, 用 Markdown 输出最终报告.
"""


async def _synthesize_final_report(
    user_input: str,
    past_steps: list[tuple[str, str]],
    current_time: str,
    draft: str = "",
) -> str:
    """用 report_model (默认 pro) 基于 past_steps 写高质量最终报告.

    只在 Replanner 决定 is_finished=true 时调一次, 给最终报告质量兜底.
    Replanner 自己用 flash 做决策, 这里用 pro 专职 polish, 质量/速度两头兼顾.
    失败时返回 draft (或 _force_summary 做进一步兜底).
    """
    report_model = settings.agent_report_model or settings.dashscope_chat_model
    decide_model = settings.agent_planner_model or settings.dashscope_router_model
    # 如果 report_model 和 decide 用的是同一个模型, 说明用户没分层, 直接用 draft 省一次调用.
    if not draft.strip() and not past_steps:
        return ""
    if report_model == decide_model and draft.strip():
        logger.debug(
            f"[Report] report_model={report_model} 与 replanner 同模型且草稿非空, 跳过二次合成"
        )
        return draft
    try:
        llm = get_chat_llm(
            model=report_model,
            temperature=0.2,
            timeout=45,
            max_retries=1,
        )
        past_text = _format_past_steps(past_steps)
        resp = await llm.ainvoke([
            {"role": "system", "content": _REPORT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _REPORT_USER_TEMPLATE.format(
                    user_input=user_input,
                    past_steps_text=past_text,
                    draft=draft or "(Replanner 未提供草稿)",
                    current_time=current_time,
                ),
            },
        ])
        content = getattr(resp, "content", str(resp))
        if isinstance(content, list):
            content = "".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content
            )
        text = (content or "").strip()
        if not text:
            logger.warning("[Report] pro 返回空文本, 回退 draft")
            return draft
        logger.info(
            f"[Report] 用 {report_model} 合成最终报告, len={len(text)} (草稿 len={len(draft)})"
        )
        return text
    except Exception as e:
        logger.warning(
            f"[Report] 用 {report_model} 合成报告失败 ({type(e).__name__}: {e}), 回退 draft"
        )
        return draft


def _current_report_time() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _ensure_report_time(report: str, current_time: str) -> str:
    line = f"**生成时间**: {current_time}"
    if re.search(r"^\*\*生成时间\*\*:.*$", report, flags=re.MULTILINE):
        return re.sub(r"^\*\*生成时间\*\*:.*$", line, report, count=1, flags=re.MULTILINE)
    if report.startswith("# 故障诊断报告"):
        return report.replace("# 故障诊断报告", f"# 故障诊断报告\n{line}", 1)
    return f"# 故障诊断报告\n{line}\n\n{report}"


def _build_skill_context(
    selected_skill: str,
    tried_skills: list[TriedSkill],
) -> tuple[str, str, str]:
    """为 Replanner prompt 准备 reroute 相关的上下文文本.

    Returns:
        (current_skill_line, candidate_skills_text, tried_skills_text)
    """
    registry = get_skill_registry()
    tried_names = {ts.get("skill", "") for ts in tried_skills}

    current = registry.get(selected_skill) if selected_skill else None
    if current is not None:
        current_skill_line = f"{current.name} — {current.display_name}\n适用场景: {current.description}"
    else:
        current_skill_line = f"{selected_skill or '(未选中)'}"

    # 候选菜单: 排除当前选中 + 已试过的
    excluded = tried_names | ({selected_skill} if selected_skill else set())
    candidates = [s for s in registry.all() if s.name not in excluded]
    if candidates:
        candidate_skills_text = "\n\n".join(s.to_router_card() for s in candidates)
    else:
        candidate_skills_text = "(无可选候选 Skill, 不允许 reroute)"

    if tried_skills:
        tried_skills_text = "\n".join(
            f"- {ts.get('skill', '?')}: {ts.get('reason', '(无原因)')}"
            for ts in tried_skills
        )
    else:
        tried_skills_text = "(无)"

    return (
        current_skill_line,
        candidate_skills_text,
        tried_skills_text,
    )


def _validate_reroute(
    state: PlanExecuteState,
    act: Act,
) -> tuple[bool, str]:
    """代码层校验 LLM 提议的 reroute 是否合法.

    门槛 (任一不过都拒绝):
      1. should_reroute=true 且 new_skill 非空
      2. past_steps >= agent_reroute_min_past_steps (证据足够)
      3. reroute_count < agent_max_reroutes (名额未用完)
      4. new_skill 不等于当前 selected_skill (防自循环)
      5. new_skill 不在 tried_skills 黑名单 (防回环)
      6. new_skill 在 SkillRegistry 里真实存在

    Returns:
        (allowed, deny_reason)
    """
    if not act.should_reroute or not act.new_skill:
        return False, "LLM 未提议 reroute"

    past_steps = state.get("past_steps", [])
    if len(past_steps) < settings.agent_reroute_min_past_steps:
        return False, (
            f"past_steps={len(past_steps)} < 门槛 {settings.agent_reroute_min_past_steps}, 证据不足"
        )

    reroute_count = state.get("reroute_count", 0)
    if reroute_count >= settings.agent_max_reroutes:
        return False, (
            f"reroute_count={reroute_count} 已达上限 {settings.agent_max_reroutes}"
        )

    selected_skill = state.get("selected_skill", "")
    if act.new_skill == selected_skill:
        return False, f"new_skill ({act.new_skill}) 等于当前 selected_skill"

    tried_names = {ts.get("skill", "") for ts in state.get("tried_skills", [])}
    if act.new_skill in tried_names:
        return False, f"new_skill ({act.new_skill}) 在黑名单里"

    if get_skill_registry().get(act.new_skill) is None:
        return False, f"new_skill ({act.new_skill}) 在 SkillRegistry 中不存在"

    return True, ""


async def replan_node(state: PlanExecuteState) -> PlanExecuteState:
    """Replanner 节点: 决定继续执行下一步, 或终止并给出报告."""
    user_input = state.get("input", "")
    plan = state.get("plan", [])
    past_steps = state.get("past_steps", [])
    iteration = state.get("iteration", 0)
    selected_skill = state.get("selected_skill", "")
    tried_skills = state.get("tried_skills", [])
    reroute_count = state.get("reroute_count", 0)
    current_time = _current_report_time()

    logger.info(
        f"[Replanner] 评估进度: 已执行 {len(past_steps)} 步, "
        f"iteration={iteration}, 剩余 {len(plan) - 1 if plan else 0} 步, "
        f"reroute_count={reroute_count}"
    )

    # ===== 快路径: plan 还有 ≥N 步未执行 + 上一步未失败 → 跳过 LLM =====
    # Replanner 调一次 LLM 通常 1-3s, 中间步多调几次会显著拉慢. 计划本来就有冗余余地,
    # 没必要每步都 re-evaluate; 真正需要 replan 的场景是步骤失败 / 计划快走完.
    fast_path_threshold = settings.agent_replanner_fast_path_threshold
    plan_remaining = max(0, len(plan) - 1)  # plan[0] 是刚执行完的, plan[1:] 才是真正剩余
    if (
        fast_path_threshold > 0
        and plan_remaining >= fast_path_threshold
        and not _last_step_failed(past_steps)
        and iteration < settings.agent_max_steps - 1  # 给 LLM replan 至少留一步空间
    ):
        next_plan = list(plan[1:])
        logger.info(
            f"[Replanner] 快路径: plan 还剩 {plan_remaining} 步, 上一步未失败, 跳过 LLM"
        )
        return {
            "plan": next_plan,
            "transition_history": [
                make_transition(
                    "replanner", REPLANNER_CONTINUE,
                    f"fast_path: 剩余 {len(next_plan)} 步",
                ),
            ],
        }

    # ===== 硬性兜底: 已达最大步数, 强制生成报告 =====
    if iteration >= settings.agent_max_steps:
        logger.warning(
            f"[Replanner] 已达最大步数 {settings.agent_max_steps}, 强制生成报告"
        )
        logger.warning(f"[transition] node=replanner reason={REPLANNER_MAX_STEPS_FORCE} iteration={iteration}")
        return {
            "response": _force_summary(user_input, past_steps, current_time),
            "plan": [],
            "transition_history": [
                make_transition("replanner", REPLANNER_MAX_STEPS_FORCE, f"iteration={iteration}"),
            ],
        }

    # ===== 准备 reroute 相关上下文 =====
    current_skill_line, candidate_skills_text, tried_skills_text = _build_skill_context(
        selected_skill, tried_skills
    )

    # 名额提示 (让 LLM 明确知道还能不能切)
    quota_remaining = max(0, settings.agent_max_reroutes - reroute_count)
    min_steps = settings.agent_reroute_min_past_steps
    if quota_remaining == 0:
        reroute_quota_hint = "⚠ reroute 名额已用完, 不允许再切 Skill, 请走情况 1 或 2."
    elif len(past_steps) < min_steps:
        reroute_quota_hint = (
            f"⚠ 证据不足 (past_steps={len(past_steps)} < {min_steps}), "
            f"还不允许 reroute, 请走情况 1 或 2."
        )
    else:
        reroute_quota_hint = (
            f"可以 reroute (剩余 {quota_remaining} 次), "
            "但仅限当前 Skill 方向明确不成立时."
        )

    # ===== 调用 LLM 决策 =====
    # Replanner 用轻量模型: 决策本身只需要看进度+输出结构化 Plan/Response, 不需要大模型.
    replanner_model = settings.agent_planner_model or settings.dashscope_router_model
    llm = get_chat_llm(model=replanner_model, temperature=0, timeout=30, max_retries=1)

    plan_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(plan)) if plan else "(无)"
    past_text = _format_past_steps(past_steps)

    messages = [
        {"role": "system", "content": REPLANNER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": REPLANNER_USER_TEMPLATE.format(
                input=user_input,
                current_time=current_time,
                current_skill_line=current_skill_line,
                candidate_skills_text=candidate_skills_text,
                tried_skills_text=tried_skills_text,
                reroute_count=reroute_count,
                max_reroutes=settings.agent_max_reroutes,
                reroute_quota_hint=reroute_quota_hint,
                plan_text=plan_text,
                past_steps_text=past_text,
            ),
        },
    ]

    try:
        act = await ainvoke_structured(
            llm=llm,
            schema_cls=Act,
            messages=messages,
            model_name=replanner_model,
        )
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        logger.exception(f"[Replanner] 结构化输出失败, 兜底生成报告: {e}")
        logger.warning(f"[transition] node=replanner reason={REPLANNER_LLM_FAILED} detail={detail}")
        return {
            "response": _force_summary(user_input, past_steps, current_time),
            "plan": [],
            "transition_history": [
                make_transition("replanner", REPLANNER_LLM_FAILED, detail),
            ],
        }

    # ===== Reroute: LLM 提议切换 Skill =====
    if act.should_reroute:
        allowed, deny_reason = _validate_reroute(state, act)
        if allowed:
            new_skill = act.new_skill
            new_reason = act.reroute_reason or "(LLM 未给出原因)"
            tried_entry: TriedSkill = {
                "skill": selected_skill,
                "reason": new_reason,
            }
            logger.info(
                f"[Replanner] 决策: Skill reroute {selected_skill} -> {new_skill}, 原因: {new_reason}"
            )
            logger.info(
                f"[transition] node=replanner reason={REPLANNER_REROUTE} "
                f"from={selected_skill} to={new_skill}"
            )
            return {
                "selected_skill": new_skill,
                "skill_reason": f"reroute: {new_reason}",
                "plan": [],  # 清空旧计划, 回 Planner 重新生成
                "reroute_count": reroute_count + 1,
                "tried_skills": [tried_entry],  # operator.add 追加
                "pending_reroute": True,        # 让 graph 条件边路由回 planner
                "transition_history": [
                    make_transition(
                        "replanner",
                        REPLANNER_REROUTE,
                        f"{selected_skill} -> {new_skill}: {new_reason}",
                    ),
                ],
            }
        # 不合法: 记一条 BLOCKED transition, 继续按 is_finished/plan 逻辑走
        logger.warning(
            f"[Replanner] reroute 被拒: {deny_reason} (LLM 提议 new_skill={act.new_skill!r})"
        )
        logger.warning(f"[transition] node=replanner reason={REPLANNER_REROUTE_BLOCKED} detail={deny_reason}")
        # blocked 记录会与下面的正常出口 transition 一起串联追加 (operator.add)
        blocked_transition = make_transition(
            "replanner",
            REPLANNER_REROUTE_BLOCKED,
            f"new_skill={act.new_skill!r} 被拒: {deny_reason}",
        )
    else:
        blocked_transition = None

    # ===== 终止: LLM 决定生成报告 =====
    if act.is_finished:
        draft = act.response.strip() if act.response else ""
        # 用 report_model (默认 pro) 基于 past_steps 重写一份高质量报告.
        # draft 作为参考, 但 pro 会自己重新组织 5 段结构.
        report = await _synthesize_final_report(
            user_input=user_input,
            past_steps=past_steps,
            current_time=current_time,
            draft=draft,
        )
        if not report:
            logger.warning("[Replanner] pro 合成为空且 draft 为空, 走 _force_summary 兜底")
            logger.warning(f"[transition] node=replanner reason={REPLANNER_FINISHED_EMPTY}")
            report = _force_summary(user_input, past_steps, current_time)
            report = _ensure_report_time(report, current_time)
            return {
                "response": report,
                "plan": [],
                "transition_history": [
                    make_transition("replanner", REPLANNER_FINISHED_EMPTY, "is_finished=True 但 response 为空"),
                ],
            }
        report = _ensure_report_time(report, current_time)
        logger.info(f"[Replanner] 决策: 生成最终报告 (len={len(report)})")
        finished_transition = make_transition(
            "replanner", REPLANNER_FINISHED_OK, f"report_len={len(report)}"
        )
        history = [blocked_transition, finished_transition] if blocked_transition else [finished_transition]
        return {
            "response": report,
            "plan": [],
            "transition_history": history,
        }

    # ===== 继续: LLM 给出新计划 =====
    new_plan = [s for s in (act.plan or []) if s.strip()]
    if not new_plan:
        logger.warning("[Replanner] 标记 not finished 但新计划为空, 兜底生成报告")
        logger.warning(f"[transition] node=replanner reason={REPLANNER_NOT_FINISHED_EMPTY}")
        return {
            "response": _force_summary(user_input, past_steps, current_time),
            "plan": [],
            "transition_history": [
                make_transition("replanner", REPLANNER_NOT_FINISHED_EMPTY, "is_finished=False 但 plan 为空"),
            ],
        }

    logger.info(f"[Replanner] 决策: 继续执行 {len(new_plan)} 步")
    for i, step in enumerate(new_plan, 1):
        logger.info(f"  剩余步骤 {i}: {step}")
    continue_transition = make_transition(
        "replanner", REPLANNER_CONTINUE, f"剩余 {len(new_plan)} 步"
    )
    history = [blocked_transition, continue_transition] if blocked_transition else [continue_transition]
    return {
        "plan": new_plan,
        "transition_history": history,
    }


def _force_summary(
    user_input: str,
    past_steps: list[tuple[str, str]],
    current_time: str,
) -> str:
    """硬兜底: 当 LLM 决策失败或超步数, 用模板生成简单报告."""
    if not past_steps:
        return f"# 故障诊断报告\n**生成时间**: {current_time}\n\n## 问题\n{user_input}\n\n## 结论\n诊断流程异常终止, 未能收集到有效信息, 请人工介入。"

    sections = [
        "# 故障诊断报告\n",
        f"**生成时间**: {current_time}\n",
        f"## 问题\n{user_input}\n",
        "## 收集到的信息\n",
    ]
    for i, (step, result) in enumerate(past_steps, 1):
        snippet = result[:300].replace("\n", " ")
        sections.append(f"**{i}. {step}**\n{snippet}\n")
    sections.append("## 结论\n基于以上信息, 建议进一步人工确认根因和处置方案。")
    return "\n".join(sections)
