import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

from . import state
from .config_runtime import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_RESTART_THRESHOLD,
    TASK_POLL_INTERVAL_SECONDS,
    WS_RESTART_THRESHOLD,
)
from .paths import ensure_runtime_dirs, get_legacy_tasks_file_path, get_tasks_file_path, load_heartbeat_text
from .utils import format_timestamp_ms


HEARTBEAT_REPLY_LEVELS = {
    "HEARTBEAT_NOTICE": "notice",
    "HEARTBEAT_NOTIFY": "notice",
    "HEARTBEAT_INFO": "notice",
    "HEARTBEAT_WARNING": "warning",
    "HEARTBEAT_WARN": "warning",
    "HEARTBEAT_ALERT": "alert",
    "HEARTBEAT_ERROR": "alert",
}

HEARTBEAT_LEVEL_TITLES = {
    "notice": "ℹ️ **HEARTBEAT 通知**",
    "warning": "⚠️ **HEARTBEAT 提醒**",
    "alert": "⚠️ **HEARTBEAT 告警**",
}

HEARTBEAT_REPLY_MARKER_RE = re.compile(
    r"^`?(HEARTBEAT_(?:NOTICE|NOTIFY|INFO|WARNING|WARN|ALERT|ERROR))\b`?\s*(?:(?:[:：]|[-–—])\s*)?(.*)$",
    re.IGNORECASE,
)


def parse_agent_heartbeat_reply(reply: str) -> tuple[str | None, str]:
    text = str(reply or "").strip()
    if not text or text == "HEARTBEAT_OK":
        return None, ""

    first_line, sep, rest = text.partition("\n")
    match = HEARTBEAT_REPLY_MARKER_RE.match(first_line.strip())
    if match:
        level = HEARTBEAT_REPLY_LEVELS[match.group(1).upper()]
        message_parts = []
        inline_message = match.group(2).strip()
        rest_message = rest.strip() if sep else ""
        if inline_message:
            message_parts.append(inline_message)
        if rest_message:
            message_parts.append(rest_message)
        return level, "\n".join(message_parts).strip()
    return "alert", text


def format_heartbeat_notification(level: str, messages: list[str]) -> str:
    title = HEARTBEAT_LEVEL_TITLES.get(level, HEARTBEAT_LEVEL_TITLES["alert"])
    return title + "\n\n" + "\n\n".join(messages)


def save_scheduled_tasks():
    ensure_runtime_dirs()
    with open(get_tasks_file_path(), "w", encoding="utf-8", newline="\n") as f:
        json.dump(state.scheduled_tasks, f, ensure_ascii=False, indent=2)


def load_scheduled_tasks():
    ensure_runtime_dirs()
    tasks_file_path = get_tasks_file_path()
    legacy_tasks_file_path = get_legacy_tasks_file_path()

    try:
        if not os.path.exists(tasks_file_path) and os.path.exists(legacy_tasks_file_path):
            shutil.copyfile(legacy_tasks_file_path, tasks_file_path)
            print(f"[INFO] 已迁移定时任务文件到工作区: {tasks_file_path}")

        with open(tasks_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        state.scheduled_tasks = data if isinstance(data, dict) else {}
    except FileNotFoundError:
        state.scheduled_tasks = {}
    except Exception as e:
        print(f"[WARN] 读取定时任务失败: {e}")
        state.scheduled_tasks = {}


def new_task_id() -> str:
    return f"task_{int(time.time() * 1000)}"


def now_ts() -> int:
    return int(time.time())


def format_task_summary(task: dict) -> str:
    schedule_type = task.get("schedule_type", "interval")
    if schedule_type == "interval":
        schedule_desc = f"每 {task['interval_minutes']} 分钟"
    elif schedule_type == "daily":
        schedule_desc = f"每天 {task['time_of_day']}"
    elif schedule_type == "once":
        schedule_desc = f"一次性 {task['run_at_text']}"
    else:
        schedule_desc = schedule_type
    window = ""
    if task.get("workdays") or task.get("work_time_start"):
        window = (
            f" | 工作窗口：days={task.get('workdays') or 'all'} "
            f"time={task.get('work_time_start') or '00:00'}-{task.get('work_time_end') or '23:59'}"
        )
    status = "paused" if not task.get("enabled", True) else "active"
    return (
        f"- `{task['task_id']}` | {schedule_desc} | {status} | "
        f"下次执行：{format_timestamp_ms(task.get('next_run_at'))} | "
        f"目标：`{task['reply_id']}`{window} | 提示：{task['prompt']}"
    )


def normalize_task_reply_id(reply_id: str | None, chat_id: str | None = None, open_id: str | None = None) -> str:
    candidate = str(reply_id or "").strip()
    chat_value = str(chat_id or "").strip()
    open_value = str(open_id or "").strip()

    if candidate.startswith("oc_") or candidate.startswith("ou_"):
        return candidate
    if candidate.startswith("ot_"):
        if chat_value:
            return chat_value
        raise ValueError("定时任务不能只保存 thread_id，请同时提供 chat_id")
    if candidate.startswith("om_"):
        if chat_value:
            return chat_value
        raise ValueError("定时任务不能使用 message_id 作为目标，请改用 chat_id")

    if chat_value.startswith("oc_"):
        return chat_value
    if open_value.startswith("ou_"):
        return open_value

    if candidate:
        raise ValueError(f"无效的定时任务目标 ID: {candidate}")
    raise ValueError("缺少有效的定时任务目标 ID")


def parse_hhmm(value: str) -> tuple[int, int]:
    try:
        dt = datetime.strptime(value.strip(), "%H:%M")
        return dt.hour, dt.minute
    except Exception:
        raise ValueError("时间格式必须是 HH:MM")


def parse_once_datetime(value: str) -> int:
    try:
        return int(datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").timestamp())
    except Exception:
        raise ValueError("一次性任务时间格式必须是 YYYY-MM-DD HH:MM")


def normalize_workdays(workdays) -> list[int]:
    if workdays in (None, "", []):
        return []
    if isinstance(workdays, str):
        raw = [item.strip() for item in workdays.split(",") if item.strip()]
    else:
        raw = [str(item).strip() for item in workdays if str(item).strip()]
    days = sorted({int(item) for item in raw})
    for day in days:
        if day < 1 or day > 7:
            raise ValueError("workdays 只能是 1-7，1=周一，7=周日")
    return days


def is_in_work_window(task: dict, now_dt: datetime) -> bool:
    workdays = task.get("workdays") or []
    if workdays and now_dt.isoweekday() not in workdays:
        return False
    start = task.get("work_time_start")
    end = task.get("work_time_end")
    if not start and not end:
        return True
    sh, sm = parse_hhmm(start or "00:00")
    eh, em = parse_hhmm(end or "23:59")
    current_minutes = now_dt.hour * 60 + now_dt.minute
    return sh * 60 + sm <= current_minutes <= eh * 60 + em


def next_work_window_start(task: dict, now_dt: datetime) -> datetime:
    workdays = task.get("workdays") or []
    sh, sm = parse_hhmm(task.get("work_time_start") or "00:00")
    for offset in range(0, 8):
        candidate = datetime.combine(now_dt.date() + timedelta(days=offset), datetime.min.time()).replace(hour=sh, minute=sm)
        if candidate < now_dt:
            continue
        if workdays and candidate.isoweekday() not in workdays:
            continue
        return candidate
    return now_dt + timedelta(minutes=1)


def compute_next_run_at(task: dict, now: int | None = None) -> int | None:
    now = now or now_ts()
    now_dt = datetime.fromtimestamp(now)
    schedule_type = task.get("schedule_type", "interval")
    if schedule_type == "interval":
        next_dt = now_dt + timedelta(minutes=int(task["interval_minutes"]))
    elif schedule_type == "daily":
        hour, minute = parse_hhmm(task["time_of_day"])
        next_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_dt <= now_dt:
            next_dt += timedelta(days=1)
    elif schedule_type == "once":
        run_at = int(task["run_at"])
        return run_at if run_at > now else None
    else:
        raise ValueError(f"未知 schedule_type: {schedule_type}")
    if task.get("workdays") or task.get("work_time_start"):
        probe = next_dt
        for _ in range(14):
            if is_in_work_window(task, probe):
                return int(probe.timestamp())
            probe = next_work_window_start(task, probe + timedelta(minutes=1))
        return int(probe.timestamp())
    return int(next_dt.timestamp())


def create_scheduled_task(
    interval_minutes: int | None,
    prompt: str,
    reply_id: str | None,
    created_by: str = "agent",
    schedule_type: str = "interval",
    time_of_day: str | None = None,
    run_at_text: str | None = None,
    chat_id: str | None = None,
    open_id: str | None = None,
    workdays=None,
    work_time_start: str | None = None,
    work_time_end: str | None = None,
) -> dict:
    schedule_type = str(schedule_type or "interval").strip().lower()
    if schedule_type == "interval":
        if interval_minutes is None or int(interval_minutes) <= 0:
            raise ValueError("interval_minutes 必须大于 0")
    elif schedule_type == "daily":
        if not time_of_day:
            raise ValueError("daily 任务必须提供 time_of_day")
        parse_hhmm(time_of_day)
    elif schedule_type == "once":
        if not run_at_text:
            raise ValueError("once 任务必须提供 run_at_text")
        parse_once_datetime(run_at_text)
    else:
        raise ValueError("schedule_type 仅支持 interval / daily / once")

    prompt = str(prompt).strip()
    reply_id = normalize_task_reply_id(reply_id, chat_id=chat_id, open_id=open_id)
    if not prompt:
        raise ValueError("prompt 不能为空")
    if not reply_id:
        raise ValueError("reply_id 不能为空")

    now = now_ts()
    task = {
        "task_id": new_task_id(),
        "schedule_type": schedule_type,
        "interval_minutes": int(interval_minutes) if interval_minutes else None,
        "time_of_day": time_of_day.strip() if isinstance(time_of_day, str) and time_of_day.strip() else None,
        "run_at_text": run_at_text.strip() if isinstance(run_at_text, str) and run_at_text.strip() else None,
        "run_at": parse_once_datetime(run_at_text) if schedule_type == "once" else None,
        "workdays": normalize_workdays(workdays),
        "work_time_start": work_time_start.strip() if isinstance(work_time_start, str) and work_time_start.strip() else None,
        "work_time_end": work_time_end.strip() if isinstance(work_time_end, str) and work_time_end.strip() else None,
        "prompt": prompt,
        "reply_id": reply_id,
        "chat_id": str(chat_id or "").strip() or None,
        "open_id": str(open_id or "").strip() or None,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "next_run_at": None,
        "enabled": True,
        "running": False,
    }
    task["next_run_at"] = compute_next_run_at(task, now)
    with state.tasks_lock:
        state.scheduled_tasks[task["task_id"]] = task
        save_scheduled_tasks()
    return task


def list_scheduled_tasks() -> list[dict]:
    with state.tasks_lock:
        return [dict(task) for task in sorted(state.scheduled_tasks.values(), key=lambda t: (t.get("next_run_at") or 0, t["task_id"]))]


def delete_scheduled_task(task_id: str) -> dict:
    with state.tasks_lock:
        task = state.scheduled_tasks.pop(str(task_id).strip(), None)
        if not task:
            raise ValueError(f"定时任务不存在: {task_id}")
        save_scheduled_tasks()
        return task


def get_scheduled_task(task_id: str) -> dict:
    with state.tasks_lock:
        task = state.scheduled_tasks.get(str(task_id).strip())
        if not task:
            raise ValueError(f"定时任务不存在: {task_id}")
        return dict(task)


def set_task_enabled(task_id: str, enabled: bool) -> dict:
    now = now_ts()
    with state.tasks_lock:
        task = state.scheduled_tasks.get(task_id)
        if not task:
            raise ValueError(f"定时任务不存在: {task_id}")
        task["enabled"] = bool(enabled)
        task["next_run_at"] = compute_next_run_at(task, now) if task["enabled"] else None
        task["updated_at"] = now
        save_scheduled_tasks()
        return dict(task)


def update_task_window(task_id: str, workdays=None, work_time_start: str | None = None, work_time_end: str | None = None) -> dict:
    now = now_ts()
    with state.tasks_lock:
        task = state.scheduled_tasks.get(task_id)
        if not task:
            raise ValueError(f"定时任务不存在: {task_id}")
        task["workdays"] = normalize_workdays(workdays)
        task["work_time_start"] = work_time_start.strip() if isinstance(work_time_start, str) and work_time_start.strip() else None
        task["work_time_end"] = work_time_end.strip() if isinstance(work_time_end, str) and work_time_end.strip() else None
        task["next_run_at"] = compute_next_run_at(task, now) if task.get("enabled", True) else None
        task["updated_at"] = now
        save_scheduled_tasks()
        return dict(task)


def heartbeat_rules_enabled() -> bool:
    content = load_heartbeat_text()
    if not content:
        return False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


def ws_connection_healthy() -> bool:
    ws_client = state.ws_client
    if ws_client is None:
        return False
    conn = getattr(ws_client, "_conn", None)
    if conn is None:
        return False
    closed = getattr(conn, "closed", None)
    return not closed if isinstance(closed, bool) else True


def restart_current_process(reason: str):
    script_path = os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else "bot.py")
    command = [sys.executable, script_path, *sys.argv[1:]]
    print(f"[HEARTBEAT] 准备重启进程: {reason}")
    try:
        subprocess.Popen(command, cwd=os.path.dirname(script_path))
    except Exception as e:
        print(f"[HEARTBEAT] 拉起新进程失败: {e}")
        raise
    time.sleep(2)
    os._exit(1)


def perform_heartbeat_check(send_admin_notification, run_agent_heartbeat_check):
    if not state.heartbeat_lock.acquire(blocking=False):
        return
    try:
        issues = []
        notices = []
        warnings = []
        restart_reason = None

        if state.bot_runtime_thread is None or not state.bot_runtime_thread.is_alive():
            issues.append("- Agent 运行线程异常：主 bot 线程未存活")
            restart_reason = "bot_thread_dead"

        ws_ok = ws_connection_healthy()
        if not ws_ok:
            state.ws_consecutive_failures += 1
            issues.append("- 飞书长连接异常：WebSocket 连接未建立或已断开")
            if state.ws_consecutive_failures >= WS_RESTART_THRESHOLD:
                restart_reason = restart_reason or f"ws_unhealthy_x{state.ws_consecutive_failures}"
        else:
            state.ws_consecutive_failures = 0

        agent_reply = run_agent_heartbeat_check() or "HEARTBEAT_EMPTY"
        agent_level, agent_message = parse_agent_heartbeat_reply(agent_reply)
        if agent_message:
            if agent_level == "notice":
                notices.append(agent_message)
            elif agent_level == "warning":
                warnings.append(agent_message)
            else:
                issues.append(agent_message)

        if notices:
            notify_text = format_heartbeat_notification("notice", notices)
            send_admin_notification(notify_text)
            print(f"[HEARTBEAT] 通知已发送: {notify_text[:120]}")

        if warnings:
            notify_text = format_heartbeat_notification("warning", warnings)
            send_admin_notification(notify_text)
            print(f"[HEARTBEAT] 提醒已发送: {notify_text[:120]}")

        if issues:
            state.heartbeat_consecutive_failures += 1
            notify_text = format_heartbeat_notification("alert", issues)
            send_admin_notification(notify_text)
            print(f"[HEARTBEAT] 告警已发送: {notify_text[:120]}")
            if state.heartbeat_consecutive_failures >= HEARTBEAT_RESTART_THRESHOLD:
                restart_reason = restart_reason or f"heartbeat_failed_x{state.heartbeat_consecutive_failures}"
            if restart_reason:
                restart_notice = (
                    "⚠️ **HEARTBEAT 自动恢复**\n\n"
                    f"- 原因：`{restart_reason}`\n"
                    "- 处理：准备自动重启当前进程"
                )
                send_admin_notification(restart_notice)
                restart_current_process(restart_reason)
        else:
            state.heartbeat_consecutive_failures = 0
            state.last_heartbeat_ok_at = time.time()
            if notices or warnings:
                print("[HEARTBEAT] HEARTBEAT_NOTIFICATION")
            else:
                print("[HEARTBEAT] HEARTBEAT_OK")
    except Exception as e:
        state.heartbeat_consecutive_failures += 1
        message = f"⚠️ **HEARTBEAT 执行失败**\n\n- {e}"
        send_admin_notification(message)
        print(f"[HEARTBEAT] 执行失败: {e}")
        if state.heartbeat_consecutive_failures >= HEARTBEAT_RESTART_THRESHOLD:
            restart_notice = (
                "⚠️ **HEARTBEAT 自动恢复**\n\n"
                f"- 原因：`heartbeat_exception_x{state.heartbeat_consecutive_failures}`\n"
                "- 处理：准备自动重启当前进程"
            )
            send_admin_notification(restart_notice)
            restart_current_process(f"heartbeat_exception_x{state.heartbeat_consecutive_failures}")
    finally:
        state.heartbeat_lock.release()


def start_heartbeat_loop(send_admin_notification, run_agent_heartbeat_check):
    def _loop():
        while True:
            time.sleep(HEARTBEAT_INTERVAL_SECONDS)
            perform_heartbeat_check(send_admin_notification, run_agent_heartbeat_check)

    threading.Thread(target=_loop, daemon=True).start()


def run_scheduled_task(task_id: str, ask_chatgpt, build_agent_system_prompt, send_reply, manual: bool = False):
    try:
        with state.tasks_lock:
            task = state.scheduled_tasks.get(task_id)
            if not task or not task.get("enabled", True) or task.get("running"):
                return
            now = now_ts()
            task["running"] = True
            task["last_run_at"] = now
            if task.get("schedule_type") == "once":
                task["next_run_at"] = None
                task["enabled"] = False
            else:
                task["next_run_at"] = compute_next_run_at(task, now)
            task["updated_at"] = now
            save_scheduled_tasks()
            task = dict(task)

        prompt = (
            f"现在执行一个{('手动触发的' if manual else '自动触发的')}定时任务。\n"
            f"任务 ID：{task['task_id']}\n"
            f"调度类型：{task.get('schedule_type', 'interval')}\n"
            f"目标会话：{task['reply_id']}\n\n"
            f"任务内容：\n{task['prompt']}"
        )
        reply = ask_chatgpt(prompt, build_agent_system_prompt()).strip()
        if reply:
            send_reply(task["reply_id"], f"[定时任务 {task['task_id']}]\n{reply}")
    except Exception as e:
        try:
            send_reply(task["reply_id"], f"[定时任务 {task_id}] 执行失败：{e}")
        except Exception:
            pass
        print(f"[TASK] 执行失败 {task_id}: {e}")
    finally:
        with state.tasks_lock:
            task = state.scheduled_tasks.get(task_id)
            if task:
                task["running"] = False
                if task.get("schedule_type") == "once" and not task.get("enabled", True):
                    task["completed_at"] = now_ts()
                task["updated_at"] = now_ts()
                save_scheduled_tasks()


def start_task_scheduler(ask_chatgpt, build_agent_system_prompt, send_reply):
    load_scheduled_tasks()

    def _loop():
        while True:
            try:
                now = now_ts()
                due_task_ids = []
                with state.tasks_lock:
                    for task_id, task in state.scheduled_tasks.items():
                        if not task.get("enabled", True) or task.get("running"):
                            continue
                        if int(task.get("next_run_at") or 0) <= now:
                            due_task_ids.append(task_id)
                for task_id in due_task_ids:
                    threading.Thread(
                        target=run_scheduled_task,
                        args=(task_id, ask_chatgpt, build_agent_system_prompt, send_reply, False),
                        daemon=True,
                    ).start()
            except Exception as e:
                print(f"[WARN] 定时任务调度异常: {e}")
            time.sleep(TASK_POLL_INTERVAL_SECONDS)

    threading.Thread(target=_loop, daemon=True).start()
