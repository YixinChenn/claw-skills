# 飞书 × ChatGPT 机器人

通过飞书 WebSocket 长连接实时接收消息，调用 OpenAI API 生成回复。**无需公网 IP，开箱即用。**

## 功能特性

- **多轮对话**：按会话隔离上下文，同一会话内保持连贯对话
- **自动摘要压缩**：历史过长时自动压缩为摘要，不丢断上下文
- **思考中表情**：收到消息立即在原消息上添加 🤔 表情，回复后自动移除
- **普通文本回复**：默认发送接近日常聊天的文本消息，不再使用 Markdown 卡片
- **回复串**：在线程中回复时归入消息串，普通消息则回复到会话
- **长消息分段**：超过 4000 字符自动拆分，带页码发送
- **线程安全去重**：防止飞书重推事件导致的重复回复
- **工作区编辑器**：可在工作区内列目录、读文件、写文件、追加文件、删文件、建目录
- **记忆写入**：支持按 `AGENTS.md` 指令将记忆写入工作区下的 `memory/` 目录
- **飞书技能集成**：按需加载 `lark-*` skills，通过通用 Shell 执行技能指引，不在代码中固定飞书子命令
- **定时任务**：支持周期任务、每日任务、一次性任务、暂停恢复和工作时间窗口
- **Markdown 自动导入飞书文档**：监控投递目录，将其他 Agent 生成的 `.md` 自动创建为飞书文档并通知
- **本地 Agent 完成通知**：监控通知投递目录，Codex/Claude Code 等任务结束后可写入 `.json` / `.txt` 触发飞书通知；同一来源、同一对话的通知归入同一飞书话题（包括机器人私聊）
- **Agent Runner**：可通过飞书命令启动、查看、取消本机 Codex / Claude Code 任务
- **工作区技能**：输入 `/技能名`，按需加载 `<AGENTS_PATH>/skills/<技能名>/SKILL.md`
- **心跳自检**：后台定时检查 Agent 线程、飞书长连接和 HEARTBEAT 轮询结果，可通过 `HEARTBEAT_MODEL` 单独指定模型
- **会话隔离与材料收集**：按客户和话题持久化上下文，支持 `/collect` 收集后用 `/run` 统一处理
- **聊天模型档位**：quick 使用 Sol medium，balanced 默认使用 Sol high，deep 使用配置模型 high
- **管理指令**：`/help` `/clear` `/history` 和一组 `/task-*`、`/agent-*` 指令
- **引用消息识别**：自动将引用内容注入上下文
- **启动通知**：bot 上线时可向指定会话或指定用户发送通知

## 目录结构

```
feishu-gpt/
├── app_config/        # 配置层（local.py / local.example.py）
├── bot_runtime/       # 程序运行时模块
├── tests/             # 隔离外部服务的单元测试
├── agents.example.md  # AGENTS 模板
├── AGENTS.md          # 默认工作区初始化指令
├── bot.py             # 极薄入口
├── agent-notify.ps1   # 本地任务通知投递
├── claude-topic-reply.py # Claude 话题入口与答复投递
├── restart.vbs
├── restart.bat
├── start.ps1
└── README.md
```

## 凭据与运行数据

- 真实凭据、接收人 ID、内部网关地址和本机绝对路径仅填写在 `app_config/local.py`，不要写入示例配置、测试或提交说明。
- Git 忽略本机配置、环境文件、Agent 本地设置、文档投递与临时目录、会话数据库、记忆和日志；运行目录位于工作区外时也不要手动加入仓库。
- Codex CLI 优先使用 `AGENT_RUNNER_CODEX_BIN` 或 `CODEX_CLI_PATH`；自定义 Codex 配置目录使用 `CODEX_HOME`，无需在源码中写个人路径。

本地验证（不启动机器人或调用真实服务）：

```powershell
python -B -m unittest discover -s tests -v
```

## 快速开始

### 1. 安装依赖

```bash
pip install lark-oapi openai
```

如需启用飞书 CLI 工具，还需要先安装：

```bash
npm install -g @larksuite/cli
lark-cli config init
lark-cli auth login --recommend
```

### 2. 配置

复制模板并填写：

```bash
cp app_config/local.example.py app_config/local.py
```

编辑 `app_config/local.py`：

```python
APP_ID                  = "cli_xxxxxxxx"      # 飞书开放平台 → 凭证与基础信息
APP_SECRET              = "xxxxxxxxxxxxxxxx"
NOTIFY_CHAT_ID          = ""                  # 启动通知会话 ID
NOTIFY_OPEN_ID          = ""                  # 启动通知用户 open_id
OPENAI_API_KEY          = "sk-xxxxxxxx"
OPENAI_BASE_URL         = "https://your-openai-compatible-host/v1"  # 兼容 OpenAI 的服务地址
OPENAI_MODEL            = "gpt-5"
OPENAI_TIMEOUT          = 600
HEARTBEAT_MODEL         = ""                  # 心跳模型，留空沿用 OPENAI_MODEL，例如 gpt-6.1-sol
AGENTS_PATH             = ""                  # Agent 工作区目录，默认读取其中的 AGENTS.md


DOC_IMPORT_ENABLED      = False
DOC_IMPORT_DIR          = ""                  # Markdown 投递目录，默认 <AGENTS_PATH>/doc_inbox
DOC_IMPORT_CLI_AS       = "user"              # 文档技能任务使用的身份：bot 或 user
DOC_IMPORT_FOLDER_TOKEN = ""                  # 可选：创建到指定云空间文件夹
DOC_IMPORT_WIKI_NODE    = ""                  # 可选：创建到指定知识库节点
DOC_IMPORT_WIKI_SPACE   = ""                  # 可选：创建到指定知识空间
DOC_IMPORT_LOCAL_INDEX_PATH = "doc_sources/Document_Index.md"  # 本地文档索引，相对 AGENTS_PATH
DOC_IMPORT_ONLINE_INDEX_DOC = ""              # 可选：在线文档索引 URL/token
DOC_IMPORT_BOT_AUTHOR_ENABLED = True          # 新建时由机器人创建并填充正文，再转移所有权给用户；迁移到个人空间交给 AI 流程执行

AGENT_NOTIFY_ENABLED    = True                # 是否启用本地 Agent 通知投递
AGENT_NOTIFY_DIR        = ""                  # 通知投递目录，默认 <AGENTS_PATH>/runtime_data/agent_notify
AGENT_NOTIFY_CHAT_ID    = ""                  # 可选：Agent 通知会话，空则复用 NOTIFY_CHAT_ID
AGENT_NOTIFY_OPEN_ID    = ""                  # 可选：Agent 通知用户，空则复用 NOTIFY_OPEN_ID

AGENT_RUNNER_ENABLED    = True                # 是否启用 Agent Runner
AGENT_RUNNER_DEFAULT_CWD = ""                 # 默认运行目录，空则使用 AGENTS_PATH
AGENT_RUNNER_TIMEOUT_SECONDS = 3600           # 单个 Agent Job 超时秒数
AGENT_RUNNER_MAX_CONCURRENT = 2               # 最大并发 Agent Job 数
AGENT_RUNNER_CODEX_BIN  = ""                  # 可选：Codex CLI 绝对路径
AGENT_RUNNER_CLAUDE_BIN = ""                  # 可选：Claude CLI 绝对路径
AGENT_RUNNER_ALLOWED_SENDERS = []             # 允许启动/取消 Agent Job 的 sender_id/open_id/union_id
```

默认按兼容 OpenAI 的 `POST /chat/completions` 调用接口；`gpt-6.1-sol`、`gpt-6-astra`（含日期版本）自动使用 `POST /responses`，以支持工具调用和推理上下文传递。自定义服务地址需支持对应接口。SDK 会自动带上：

- `Authorization: Bearer {OPENAI_API_KEY}`
- `Content-Type: application/json`

如需接入飞书 CLI：

- 将 `lark-*` skills 安装在工作区 `skills/`、工作区 `.agents/skills/` 或用户 `~/.agents/skills/`；同名技能优先使用工作区版本
- 先执行 `lark-cli config init` 完成应用配置
- 再执行 `lark-cli auth login --recommend` 完成用户授权
- Agent 根据技能目录选择 `read_skill` 读取完整 `SKILL.md` 和必要参考文件，再使用通用 `run_shell` 执行；未找到技能时报告缺失
- `lark-cli` 仍是技能的底层依赖，须在机器人进程的 PATH 中可用；旧 `FEISHU_CLI_*` 配置不再使用
- 对话、心跳检查、后台文档导入与在线索引同步均走技能流程，不再提供 `run_feishu_cli` 专用工具

如需启用 Markdown 自动导入飞书文档：

- 将 `DOC_IMPORT_ENABLED` 设为 `True`
- 将 `DOC_IMPORT_DIR` 设为另一个 Agent 的 Markdown 输出目录；不填则使用 `<AGENTS_PATH>/doc_inbox`
- 默认使用 `DOC_IMPORT_CLI_AS = "user"` 作为技能任务的默认操作身份
- 启用 `DOC_IMPORT_BOT_AUTHOR_ENABLED` 时，新建文档会先用 bot 身份创建并填充正文，再转移所有权给当前 CLI 用户；由同一技能任务继续迁移到用户个人空间，并显式给机器人保留 `full_access`；若配置了目标文件夹或知识库，则使用该目标位置
- 需要创建到指定位置时，填写 `DOC_IMPORT_FOLDER_TOKEN`、`DOC_IMPORT_WIKI_NODE` 或 `DOC_IMPORT_WIKI_SPACE` 其中一个
- 同标题 Markdown 会复用已创建文档并执行覆盖更新，不会重复新建；标题索引保存在投递目录下的 `doc_index.json`
- 每次创建/更新文档后会同步维护 `<AGENTS_PATH>/doc_sources/Document_Index.md`；配置 `DOC_IMPORT_ONLINE_INDEX_DOC` 后，还会覆盖同步到在线文档索引
- 已有文档由技能按配置身份更新，并按机器人参与配置维护协作权限
- 后台操作会使用模型并增加响应时间和 token 消耗；技能缺失、返回格式错误、需要确认或部分完成均进入失败路径，错误记录保留返回结果，重试前应核查远端是否已创建文档
- 导入成功后源 `.md` 会移动到 `processed/`，失败会移动到 `failed/`
- 通知目标优先使用 `DOC_IMPORT_NOTIFY_CHAT_ID` / `DOC_IMPORT_NOTIFY_OPEN_ID`，不填则复用 `NOTIFY_CHAT_ID` / `NOTIFY_OPEN_ID`

如需让本地 Codex / Claude Code 任务结束后通知你：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\agent-notify.ps1 -Source codex -ConversationId "实际 Codex 对话 ID" -Status success -Title "Codex 任务完成" -Message "摘要内容"
```

如果 `AGENTS_PATH` 指向其他工作区，请在 `app_config/local.py` 固定 `AGENT_NOTIFY_DIR`，或调用脚本时传 `-Dir`。机器人会处理投递目录下的 `.json` / `.txt` 文件，成功后移动到 `processed/`，失败后移动到 `failed/`。

通知话题规则：

- 首次通知创建一条话题根消息，通知正文及同一对话的后续通知都以话题回复发送；私聊继续使用原 `AGENT_NOTIFY_OPEN_ID` / `NOTIFY_OPEN_ID` 配置，无需建群。
- 按通知目标、`source` 和 `conversation_id` 隔离；映射保存在 `<AGENTS_PATH>/runtime_data/agent_notification_threads.json`，重启后继续复用。
- 建议显式传 `-ConversationId`。脚本也读取 `AGENT_CONVERSATION_ID`，或根据来源读取 `CODEX_THREAD_ID` / `CODEX_CONVERSATION_ID` / `CLAUDE_SESSION_ID`；不再猜测磁盘上最近的 Claude 会话，避免并行聊天串话题。
- JSON 兼容 `conversationId`、`session_id`、`thread_id` 字段；缺少有效对话 ID 时，每条通知建立独立话题，不合并到其他对话。
- 发送失败进入 `failed/` 并记录错误，不作为成功投递归档；话题回复失败不会自动改发普通正文。
- 在绑定 Codex 对话的飞书话题内回复文字，会自动按绑定 ID 调用现有 `codex exec resume` 链路，最终结果回到同一个话题；不再需要填写 ID 或 `/agent-resume`。
- 仅允许 `AGENT_RUNNER_ALLOWED_SENDERS` 白名单中的用户转发，私聊还核对通知接收人；普通聊天不走此路由。
- 同一 Codex session 的话题回复依次处理：上一条完成后再处理下一条。此行为是排队续聊。2026-11-08 实测：桌面会话仍持有写锁时，CLI 返回 `already has an active writer`，不能即时追加；此时明确回报未执行，需等待原会话释放后重试。
- Claude 入口只加载本地配置并原子写入队列，不加载飞书/OpenAI SDK；首次建话题和后续回复均由常驻通知监听发送。CLI 返回仅代表已入队。更新监听代码后需重启机器人。
- Claude 入口执行仍遵循客户端权限审批；inbox 消息不等于命令批准。也可在机器人所在机器显式指定 `--session-id` 创建话题，不必让 Claude 执行建话题命令。
- Claude 成功写入 inbox、Codex 成功接收话题请求时，在用户原消息添加 `Typing`（敲键盘）表情，不单独发送进度消息；表情表示接收，不表示任务完成，表情失败不阻塞处理。答复成功发送后撤销该表情；Claude 的清理记录持久化，撤销失败可由监听重试，不重发答复。
- 首次从 Claude 发起可续聊话题时，调用 `claude-topic-reply.py --start --text-file <UTF-8正文文件>`；脚本从当前 `CLAUDE_CODE_MESSAGING_SOCKET` 精确识别 session，也可显式传 `--session-id <ID>`。它把绑定信息投递到机器人已配置的通知目录，由通知监听创建根消息和话题回复；机器人需在线。只执行普通 `lark-cli im +messages-send` 不会建立话题绑定。
- Codex 首次创建话题仍使用 `agent-notify.ps1 -Source codex -ConversationId <真实对话ID> -Dir <机器人实际通知目录> -Message <正文>`；通用飞书私聊发送同样不会自动绑定。不要将首次通知和话题内回复混用；收到话题消息后再沿已有绑定续聊。
- Claude 话题（来源 `claude` / `claude-code` / `claude-cli` / `claude-desktop`）按绑定 `session_id` / `conversation_id` 投递到客户端原 session 的 Windows inbox，不启动 `claude -p --resume`。需在运行机器人的同一 Windows 用户下保持原 Claude 会话打开；离线、认证失配或协议不兼容会在原话题报错。
- Claude 每次投递重新读取 `%USERPROFILE%/.claude/sessions`（设置 `CLAUDE_CONFIG_DIR` 时使用对应目录），核对进程启动时间、管道所属 PID 和认证 key；目前支持实测 `peerProtocol=1` 的 Windows named pipe。密钥只在内存中使用。
- Claude 收到消息后，按附带指引调用本项目 `claude-topic-reply.py --request-id <ID> --text-file <UTF-8正文文件>`，直接回复原话题。程序按请求 ID 查回原根消息，Claude 不需要选择接收人。需允许目标 session 执行此 Python 脚本；权限受限时仍需在 Claude 客户端处理，peer 消息不能代替批准。
- 请求路由与去重记录保存在 `<AGENTS_PATH>/runtime_data/claude_topic_requests.sqlite3`；回复有效期 24 小时。每个请求一份最终答复，同正文重试复用飞书幂等 ID。相同飞书消息不会因重推再次投递；结果不确定时先查看 Claude 原会话，再决定是否重新发消息。
- Claude 分支仅保证按 session 顺序投递，不等待上一条模型回复后才投递下一条；“已写入 inbox”不代表模型已接收或执行。Claude 的接收策略可能暂存或拒绝消息，飞书回复还取决于目标会话调用回复脚本。
- 新通知会保存工作目录；旧 Codex 话题仍可按 ID 续聊，未保存目录时沿用 Runner 默认目录。话题附件暂不转发。
- 2026-11-08 已实测空闲 session 的 A/B 两条回复顺序执行并回到同一飞书话题；桌面原聊天重新打开后可见两条提问和回答。CLI 执行期间桌面可能提示“已在另一个应用中打开”，不支持两端同时写入。
- 此规则用于通知投递目录创建的话题；独立 `/agent-run` 命令的运行结果回传仍使用 Runner 原有路径。

如需从飞书启动本机 Codex / Claude Code 任务：

```text
/agent-run codex --cwd D:\Projects\ExampleAgent -- 总结当前项目结构
/agent-run claude --cwd D:\Projects\ExampleAgent -- 检查 README 是否需要更新
```

如需继续已有 Codex / Claude Code 对话：

```text
/agent-resume codex <conversation_id> --cwd D:\Projects\ExampleAgent -- 继续刚才的任务，检查剩余问题
/agent-resume claude <session_id> --cwd D:\Projects\ExampleAgent -- 继续刚才的任务，检查剩余问题
/agent-resume codex last --cwd D:\Projects\ExampleAgent -- 继续最近一次对话
```

也可以直接用自然语言，例如：

```text
让 Claude 在 D:\Projects\ExampleAgent 里检查 README 是否需要更新
让 Codex 在当前工作区总结项目结构
让 Codex 继续对话 019e...，检查剩余问题
看一下最近的 Agent 任务
取消 job_codex_xxx
```

Runner 会把日志写到 `<AGENTS_PATH>/runtime_data/agent_jobs/`，并在任务结束后回发状态和最近日志。可用 `/agent-status` 查看最近任务，`/agent-tail <job_id>` 查看日志，`/agent-cancel <job_id>` 请求取消；自然语言也会优先走同一套 Agent Runner 工具。

如果命令或自然语言里没有指定 `--cwd` / 工作区，Runner 会优先使用 `AGENT_RUNNER_DEFAULT_CWD`；该配置为空时回退到 `AGENTS_PATH`。

Agent Runner 默认需要发送人在 `AGENT_RUNNER_ALLOWED_SENDERS` 白名单中；不在白名单时会直接回复“你没资格啊，你没资格”。白名单可以填写飞书 `sender_id`、`open_id` 或 `union_id`。

如需自定义 Agent 初始化指令：

```bash
cp Agents.example.md <你的工作区>/AGENTS.md
```

然后在 `app_config/local.py` 中把 `AGENTS_PATH` 指到这个工作区目录。机器人会读取该目录下的 `AGENTS.md`，并把里面的内容整体作为初始化指令。该文件中提到的文件名和相对路径，都按这个工作区解析。

### 3. 飞书开放平台配置

1. 创建应用，开启**机器人**能力
2. 订阅事件：`im.message.receive_v1`（接收消息）
3. 开通权限：
   - `im:message`（发送消息）
   - `im:message:send_as_bot`
   - `im:message:retrieve_for_bot`（读取引用消息内容）
   - `im:message.reaction:write`（添加/移除表情回复）

### 4. 启动

```bash
python bot.py
```

默认会同时启动：

- 飞书机器人
- 本地对话命令行

Windows 下也可以直接运行：

```powershell
.\start.ps1
```

本地对话模式：

```bash
python bot.py --local-chat
```

或双击 `restart.vbs`（Windows，自动关闭旧进程并重启）。

## 一键重启

双击 `restart.vbs` 即可：

1. 读取工作区下的 `runtime_data/bot.pid`，精准杀掉旧进程
2. 启动新的 bot 进程

> `restart.vbs` 作为入口，显式以 `cmd /k` 打开窗口，避免 Windows 闪退问题。

## 管理指令

技能依次从 `<AGENTS_PATH>/skills/`、`<AGENTS_PATH>/.agents/skills/` 和 `~/.agents/skills/`（仅 `lark-*`）发现，每个技能使用独立目录和 `SKILL.md`。例如：

```text
D:\Projects\ExampleAgent\skills\example-skill\SKILL.md
```

发送 `/example-skill` 或 `/example-skill <任务内容>` 时，机器人会在本轮读取并遵循该技能；技能中的相对路径按 `example-skill` 目录解析。内置管理指令优先于同名技能。

| 指令 | 说明 |
|------|------|
| `/help` | 显示可用指令列表 |
| `/clear` | 清除当前会话的对话历史 |
| `/history` | 查看上下文保留轮数、当前模型和工作区 |
| `/skills` | 查看所有已发现的技能 |
| `/task-add <分钟> <任务内容>` | 创建按分钟循环的定时任务 |
| `/task-add-daily <HH:MM> <任务内容>` | 创建每日任务 |
| `/task-add-once <YYYY-MM-DD> <HH:MM> <任务内容>` | 创建一次性任务 |
| `/task-list` | 查看当前定时任务 |
| `/task-del <task_id>` | 删除定时任务 |
| `/task-run <task_id>` | 立即执行一次定时任务 |
| `/task-pause <task_id>` | 暂停定时任务 |
| `/task-resume <task_id>` | 恢复定时任务 |
| `/task-window <task_id> <days> <HH:MM-HH:MM>` | 设置工作时间窗口 |
| `/agent-run <codex\|claude> [--cwd <目录> --] <任务>` | 启动本机 Agent Job |
| `/agent-resume <codex\|claude> <会话ID\|last> [--cwd <目录> --] <任务>` | 继续已有 Codex/Claude 会话 |
| `/agent-status [job_id]` | 查看最近 Agent Job 或指定任务详情 |
| `/agent-tail <job_id> [行数]` | 查看 Agent Job 日志 |
| `/agent-cancel <job_id>` | 请求取消 Agent Job |

## 关键参数

| 参数 | 位置 | 默认值 | 说明 |
|------|------|--------|------|
| `NOTIFY_CHAT_ID` | app_config/local.py | 空 | 启动时向指定会话发送上线通知 |
| `NOTIFY_OPEN_ID` | app_config/local.py | 空 | 启动时向指定用户 open_id 发送上线通知 |
| `OPENAI_MODEL` | app_config/local.py | gpt-5 | 调用的 OpenAI 模型 |
| `OPENAI_TIMEOUT` | app_config/local.py | 600 | OpenAI API 调用超时秒数 |
| `HEARTBEAT_MODEL` | app_config/local.py | 空 | HEARTBEAT 独立模型，留空沿用 `OPENAI_MODEL`；不受会话 `/model` 影响，修改后重启生效 |
| `AGENTS_PATH` | app_config/local.py | 空 | Agent 工作区目录，程序会读取其中的 `AGENTS.md` |
| `DOC_IMPORT_ENABLED` | app_config/local.py | False | 是否启用 Markdown 自动导入飞书文档 |
| `DOC_IMPORT_DIR` | app_config/local.py | 空 | Markdown 投递目录，空则使用 `<AGENTS_PATH>/doc_inbox` |
| `DOC_IMPORT_CLI_AS` | app_config/local.py | user | 创建/更新文档时使用的 lark-cli 身份 |
| `DOC_IMPORT_FOLDER_TOKEN` | app_config/local.py | 空 | 可选：创建到指定云空间文件夹 |
| `DOC_IMPORT_WIKI_NODE` | app_config/local.py | 空 | 可选：创建到指定知识库节点 |
| `DOC_IMPORT_WIKI_SPACE` | app_config/local.py | 空 | 可选：创建到指定知识空间 |
| `DOC_IMPORT_LOCAL_INDEX_PATH` | app_config/local.py | doc_sources/Document_Index.md | 本地文档索引路径，相对 AGENTS_PATH |
| `DOC_IMPORT_ONLINE_INDEX_DOC` | app_config/local.py | 空 | 在线文档索引 URL 或 token |
| `DOC_IMPORT_BOT_AUTHOR_ENABLED` | app_config/local.py | True | 新建时是否由机器人创建并填充正文，再转移所有权给用户；更新时是否尝试补机器人协作编辑 |
| `DOC_IMPORT_NOTIFY_CHAT_ID` | app_config/local.py | 空 | 可选：导入结果通知会话 |
| `DOC_IMPORT_NOTIFY_OPEN_ID` | app_config/local.py | 空 | 可选：导入结果通知用户 |
| `AGENT_NOTIFY_ENABLED` | app_config/local.py | True | 是否启用本地 Agent 通知投递 |
| `AGENT_NOTIFY_DIR` | app_config/local.py | 空 | 通知投递目录，空则使用 `<AGENTS_PATH>/runtime_data/agent_notify` |
| `AGENT_NOTIFY_CHAT_ID` | app_config/local.py | 空 | 可选：Agent 通知会话，空则复用 `NOTIFY_CHAT_ID` |
| `AGENT_NOTIFY_OPEN_ID` | app_config/local.py | 空 | 可选：Agent 通知用户，空则复用 `NOTIFY_OPEN_ID` |
| `AGENT_RUNNER_ENABLED` | app_config/local.py | True | 是否启用 Agent Runner |
| `AGENT_RUNNER_DEFAULT_CWD` | app_config/local.py | 空 | 默认运行目录，空则使用 `AGENTS_PATH` |
| `AGENT_RUNNER_TIMEOUT_SECONDS` | app_config/local.py | 3600 | 单个 Agent Job 超时秒数 |
| `AGENT_RUNNER_MAX_CONCURRENT` | app_config/local.py | 2 | 最大并发 Agent Job 数 |
| `AGENT_RUNNER_CODEX_BIN` | app_config/local.py | 空 | 可选：Codex CLI 绝对路径 |
| `AGENT_RUNNER_CLAUDE_BIN` | app_config/local.py | 空 | 可选：Claude CLI 绝对路径 |
| `AGENT_RUNNER_ALLOWED_SENDERS` | app_config/local.py | 空列表 | 允许启动/取消 Agent Job 的飞书 sender_id/open_id/union_id |
| `runtime_data/` | 工作区运行目录 | - | 存放 `bot.pid` 和 `scheduled_tasks.json` |

## 常见问题

**Q: 消息重复发送**  
A: 已通过原子去重 + 后台线程解决。若仍出现，检查网络稳定性。

**Q: 思考中表情添加失败**  
A: 需在飞书开放平台开通 `im:message.reaction:write` 权限。

**Q: 引用内容未被识别**  
A: 需开通 `im:message:retrieve_for_bot` 权限；缺少权限时正常回复不受影响。

**Q: OpenAI 认证失败**  
A: 检查 `app_config/local.py` 中的 `OPENAI_API_KEY` 是否正确，项目余额和模型权限是否正常。

**Q: 飞书 CLI 不可用 / Agent 调不了飞书工具？**  
A: 先确认本机已安装 Node.js 和 `lark-cli`，再执行 `lark-cli auth status` 与 `lark-cli doctor` 检查登录和配置状态。

**Q: 如何在本地直接和 Agent 对话？**  
A: 运行 `python bot.py` 会同时启动飞书机器人和本地对话；如果只想开本地对话，运行 `python bot.py --local-chat`。

**Q: 现在可以直接修改本地文件吗？**  
A: 可以。Agent 已支持工作区内的文件工具调用，能按要求读写、创建、删除文件，并可将记忆写入 `memory/` 目录。

**Q: 响应超时**  
A: 默认等待 600 秒。复杂问题可能仍不够，在 `app_config/local.py` 中调大 `OPENAI_TIMEOUT`。

**Q: 为什么现在不是 Markdown 卡片了？**  
A: 当前默认发送普通文本消息，目的是让聊天体验更自然；如需重新做卡片层，可以在 `bot_runtime/messaging.py` 扩展。

**Q: 重启脚本双击闪退**  
A: 直接双击 `restart.vbs`，不要双击 `restart.bat`。`.bat` 文件在某些 Windows 配置下会闪退，`.vbs` 会显式打开持久窗口。

## 客服会话

飞书消息按客户身份隔离，使用 `<AGENTS_PATH>/runtime_data/customer_sessions.sqlite3` 保存会话、原始文字消息、摘要和近期上下文。重启后自动恢复当前会话；图片二进制及完整工具执行过程不保存在这个数据库中。正在执行且未完成的请求不自动重跑。

- 私聊按聊天和用户隔离；群聊按群、用户及话题/回复链隔离，同一话题的不同用户仍各自独立。无话题时按群和用户接续；后续回复根消息时沿用原会话。
- 同一会话的消息和指令按入队顺序执行，不同会话可并行。群聊仍需 @机器人。
- `/new` 或 `新问题`：归档当前咨询并新建会话。
- `/end` 或 `结束咨询`：结束当前咨询，下次提问自动新建。
- `/history`：查看当前会话编号、近期轮数及是否含摘要。
- `/clear`：归档并新建，不物理删除数据库中的旧消息。
- `/help`：普通客户只看到客服指令。

管理员沿用 `AGENT_RUNNER_ALLOWED_SENDERS` 白名单（填写飞书用户 ID）。白名单为空时，所有飞书用户均按普通客户处理；管理员与普通客户的上下文也相互隔离。管理员保留原有工具和管理指令，本地终端模式不受此限制。

普通客户使用独立客服提示词，不加载内部 AGENTS/技能提示词，不开放 Shell、文件、技能、定时任务或 Agent Runner 工具；程序同时在模型调用入口和工具执行入口限制权限。普通客户不自动读取被引用消息，避免跨客户带入内部信息。

此版本未接业务知识库、订单接口或人工客服系统；业务资料不足时应澄清，不应编造政策或声称已完成业务操作。现有进程内旧历史不会自动迁入数据库。备份时请停止机器人并复制 SQLite 文件；暂不提供历史记录自动清理或多实例消息调度。

### 管理员模型档位（飞书会话）

普通客户不能调整模型参数。管理员可使用以下指令，设置仅作用于当前会话，持久化到 SQLite；`/new`、`/clear` 或结束咨询后重新提问都会恢复默认。默认使用 balanced（`gpt-6.1-sol` + `high`）；未列入支持范围的旧模型仍沿用 `OPENAI_MODEL` 和服务端默认 effort。

| 指令 | 效果 |
| --- | --- |
| `/mode quick` | gpt-6.1-sol + medium |
| `/mode balanced` | gpt-6.1-sol + high（默认） |
| `/mode deep` | OPENAI_MODEL + high（当前为 gpt-6-astra） |
| `/effort low` | 只修改当前会话 effort，可选 low/medium/high/xhigh/max |
| `/model gpt-6.1-sol` | 只修改模型，保留当前 effort |
| `/models` | 列出支持的模型与 effort |
| `/status` | 显示当前设置，以及上次主回答实际返回的模型、effort、服务档位；未返回的字段显示 null，不推断生效 |
| `/ask --effort high -- 问题正文` | 只覆盖本轮，包括后续工具调用 |
| `/ask --model gpt-6.1-sol --effort low -- 问题正文` | 同时覆盖本轮模型与 effort |

优先级：单次 `/ask` 参数 > 当前会话设置 > 默认设置。`/mode` 会同时应用对应档位的模型和 effort；deep 沿用 `OPENAI_MODEL`。已有会话显式保存的模型设置保持不变，可用 `/mode balanced` 切换，或用 `/new` 恢复默认。正文从独立的 `--` 分隔符之后开始，不解析正文里的命令、路径或参数。无效参数明确报错，不修改设置、不调用模型。切换模型保留本会话的文字历史；模型选择不改变工具权限。

当前允许选择 `gpt-6-astra`、`gpt-6.1-sol`。网关实测确认模型切换和 low/medium/high；xhigh/max 为官方支持值，尚未在此网关逐一实测，网关拒绝时会显示调用错误，不会静默降档。Fast/priority 实测返回 default，因此暂不发送 Fast 参数；`/fast on` 或 `/ask --fast on` 不会启用加速。

本地终端、定时任务和 Agent Runner 子进程不使用这些飞书会话设置。已有数据库通过新增独立设置表兼容，无需删除历史数据。重启机器人后生效。

### 显式收集模式

管理员和普通客户都可以先收集、再统一处理；权限仍沿用各自原有规则。

```text
/collect 把接下来的材料整理成待办，按负责人分组
第一条材料
第二条材料
/run 补上截止时间，未知的标记为待确认
```

- `/collect [要求]`：开始一个批次。已有批次时不会覆盖。
- 收集期间普通消息只保存、不逐条回复、不调用模型，也不执行脚本。除收集控制指令外，其他斜杠指令也视作材料；`/new`、`/clear`、`/end` 会提示先结束批次。
- `/pending`：查看数量、图片数量、要求。材料和要求保存在 SQLite，重启后仍在，不会自动执行。
- `/run [补充要求]`：有材料和明确要求才执行。成功后退出收集模式；失败保留材料，用户可显式重试。重启前运行未完成的批次重试前要核对已有操作，避免重复执行。
- `/cancel`：取消当前批次并删除收集材料。运行中的指令按会话队列处理；撤回 `/run` 可请求中断当前处理。
- 撤回材料消息会同步删除该条的文本和图片；最后一条材料撤回时自动取消批次。撤回 `/collect` 会取消整批。撤回先于入队保存时也不会重新收集该消息。
- 处理期间撤回材料，会请求中断并阻止旧批次结果写回；剩余材料保留等待新的 `/run`。已执行的本地/外部操作、已发送的回答或已进入模型请求的内容不能自动撤销。完成后的正常聊天历史不属于收集暂存区。

每批最多 30 条、40000 字符、2 张图片，单张图片编码后不超过 500000 字符；超限明确提示本条未收集。支持文本、纯文本富文本和单独发送的图片；合并转发、文件、音视频及含内嵌图片/视频的富文本暂不展开，会提示改用逐条文本/单独图片。处理要求与材料分开标记，材料中的命令不能视为操作授权。

群聊仍需每条 @机器人，并在同一会话/话题中发送；不同客户、群和话题不会合并。尚未实现自动连发合并。本地终端模式不支持这些收集指令。重启机器人后生效。
