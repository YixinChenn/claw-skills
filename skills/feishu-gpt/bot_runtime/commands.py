import json
import threading

from . import state
from .agent import ask_chatgpt
from .agent_runner import (
    cancel_agent_job,
    create_agent_job,
    format_agent_job_detail,
    format_agent_job_summary,
    get_agent_job,
    AGENT_RUNNER_DENIED_MESSAGE,
    list_agent_jobs,
    require_agent_runner_authorized,
    tail_agent_job_log,
)
from .config_runtime import CONFIG_SOURCE_NAME, OPENAI_BASE_URL, OPENAI_MODEL
from .messaging import send_card, send_reply
from .paths import build_agent_system_prompt, get_agent_workspace, get_agents_file_path, get_tool_call_log_path
from .scheduler import (
    create_scheduled_task,
    delete_scheduled_task,
    format_task_summary,
    get_scheduled_task,
    list_scheduled_tasks,
    run_scheduled_task,
    set_task_enabled,
    update_task_window,
)

COMMANDS = {
    "/help": "显示可用指令列表",
    "/clear": "清除当前会话的对话历史",
    "/history": "查看当前上下文保留轮数和工作区",
    "/model": "查看配置模型和最近一次 API 响应模型",
    "/toollog": "查看最近工具调用日志：/toollog [条数，默认 20，最大 100]",
    "/task-add": "创建定时任务：/task-add <分钟> <任务内容>",
    "/task-add-daily": "创建每日任务：/task-add-daily <HH:MM> <任务内容>",
    "/task-add-once": "创建一次性任务：/task-add-once <YYYY-MM-DD HH:MM> <任务内容>",
    "/task-list": "查看当前定时任务",
    "/task-del": "删除定时任务：/task-del <task_id>",
    "/task-run": "立即执行定时任务：/task-run <task_id>",
    "/task-pause": "暂停定时任务：/task-pause <task_id>",
    "/task-resume": "恢复定时任务：/task-resume <task_id>",
    "/task-window": "设置工作时间：/task-window <task_id> <days> <HH:MM-HH:MM>",
    "/agent-run": "启动 Codex/Claude：/agent-run <codex|claude> [--cwd <目录> --] <任务>",
    "/agent-resume": "继续 Codex/Claude 会话：/agent-resume <codex|claude> <会话ID|last> [--cwd <目录> --] <任务>",
    "/agent-status": "查看 Agent Job：/agent-status [job_id]",
    "/agent-tail": "查看 Agent Job 日志：/agent-tail <job_id> [行数]",
    "/agent-cancel": "取消 Agent Job：/agent-cancel <job_id>",
}


def _tail_tool_logs(limit: int) -> list[dict]:
    path = get_tool_call_log_path()
    with open(path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    items = []
    for line in lines[-limit:]:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            items.append({"event": "invalid_json", "raw": line[:500]})
    return items


def _format_tool_log_entry(entry: dict) -> str:
    event = entry.get("event", "unknown")
    trace_id = entry.get("trace_id", "-")
    round_value = entry.get("round")
    tool_name = entry.get("tool_name")
    ok = entry.get("ok")

    parts = [f"- `{event}`", f"trace=`{trace_id}`"]
    if round_value is not None:
        parts.append(f"round=`{round_value}`")
    if tool_name:
        parts.append(f"tool=`{tool_name}`")
    if ok is not None:
        parts.append(f"ok=`{ok}`")

    preview = (
        entry.get("reason")
        or entry.get("output_preview")
        or entry.get("result_preview")
        or entry.get("arguments_preview")
        or entry.get("prompt_preview")
        or entry.get("raw")
        or ""
    )
    preview_text = str(preview).replace("\r", " ").replace("\n", " ").strip()
    if preview_text:
        if len(preview_text) > 160:
            preview_text = preview_text[:160] + "..."
        parts.append(preview_text)
    return " | ".join(parts)


def format_model_status() -> str:
    lines = [
        f"🤖 配置请求模型：`{OPENAI_MODEL}`",
        f"🔌 配置来源：`{CONFIG_SOURCE_NAME}`",
    ]
    if OPENAI_BASE_URL:
        lines.append(f"🌐 Base URL：`{OPENAI_BASE_URL}`")
    lines.append(f"📡 最近 API 响应模型：`{state.last_response_model or '尚无'}`")
    return "\n".join(lines)


def _strip_outer_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1].strip()
    return text


def _parse_agent_run_args(raw: str) -> tuple[str, str | None, str]:
    parts = str(raw or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("用法：`/agent-run <codex|claude> [--cwd <目录> --] <任务>`")
    agent, rest = parts[0], parts[1].strip()
    cwd = None
    if rest.startswith("--cwd "):
        marker = " -- "
        marker_index = rest.find(marker)
        if marker_index < 0:
            raise ValueError("使用 --cwd 时必须写成：`/agent-run codex --cwd <目录> -- <任务>`")
        cwd = _strip_outer_quotes(rest[len("--cwd "):marker_index])
        task = rest[marker_index + len(marker):].strip()
    else:
        task = rest
    if not task:
        raise ValueError("任务内容不能为空")
    return agent, cwd, task


def _parse_agent_resume_args(raw: str) -> tuple[str, str, str | None, str]:
    parts = str(raw or "").strip().split(maxsplit=2)
    if len(parts) != 3:
        raise ValueError("用法：`/agent-resume <codex|claude> <会话ID|last> [--cwd <目录> --] <任务>`")
    agent, conversation_id, rest = parts[0], parts[1], parts[2].strip()
    cwd = None
    if rest.startswith("--cwd "):
        marker = " -- "
        marker_index = rest.find(marker)
        if marker_index < 0:
            raise ValueError("使用 --cwd 时必须写成：`/agent-resume codex <会话ID> --cwd <目录> -- <任务>`")
        cwd = _strip_outer_quotes(rest[len("--cwd "):marker_index])
        task = rest[marker_index + len(marker):].strip()
    else:
        task = rest
    if not task:
        raise ValueError("任务内容不能为空")
    return agent, conversation_id, cwd, task


def _authorize_agent_runner_or_reply(reply_id: str, sender_ids) -> bool:
    try:
        require_agent_runner_authorized(sender_ids)
        return True
    except PermissionError:
        send_card(reply_id, AGENT_RUNNER_DENIED_MESSAGE)
        return False


def handle_command(chat_id: str, text: str, reply_id: str, sender_ids=None) -> bool:
    cmd = text.strip()

    if cmd == "/help":
        lines = ["**可用指令：**\n"]
        for command, desc in COMMANDS.items():
            lines.append(f"- `{command}` — {desc}")
        send_card(reply_id, "\n".join(lines))
        return True

    if cmd == "/clear":
        state.conversations.pop(chat_id, None)
        send_card(reply_id, "✅ 已清除当前会话的对话历史")
        return True

    if cmd == "/history":
        history = state.conversations.get(chat_id, [])
        non_summary = [turn for turn in history if turn["role"] != "summary"]
        has_summary = any(turn["role"] == "summary" for turn in history)
        turns = len(non_summary) // 2
        message = f"📊 当前保留 **{turns}** 轮完整对话"
        if has_summary:
            message += "（另有更早对话已压缩为摘要）"
        message += f"\n🤖 当前模型：`{OPENAI_MODEL}`"
        message += f"\n📡 最近 API 响应模型：`{state.last_response_model or '尚无'}`"
        message += "\n🧩 Shell 工具：`enabled`"
        message += f"\n📁 工作区：`{get_agent_workspace()}`"
        message += f"\n📄 AGENTS 文件：`{get_agents_file_path()}`"
        send_card(reply_id, message)
        return True

    if cmd == "/model":
        send_card(reply_id, format_model_status())
        return True

    if cmd.startswith("/toollog"):
        parts = cmd.split(maxsplit=1)
        limit = 20
        if len(parts) == 2:
            try:
                limit = max(1, min(int(parts[1].strip()), 100))
            except ValueError:
                send_card(reply_id, "用法：`/toollog [条数]`，条数范围 1-100")
                return True
        try:
            entries = _tail_tool_logs(limit)
            if not entries:
                send_card(reply_id, "当前没有工具调用日志。")
                return True
            lines = [f"**最近 {len(entries)} 条工具调用日志**", f"日志文件：`{get_tool_call_log_path()}`", ""]
            lines.extend(_format_tool_log_entry(entry) for entry in entries)
            send_reply(reply_id, "\n".join(lines))
        except FileNotFoundError:
            send_card(reply_id, "工具调用日志文件尚未生成。")
        except Exception as e:
            send_card(reply_id, f"读取工具调用日志失败：{e}")
        return True

    if cmd == "/task-list":
        tasks = list_scheduled_tasks()
        if not tasks:
            send_card(reply_id, "当前没有定时任务。")
            return True
        send_card(reply_id, "**定时任务列表**\n\n" + "\n".join(format_task_summary(task) for task in tasks))
        return True

    if cmd.startswith("/task-add "):
        parts = cmd[len("/task-add "):].strip().split(maxsplit=1)
        if len(parts) != 2:
            send_card(reply_id, "用法：`/task-add <分钟> <任务内容>`")
            return True
        try:
            task = create_scheduled_task(int(parts[0]), parts[1], chat_id, created_by="command", chat_id=chat_id)
            send_card(reply_id, f"已创建定时任务：\n{format_task_summary(task)}")
        except Exception as e:
            send_card(reply_id, f"创建定时任务失败：{e}")
        return True

    if cmd.startswith("/task-add-daily "):
        parts = cmd[len("/task-add-daily "):].strip().split(maxsplit=1)
        if len(parts) != 2:
            send_card(reply_id, "用法：`/task-add-daily <HH:MM> <任务内容>`")
            return True
        try:
            task = create_scheduled_task(None, parts[1], chat_id, created_by="command", schedule_type="daily", time_of_day=parts[0], chat_id=chat_id)
            send_card(reply_id, f"已创建每日任务：\n{format_task_summary(task)}")
        except Exception as e:
            send_card(reply_id, f"创建每日任务失败：{e}")
        return True

    if cmd.startswith("/task-add-once "):
        parts = cmd[len("/task-add-once "):].strip().split(maxsplit=2)
        if len(parts) != 3:
            send_card(reply_id, "用法：`/task-add-once <YYYY-MM-DD> <HH:MM> <任务内容>`")
            return True
        try:
            task = create_scheduled_task(
                None,
                parts[2],
                chat_id,
                created_by="command",
                schedule_type="once",
                run_at_text=f"{parts[0]} {parts[1]}",
                chat_id=chat_id,
            )
            send_card(reply_id, f"已创建一次性任务：\n{format_task_summary(task)}")
        except Exception as e:
            send_card(reply_id, f"创建一次性任务失败：{e}")
        return True

    if cmd.startswith("/task-del "):
        try:
            task = delete_scheduled_task(cmd[len("/task-del "):].strip())
            send_card(reply_id, f"已删除定时任务：`{task['task_id']}`")
        except Exception as e:
            send_card(reply_id, f"删除定时任务失败：{e}")
        return True

    if cmd.startswith("/task-run "):
        try:
            task = get_scheduled_task(cmd[len("/task-run "):].strip())
            threading.Thread(
                target=run_scheduled_task,
                args=(task["task_id"], ask_chatgpt, build_agent_system_prompt, send_reply, True),
                daemon=True,
            ).start()
            send_card(reply_id, f"已触发立即执行：`{task['task_id']}`")
        except Exception as e:
            send_card(reply_id, f"执行定时任务失败：{e}")
        return True

    if cmd.startswith("/task-pause "):
        try:
            task = set_task_enabled(cmd[len("/task-pause "):].strip(), False)
            send_card(reply_id, f"已暂停定时任务：\n{format_task_summary(task)}")
        except Exception as e:
            send_card(reply_id, f"暂停定时任务失败：{e}")
        return True

    if cmd.startswith("/task-resume "):
        try:
            task = set_task_enabled(cmd[len("/task-resume "):].strip(), True)
            send_card(reply_id, f"已恢复定时任务：\n{format_task_summary(task)}")
        except Exception as e:
            send_card(reply_id, f"恢复定时任务失败：{e}")
        return True

    if cmd.startswith("/task-window "):
        parts = cmd[len("/task-window "):].strip().split(maxsplit=2)
        if len(parts) != 3 or "-" not in parts[2]:
            send_card(reply_id, "用法：`/task-window <task_id> <days> <HH:MM-HH:MM>`，如 `/task-window task_xxx 1,2,3,4,5 09:00-18:00`")
            return True
        try:
            start_text, end_text = parts[2].split("-", 1)
            workdays = [] if parts[1].lower() == "all" else [int(item) for item in parts[1].split(",") if item.strip()]
            task = update_task_window(parts[0], workdays=workdays, work_time_start=start_text, work_time_end=end_text)
            send_card(reply_id, f"已更新工作时间窗口：\n{format_task_summary(task)}")
        except Exception as e:
            send_card(reply_id, f"设置工作时间窗口失败：{e}")
        return True

    if cmd.startswith("/agent-run "):
        if not _authorize_agent_runner_or_reply(reply_id, sender_ids):
            return True
        try:
            agent, cwd, task = _parse_agent_run_args(cmd[len("/agent-run "):])
            job = create_agent_job(agent, task, cwd, reply_id, chat_id, send_reply)
            send_card(reply_id, "已启动 Agent Job：\n" + format_agent_job_detail(job))
        except Exception as e:
            send_card(reply_id, f"启动 Agent Job 失败：{e}")
        return True

    if cmd.startswith("/agent-resume "):
        if not _authorize_agent_runner_or_reply(reply_id, sender_ids):
            return True
        try:
            agent, conversation_id, cwd, task = _parse_agent_resume_args(cmd[len("/agent-resume "):])
            job = create_agent_job(agent, task, cwd, reply_id, chat_id, send_reply, conversation_id=conversation_id)
            send_card(reply_id, "已启动续聊 Agent Job：\n" + format_agent_job_detail(job))
        except Exception as e:
            send_card(reply_id, f"启动续聊 Agent Job 失败：{e}")
        return True

    if cmd == "/agent-status":
        jobs = list_agent_jobs(10)
        if not jobs:
            send_card(reply_id, "当前没有 Agent Job。")
            return True
        send_card(reply_id, "**最近 Agent Job**\n\n" + "\n".join(format_agent_job_summary(job) for job in jobs))
        return True

    if cmd.startswith("/agent-status "):
        try:
            job = get_agent_job(cmd[len("/agent-status "):].strip())
            send_card(reply_id, format_agent_job_detail(job))
        except Exception as e:
            send_card(reply_id, f"查看 Agent Job 失败：{e}")
        return True

    if cmd.startswith("/agent-tail "):
        parts = cmd[len("/agent-tail "):].strip().split(maxsplit=1)
        if not parts:
            send_card(reply_id, "用法：`/agent-tail <job_id> [行数]`")
            return True
        try:
            line_count = int(parts[1]) if len(parts) > 1 else 80
            send_reply(reply_id, tail_agent_job_log(parts[0], line_count))
        except Exception as e:
            send_card(reply_id, f"读取 Agent Job 日志失败：{e}")
        return True

    if cmd.startswith("/agent-cancel "):
        if not _authorize_agent_runner_or_reply(reply_id, sender_ids):
            return True
        try:
            job = cancel_agent_job(cmd[len("/agent-cancel "):].strip())
            send_card(reply_id, "已请求取消 Agent Job：\n" + format_agent_job_detail(job))
        except Exception as e:
            send_card(reply_id, f"取消 Agent Job 失败：{e}")
        return True

    return False
