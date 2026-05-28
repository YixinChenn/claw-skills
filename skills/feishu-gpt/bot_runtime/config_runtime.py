import logging

import lark_oapi as lark
from openai import OpenAI

from app_config import CONFIG_SOURCE, settings

CONFIG_SOURCE_NAME = CONFIG_SOURCE

APP_ID = settings.app_id
APP_SECRET = settings.app_secret
NOTIFY_CHAT_ID = settings.notify_chat_id
NOTIFY_OPEN_ID = settings.notify_open_id
OPENAI_API_KEY = settings.openai_api_key
OPENAI_BASE_URL = settings.openai_base_url
OPENAI_MODEL = settings.openai_model
OPENAI_TIMEOUT = settings.openai_timeout
AGENTS_PATH = settings.agents_path

FEISHU_CLI_ENABLED = settings.feishu_cli_enabled
FEISHU_CLI_BIN = settings.feishu_cli_bin
FEISHU_CLI_AS = settings.feishu_cli_as
FEISHU_CLI_TIMEOUT = settings.feishu_cli_timeout
FEISHU_CLI_EXTRA_ARGS = [str(item) for item in settings.feishu_cli_extra_args if str(item).strip()]

DOC_IMPORT_ENABLED = settings.doc_import_enabled
DOC_IMPORT_DIR = settings.doc_import_dir
DOC_IMPORT_POLL_SECONDS = settings.doc_import_poll_seconds
DOC_IMPORT_STABLE_SECONDS = settings.doc_import_stable_seconds
DOC_IMPORT_CLI_AS = settings.doc_import_cli_as
DOC_IMPORT_FOLDER_TOKEN = settings.doc_import_folder_token
DOC_IMPORT_WIKI_NODE = settings.doc_import_wiki_node
DOC_IMPORT_WIKI_SPACE = settings.doc_import_wiki_space
DOC_IMPORT_NOTIFY_CHAT_ID = settings.doc_import_notify_chat_id
DOC_IMPORT_NOTIFY_OPEN_ID = settings.doc_import_notify_open_id

AGENT_NOTIFY_ENABLED = settings.agent_notify_enabled
AGENT_NOTIFY_DIR = settings.agent_notify_dir
AGENT_NOTIFY_POLL_SECONDS = settings.agent_notify_poll_seconds
AGENT_NOTIFY_STABLE_SECONDS = settings.agent_notify_stable_seconds
AGENT_NOTIFY_CHAT_ID = settings.agent_notify_chat_id
AGENT_NOTIFY_OPEN_ID = settings.agent_notify_open_id

AGENT_RUNNER_ENABLED = settings.agent_runner_enabled
AGENT_RUNNER_DEFAULT_CWD = settings.agent_runner_default_cwd
AGENT_RUNNER_TIMEOUT_SECONDS = settings.agent_runner_timeout_seconds
AGENT_RUNNER_MAX_CONCURRENT = settings.agent_runner_max_concurrent
AGENT_RUNNER_CODEX_BIN = settings.agent_runner_codex_bin
AGENT_RUNNER_CLAUDE_BIN = settings.agent_runner_claude_bin
AGENT_RUNNER_CODEX_ARGS = [str(item) for item in settings.agent_runner_codex_args if str(item).strip()]
AGENT_RUNNER_CLAUDE_ARGS = [str(item) for item in settings.agent_runner_claude_args if str(item).strip()]
AGENT_RUNNER_ALLOWED_SENDERS = {str(item).strip() for item in settings.agent_runner_allowed_senders if str(item).strip()}

MAX_IDS = 1000
MAX_HISTORY = 10
COMPRESS_AT = 8
KEEP_RECENT = 4
MSG_CHUNK_SIZE = 4000
MAX_CONSECUTIVE_TOOL_REPEATS = 2
TOOL_LOG_PREVIEW_CHARS = 2000
HEARTBEAT_INTERVAL_SECONDS = 1800
WS_RESTART_THRESHOLD = 2
HEARTBEAT_RESTART_THRESHOLD = 3
TASK_POLL_INTERVAL_SECONDS = 5


class _SuppressUnhandledEvents(logging.Filter):
    def filter(self, record):
        return "processor not found" not in record.getMessage()


_filter = _SuppressUnhandledEvents()
for _name in ["lark_oapi", "Lark", "", "root"]:
    logging.getLogger(_name).addFilter(_filter)

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
openai_client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_BASE_URL or None,
    timeout=OPENAI_TIMEOUT,
)
