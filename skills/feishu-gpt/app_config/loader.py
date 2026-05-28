from importlib import import_module

from .defaults import AppSettings

CONFIG_SOURCE = "defaults"


def _read_module(name: str):
    try:
        return import_module(name)
    except ModuleNotFoundError:
        return None


def _to_list(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(item) for item in (value or []) if str(item).strip()]


def load_settings() -> AppSettings:
    global CONFIG_SOURCE

    module = _read_module("app_config.local")
    if module is not None:
        CONFIG_SOURCE = "app_config.local"
    else:
        return AppSettings()

    return AppSettings(
        app_id=str(getattr(module, "APP_ID", "") or ""),
        app_secret=str(getattr(module, "APP_SECRET", "") or ""),
        notify_chat_id=str(getattr(module, "NOTIFY_CHAT_ID", "") or ""),
        notify_open_id=str(getattr(module, "NOTIFY_OPEN_ID", "") or ""),
        openai_api_key=str(getattr(module, "OPENAI_API_KEY", "") or ""),
        openai_base_url=str(getattr(module, "OPENAI_BASE_URL", "") or ""),
        openai_model=str(getattr(module, "OPENAI_MODEL", "gpt-5") or "gpt-5"),
        openai_timeout=int(getattr(module, "OPENAI_TIMEOUT", 600) or 600),
        agents_path=str(getattr(module, "AGENTS_PATH", "") or ""),
        feishu_cli_enabled=bool(getattr(module, "FEISHU_CLI_ENABLED", False)),
        feishu_cli_bin=str(getattr(module, "FEISHU_CLI_BIN", "lark-cli") or "lark-cli"),
        feishu_cli_as=str(getattr(module, "FEISHU_CLI_AS", "") or ""),
        feishu_cli_timeout=int(getattr(module, "FEISHU_CLI_TIMEOUT", 120) or 120),
        feishu_cli_extra_args=_to_list(getattr(module, "FEISHU_CLI_EXTRA_ARGS", [])),
        doc_import_enabled=bool(getattr(module, "DOC_IMPORT_ENABLED", False)),
        doc_import_dir=str(getattr(module, "DOC_IMPORT_DIR", "") or ""),
        doc_import_poll_seconds=int(getattr(module, "DOC_IMPORT_POLL_SECONDS", 10) or 10),
        doc_import_stable_seconds=int(getattr(module, "DOC_IMPORT_STABLE_SECONDS", 5) or 5),
        doc_import_cli_as=str(getattr(module, "DOC_IMPORT_CLI_AS", "bot") or "bot"),
        doc_import_folder_token=str(getattr(module, "DOC_IMPORT_FOLDER_TOKEN", "") or ""),
        doc_import_wiki_node=str(getattr(module, "DOC_IMPORT_WIKI_NODE", "") or ""),
        doc_import_wiki_space=str(getattr(module, "DOC_IMPORT_WIKI_SPACE", "") or ""),
        doc_import_notify_chat_id=str(getattr(module, "DOC_IMPORT_NOTIFY_CHAT_ID", "") or ""),
        doc_import_notify_open_id=str(getattr(module, "DOC_IMPORT_NOTIFY_OPEN_ID", "") or ""),
        agent_notify_enabled=bool(getattr(module, "AGENT_NOTIFY_ENABLED", True)),
        agent_notify_dir=str(getattr(module, "AGENT_NOTIFY_DIR", "") or ""),
        agent_notify_poll_seconds=int(getattr(module, "AGENT_NOTIFY_POLL_SECONDS", 5) or 5),
        agent_notify_stable_seconds=int(getattr(module, "AGENT_NOTIFY_STABLE_SECONDS", 2) or 2),
        agent_notify_chat_id=str(getattr(module, "AGENT_NOTIFY_CHAT_ID", "") or ""),
        agent_notify_open_id=str(getattr(module, "AGENT_NOTIFY_OPEN_ID", "") or ""),
        agent_runner_enabled=bool(getattr(module, "AGENT_RUNNER_ENABLED", True)),
        agent_runner_default_cwd=str(getattr(module, "AGENT_RUNNER_DEFAULT_CWD", "") or ""),
        agent_runner_timeout_seconds=int(getattr(module, "AGENT_RUNNER_TIMEOUT_SECONDS", 3600) or 3600),
        agent_runner_max_concurrent=int(getattr(module, "AGENT_RUNNER_MAX_CONCURRENT", 2) or 2),
        agent_runner_codex_bin=str(getattr(module, "AGENT_RUNNER_CODEX_BIN", "") or ""),
        agent_runner_claude_bin=str(getattr(module, "AGENT_RUNNER_CLAUDE_BIN", "") or ""),
        agent_runner_codex_args=_to_list(getattr(module, "AGENT_RUNNER_CODEX_ARGS", ["exec", "--skip-git-repo-check", "--color", "never"])),
        agent_runner_claude_args=_to_list(getattr(module, "AGENT_RUNNER_CLAUDE_ARGS", ["-p", "--permission-mode", "acceptEdits"])),
        agent_runner_allowed_senders=_to_list(getattr(module, "AGENT_RUNNER_ALLOWED_SENDERS", [])),
    )


settings = load_settings()
