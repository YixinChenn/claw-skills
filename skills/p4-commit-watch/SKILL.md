---
name: p4-commit-watch
description: 监控 Perforce (P4) 代码仓库的提交活动，支持个人提交半天巡查和组员每日日报两种模式。自动拉取完整提交描述、文件变更列表和代码 diff，并通过 AI 分析改动意图。支持配置多个 stream 仓库，统一监控。

**当以下情况时使用此 Skill**：
(1) 需要设置 p4 提交监控、定时日报
(2) 需要分析某次 p4 提交的改动内容和意图
(3) 需要查看指定用户或组员最近的 p4 提交
(4) 需要配置多仓库（多 stream）的提交巡查
(5) 用户提到"p4 监控"、"提交分析"、"组员日报"、"代码巡查"
---

# P4 Commit Watch

监控 Perforce 提交，生成带 AI diff 分析的报告。

---

## ⚠️ 输出规则（最高优先级，任何模式下都适用）

- **在最终报告输出之前，绝对不输出任何文字**
- 不输出步骤进度（"我已读取..."、"正在生成..."、"开始写入..."等）
- 不输出中间分析、工具调用结果说明、状态确认
- 只有最终报告内容是输出，其他全部静默执行
- Obsidian 写入、git push 等后台动作不在输出中提及

---

## 目录结构

```
p4-commit-watch/
├── SKILL.md                    ← 完整执行说明（本文件）
├── p4-watch-config.json        ← 用户配置文件
├── scripts/
│   ├── p4-self-watch.ps1       ← 个人提交监控
│   └── p4-team-daily.ps1       ← 组员日报
├── references/
│   └── config-example.json     ← 配置示例
└── reports/                    ← 自动生成的历史报告
```

---

## 模式一：组员日报（team-daily）

### 执行步骤

**第一步：运行脚本**
```
pwsh -NonInteractive -ExecutionPolicy Bypass -File C:\Users\banqiang\.openclaw\skills\p4-commit-watch\scripts\p4-team-daily.ps1 -HoursBack 24
```

**第二步：收集所有 DIFF_FILE 路径**

脚本末尾输出 `DIFF_PATHS:path1,path2,...`（逗号分隔）。
Split 逗号得到路径列表。如无该行，逐行扫描提取 `DIFF_FILE: ` 后面的路径。

**第三步：逐个读取 diff 文件（强制，不得跳过）**

对列表中**每一个**路径，必须调用 `read` 工具读取内容。
不得凭文件列表猜测，不得跳过任何路径。

**第四步：生成报告（按分析规则，见下方）**

**第五步：Evaluator 质量评估（见下方）**

**第六步：Obsidian 落盘**

将最终报告整理成 Markdown，写入：
`E:\Repos\ForObsidian\职业发展\03-管理\P4观察\日报\YYYY-MM-DD-组员提交观察.md`

Markdown 结构：
- 标题：`# YYYY-MM-DD 组员提交观察`
- 概览：成员数、总提交数、有提交人数
- 分成员小节：提交数、摘要、Lua Code Review（如有）
- 末尾「观察备注」节留空

**第七步：输出最终报告**（唯一对外输出）

---

## 模式二：个人巡查（self-watch）

### 执行步骤

**第一步：运行脚本**
```
pwsh -NonInteractive -ExecutionPolicy Bypass -File C:\Users\banqiang\.openclaw\skills\p4-commit-watch\scripts\p4-self-watch.ps1 -HoursBack 12
```

**第二步 ~ 第四步**：同组员日报（收集 DIFF_FILE → 逐个读取 → 生成报告）

**第五步：输出报告**

直接把报告内容作为你的回复输出（不要调用任何飞书或消息发送工具，系统会自动路由给用户）。
如无提交，输出「过去 12 小时无提交」。
不输出任何「发送给 xxx」「无法发送」「权限」等说明文字。

> 个人巡查**不做** Evaluator 评估，**不做** Obsidian 落盘。

---

## AI 分析规则

### 提交分类优先级

#### ❌ 低价值提交 → 跳过 AI 分析，不输出任何分析文字

以下类型直接跳过，CL 基础信息（CL 号、时间、文件数）保留，不加分析段落：
- `collection` 类：描述含 `Added X objects to collection` / `Removed ... from collection`
- `DataLayerSave` 类：描述含 `[DataLayerSave]` 或 `导出文件配置`
- `Undo changelist` 类：描述以 `Undo changelist` 开头
- 纯 `.collection` 文件变更（FILES 全为 Other，无 BP/Lua/C++）

#### ✅ 含 Lua 文件（Lua > 0）→ 专项 Code Review（必做，不得省略）

**执行 Code Review 之前，必须先读取项目约定文档：**
`C:\Users\banqiang\.openclaw\skills\p4-commit-watch\references\lua-review-conventions.md`
按照其中的约定校正判断，避免将已知等效写法或项目惯例误判为问题。

```
[Code Review]
修改意图
  原有逻辑：原来是...（1句，说明改动前的行为或约定）
  本次改动：将...改为...，目的是...（1句）
  语义变化：如果本次改动改变了某个值/路径/函数的语义约定，在此单独说明；若无变化则写「无」

风险分析
- [高/中/低] 逻辑风险：条件分支/边界判断/空值处理是否有问题（无问题则写「未发现明确高风险问题」）
- [高/中/低] 耦合风险：是否影响其他模块、是否有硬编码或遗留 debug 代码（无则写「未发现」）
- 描述一致性：commit 描述与实际改动是否吻合（吻合/存疑+说明）

建议
  如有改进建议则写，否则写「无」

审查盲区
  仅基于 diff，未覆盖：xxx（如：未看到调用方约定、相关接口定义、运行时验证结果等）

审查结论
  [可合入 / 建议改后再审 / 需深度排查]（1句核心理由）
```

#### ✅ 纯 BP/C++ 改动（无 Lua）→ 1 句摘要

```
[AI分析] 本次提交是...（意图，1句）。
```

### 注意事项

- `.uasset`/`.umap` 在 diff 中显示为 `(binary)`，**无法分析**，直接忽略该文件
- 若一个 CL 只有二进制文件变更（无 Lua/C++ 文本 diff），不输出 AI 分析
- `[add]` 新增的 Lua 文件，脚本已用 `p4 print` 补充内容，标注为 `(text/new)`，可正常阅读分析

---

## Evaluator 质量评估（仅组员日报）

对生成的日报文本按三个维度打分（1-10）：

| 维度 | 说明 | 硬性要求 |
|------|------|----------|
| **Coverage** | 所有 Lua > 0 的 CL 是否都有 [Code Review] | 漏 1 个 → 5分，漏 2 个以上 → 1分 |
| **Signal-to-noise** | collection/DataLayer/Undo 类是否还在堆废话式分析 | 有就扣分 |
| **Readability** | 整体结构清晰，能快速定位每个人的核心改动 | — |

**通过条件**：Coverage = 10 且 Signal-to-noise ≥ 6 且 Readability ≥ 6 且 Overall ≥ 7

**Evaluator JSON 要求**：
- 单行紧凑格式，不得换行或缩进
- feedback 严格 ≤ 30 字，逗号分隔核心问题
- 示例：`{"scores":{"coverage":10,"signal_to_noise":8,"readability":8},"overall":8.7,"pass":true,"feedback":""}`
- 评估 JSON 本身**不对外输出**，只影响最终报告格式

**输出格式：**

通过 → 直接输出日报内容，无任何前缀后缀

不通过 → 精确格式如下（注意空行）：
```
⚠️ 本报告未通过质量评估

评分：Coverage=X, Signal-to-noise=Y, Readability=Z, Overall=V

反馈：<feedback 原文，不换行>

--- 以下为原始报告 ---

<完整日报内容>
```

---

## 模式三：组员周报（weekly）

### 执行步骤

**第一步：运行脚本**
```
pwsh -NonInteractive -ExecutionPolicy Bypass -File C:\Users\banqiang\.openclaw\skills\p4-commit-watch\scripts\p4-weekly.ps1 -HoursBack 168
```

**第二步：收集所有 DIFF_FILE 路径**

同模式一，脚本末尾输出 `DIFF_PATHS:path1,path2,...`。

**第三步：逐个读取 diff 文件（强制，不得跳过）**

对列表中每一个路径，必须调用 `read` 工具读取内容。

**第四步：生成周维度摘要**

与日报不同，周报按**人**汇总，不做 per-CL 分析。对每位有提交的成员输出：

```
[月初]
- 功能A：简述（1句）
- 功能B：简述（1句）
```

规则：
- **跳过低价值提交**（collection / DataLayer / Undo），不提及、不计入 bullet
- **含 Lua 的提交**：提取功能关键词，将同一功能域的多个 CL 合并为 1 条 bullet
- **纯 BP/C++ 提交**：1 句概括，如有多个相关 CL 合并说明
- 每人 **最多 5 条** bullet，视有价值内容多少决定，不强制凑满
- 语言：中文，简洁，适合直接填入飞书表格

无提交的成员：本周列留空。

**第五步：读取上周周报文档**

使用飞书 MCP 搜索上周的周报文档（标题格式 `2.8版本周报-程序`）：
```
mcp__pape-lark-mcp__search_docs(query="2.8版本周报-程序")
```
从结果中取 `edit_time` 最新的一条（即上周那份）。

再用 MCP 读取该文档内容：
```
mcp__pape-lark-mcp__get_doc_content(doc_id=<token>)
```
定位第 2 节表格「本周完成的工作内容」列，逐成员提取文本内容，存为「上周数据」。若找不到上周文档，「上周」列全部留空。

**第六步：新建本周飞书周报文档**

用 `docs +create --api-version v2` 新建文档，标题：`2.8版本周报-程序-YYYY年M月D日`（当天日期），存放到指定目录（`--parent-token`）。

文档结构（DocxXML）：

> ⚠️ 使用 `--command overwrite` 时，内容必须以 `<title>文档标题</title>` 开头，否则标题 block 会被清空。

**Part 1：逐人核对表**（Markdown 表格，两列：姓名 / 本周工作）
- 全体成员均出现，无提交标注「（本周无代码提交）」
- 每人 bullet 列在同一单元格内，换行分隔

**Part 2：组级汇报段落**（紧接 Part 1 之后）
- 一段连贯文字，不出现人名、不出现分类标题
- 排序：新功能/玩法 → 系统/状态 → Bug修复 → 技术底层
- 整段不超过 5 句，同类跨人内容用分号衔接

**第七步：输出最终报告**（唯一对外输出）

报告由三部分组成，按以下顺序输出：

**① 飞书文档链接**
```
📋 周报文档已创建：<链接>（上周数据来源：<上周文档标题> 或 「未找到，列留空」）
```

**② Part 1：逐人核对表**（Markdown 表格，供负责人逐条复核）

```markdown
### 程序组本周工作核对表（YYYY年M月第N周）

| 姓名 | 本周工作 |
|------|---------|
| 月初 | • 灵魂甬道 QTE 框架：新增分支选择与超时逻辑，支持多段触发 |
|      | • 角色状态同步：修复 QTE 期间受击打断的边界问题 |
| 芒果 | • 蘑菇人乐园 Room1/2/4：玩法逻辑接入并完成首轮迭代 |
| 天同 | • 磐城浊化状态系统：实现累积衰减与视觉反馈驱动 |
|      | • 噗灵合并修复：修复多实体并发合并时的引用错误 |
| 莲莲 | （本周无代码提交） |
```

所有 members 均出现在表格中，无提交标注「（本周无代码提交）」。

**③ Part 2：组级汇报段落**（整合连贯，无分类标题，供向大组汇报粘贴）

```markdown
### 程序组本周工作汇报（YYYY年M月第N周）

本周在玩法侧，灵魂甬道 QTE 框架完成完善，新增分支选择与超时机制，并修复了 QTE 期间受击打断的边界异常；蘑菇人乐园 Room1/2/4 玩法逻辑完成接入与首轮迭代。系统侧，磐城浊化状态系统实现落地，支持累积衰减与视觉反馈驱动，同步修复了噗灵多实体并发合并的引用错误。此外完成了若干引擎 C++ bug fix。
```

Part 2 生成规则：
- **分行输出**，每行一句，对应一类工作（用 `<ul><li>` 列表）
- 排序：新功能/玩法 → 系统/状态 → Bug修复 → 技术底层
- 同类工作跨人合并，控制在 4～5 行以内
- **不出现人名，不出现技术细节**（如具体 API 名称、架构改法），只写功能层面的结论
- **不加分类前缀**（不写"新玩法交付："这种小标题，直接写内容）
- **不要**"本周在玩法侧"式的段落开头，直接从内容起
- 用词规范：**禁用**"上线""落地"，改用"接入""实现""完成"；枚举/状态类变化用"迭代"概括，不写具体"X态"；**禁用**"体系"，改用"结构""方案""框架"
- Bug 修复合并为一行带过，不展开细节
- **禁止推断完成状态**：提交记录只证明本周做了什么，不能得出"全部完成""已完成"等结论，除非 commit 描述中明确写明
- 无提交成员不出现在 Part 2

**第八步：按提交记录复核（必做）**

对照第一步脚本输出的原始报告（`reports/YYYY-MM-DD-weekly.txt`）逐人校验：

- 每条 bullet 必须能在原始报告中找到对应的 CL 描述或文件路径作为依据
- 检查是否有张冠李戴（某条工作归在了错误的人名下）
- 检查是否有内容凭空生成（原始数据中没有对应提交）
- 检查无提交成员是否确实 0 CL（而非被漏扫）

发现错误时：直接修正飞书文档（`docs +update --api-version v2 --command overwrite`），同步更新 Part 2 段落，不需要询问用户。复核完成后在最终输出末尾追加一行：`✅ 已按原始提交记录复核，[X] 处修正` 或 `✅ 已复核，内容与提交记录一致`。

> 周报模式**不做** Evaluator 评估，**不做** Obsidian 落盘。

---

## Cron 触发 Prompt（极简）

cron 只做触发，执行逻辑全在本 SKILL.md 中。

**组员日报（每天 22:00）：**
```
读取并严格执行 C:\Users\banqiang\.openclaw\skills\p4-commit-watch\SKILL.md 中「模式一：组员日报」的完整流程。
```

**个人巡查 AM（每天 09:00）：**
```
读取并严格执行 C:\Users\banqiang\.openclaw\skills\p4-commit-watch\SKILL.md 中「模式二：个人巡查」的完整流程。
```

**个人巡查 PM（每天 21:00）：**
```
读取并严格执行 C:\Users\banqiang\.openclaw\skills\p4-commit-watch\SKILL.md 中「模式二：个人巡查」的完整流程。
```

**组员周报（每周日 22:00）：**
```
读取并严格执行 C:\Users\banqiang\.openclaw\skills\p4-commit-watch\SKILL.md 中「模式三：组员周报」的完整流程。
```

---

## 配置文件说明（p4-watch-config.json）

```json
{
  "repos": [
    {
      "name": "Stb_2.5",
      "stream": "//root/game/Stb_2.5/...",
      "label": "稳定版 2.5"
    }
  ],
  "self_watch": {
    "p4_user": "shuiliu",
    "hours_back": 12,
    "enabled": true
  },
  "team_watch": {
    "members": ["dashu", "chenglong"],
    "hours_back": 24,
    "enabled": true
  },
  "notify": {
    "feishu_open_id": "ou_xxx"
  }
}
```

---

## 已知限制

- Windows 控制台中 p4 中文输出可能乱码，脚本已通过 `p4 describe -s` 绕过
- diff 内容中中文字符串可能仍有乱码（不影响逻辑分析）
- 非代码文件（`.uasset`、`.umap` 等）不生成 diff，只统计数量
- p4 连接配置依赖系统环境变量（P4PORT / P4USER / P4CLIENT）
- `[add]` 新增 Lua 文件通过 `p4 print` 补充，`[edit]`/`[integrate]` 走标准 diff
