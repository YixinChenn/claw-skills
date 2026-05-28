import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from . import state
from .config_runtime import (
    AGENT_RUNNER_CLAUDE_ARGS,
    AGENT_RUNNER_CLAUDE_BIN,
    AGENT_RUNNER_CODEX_ARGS,
    AGENT_RUNNER_CODEX_BIN,
    AGENT_RUNNER_ALLOWED_SENDERS,
    AGENT_RUNNER_DEFAULT_CWD,
    AGENT_RUNNER_ENABLED,
    AGENT_RUNNER_MAX_CONCURRENT,
    AGENT_RUNNER_TIMEOUT_SECONDS,
)
from .paths import ensure_runtime_dirs, get_agent_workspace, get_runtime_data_dir
from .utils import format_timestamp_ms

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
ACTIVE_STATUSES = {"queued", "running", "cancel_requested"}
AGENT_RUNNER_DENIED_MESSAGE = "你没资格啊，你没资格"


def get_agent_jobs_file_path() -> str:
    return os.path.join(get_runtime_data_dir(), "agent_jobs.json")


def get_agent_job_logs_dir() -> str:
    return os.path.join(get_runtime_data_dir(), "agent_jobs")


def ensure_agent_runner_dirs():
    ensure_runtime_dirs()
    os.makedirs(get_agent_job_logs_dir(), exist_ok=True)


def _safe_id_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip())[:80] or "job"


def new_agent_job_id(agent_name: str) -> str:
    return f"job_{_safe_id_part(agent_name)}_{int(time.time() * 1000)}"


def normalize_sender_ids(sender_ids) -> set[str]:
    if isinstance(sender_ids, str):
        values = [sender_ids]
    else:
        values = list(sender_ids or [])
    return {str(item).strip() for item in values if str(item).strip()}


def is_agent_runner_authorized(sender_ids) -> bool:
    allowed = {str(item).strip() for item in AGENT_RUNNER_ALLOWED_SENDERS if str(item).strip()}
    if not allowed:
        return False
    return bool(normalize_sender_ids(sender_ids) & allowed)


def require_agent_runner_authorized(sender_ids):
    if not is_agent_runner_authorized(sender_ids):
        raise PermissionError(AGENT_RUNNER_DENIED_MESSAGE)


def load_agent_jobs():
    ensure_agent_runner_dirs()
    try:
        with open(get_agent_jobs_file_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        state.agent_jobs = data if isinstance(data, dict) else {}
        changed = False
        now = int(time.time())
        for job in state.agent_jobs.values():
            if job.get("status") in ACTIVE_STATUSES:
                job["status"] = "interrupted"
                job["ended_at"] = job.get("ended_at") or now
                job["error"] = job.get("error") or "机器人进程重启，无法继续跟踪原 Agent 子进程"
                changed = True
        if changed:
            save_agent_jobs()
    except FileNotFoundError:
        state.agent_jobs = {}
    except Exception as e:
        print(f"[AGENT_RUNNER] 读取任务状态失败: {e}")
        state.agent_jobs = {}
    finally:
        state.agent_jobs_loaded = True


def save_agent_jobs():
    ensure_agent_runner_dirs()
    target = get_agent_jobs_file_path()
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", suffix=".tmp", dir=os.path.dirname(target), delete=False) as f:
            temp_path = f.name
            json.dump(state.agent_jobs, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, target)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _ensure_jobs_loaded():
    if not state.agent_jobs_loaded:
        load_agent_jobs()


def _normalize_agent(agent: str) -> str:
    value = str(agent or "").strip().lower()
    aliases = {
        "codex": "codex",
        "openai": "codex",
        "claude": "claude",
        "claude-code": "claude",
    }
    if value not in aliases:
        raise ValueError("agent 仅支持 codex / claude")
    return aliases[value]


def resolve_agent_cwd(cwd: str | None) -> str:
    raw = str(cwd or "").strip()
    if not raw:
        default_cwd = str(AGENT_RUNNER_DEFAULT_CWD or "").strip()
        raw = default_cwd or get_agent_workspace()
    if raw:
        expanded = os.path.expandvars(raw.strip("\"'"))
        resolved = os.path.abspath(expanded if os.path.isabs(expanded) else os.path.join(get_agent_workspace(), expanded))
    else:
        resolved = os.path.abspath(get_agent_workspace())
    if not os.path.isdir(resolved):
        raise NotADirectoryError(f"工作目录不存在: {resolved}")
    return resolved


def _which(name: str) -> str | None:
    if not name:
        return None
    if os.path.isabs(name) or os.path.sep in name or (os.path.altsep and os.path.altsep in name):
        return name if os.path.exists(name) else None
    if os.name == "nt":
        for candidate in [name, f"{name}.cmd", f"{name}.exe", f"{name}.ps1"]:
            path = shutil.which(candidate)
            if path:
                return path
    return shutil.which(name)


def _extract_codex_cli_path_from_config() -> str | None:
    config_candidates = [
        os.environ.get("CODEX_HOME", ""),
        os.path.join(os.path.dirname(get_agent_workspace()), "Codex", "Config"),
        r"D:\Workspace\AI\Codex\Config",
        os.path.join(os.path.expanduser("~"), ".codex"),
    ]
    binary_candidates = [
        os.environ.get("CODEX_CLI_PATH", ""),
        r"D:\Workspace\AI\Codex\Config\.sandbox-bin\codex.exe",
    ]
    for candidate in binary_candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    for config_dir in config_candidates:
        if not config_dir:
            continue
        config_path = os.path.join(config_dir, "config.toml")
        if not os.path.exists(config_path):
            continue
        try:
            with open(config_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            match = re.search(r"CODEX_CLI_PATH\s*=\s*'([^']+)'", text)
            if match and os.path.exists(match.group(1)):
                return match.group(1)
        except Exception:
            continue
    return None


def resolve_agent_binary(agent: str) -> str:
    if agent == "codex":
        configured = _which(AGENT_RUNNER_CODEX_BIN)
        if configured:
            return configured
        extracted = _extract_codex_cli_path_from_config()
        if extracted:
            return extracted
        found = _which("codex")
        if found:
            return found
        raise FileNotFoundError("未找到 Codex CLI，请配置 AGENT_RUNNER_CODEX_BIN")

    configured = _which(AGENT_RUNNER_CLAUDE_BIN)
    if configured:
        return configured
    found = _which("claude.cmd") or _which("claude")
    if found:
        return found
    raise FileNotFoundError("未找到 Claude Code CLI，请配置 AGENT_RUNNER_CLAUDE_BIN")


def _is_last_session(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"", "last", "--last", "latest"}


def build_agent_command(agent: str, cwd: str, prompt: str, conversation_id: str | None = None) -> list[str]:
    binary = resolve_agent_binary(agent)
    if agent == "codex":
        if conversation_id is not None:
            args = ["exec", "resume", "--skip-git-repo-check"]
            if _is_last_session(conversation_id):
                args.append("--last")
            else:
                args.append(str(conversation_id).strip())
            return [binary, *args, prompt]
        args = AGENT_RUNNER_CODEX_ARGS or ["exec", "--skip-git-repo-check", "--color", "never"]
        return [binary, *args, "--cd", cwd, prompt]
    args = AGENT_RUNNER_CLAUDE_ARGS or ["-p", "--permission-mode", "acceptEdits"]
    if conversation_id is not None:
        resume_args = ["--continue"] if _is_last_session(conversation_id) else ["--resume", str(conversation_id).strip()]
        return [binary, *args, *resume_args, prompt]
    return [binary, *args, prompt]


def _command_preview(command: list[str]) -> str:
    if not command:
        return ""
    shown = [command[0], *command[1:-1], "<prompt>"]
    return " ".join(f'"{part}"' if " " in part else part for part in shown)


def _running_job_count() -> int:
    return sum(1 for job in state.agent_jobs.values() if job.get("status") in ACTIVE_STATUSES)


def create_agent_job(agent: str, prompt: str, cwd: str | None, reply_id: str, chat_id: str | None, send_reply, conversation_id: str | None = None) -> dict:
    if not AGENT_RUNNER_ENABLED:
        raise RuntimeError("Agent Runner 未启用")
    normalized_agent = _normalize_agent(agent)
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("任务内容不能为空")
    conversation_id = str(conversation_id).strip() if conversation_id is not None and str(conversation_id).strip() else None
    resolved_cwd = resolve_agent_cwd(cwd)
    command = build_agent_command(normalized_agent, resolved_cwd, prompt, conversation_id)

    with state.agent_jobs_lock:
        _ensure_jobs_loaded()
        max_concurrent = max(1, int(AGENT_RUNNER_MAX_CONCURRENT or 1))
        if _running_job_count() >= max_concurrent:
            raise RuntimeError(f"运行中 Agent Job 已达到并发上限: {max_concurrent}")
        job_id = new_agent_job_id(normalized_agent)
        log_path = os.path.join(get_agent_job_logs_dir(), f"{job_id}.log")
        now = int(time.time())
        job = {
            "job_id": job_id,
            "agent": normalized_agent,
            "mode": "resume" if conversation_id is not None else "new",
            "conversation_id": conversation_id,
            "status": "queued",
            "cwd": resolved_cwd,
            "prompt": prompt,
            "reply_id": reply_id,
            "chat_id": chat_id,
            "log_path": log_path,
            "command_preview": _command_preview(command),
            "created_at": now,
            "started_at": None,
            "ended_at": None,
            "pid": None,
            "exit_code": None,
            "error": "",
        }
        state.agent_jobs[job_id] = job
        save_agent_jobs()

    threading.Thread(target=_run_agent_job, args=(job_id, command, send_reply), daemon=True, name=f"agent-runner-{job_id}").start()
    return dict(job)


def _set_job_fields(job_id: str, **fields) -> dict | None:
    with state.agent_jobs_lock:
        _ensure_jobs_loaded()
        job = state.agent_jobs.get(job_id)
        if not job:
            return None
        job.update(fields)
        save_agent_jobs()
        return dict(job)


def _terminate_process_tree(process, force: bool = False):
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        command = ["taskkill", "/PID", str(process.pid), "/T"]
        if force:
            command.append("/F")
        subprocess.run(command, capture_output=True, text=True, timeout=10)
        return
    try:
        process.kill() if force else process.terminate()
    except Exception:
        pass


def _run_agent_job(job_id: str, command: list[str], send_reply):
    process = None
    timeout_seconds = max(60, int(AGENT_RUNNER_TIMEOUT_SECONDS or 3600))
    try:
        job = _set_job_fields(job_id, status="running", started_at=int(time.time()))
        if not job:
            return
        os.makedirs(os.path.dirname(job["log_path"]), exist_ok=True)
        with open(job["log_path"], "a", encoding="utf-8", newline="\n") as log_file:
            log_file.write(f"[AGENT_RUNNER] start job={job_id} cwd={job['cwd']}\n")
            log_file.write(f"[AGENT_RUNNER] command={job['command_preview']}\n\n")
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process = subprocess.Popen(
                command,
                cwd=job["cwd"],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            with state.agent_jobs_lock:
                state.agent_job_processes[job_id] = process
                job_ref = state.agent_jobs.get(job_id)
                if job_ref is not None:
                    job_ref["pid"] = process.pid
                    save_agent_jobs()
            try:
                exit_code = process.wait(timeout=timeout_seconds)
                with state.agent_jobs_lock:
                    current = state.agent_jobs.get(job_id, {})
                    status = "cancelled" if current.get("status") == "cancel_requested" else ("succeeded" if exit_code == 0 else "failed")
                job = _set_job_fields(job_id, status=status, exit_code=exit_code, ended_at=int(time.time())) or job
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process, force=True)
                exit_code = process.wait()
                job = _set_job_fields(job_id, status="timed_out", exit_code=exit_code, ended_at=int(time.time()), error=f"执行超时（>{timeout_seconds}s）") or job
            log_file.write(f"\n[AGENT_RUNNER] end job={job_id} status={job.get('status')} exit_code={job.get('exit_code')}\n")
    except Exception as e:
        job = _set_job_fields(job_id, status="failed", ended_at=int(time.time()), error=str(e)) or {"job_id": job_id, "reply_id": "", "status": "failed", "error": str(e)}
        try:
            with open(job.get("log_path", os.path.join(get_agent_job_logs_dir(), f"{job_id}.log")), "a", encoding="utf-8", newline="\n") as log_file:
                log_file.write(f"\n[AGENT_RUNNER] error: {e}\n")
        except Exception:
            pass
    finally:
        with state.agent_jobs_lock:
            state.agent_job_processes.pop(job_id, None)
        if job and job.get("reply_id"):
            send_reply(job["reply_id"], format_agent_job_completion(job))


def list_agent_jobs(limit: int = 10) -> list[dict]:
    with state.agent_jobs_lock:
        _ensure_jobs_loaded()
        jobs = sorted(state.agent_jobs.values(), key=lambda item: (item.get("created_at") or 0, item.get("job_id") or ""), reverse=True)
        return [dict(job) for job in jobs[: max(1, min(int(limit or 10), 50))]]


def get_agent_job(job_id: str) -> dict:
    with state.agent_jobs_lock:
        _ensure_jobs_loaded()
        job = state.agent_jobs.get(str(job_id or "").strip())
        if not job:
            raise ValueError(f"Agent Job 不存在: {job_id}")
        return dict(job)


def cancel_agent_job(job_id: str) -> dict:
    job_id = str(job_id or "").strip()
    with state.agent_jobs_lock:
        _ensure_jobs_loaded()
        job = state.agent_jobs.get(job_id)
        if not job:
            raise ValueError(f"Agent Job 不存在: {job_id}")
        if job.get("status") in TERMINAL_STATUSES:
            return dict(job)
        job["status"] = "cancel_requested"
        job["updated_at"] = int(time.time())
        process = state.agent_job_processes.get(job_id)
        save_agent_jobs()
    if process is not None:
        _terminate_process_tree(process, force=True)
    return get_agent_job(job_id)


def tail_agent_job_log(job_id: str, line_count: int = 80) -> str:
    job = get_agent_job(job_id)
    path = job.get("log_path", "")
    if not path or not os.path.exists(path):
        return "日志尚未生成。"
    line_count = max(1, min(int(line_count or 80), 300))
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-line_count:]).strip() or "日志为空。"


def format_agent_job_summary(job: dict) -> str:
    conversation = f" | 会话：`{job.get('conversation_id')}`" if job.get("conversation_id") else ""
    return (
        f"- `{job['job_id']}` | {job.get('agent')} | {job.get('mode', 'new')} | {job.get('status')}{conversation} | "
        f"创建：{format_timestamp_ms((job.get('created_at') or 0) * 1000)} | "
        f"目录：`{job.get('cwd')}` | 提示：{str(job.get('prompt') or '')[:80]}"
    )


def format_agent_job_detail(job: dict) -> str:
    lines = [
        f"任务：`{job['job_id']}`",
        f"Agent：`{job.get('agent')}`",
        f"模式：`{job.get('mode', 'new')}`",
        f"会话：`{job.get('conversation_id') or ''}`",
        f"状态：`{job.get('status')}`",
        f"退出码：`{job.get('exit_code')}`",
        f"目录：`{job.get('cwd')}`",
        f"日志：`{job.get('log_path')}`",
        f"创建：{format_timestamp_ms((job.get('created_at') or 0) * 1000)}",
    ]
    if job.get("started_at"):
        lines.append(f"开始：{format_timestamp_ms(job['started_at'] * 1000)}")
    if job.get("ended_at"):
        lines.append(f"结束：{format_timestamp_ms(job['ended_at'] * 1000)}")
    if job.get("error"):
        lines.append(f"错误：{job['error']}")
    lines.extend(["", "提示：", str(job.get("prompt") or "")])
    return "\n".join(lines)


def format_agent_job_completion(job: dict) -> str:
    status = job.get("status")
    title = "Agent Job 完成" if status == "succeeded" else "Agent Job 结束"
    lines = [
        f"[{title}]",
        f"任务：`{job.get('job_id')}`",
        f"Agent：`{job.get('agent')}`",
        f"模式：`{job.get('mode', 'new')}`",
        f"会话：`{job.get('conversation_id') or ''}`",
        f"状态：`{status}`",
        f"退出码：`{job.get('exit_code')}`",
        f"目录：`{job.get('cwd')}`",
        f"日志：`{job.get('log_path')}`",
    ]
    if job.get("error"):
        lines.append(f"错误：{job['error']}")
    try:
        tail = tail_agent_job_log(job["job_id"], 40)
        if tail:
            lines.extend(["", "最近日志：", tail[-3000:]])
    except Exception:
        pass
    return "\n".join(lines)
