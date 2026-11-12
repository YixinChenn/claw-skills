import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime

from .config_runtime import (
    APP_ID,
    DOC_IMPORT_BOT_AUTHOR_ENABLED,
    DOC_IMPORT_CLI_AS,
    DOC_IMPORT_DIR,
    DOC_IMPORT_ENABLED,
    DOC_IMPORT_FOLDER_TOKEN,
    DOC_IMPORT_LOCAL_INDEX_PATH,
    DOC_IMPORT_NOTIFY_CHAT_ID,
    DOC_IMPORT_NOTIFY_OPEN_ID,
    DOC_IMPORT_ONLINE_INDEX_DOC,
    DOC_IMPORT_POLL_SECONDS,
    DOC_IMPORT_STABLE_SECONDS,
    DOC_IMPORT_WIKI_NODE,
    DOC_IMPORT_WIKI_SPACE,
    NOTIFY_CHAT_ID,
    NOTIFY_OPEN_ID,
)
from .messaging import send_card_to_chat, send_card_to_open_id
from .paths import get_agent_workspace
from .skill_tasks import run_lark_skill_task


def get_doc_import_dir() -> str:
    raw = str(DOC_IMPORT_DIR or "").strip()
    if raw:
        return os.path.abspath(raw)
    return os.path.join(get_agent_workspace(), "doc_inbox")


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return cleaned or "document.md"


def _archive_path(root: str, folder: str, path: str) -> str:
    target_dir = os.path.join(root, folder)
    os.makedirs(target_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(target_dir, f"{stamp}_{_safe_name(os.path.basename(path))}")


def _doc_index_path() -> str:
    return os.path.join(get_doc_import_dir(), "doc_index.json")


def _document_index_path() -> str:
    raw = str(DOC_IMPORT_LOCAL_INDEX_PATH or "").strip() or "doc_sources/Document_Index.md"
    if os.path.isabs(raw):
        return raw
    return os.path.join(get_agent_workspace(), raw)


def _load_doc_index() -> dict:
    path = _doc_index_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[DOC_IMPORT] 读取标题索引失败: {e}")
        return {}


def _save_doc_index(index: dict):
    path = _doc_index_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def _table_cell(text: str) -> str:
    return str(text or "").replace("\n", " ").replace("|", "\\|").strip()


def _doc_ref_token(value: str) -> str:
    text = str(value or "").strip()
    match = re.search(r"/(?:docx|doc)/([^/?#]+)", text)
    if match:
        return match.group(1)
    return "" if _is_url(text) else text


def _index_key(item: dict) -> str:
    return _doc_ref_token(item.get("doc_url") or item.get("doc_id") or "")


def _is_corrupt_title(title: str) -> bool:
    text = str(title or "").strip()
    return bool(re.fullmatch(r"\?{3,}", text)) or "\ufffd" in text


def _split_markdown_table_row(line: str) -> list[str]:
    text = str(line or "").strip()
    if not text.startswith("|") or not text.endswith("|"):
        return []
    cells = []
    current = []
    escaped = False
    for char in text[1:-1]:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def _read_document_index_entries():
    path = _document_index_path()
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[DOC_IMPORT] 读取本地文档索引失败: {e}")
        return None

    online_token = _doc_ref_token(DOC_IMPORT_ONLINE_INDEX_DOC)
    entries = {}
    for line in lines:
        cells = _split_markdown_table_row(line)
        if len(cells) < 2:
            continue
        title, link = cells[0].strip(), cells[1].strip()
        note = cells[2].strip() if len(cells) > 2 else ""
        if title in {"文档", "---"} or re.fullmatch(r"-+", title):
            continue
        key = _doc_ref_token(link)
        if not title or not key:
            continue
        if online_token and key == online_token:
            continue
        entries[key] = {
            "title": title,
            "doc_url": link,
            "note": note,
        }
    return entries


def _apply_document_index_edits(index: dict) -> dict:
    entries = _read_document_index_entries()
    if entries is None:
        return index

    existing_by_key = {}
    for item in index.values():
        key = _index_key(item)
        if key:
            existing_by_key[key] = item

    filtered = {}
    for key, local in entries.items():
        existing = existing_by_key.get(key) or {}
        local_title = str(local.get("title") or "").strip()
        existing_title = str(existing.get("title") or "").strip()
        title = existing_title if _is_corrupt_title(local_title) and existing_title else local_title
        if not title:
            continue
        filtered[title] = {
            "title": title,
            "doc_id": existing.get("doc_id") or key,
            "doc_url": local.get("doc_url") or existing.get("doc_url", ""),
            "updated_at": existing.get("updated_at") or int(time.time()),
            "note": local.get("note") or existing.get("note", ""),
        }
    return filtered


def _format_index_note(item: dict) -> str:
    note = str(item.get("note") or "").strip()
    if note:
        return note
    updated_at = int(item.get("updated_at") or 0)
    if updated_at:
        return datetime.fromtimestamp(updated_at).strftime("%Y-%m-%d 更新")
    return ""


def _format_document_index(index: dict) -> str:
    online_doc = str(DOC_IMPORT_ONLINE_INDEX_DOC or "").strip()
    online_token = _doc_ref_token(online_doc)
    rows = []
    if online_doc:
        rows.append(("文档汇总", online_doc, "本汇总文档"))
    for item in index.values():
        title = str(item.get("title") or "").strip()
        link = str(item.get("doc_url") or item.get("doc_id") or "").strip()
        if not title or not link:
            continue
        if online_token and _doc_ref_token(link) == online_token:
            continue
        rows.append((title, link, _format_index_note(item)))

    lines = [
        "# 菇菇GPT 文档汇总",
        "",
        "> 维护约定：以后每当我新增、更新或删除飞书文档时，需要同步更新本文档、`doc_sources/Document_Index.md` 和 `doc_inbox/doc_index.json`。",
        "",
        "## 文档列表",
        "",
        "| 文档 | 链接 | 备注 |",
        "|---|---|---|",
    ]
    for title, link, note in rows:
        lines.append(f"| {_table_cell(title)} | {_table_cell(link)} | {_table_cell(note)} |")
    return "\n".join(lines).rstrip() + "\n"


def _write_document_index(index: dict) -> str:
    path = _document_index_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = _format_document_index(index)
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(content)
    return path


def _sync_online_document_index(markdown: str):
    doc_ref = str(DOC_IMPORT_ONLINE_INDEX_DOC or "").strip()
    if not doc_ref:
        return

    markdown = re.sub(r"^\ufeff?\s*#\s+.+?(?:\r?\n){2,}", "", str(markdown or ""), count=1)
    temp_path = ""
    try:
        temp_path, _ = _create_temp_markdown_file(markdown)
        run_lark_skill_task(
            "按给定 Markdown 完整覆盖在线文档索引并设置标题；只操作指定在线索引。",
            {"doc_ref": doc_ref, "title": "菇菇GPT 文档汇总", "markdown_path": temp_path, "identity": DOC_IMPORT_CLI_AS or "user"},
            ("lark-doc",),
        )
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _sync_document_indexes(index: dict):
    index = _repair_corrupt_titles(index)
    path = _write_document_index(index)
    with open(path, "r", encoding="utf-8-sig") as f:
        markdown = f.read()
    try:
        _sync_online_document_index(markdown)
        return ""
    except Exception as e:
        warning = str(e)
        print(f"[DOC_IMPORT] 同步在线文档索引失败: {warning}")
        return warning


def _is_url(value: str) -> bool:
    return str(value or "").startswith(("http://", "https://"))


def _inspect_doc_title(doc_ref: str) -> str:
    ref = str(doc_ref or "").strip()
    if not ref:
        return ""
    try:
        result = run_lark_skill_task("读取指定文档的真实标题，不修改文档。", {"doc_ref": ref, "identity": "bot"}, ("lark-drive",), ("title",))
        return result["title"]
    except Exception as e:
        print(f"[DOC_IMPORT] 获取文档标题失败: {e}")
    return ""


def _repair_corrupt_titles(index: dict) -> dict:
    repaired = {}
    for item in index.values():
        title = str(item.get("title") or "").strip()
        if _is_corrupt_title(title):
            inspected = _inspect_doc_title(item.get("doc_url") or item.get("doc_id") or "")
            if inspected:
                title = inspected
                item = dict(item)
                item["title"] = title
        repaired[title] = item
    return repaired


def _bootstrap_doc_index_from_processed(index: dict) -> dict:
    processed_dir = os.path.join(get_doc_import_dir(), "processed")
    if not os.path.isdir(processed_dir):
        return index
    changed = False
    for name in sorted(os.listdir(processed_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(processed_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                item = json.load(f)
        except Exception:
            continue
        title = str(item.get("title") or "").strip()
        doc_ref = str(item.get("doc_id") or item.get("doc_url") or "").strip()
        if not title or not doc_ref:
            continue
        existing = index.get(title) or {}
        existing_url = str(existing.get("doc_url") or "").strip()
        item_url = str(item.get("doc_url") or "").strip()
        if title not in index or (not _is_url(existing_url) and _is_url(item_url)):
            index[title] = {
                "title": title,
                "doc_id": existing.get("doc_id") or item.get("doc_id", ""),
                "doc_url": item_url if _is_url(item_url) else existing_url,
                "updated_at": int(time.time()),
            }
            changed = True
    if changed:
        _save_doc_index(index)
    return index


def _remember_doc(title: str, result: dict):
    index = _apply_document_index_edits(_bootstrap_doc_index_from_processed(_load_doc_index()))
    existing = index.get(title) or {}
    result_url = str(result.get("doc_url") or "").strip()
    updated_at = int(time.time())
    action_text = "更新" if result.get("action") == "updated" else "创建"
    index[title] = {
        "title": title,
        "doc_id": result.get("doc_id", "") or existing.get("doc_id", ""),
        "doc_url": result_url if _is_url(result_url) else existing.get("doc_url", ""),
        "updated_at": updated_at,
        "note": datetime.fromtimestamp(updated_at).strftime(f"%Y-%m-%d {action_text}"),
    }
    index = _repair_corrupt_titles(index)
    _save_doc_index(index)
    index_sync_error = _sync_document_indexes(index)
    if index_sync_error:
        result["index_sync_error"] = index_sync_error


def _existing_doc_entry(title: str) -> dict:
    index = _apply_document_index_edits(_bootstrap_doc_index_from_processed(_load_doc_index()))
    return index.get(title) or {}


def _existing_doc_ref(title: str) -> str:
    item = _existing_doc_entry(title)
    return str(item.get("doc_id") or item.get("doc_url") or "").strip()


def _is_file_stable(path: str) -> bool:
    stable_seconds = max(1, int(DOC_IMPORT_STABLE_SECONDS or 5))
    try:
        stat = os.stat(path)
    except OSError:
        return False
    if time.time() - stat.st_mtime < stable_seconds:
        return False
    return stat.st_size > 0


def _read_markdown(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        return f.read().strip()


def _create_temp_markdown_file(markdown: str) -> tuple[str, str]:
    temp_dir = os.path.join(os.getcwd(), ".doc_import_tmp")
    os.makedirs(temp_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", suffix=".md", dir=temp_dir, delete=False) as f:
        f.write(markdown)
        temp_path = f.name
    return temp_path, os.path.relpath(temp_path, os.getcwd())


def _extract_title(path: str, markdown: str) -> tuple[str, str]:
    lines = markdown.splitlines()
    for index, line in enumerate(lines[:20]):
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            title = match.group(1).strip()
            body = "\n".join(lines[:index] + lines[index + 1:]).lstrip()
            return title, body or markdown
    return os.path.splitext(os.path.basename(path))[0], markdown


def _import_document_with_skills(path: str, doc_ref: str = "") -> dict:
    markdown = _read_markdown(path)
    if not markdown:
        raise ValueError("Markdown 文件为空")
    title, body = _extract_title(path, markdown)
    temp_path, _ = _create_temp_markdown_file(body)
    try:
        task = (
            "原样导入给定 Markdown 正文，标题单独设置。doc_ref 非空时覆盖更新该文档，禁止创建副本；为空时创建一次。"
            "创建和更新默认使用 identity；bot_author_enabled 为真时，新建须由机器人创建并填充正文，"
            "转移给当前登录用户并保留机器人 full_access，然后迁移到用户个人空间；"
            "若指定目标位置则尊重指定位置，不再迁移个人空间。更新时仅在 bot_author_enabled 为真时让机器人参与协作编辑。"
            "目标位置优先 wiki_node，其次 wiki_space，最后 folder_token。"
            "返回实际 doc_id、doc_url，以及 bot_author、owner_transfer、personal_space_move、bot_permission 的实际状态；"
            "未配置的后续步骤标记为 not_requested，不要伪报成功。"
        )
        skills = ("lark-doc", "lark-drive")
        if DOC_IMPORT_WIKI_NODE or DOC_IMPORT_WIKI_SPACE:
            skills += ("lark-wiki",)
        result = run_lark_skill_task(task, {
            "doc_ref": doc_ref, "title": title, "markdown_path": temp_path,
            "identity": DOC_IMPORT_CLI_AS or "user", "bot_author_enabled": DOC_IMPORT_BOT_AUTHOR_ENABLED,
            "bot_app_id": APP_ID, "wiki_node": DOC_IMPORT_WIKI_NODE,
            "wiki_space": DOC_IMPORT_WIKI_SPACE, "folder_token": DOC_IMPORT_FOLDER_TOKEN,
        }, skills, ("doc_id", "doc_url"))
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    result.update(action="updated" if doc_ref else "created", title=title)
    _remember_doc(title, result)
    return result


def create_lark_doc_from_markdown(path: str) -> dict:
    return _import_document_with_skills(path)


def update_lark_doc_from_markdown(path: str, doc_ref: str) -> dict:
    return _import_document_with_skills(path, doc_ref)


def import_lark_doc_from_markdown(path: str) -> dict:
    markdown = _read_markdown(path)
    if not markdown:
        raise ValueError("Markdown 文件为空")
    title, _body = _extract_title(path, markdown)
    doc_ref = _existing_doc_ref(title)
    if doc_ref:
        return update_lark_doc_from_markdown(path, doc_ref)
    return create_lark_doc_from_markdown(path)


def _notify(text: str):
    chat_id = DOC_IMPORT_NOTIFY_CHAT_ID or NOTIFY_CHAT_ID
    open_id = DOC_IMPORT_NOTIFY_OPEN_ID or NOTIFY_OPEN_ID
    sent = False
    if chat_id:
        send_card_to_chat(chat_id, text)
        sent = True
    if open_id:
        send_card_to_open_id(open_id, text)
        sent = True
    if not sent:
        print("[DOC_IMPORT] 未配置通知目标: " + text.replace("\n", " ")[:200])


def process_markdown_file(path: str):
    root = get_doc_import_dir()
    try:
        result = import_lark_doc_from_markdown(path)
        url_text = result.get("doc_url") or result.get("doc_id") or "未解析到文档链接，请查看日志"
        action_text = "已更新" if result.get("action") == "updated" else "已导入"
        lines = [
            f"✅ **Markdown {action_text}飞书文档**",
            "",
            f"- 标题：{result['title']}",
            f"- 链接：{url_text}",
        ]
        if result.get("personal_space_move") == "pending_ai_migration":
            lines.append("- 个人空间迁移：待 AI 继续执行")
        _notify("\n".join(lines))
        archived = _archive_path(root, "processed", path)
        shutil.move(path, archived)
        with open(archived + ".json", "w", encoding="utf-8", newline="\n") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[DOC_IMPORT] 已导入: {path} -> {url_text}")
    except Exception as e:
        failed = _archive_path(root, "failed", path)
        shutil.move(path, failed)
        with open(failed + ".error.txt", "w", encoding="utf-8", newline="\n") as f:
            f.write(str(e))
        _notify(f"⚠️ **Markdown 导入飞书文档失败**\n\n- 文件：{os.path.basename(path)}\n- 错误：{e}")
        print(f"[DOC_IMPORT] 导入失败: {path}: {e}")


def scan_doc_import_dir():
    root = get_doc_import_dir()
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, "processed"), exist_ok=True)
    os.makedirs(os.path.join(root, "failed"), exist_ok=True)
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        if not name.lower().endswith(".md"):
            continue
        if _is_file_stable(path):
            process_markdown_file(path)


def start_doc_import_watcher():
    if not DOC_IMPORT_ENABLED:
        return

    def _loop():
        root = get_doc_import_dir()
        print(f"[DOC_IMPORT] 已启用 Markdown 导入: {root}")
        while True:
            try:
                scan_doc_import_dir()
            except Exception as e:
                print(f"[DOC_IMPORT] 扫描异常: {e}")
            time.sleep(max(2, int(DOC_IMPORT_POLL_SECONDS or 10)))

    threading.Thread(target=_loop, daemon=True).start()
