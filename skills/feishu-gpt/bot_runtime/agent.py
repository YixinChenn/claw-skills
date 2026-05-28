import json
import os
import time

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
from .paths import build_agent_system_prompt, ensure_runtime_dirs, get_tool_call_log_path, load_heartbeat_text
from .tools import execute_tool, get_all_tools

TOOL_LOG_RETENTION_MS = 7 * 24 * 60 * 60 * 1000


class ThinkingInterrupted(Exception):
    pass


def _raise_if_cancelled(cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise ThinkingInterrupted("消息已撤回，已中断本次思考")


def _preview_text(value, limit: int = TOOL_LOG_PREVIEW_CHARS):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


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
    return "full"


def _build_tool_budget_prompt(tool_mode: str) -> str:
    if tool_mode == "none":
        return "本轮禁止使用任何工具。请仅基于现有上下文直接作答。"
    if tool_mode == "read_only":
        scope = "本轮只开放只读工具：`list_dir`、`read_file`。禁止写文件、删文件、执行 shell、管理定时任务。"
    else:
        scope = "本轮开放完整工具集。只有在确实需要读取、修改文件、执行 shell 或管理定时任务时才调用工具。"
    return (
        f"{scope}\n"
        "不设置固定工具调用轮数上限；能直接回答就直接回答。\n"
        "禁止重复调用等价工具；如果已经收集到足够信息，立即停止工具调用并给出结论。"
    )


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


def _build_user_content(prompt: str, image_data_urls: list[str] | None = None):
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
) -> str:
    if not OPENAI_API_KEY:
        return "（未配置 OPENAI_API_KEY）"
    try:
        _raise_if_cancelled(cancel_event)
        resolved_tool_mode = "none" if not allow_tools else _infer_tool_mode(prompt, tool_mode)
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
                return output or "（ChatGPT 没有返回内容）"

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
                        "content": tool_result,
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
        lines.append(f"{label}：{turn['content']}")
    return "\n".join(lines)


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
    history = state.conversations.get(chat_id, [])
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
        quoted_block = f"用户引用了以下内容：\n> {quoted_text}\n\n"

    if history or quoted_text:
        reply_suffix = "（请直接回复最新的用户问题）"

    return f"{history_block}{quoted_block}用户：{user_text}\n{reply_suffix}".strip()


def update_history(chat_id: str, user_text: str, assistant_reply: str):
    history = state.conversations.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_reply})
    non_summary = [turn for turn in history if turn["role"] != "summary"]
    if len(non_summary) > COMPRESS_AT * 2:
        compress_history(chat_id)


def run_agent_heartbeat_check() -> str:
    heartbeat_text = load_heartbeat_text()
    prompt = (
        "现在执行一次 HEARTBEAT 轮询。\n"
        "请严格根据当前工作区的 AGENTS.md 和 HEARTBEAT.md 执行自检。\n"
        "本轮 HEARTBEAT 开放工具；你可以在必要时读取、创建、修改工作区文件，执行必要的 shell / lark-cli 命令。\n"
        "你可以并应当在需要记录轮询状态时读取和更新 `memory/heartbeat-state.json`；如果该文件不存在，可在工作区下创建。\n"
        "如果 HEARTBEAT.md 为空、仅注释，或检查结果正常且无需通知，请只输出 `HEARTBEAT_OK`。\n"
        "如果需要通知用户，请直接输出要发送给用户的正文，不要输出解释、前言、代码块或额外包装。"
    )
    if heartbeat_text:
        prompt += "\n\n下面是 HEARTBEAT.md 当前内容，供你参考：\n" + heartbeat_text
    return ask_chatgpt(prompt, build_agent_system_prompt(), tool_mode="full", trace_id="heartbeat", allow_tools=True).strip()
