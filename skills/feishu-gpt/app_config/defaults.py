from dataclasses import dataclass, field


@dataclass(frozen=True)
class AppSettings:
    app_id: str = ""
    app_secret: str = ""
    notify_chat_id: str = ""
    notify_open_id: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = "gpt-5"
    openai_timeout: int = 600
    agents_path: str = ""
    feishu_cli_enabled: bool = False
    feishu_cli_bin: str = "lark-cli"
    feishu_cli_as: str = ""
    feishu_cli_timeout: int = 120
    feishu_cli_extra_args: list[str] = field(default_factory=list)
    doc_import_enabled: bool = False
    doc_import_dir: str = ""
    doc_import_poll_seconds: int = 10
    doc_import_stable_seconds: int = 5
    doc_import_cli_as: str = "bot"
    doc_import_folder_token: str = ""
    doc_import_wiki_node: str = ""
    doc_import_wiki_space: str = ""
    doc_import_notify_chat_id: str = ""
    doc_import_notify_open_id: str = ""
    agent_notify_enabled: bool = True
    agent_notify_dir: str = ""
    agent_notify_poll_seconds: int = 5
    agent_notify_stable_seconds: int = 2
    agent_notify_chat_id: str = ""
    agent_notify_open_id: str = ""
    agent_runner_enabled: bool = True
    agent_runner_default_cwd: str = ""
    agent_runner_timeout_seconds: int = 3600
    agent_runner_max_concurrent: int = 2
    agent_runner_codex_bin: str = ""
    agent_runner_claude_bin: str = ""
    agent_runner_codex_args: list[str] = field(default_factory=lambda: ["exec", "--skip-git-repo-check", "--color", "never"])
    agent_runner_claude_args: list[str] = field(default_factory=lambda: ["-p", "--permission-mode", "acceptEdits"])
    agent_runner_allowed_senders: list[str] = field(default_factory=list)
