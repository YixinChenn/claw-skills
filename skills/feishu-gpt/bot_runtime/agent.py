import json
import os
import re
import time
from datetime import datetime

from openai import APIError, APITimeoutError, AuthenticationError

from . import state
from .config_runtime import (
    COMPRESS_AT,
    KEEP_RECENT,
    MAX_CONSECUTIVE_TOOL_REPEATS,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    TOOL_LOG_PREVIEW_CHARS,
    openai_client,
)
from .paths import build_agent_system_prompt, ensure_runtime_dirs, get_agent_workspace, get_runtime_data_dir, get_tool_call_log_path, load_heartbeat_text
from .tools import execute_tool, get_all_tools

TOOL_LOG_RETENTION_MS = 7 * 24 * 60 * 60 * 1000
MAX_PROMPT_CHARS = 180_000
MAX_HISTORY_CHARS = 90_000
MAX_HISTORY_ENTRY_CHARS = 12_000
MAX_CURRENT_USER_CHARS = 60_000
MAX_QUOTED_CHARS = 30_000
MAX_SUMMARY_SOURCE_CHARS = 120_000
MAX_IMAGE_DATA_URL_CHARS = 500_000
MAX_IMAGE_DATA_URLS = 2
MAX_TOOL_RESULT_CHARS = 60_000
LARK_CLI_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")
NO_TOOL_KEYWORDS = (
    "不用工具",
    "不要用工具",
    "禁止工具",
    "别用工具",
    "直接回答",
)
READ_ONLY_INTENT_KEYWORDS = (
    "只读",
    "不要修改",
    "别修改",
    "不修改",
    "不要改",
    "别改",
)
FULL_TOOL_KEYWORDS = (
    "修改",
    "改一下",
    "帮我改",
    "修复",
    "根治",
    "优化",
    "实现",
    "创建",
    "新增",
    "写入",
    "保存",
    "删除",
    "移动",
    "重命名",
    "执行",
    "运行",
    "跑一下",
    "发送",
    "回复",
    "上传",
    "下载",
    "导入",
    "命令",
    "shell",
    "powershell",
    "lark-cli",
    "飞书",
    "文档",
    "日程",
    "会议",
    "定时",
    "提醒",
    "通知",
    "任务",
    "codex",
    "claude",
    "agent",
    "checkout",
    "时间",
    "日期",
    "今天",
    "明天",
    "现在几点",
)
READ_ONLY_TOOL_KEYWORDS = (
    "查看",
    "读取",
    "检查",
    "分析",
    "解释",
    "总结",
    "为什么",
    "原因",
    "报错",
    "定位",
    "查询",
    "搜索",
    "文件",
    "目录",
    "代码",
    "配置",
    "日志",
    ".py",
    ".md",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
)


class ThinkingInterrupted(Exception):
    pass


def _raise_if_cancelled(cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise ThinkingInterrupted("消息已撤回，已中断本次思考")


def _preview_text(value, limit: int = TOOL_LOG_PREVIEW_CHARS):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def _limit_text(text: str, limit: int, label: str) -> str:
    text = str(text or "")
    if limit <= 0 or len(text) <= limit:
        return text
    marker = f"\n\n[系统提示：{label}过长，已截断；原始长度 {len(text)} 字符，仅保留前 {limit} 字符。]"
    keep = max(0, limit - len(marker))
    return text[:keep].rstrip() + marker


def _trim_history_entry(turn: dict) -> dict:
    role = turn.get("role")
    limit = MAX_HISTORY_ENTRY_CHARS * 2 if role == "summary" else MAX_HISTORY_ENTRY_CHARS
    return {
        "role": role,
        "content": _limit_text(turn.get("content", ""), limit, "单条历史记录"),
    }


def _history_chars(history: list[dict]) -> int:
    return sum(len(str(turn.get("content", ""))) for turn in history)


def _fit_history(history: list[dict], limit: int = MAX_HISTORY_CHARS) -> list[dict]:
    fitted = []
    used = 0
    for turn in reversed(history):
        trimmed = _trim_history_entry(turn)
        content_len = len(trimmed["content"])
        if fitted and used + content_len > limit:
            break
        if content_len > limit:
            trimmed["content"] = _limit_text(trimmed["content"], limit, "历史记录")
            content_len = len(trimmed["content"])
        fitted.append(trimmed)
        used += content_len
    fitted.reverse()
    skipped = len(history) - len(fitted)
    if skipped > 0:
        fitted.insert(
            0,
            {
                "role": "summary",
                "content": f"[系统提示：有 {skipped} 条更早历史因上下文长度限制未带入本轮请求。]",
            },
        )
    return fitted


def _limit_image_data_urls(image_data_urls: list[str] | None) -> tuple[list[str], str]:
    if not image_data_urls:
        return [], ""
    kept = []
    dropped = 0
    for data_url in image_data_urls:
        if not data_url:
            continue
        if len(kept) >= MAX_IMAGE_DATA_URLS or len(data_url) > MAX_IMAGE_DATA_URL_CHARS:
            dropped += 1
            continue
        kept.append(data_url)
    if not dropped:
        return kept, ""
    note = f"[系统提示：有 {dropped} 张图片因数量或体积超过上下文预算未发送给模型。]"
    return kept, note


def _latest_user_request(prompt: str) -> str:
    marker = "用户："
    index = str(prompt or "").rfind(marker)
    if index < 0:
        return str(prompt or "")
    latest = str(prompt)[index + len(marker):]
    suffix = "\n（请直接回复最新的用户问题）"
    if latest.endswith(suffix):
        latest = latest[: -len(suffix)]
    return latest.strip()


def _prune_tool_log(path: str, now_ms: int):
    if not os.path.exists(path):
        return

    cutoff_ms = now_ms - TOOL_LOG_RETENTION_MS
    retained_lines = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if int(entry.get("ts", 0)) >= cutoff_ms:
                retained_lines.append(json.dumps(entry, ensure_ascii=False))

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        if retained_lines:
            f.write("\n".join(retained_lines) + "\n")


def _log_tool_event(trace_id: str, event_type: str, payload: dict):
    try:
        ensure_runtime_dirs()
        now_ms = int(time.time() * 1000)
        log_path = get_tool_call_log_path()
        _prune_tool_log(log_path, now_ms)
        record = {
            "ts": now_ms,
            "trace_id": trace_id,
            "event": event_type,
            **payload,
        }
        with open(log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[WARN] 记录工具日志失败: {e}")


def _infer_tool_mode(prompt: str, requested_mode: str) -> str:
    mode = str(requested_mode or "auto").strip().lower()
    if mode in {"none", "read_only", "full"}:
        return mode
    latest = _latest_user_request(prompt).lower()
    if any(keyword in latest for keyword in NO_TOOL_KEYWORDS):
        return "none"
    if any(keyword in latest for keyword in READ_ONLY_INTENT_KEYWORDS):
        return "read_only"
    if any(keyword in latest for keyword in FULL_TOOL_KEYWORDS):
        return "full"
    if any(keyword in latest for keyword in READ_ONLY_TOOL_KEYWORDS):
        return "read_only"
    return "none"


def _build_tool_budget_prompt(tool_mode: str) -> str:
    if tool_mode == "none":
        return "本轮暂不开放工具；能直接回答就直接回答。如你判断必须使用工具才能完成，请只输出 `TOOL_REQUIRED:full` 或 `TOOL_REQUIRED:read_only`。"
    if tool_mode == "read_only":
        scope = "本轮只开放只读工具：`list_dir`、`read_file`。禁止写入、删除、执行 shell 或管理任务；如必须升级，请只输出 `TOOL_REQUIRED:full`。"
    else:
        scope = "本轮开放完整工具集。"
    return (
        f"{scope}\n"
        "能直接回答就直接回答；禁止重复调用等价工具；信息足够后立即给出结论。"
    )


def _parse_tool_escalation(output: str, current_mode: str) -> str | None:
    text = (output or "").strip()
    if not text:
        return None
    for line in text.splitlines()[:3]:
        normalized = line.strip().strip("`").replace("：", ":").lower()
        if not normalized.startswith("tool_required"):
            continue
        if "read_only" in normalized or "readonly" in normalized or "read-only" in normalized:
            return "read_only" if current_mode == "none" else None
        return "full" if current_mode != "full" else None
    if current_mode != "full" and any(phrase in text for phrase in ("无法调用工具", "不能调用工具", "需要调用工具", "需要使用工具")):
        return "full"
    return None


def _is_tool_required_sentinel(output: str) -> bool:
    text = (output or "").strip()
    if not text:
        return False
    for line in text.splitlines()[:3]:
        normalized = line.strip().strip("`").replace("：", ":").lower()
        if normalized.startswith("tool_required"):
            return True
    return False


def _format_tool_escalation_blocked(escalated_mode: str) -> str:
    label = "完整工具权限" if escalated_mode == "full" else "只读工具权限"
    return f"（需要{label}才能继续，但本轮工具权限被用户指令或调用配置限制，不能自动升级。）"


def _allow_tool_escalation(
    prompt: str,
    requested_mode: str,
    allow_tools: bool,
    current_mode: str,
    escalated_mode: str,
    auto_escalation_depth: int = 0,
) -> bool:
    if not allow_tools:
        return False
    mode = str(requested_mode or "auto").strip().lower()
    latest = _latest_user_request(prompt).lower()
    if any(keyword in latest for keyword in NO_TOOL_KEYWORDS):
        return False
    if mode in {"none", "read_only", "full"}:
        if (
            auto_escalation_depth > 0
            and mode == current_mode == "read_only"
            and escalated_mode == "full"
            and not any(keyword in latest for keyword in READ_ONLY_INTENT_KEYWORDS)
        ):
            return True
        return False
    if any(keyword in latest for keyword in READ_ONLY_INTENT_KEYWORDS):
        return False
    return True


def _make_tool_signature(tool_call) -> str:
    try:
        arguments = json.loads(tool_call.function.arguments or "{}")
    except Exception:
        arguments = {"raw_arguments": tool_call.function.arguments}
    return json.dumps(
        {
            "name": tool_call.function.name,
            "arguments": arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _create_chat_completion(messages: list[dict], tools: list | None):
    kwargs = {
        "model": OPENAI_MODEL,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    response = openai_client.chat.completions.create(**kwargs)
    state.last_response_model = str(getattr(response, "model", "") or "")
    return response


def _parse_lark_cli_version(text: str) -> str | None:
    match = LARK_CLI_VERSION_RE.search(str(text or ""))
    return match.group(1) if match else None


def _cli_payload_output(payload: dict) -> str:
    return "\n".join(part for part in [payload.get("stdout", ""), payload.get("stderr", "")] if part)


def _write_heartbeat_cli_log(label: str, text: str) -> str | None:
    if not text:
        return None
    try:
        log_dir = os.path.join(get_runtime_data_dir(), "heartbeat_logs")
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("._") or "cli"
        path = os.path.join(log_dir, f"{stamp}_{safe_label}.log")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.rstrip() + "\n")
        return path
    except Exception as exc:
        print(f"[HEARTBEAT] 写入 CLI 错误日志失败: {exc}")
        return None


def _run_feishu_cli_payload(args: list[str]) -> dict:
    try:
        payload = json.loads(execute_tool("run_feishu_cli", {"args": args}))
        payload["ok"] = True
        return payload
    except Exception as exc:
        message = str(exc)
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            payload = {"command": ["lark-cli", *args], "exit_code": None, "stdout": "", "stderr": message}
        payload["ok"] = False
        return payload


def _load_heartbeat_state() -> dict:
    path = os.path.join(get_agent_workspace(), "memory", "heartbeat-state.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _write_heartbeat_state(data: dict):
    path = os.path.join(get_agent_workspace(), "memory", "heartbeat-state.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _run_lark_cli_update_for_heartbeat() -> str:
    state_data = _load_heartbeat_state()
    lark_cli_state = state_data.get("larkCli")
    if not isinstance(lark_cli_state, dict):
        lark_cli_state = {}
    last_checks = state_data.get("lastChecks")
    if not isinstance(last_checks, dict):
        last_checks = {}

    previous_version = str(lark_cli_state.get("version") or "").strip() or None
    now = datetime.now().astimezone()
    update_payload = _run_feishu_cli_payload(["update"])
    update_output = _cli_payload_output(update_payload)
    if update_payload.get("exit_code") not in (0, None) or update_payload.get("ok") is False:
        error_text = update_output or json.dumps(update_payload, ensure_ascii=False)
        error_log_path = _write_heartbeat_cli_log("lark_cli_update_error", error_text)
        error_summary = _limit_text(error_text, 360, "lark-cli update 错误")
        lark_cli_state.update(
            {
                "packageName": "@larksuite/cli",
                "version": previous_version,
                "lastUpdateCheckAt": now.isoformat(),
                "lastUpdateResult": "failed",
                "lastError": error_summary,
                "lastErrorLog": error_log_path,
            }
        )
        last_checks["larkCliUpdate"] = int(now.timestamp())
        state_data["larkCli"] = lark_cli_state
        state_data["lastChecks"] = last_checks
        _write_heartbeat_state(state_data)
        log_note = f"\n- 详细日志：`{error_log_path}`" if error_log_path else ""
        return f"HEARTBEAT_WARNING\nlark-cli update 执行失败，已记录到 heartbeat-state.json。{log_note}\n- 摘要：{error_summary}"

    version_payload = _run_feishu_cli_payload(["--version"])
    version_output = _cli_payload_output(version_payload)
    current_version = _parse_lark_cli_version(version_output) or _parse_lark_cli_version(update_output) or previous_version

    updated = bool(previous_version and current_version and previous_version != current_version)
    if updated:
        verify_payload = _run_feishu_cli_payload(["update"])
        verify_output = _cli_payload_output(verify_payload)
    else:
        verify_output = update_output
    lark_cli_state.update(
        {
            "previousVersion": previous_version or current_version,
            "packageName": "@larksuite/cli",
            "version": current_version,
            "latestVersion": current_version,
            "npmPackageVersion": current_version,
            "lastUpdateCheckAt": now.isoformat(),
            "lastSuccessfulCheckAt": now.isoformat(),
            "lastUpdateResult": "updated" if updated else "already up to date",
            "lastError": None,
        }
    )
    last_checks["larkCliUpdate"] = int(now.timestamp())
    state_data["larkCli"] = lark_cli_state
    state_data["lastChecks"] = last_checks
    _write_heartbeat_state(state_data)

    if updated:
        return f"HEARTBEAT_NOTICE\nlark-cli 已更新到 {current_version}，并已完成二次 `lark-cli update` 验证：{verify_output}"
    return "HEARTBEAT_OK"


def _build_user_content(prompt: str, image_data_urls: list[str] | None = None):
    image_data_urls, image_note = _limit_image_data_urls(image_data_urls)
    if image_note:
        prompt = f"{prompt}\n\n{image_note}".strip()
    if not image_data_urls:
        return prompt
    content = [{"type": "text", "text": prompt}]
    for data_url in image_data_urls:
        if data_url:
            content.append({"type": "image_url", "image_url": {"url": data_url}})
    return content


def _complete_without_tools(messages: list[dict], reason: str, trace_id: str, cancel_event=None) -> str:
    _raise_if_cancelled(cancel_event)
    final_messages = list(messages) + [
        {
            "role": "system",
            "content": (
                f"停止继续调用工具。原因：{reason}\n"
                "请严格基于当前已知信息直接给出最好答案；如果信息不足，请明确指出缺口，但不要再请求调用工具。"
            ),
        }
    ]
    response = _create_chat_completion(final_messages, None)
    _raise_if_cancelled(cancel_event)
    message = response.choices[0].message
    output = (message.content or "").strip() or f"（已停止工具调用：{reason}）"
    _log_tool_event(trace_id, "forced_final_answer", {"reason": reason, "output_preview": _preview_text(output)})
    return output


def ask_chatgpt(
    prompt: str,
    system_prompt: str = "",
    cancel_event=None,
    tool_mode: str = "auto",
    trace_id: str | None = None,
    allow_tools: bool = True,
    image_data_urls: list[str] | None = None,
    auto_escalation_depth: int = 0,
) -> str:
    if not OPENAI_API_KEY:
        return "（未配置 OPENAI_API_KEY）"
    try:
        _raise_if_cancelled(cancel_event)
        requested_tool_mode = str(tool_mode or "auto").strip().lower()
        resolved_tool_mode = "none" if not allow_tools else _infer_tool_mode(prompt, requested_tool_mode)
        active_trace_id = trace_id or f"trace_{int(time.time() * 1000)}"
        runtime_system_prompt = "\n\n".join(part for part in [system_prompt, _build_tool_budget_prompt(resolved_tool_mode)] if part)
        messages = []
        if runtime_system_prompt:
            messages.append({"role": "system", "content": runtime_system_prompt})
        messages.append({"role": "user", "content": _build_user_content(prompt, image_data_urls)})
        tools = get_all_tools(resolved_tool_mode)
        previous_batch_signature = None
        repeated_batch_count = 0
        step_index = 0
        _log_tool_event(
            active_trace_id,
            "session_start",
            {
                "tool_mode": resolved_tool_mode,
                "tool_count": len(tools),
                "prompt_preview": _preview_text(prompt),
            },
        )

        while True:
            step_index += 1
            _raise_if_cancelled(cancel_event)
            response = _create_chat_completion(messages, tools)
            _raise_if_cancelled(cancel_event)
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []

            if not tool_calls:
                output = (message.content or "").strip()
                _log_tool_event(active_trace_id, "final_answer", {"output_preview": _preview_text(output or "（空响应）")})
                escalated_mode = _parse_tool_escalation(output, resolved_tool_mode)
                if escalated_mode and _allow_tool_escalation(
                    prompt,
                    requested_tool_mode,
                    allow_tools,
                    resolved_tool_mode,
                    escalated_mode,
                    auto_escalation_depth,
                ):
                    _log_tool_event(
                        active_trace_id,
                        "tool_mode_escalated",
                        {
                            "from_mode": resolved_tool_mode,
                            "to_mode": escalated_mode,
                            "reason_preview": _preview_text(output),
                        },
                    )
                    return ask_chatgpt(
                        prompt,
                        system_prompt,
                        cancel_event=cancel_event,
                        tool_mode=escalated_mode,
                        trace_id=f"{active_trace_id}:tool_escalated",
                        allow_tools=allow_tools,
                        image_data_urls=image_data_urls,
                        auto_escalation_depth=auto_escalation_depth + 1,
                    )
                if escalated_mode and _is_tool_required_sentinel(output):
                    blocked_output = _format_tool_escalation_blocked(escalated_mode)
                    _log_tool_event(
                        active_trace_id,
                        "tool_mode_escalation_blocked",
                        {
                            "from_mode": resolved_tool_mode,
                            "to_mode": escalated_mode,
                            "reason_preview": _preview_text(output),
                        },
                    )
                    return blocked_output
                return output

            batch_signature = json.dumps([_make_tool_signature(tool_call) for tool_call in tool_calls], ensure_ascii=False)
            repeated_batch_count = repeated_batch_count + 1 if batch_signature == previous_batch_signature else 1
            previous_batch_signature = batch_signature
            _log_tool_event(
                active_trace_id,
                "tool_batch_requested",
                {
                    "round": step_index,
                    "tool_calls": [
                        {
                            "name": tool_call.function.name,
                            "arguments_preview": _preview_text(tool_call.function.arguments or "{}"),
                        }
                        for tool_call in tool_calls
                    ],
                    "repeated_batch_count": repeated_batch_count,
                },
            )

            if repeated_batch_count >= MAX_CONSECUTIVE_TOOL_REPEATS:
                return _complete_without_tools(messages, "检测到重复工具调用循环", active_trace_id, cancel_event)

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments,
                            },
                        }
                        for tool_call in tool_calls
                    ],
                }
            )

            for tool_call in tool_calls:
                _raise_if_cancelled(cancel_event)
                tool_start = time.perf_counter()
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                    tool_result = execute_tool(tool_call.function.name, arguments)
                    duration_ms = int((time.perf_counter() - tool_start) * 1000)
                    _log_tool_event(
                        active_trace_id,
                        "tool_result",
                        {
                            "round": step_index,
                            "tool_name": tool_call.function.name,
                            "ok": True,
                            "duration_ms": duration_ms,
                            "arguments_preview": _preview_text(arguments),
                            "result_preview": _preview_text(tool_result),
                        },
                    )
                except Exception as e:
                    duration_ms = int((time.perf_counter() - tool_start) * 1000)
                    tool_result = json.dumps(
                        {"error": str(e), "tool": tool_call.function.name},
                        ensure_ascii=False,
                    )
                    _log_tool_event(
                        active_trace_id,
                        "tool_result",
                        {
                            "round": step_index,
                            "tool_name": tool_call.function.name,
                            "ok": False,
                            "duration_ms": duration_ms,
                            "arguments_preview": _preview_text(tool_call.function.arguments or "{}"),
                            "result_preview": _preview_text(tool_result),
                        },
                    )
                _raise_if_cancelled(cancel_event)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": _limit_text(tool_result, MAX_TOOL_RESULT_CHARS, "工具结果"),
                    }
                )

    except ThinkingInterrupted:
        raise
    except APITimeoutError:
        return "（响应超时，请重试）"
    except AuthenticationError:
        return "（OpenAI 认证失败，请检查 OPENAI_API_KEY）"
    except APIError as e:
        return f"（OpenAI API 出错：{e}）"
    except Exception as e:
        return f"（调用出错：{e}）"


def _format_history_for_summary(turns: list[dict]) -> str:
    lines = []
    for turn in turns:
        label = "用户" if turn["role"] == "user" else "助手"
        lines.append(f"{label}：{_limit_text(turn['content'], MAX_HISTORY_ENTRY_CHARS, '摘要来源记录')}")
    return _limit_text("\n".join(lines), MAX_SUMMARY_SOURCE_CHARS, "摘要来源")


def compress_history(chat_id: str):
    history = state.conversations.get(chat_id, [])
    keep = KEEP_RECENT * 2

    if history and history[0]["role"] == "summary":
        prev_summary = history[0]["content"]
        to_compress = history[1:-keep] if len(history) > keep + 1 else []
        recent = history[-keep:]
    else:
        prev_summary = None
        to_compress = history[:-keep]
        recent = history[-keep:]

    if not to_compress:
        return

    turns_text = _format_history_for_summary(to_compress)
    if prev_summary:
        prompt = (
            f"以下是已有的对话摘要：\n{prev_summary}\n\n"
            "请将下面的新对话整合进摘要，保留关键信息、决策和上下文，输出更新后的摘要：\n\n"
            f"{turns_text}"
        )
    else:
        prompt = "请将以下对话压缩成简洁摘要，保留关键信息、决策和上下文：\n\n" + turns_text

    print(f"[压缩历史] chat={chat_id}，压缩 {len(to_compress) // 2} 轮...")
    summary = ask_chatgpt(prompt, build_agent_system_prompt(), tool_mode="none", trace_id=f"compress:{chat_id}", allow_tools=False)
    state.conversations[chat_id] = [{"role": "summary", "content": summary}] + recent


def build_prompt(chat_id: str, user_text: str, quoted_text: str | None = None) -> str:
    history = _fit_history(state.conversations.get(chat_id, []))
    history_block = ""
    quoted_block = ""
    reply_suffix = ""

    if history:
        lines = ["以下是本次会话的历史记录：", ""]
        for turn in history:
            if turn["role"] == "summary":
                lines.append(f"[历史摘要]\n{turn['content']}\n")
            elif turn["role"] == "user":
                lines.append(f"用户：{turn['content']}")
            else:
                lines.append(f"助手：{turn['content']}")
        history_block = "\n".join(lines).strip() + "\n\n"

    if quoted_text:
        quoted_block = f"用户引用了以下内容：\n> {_limit_text(quoted_text, MAX_QUOTED_CHARS, '引用消息')}\n\n"

    if history or quoted_text:
        reply_suffix = "（请直接回复最新的用户问题）"

    user_text = _limit_text(user_text, MAX_CURRENT_USER_CHARS, "用户最新消息")
    return _limit_text(f"{history_block}{quoted_block}用户：{user_text}\n{reply_suffix}".strip(), MAX_PROMPT_CHARS, "本轮请求")


def update_history(chat_id: str, user_text: str, assistant_reply: str):
    history = state.conversations.setdefault(chat_id, [])
    history.append({"role": "user", "content": _limit_text(user_text, MAX_HISTORY_ENTRY_CHARS, "用户历史")})
    history.append({"role": "assistant", "content": _limit_text(assistant_reply, MAX_HISTORY_ENTRY_CHARS, "助手历史")})
    non_summary = [turn for turn in history if turn["role"] != "summary"]
    if len(non_summary) > COMPRESS_AT * 2 or _history_chars(history) > MAX_HISTORY_CHARS:
        compress_history(chat_id)


def run_agent_heartbeat_check() -> str:
    heartbeat_text = load_heartbeat_text()
    if "lark-cli" in heartbeat_text and "更新" in heartbeat_text:
        return _run_lark_cli_update_for_heartbeat()

    prompt = (
        "现在执行一次 HEARTBEAT 轮询。\n"
        "请严格根据当前工作区的 AGENTS.md 和 HEARTBEAT.md 执行自检。\n"
        "本轮 HEARTBEAT 开放工具；你可以在必要时读取、创建、修改工作区文件，执行必要的 shell / lark-cli 命令。\n"
        "你可以并应当在需要记录轮询状态时读取和更新 `memory/heartbeat-state.json`；如果该文件不存在，可在工作区下创建。\n"
        "如果 HEARTBEAT.md 为空、仅注释，或检查结果正常且无需通知，请只输出 `HEARTBEAT_OK`。\n"
        "如果需要通知用户，请用固定首行声明信息级别：普通信息用 `HEARTBEAT_NOTICE`，潜在风险用 `HEARTBEAT_WARNING`，真实故障或需要人工处理的异常用 `HEARTBEAT_ALERT`。\n"
        "从第二行开始输出要发送给用户的正文，不要输出解释、前言、代码块或额外包装。"
    )
    if heartbeat_text:
        prompt += "\n\n下面是 HEARTBEAT.md 当前内容，供你参考：\n" + heartbeat_text
    return ask_chatgpt(prompt, build_agent_system_prompt(), tool_mode="full", trace_id="heartbeat", allow_tools=True).strip()
