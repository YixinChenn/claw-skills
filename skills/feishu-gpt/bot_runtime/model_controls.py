"""Explicit administrator model controls; no inference or permission escalation."""

import re


MODELS = ("gpt-6-astra", "gpt-6.1-sol")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODES = {
    "quick": {"model": "gpt-6.1-sol", "effort": "medium"},
    "balanced": {"model": "gpt-6.1-sol", "effort": "high"},
    "deep": {"effort": "high"},
}
CONTROL_COMMANDS = {"/mode", "/effort", "/model", "/models", "/status", "/ask", "/fast"}
FAST_UNAVAILABLE = "当前网关测试返回标准档位，Fast 尚未验证生效，暂不支持开启。"


def default_options(model):
    return {"model": model, **(MODES["balanced"] if model in MODELS else {})}


def validate_options(options):
    if options.get("model") not in MODELS:
        raise ValueError("仅支持已验证的模型：" + "、".join(MODELS))
    if options.get("effort") not in EFFORTS:
        raise ValueError("effort 可选：" + " / ".join(EFFORTS))
    return options


def describe(options):
    return f"模型：{options['model']}\neffort：{options.get('effort', '服务端默认')}\nFast：关闭（当前网关未验证生效）"


def parse_control(text, current, default_model):
    """Return (reply, persistent settings, one-turn settings, question)."""
    parts = text.strip().split(maxsplit=1)
    command = parts[0] if parts else ""
    value = parts[1].strip() if len(parts) > 1 else ""
    if command not in CONTROL_COMMANDS:
        return None, None, None, None
    if command == "/ask":
        match = re.fullmatch(r"/ask\s+(.*?)\s+--\s+([\s\S]+)", text.strip())
        if not match:
            raise ValueError("用法：/ask --effort high [--model gpt-6-astra] -- 问题正文")
        tokens = match[1].split()
        if len(tokens) % 2:
            raise ValueError("每个 /ask 参数都需要一个值")
        options, seen = dict(current), set()
        for flag, value in zip(tokens[::2], tokens[1::2]):
            if flag not in {"--model", "--effort"}:
                raise ValueError(FAST_UNAVAILABLE if flag == "--fast" else f"不支持的参数：{flag}")
            if flag in seen:
                raise ValueError(f"参数重复：{flag}")
            seen.add(flag)
            options[flag[2:]] = value
        return None, None, validate_options(options), match[2].strip()
    if command in {"/models", "/status"}:
        if value:
            raise ValueError(f"{command} 不接受参数")
        if command == "/models":
            return "可选模型：" + "、".join(MODELS) + "\neffort：" + " / ".join(EFFORTS), None, None, None
        return describe(current), None, None, None
    if command == "/fast":
        if value not in {"", "on", "off"}:
            raise ValueError("用法：/fast on|off")
        return FAST_UNAVAILABLE if value == "on" else "Fast：关闭（当前网关未验证生效）", None, None, None
    if not value:
        return describe(current), None, None, None
    options = dict(current)
    if command == "/mode":
        if value not in MODES:
            raise ValueError("mode 可选：quick / balanced / deep")
        options = {"model": default_model, **MODES[value]}
    elif command == "/model":
        options["model"] = value
        options.setdefault("effort", "medium")
    else:
        options["effort"] = value
    validate_options(options)
    return "当前会话已更新：\n" + describe(options), options, None, None
