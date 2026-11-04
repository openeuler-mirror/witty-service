"""内置定时任务模板（中文规范定义 + 英文覆盖）。

模板是**可本地化的内容**，而不是接口层的展示元数据，因此定义在领域层：
- ``BUILTIN_TEMPLATES`` 以中文为规范来源，调度配置与 ``key`` 与语言无关；
- ``_EN_TEXTS`` 只覆盖面向用户的 ``name`` / ``description`` / ``content``；
- 缺少英文翻译时回退中文，永不返回空文案。
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ScheduledTaskTemplate:
    """内置定时任务模板，仅作新建参考。"""

    key: str
    name: str
    description: str
    schedule_type: str
    cron_expr: str | None
    interval_seconds: int | None
    timezone: str
    content: str
    workspace_folder: str | None


BUILTIN_TEMPLATES: tuple[ScheduledTaskTemplate, ...] = (
    ScheduledTaskTemplate(
        key="unattended_ops_inspection",
        name="无人值守系统巡检",
        description=(
            "每天定时按资源、磁盘、进程、服务、日志、安全分层巡检 Linux 主机，"
            "输出带状态分级的巡检日报到工作区；巡检项、阈值、触发时间与执行智能体均可按需调整。"
        ),
        schedule_type="cron",
        cron_expr="0 7 * * *",
        interval_seconds=None,
        timezone="Asia/Shanghai",
        content=(
            "请以 Linux 系统管理员的身份执行一次无人值守巡检，按分层巡检项采集现状、"
            "识别异常，并输出巡检日报：\n"
            "1. 系统概况：`hostname`、`uname -r`、`uptime -p`、`timedatectl`（校时是否正常）；\n"
            "2. CPU 与负载：`nproc`、`uptime`、`top -bn1 | head -15`，"
            "比较 load average 与 CPU 核数，关注负载是否长期高于核数；\n"
            "3. 内存与交换：`free -h`，关注 available 内存与 swap 占用；\n"
            "4. 磁盘与文件系统：`df -h`（空间）、`df -i`（inode），"
            "并检查是否有分区被只读挂载；\n"
            "5. 内核与硬件异常：`dmesg -T | tail -100`，检索 OOM-killer、I/O error、MCE 等关键字；\n"
            "6. 进程健康：`ps -eo pid,ppid,stat,pcpu,pmem,comm --sort=-pcpu | head -10`"
            "（内存占用同理按 `-pmem` 排序），检查是否有僵尸进程（STAT 含 Z）或异常高占用进程；\n"
            "7. 服务与端口：`systemctl --failed` 查看失败单元，`ss -tulnp` 核对关键服务端口是否在监听；\n"
            "8. 日志与安全：`journalctl -p err --since '24 hours ago' --no-pager` 汇总错误日志，"
            "`lastb | head` 与 `last` 查看近期登录失败与异常来源。\n"
            "巡检一律使用只读命令，不改变系统状态；命令或工具缺失时跳过并注明。\n"
            "对每项给出状态判定：❌异常 / ⚠️警告 / ✅正常。参考基线（可按本机环境调整）："
            "负载持续高于核数、内存/inode/磁盘使用率 > 80%、存在失败服务或只读挂载、"
            "出现 OOM/I/O 错误或错误日志，均判为异常或警告。\n"
            "最后生成 Markdown 巡检日报写入工作区 `ops-reports` 目录（文件名含日期），"
            "结构包含：巡检概况（主机/内核/时间/时区）、分项结果与状态、异常明细、"
            "按优先级排序的处理建议；若目录中已有上次报告，补充关键指标的环比变化。\n"
            "可按实际环境增删巡检项、调整阈值、触发时间（间隔 / 每天 / 每周 / 自定义 cron）"
            "与时区，并更换执行智能体。"
        ),
        workspace_folder="ops-reports",
    ),
    ScheduledTaskTemplate(
        key="daily_ai_news",
        name="每日 AI 新闻推送",
        description="每天汇总 AI 领域的重要动态，侧重 AI coding 与具身智能进展。",
        schedule_type="cron",
        cron_expr="0 9 * * *",
        interval_seconds=None,
        timezone="Asia/Shanghai",
        content=(
            "请推送今天的 AI 领域重要动态：关注 AI coding 与具身智能方向，"
            "筛选 3-5 条值得关注的信息，逐条给出要点摘要与来源链接，"
            "并说明每条动态对我的实际影响。"
        ),
        workspace_folder=None,
    ),
    ScheduledTaskTemplate(
        key="daily_repo_digest",
        name="每日代码仓库巡检",
        description="工作日上午汇总我参与仓库的 PR 与 Issue 进展，输出待关注事项。",
        schedule_type="cron",
        cron_expr="0 10 * * mon-fri",
        interval_seconds=None,
        timezone="Asia/Shanghai",
        content=(
            "请巡检我参与的代码仓库：汇总最近的 Pull Request 与 Issue 进展，"
            "输出关键变更、待处理事项以及需要我关注的风险点，按优先级排序。"
        ),
        workspace_folder=None,
    ),
    ScheduledTaskTemplate(
        key="weekly_work_report",
        name="每周工作周报",
        description="每周五汇总本周工作进展与下周计划，输出结构化周报。",
        schedule_type="cron",
        cron_expr="0 18 * * fri",
        interval_seconds=None,
        timezone="Asia/Shanghai",
        content=(
            "请生成本周工作周报：汇总本周已完成事项、进行中事项、遇到的问题，"
            "并给出下周计划，按「完成 / 进行中 / 风险 / 下周计划」结构化输出。"
        ),
        workspace_folder=None,
    ),
    ScheduledTaskTemplate(
        key="weekly_dependency_check",
        name="每周依赖与安全更新检查",
        description="每周一检查项目依赖的版本更新与已知安全问题，给出升级建议。",
        schedule_type="cron",
        cron_expr="0 9 * * mon",
        interval_seconds=None,
        timezone="Asia/Shanghai",
        content=(
            "请检查项目主要依赖：列出有更新的依赖版本与已知安全问题，"
            "给出升级建议以及每项升级的影响面评估，标注需要优先处理的高风险项。"
        ),
        workspace_folder=None,
    ),
    ScheduledTaskTemplate(
        key="interview_prep_reminder",
        name="面试准备提醒",
        description="工作日上午每 2 小时提醒复习大模型与编程知识，并生成模拟面试题。",
        schedule_type="cron",
        cron_expr="0 9-18/2 * * mon-fri",
        interval_seconds=None,
        timezone="Asia/Shanghai",
        content=(
            "请提醒我进行面试准备：复习大模型与编程的高频考点，"
            "并生成 3 个模拟面试问题，附上回答要点与常见追问。"
        ),
        workspace_folder=None,
    ),
    ScheduledTaskTemplate(
        key="meeting_prep_reminder",
        name="会议前准备",
        description="每个工作日早上提醒整理当天会议的议题、目标与待确认问题。",
        schedule_type="cron",
        cron_expr="30 9 * * mon-fri",
        interval_seconds=None,
        timezone="Asia/Shanghai",
        content=(
            "请帮我做会议前准备：整理今天会议的议题、目标、待确认问题和关键结论，"
            "输出一份便于会前快速过一遍的准备清单。"
        ),
        workspace_folder=None,
    ),
    ScheduledTaskTemplate(
        key="focus_break_reminder",
        name="专注与休息提醒",
        description="每 2 小时提醒暂存当前进度、短暂休息并明确下一步行动。",
        schedule_type="interval",
        cron_expr=None,
        interval_seconds=7200,
        timezone="Asia/Shanghai",
        content=(
            "请提醒我暂存当前任务进度、起身短暂休息，并帮我列出返回工作后的"
            "下一步行动，让我能快速回到专注状态。"
        ),
        workspace_folder=None,
    ),
)


# 内置模板的英文文案：key -> (name, description, content)。
# 仅覆盖面向用户展示/预填的内容；调度配置与 key 不随语言变化。
_EN_TEXTS: dict[str, tuple[str, str, str]] = {
    "unattended_ops_inspection": (
        "Unattended System Inspection",
        (
            "Run a scheduled, layered inspection of Linux hosts covering resources, "
            "disks, processes, services, logs, and security, and write a "
            "severity-graded daily report to the workspace. Inspection items, "
            "thresholds, trigger time, and the executing agent are all configurable."
        ),
        (
            "Act as a Linux system administrator and perform an unattended inspection. "
            "Collect the current state by layered inspection items, identify anomalies, "
            "and produce a daily inspection report:\n"
            "1. System overview: `hostname`, `uname -r`, `uptime -p`, `timedatectl` "
            "(whether time sync is healthy);\n"
            "2. CPU and load: `nproc`, `uptime`, `top -bn1 | head -15`; compare load "
            "average with the CPU count and flag sustained load above the core count;\n"
            "3. Memory and swap: `free -h`; focus on available memory and swap usage;\n"
            "4. Disks and filesystems: `df -h` (space), `df -i` (inodes), and check "
            "whether any partition is mounted read-only;\n"
            "5. Kernel and hardware anomalies: `dmesg -T | tail -100`; search for "
            "OOM-killer, I/O error, MCE, and similar keywords;\n"
            "6. Process health: `ps -eo pid,ppid,stat,pcpu,pmem,comm --sort=-pcpu | "
            "head -10` (sort by `-pmem` for memory usage); check for zombie processes "
            "(STAT contains Z) or processes with abnormally high usage;\n"
            "7. Services and ports: `systemctl --failed` for failed units; `ss -tulnp` "
            "to verify that key service ports are listening;\n"
            "8. Logs and security: `journalctl -p err --since '24 hours ago' "
            "--no-pager` to summarize error logs; `lastb | head` and `last` to review "
            "recent failed logins and unusual sources.\n"
            "Use read-only commands only and do not change system state. Skip missing "
            "commands or tools and note them.\n"
            "Assign a status to each item: ❌ abnormal / ⚠️ warning / ✅ normal. "
            "Reference baseline (adjust to the local environment): sustained load above "
            "the core count, memory/inode/disk usage above 80%, failed services or "
            "read-only mounts, OOM/I/O errors, or error logs are all classified as "
            "abnormal or warning.\n"
            "Finally, generate a Markdown daily inspection report in the workspace "
            "`ops-reports` directory (filename includes the date). The structure must "
            "include: inspection overview (host/kernel/time/timezone), per-item results "
            "and status, anomaly details, and prioritized remediation recommendations. "
            "If a previous report exists in the directory, include period-over-period "
            "changes for key metrics.\n"
            "Inspection items, thresholds, trigger time (interval / daily / weekly / "
            "custom cron), and timezone can be adjusted to the actual environment, and "
            "the executing agent can be changed."
        ),
    ),
    "daily_ai_news": (
        "Daily AI News Digest",
        "Summarize important developments in AI every day, focusing on AI coding and embodied intelligence.",
        (
            "Share today's important developments in AI: focus on AI coding and "
            "embodied intelligence, select 3-5 noteworthy items, provide a key-point "
            "summary and source link for each, and explain the practical impact of each "
            "item on me."
        ),
    ),
    "daily_repo_digest": (
        "Daily Repository Inspection",
        (
            "On weekday mornings, summarize PR and issue progress in the repositories "
            "I contribute to and output items requiring attention."
        ),
        (
            "Inspect the repositories I contribute to: summarize recent pull request "
            "and issue progress, output key changes, pending items, and risk points I "
            "should watch for, ordered by priority."
        ),
    ),
    "weekly_work_report": (
        "Weekly Work Report",
        (
            "Every Friday, summarize this week's progress and next week's plan in a "
            "structured report."
        ),
        (
            "Generate this week's work report: summarize completed items, in-progress "
            "items, and problems encountered, then provide next week's plan in a "
            "structured format: completed / in progress / risks / next week's plan."
        ),
    ),
    "weekly_dependency_check": (
        "Weekly Dependency and Security Update Check",
        (
            "Every Monday, check project dependencies for version updates and known "
            "security issues and provide upgrade recommendations."
        ),
        (
            "Check the project's main dependencies: list dependencies with available "
            "updates and known security issues, provide upgrade recommendations and an "
            "impact assessment for each upgrade, and flag high-risk items that need "
            "priority handling."
        ),
    ),
    "interview_prep_reminder": (
        "Interview Preparation Reminder",
        (
            "Every 2 hours on weekday mornings, remind me to review LLM and "
            "programming knowledge and generate mock interview questions."
        ),
        (
            "Remind me to prepare for interviews: review high-frequency LLM and "
            "programming topics, and generate 3 mock interview questions with key "
            "talking points and common follow-up questions."
        ),
    ),
    "meeting_prep_reminder": (
        "Meeting Preparation",
        (
            "Every weekday morning, remind me to organize the topics, goals, and open "
            "questions for the day's meetings."
        ),
        (
            "Help me prepare for meetings: organize today's meeting topics, goals, "
            "open questions, and key conclusions, and output a checklist that is quick "
            "to review before the meeting."
        ),
    ),
    "focus_break_reminder": (
        "Focus and Break Reminder",
        (
            "Every 2 hours, remind me to save progress, take a short break, and "
            "clarify the next action."
        ),
        (
            "Remind me to save the current task progress, stand up for a short break, "
            "and list the next action for when I return to work so I can get back into "
            "focus quickly."
        ),
    ),
}


def localize_scheduled_template(
    template: ScheduledTaskTemplate, locale: str
) -> ScheduledTaskTemplate:
    """按语言返回模板副本；仅 en-US 替换文案，其余语言原样返回。"""
    if locale != "en-US":
        return template
    texts = _EN_TEXTS.get(template.key)
    if texts is None:
        return template
    name, description, content = texts
    return replace(
        template,
        name=name,
        description=description,
        content=content,
    )
