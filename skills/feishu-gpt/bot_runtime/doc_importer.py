import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime

from .config_runtime import (
    DOC_IMPORT_CLI_AS,
    DOC_IMPORT_DIR,
    DOC_IMPORT_ENABLED,
    DOC_IMPORT_FOLDER_TOKEN,
    DOC_IMPORT_NOTIFY_CHAT_ID,
    DOC_IMPORT_NOTIFY_OPEN_ID,
    DOC_IMPORT_POLL_SECONDS,
    DOC_IMPORT_STABLE_SECONDS,
    DOC_IMPORT_WIKI_NODE,
    DOC_IMPORT_WIKI_SPACE,
    NOTIFY_CHAT_ID,
    NOTIFY_OPEN_ID,
)
from .messaging import send_card_to_chat, send_card_to_open_id
from .paths import get_agent_workspace
from .tools import run_feishu_cli


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


def _is_url(value: str) -> bool:
    return str(value or "").startswith(("http://", "https://"))


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
    index = _bootstrap_doc_index_from_processed(_load_doc_index())
    existing = index.get(title) or {}
    result_url = str(result.get("doc_url") or "").strip()
    index[title] = {
        "title": title,
        "doc_id": result.get("doc_id", "") or existing.get("doc_id", ""),
        "doc_url": result_url if _is_url(result_url) else existing.get("doc_url", ""),
        "updated_at": int(time.time()),
    }
    _save_doc_index(index)


def _existing_doc_entry(title: str) -> dict:
    index = _bootstrap_doc_index_from_processed(_load_doc_index())
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


def _append_target_args(args: list[str]):
    if DOC_IMPORT_WIKI_NODE:
        args.extend(["--wiki-node", DOC_IMPORT_WIKI_NODE])
    elif DOC_IMPORT_WIKI_SPACE:
        args.extend(["--wiki-space", DOC_IMPORT_WIKI_SPACE])
    elif DOC_IMPORT_FOLDER_TOKEN:
        args.extend(["--folder-token", DOC_IMPORT_FOLDER_TOKEN])


def _parse_json_object(text: str) -> dict:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return {}


def _find_value(data, keys: set[str]) -> str:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in keys and isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            found = _find_value(value, keys)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_value(item, keys)
            if found:
                return found
    return ""


def create_lark_doc_from_markdown(path: str) -> dict:
    markdown = _read_markdown(path)
    if not markdown:
        raise ValueError("Markdown 文件为空")
    title, body = _extract_title(path, markdown)

    temp_path = ""
    try:
        temp_path, temp_relpath = _create_temp_markdown_file(body)

        args = ["docs", "+create"]
        if DOC_IMPORT_CLI_AS:
            args.extend(["--as", DOC_IMPORT_CLI_AS])
        args.extend(["--title", title, "--markdown", f"@{temp_relpath}"])
        _append_target_args(args)

        payload = json.loads(run_feishu_cli(args))
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
    stdout_data = _parse_json_object(payload.get("stdout", ""))
    doc_url = _find_value(stdout_data, {"doc_url", "url"})
    doc_id = _find_value(stdout_data, {"doc_id", "document_id", "token"})
    result = {
        "action": "created",
        "title": title,
        "doc_url": doc_url,
        "doc_id": doc_id,
        "stdout": payload.get("stdout", ""),
    }
    _remember_doc(title, result)
    return result


def update_lark_doc_from_markdown(path: str, doc_ref: str) -> dict:
    markdown = _read_markdown(path)
    if not markdown:
        raise ValueError("Markdown 文件为空")
    title, body = _extract_title(path, markdown)

    temp_path = ""
    try:
        temp_path, temp_relpath = _create_temp_markdown_file(body)

        args = ["docs", "+update"]
        if DOC_IMPORT_CLI_AS:
            args.extend(["--as", DOC_IMPORT_CLI_AS])
        args.extend(["--doc", doc_ref, "--mode", "overwrite", "--new-title", title, "--markdown", f"@{temp_relpath}"])

        payload = json.loads(run_feishu_cli(args))
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    stdout_data = _parse_json_object(payload.get("stdout", ""))
    existing = _existing_doc_entry(title)
    doc_url = _find_value(stdout_data, {"doc_url", "url"}) or existing.get("doc_url", "")
    doc_id = _find_value(stdout_data, {"doc_id", "document_id", "token"}) or doc_ref
    result = {
        "action": "updated",
        "title": title,
        "doc_url": doc_url,
        "doc_id": doc_id,
        "stdout": payload.get("stdout", ""),
    }
    _remember_doc(title, result)
    return result


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
        _notify(f"✅ **Markdown {action_text}飞书文档**\n\n- 标题：{result['title']}\n- 链接：{url_text}")
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
