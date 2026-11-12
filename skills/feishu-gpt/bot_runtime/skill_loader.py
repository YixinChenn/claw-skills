import os
import re
from pathlib import Path


SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SKILL_INVOCATION_PATTERN = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]{0,127})(?=\s|$)")


def get_workspace_skills_dir(workspace: str) -> str:
    return os.path.join(os.path.abspath(workspace), "skills")


def parse_workspace_skill_invocation(text: str) -> str | None:
    match = SKILL_INVOCATION_PATTERN.match(str(text or "").lstrip())
    return match.group(1) if match else None


def get_workspace_skill_file(workspace: str, skill_name: str) -> str | None:
    name = str(skill_name or "").strip()
    if not SKILL_NAME_PATTERN.fullmatch(name):
        return None
    path = discover_skills(workspace).get(name)
    return str(path) if path else None


def workspace_skill_exists(workspace: str, skill_name: str) -> bool:
    path = get_workspace_skill_file(workspace, skill_name)
    return bool(path and os.path.isfile(path))


def list_workspace_skills(workspace: str) -> list[str]:
    return sorted(discover_skills(workspace), key=str.casefold)


def build_workspace_skill_prompt(workspace: str, invocation_text: str | None) -> str:
    skill_name = parse_workspace_skill_invocation(invocation_text or "")
    if not skill_name:
        return ""

    skill_file = get_workspace_skill_file(workspace, skill_name)
    if not skill_file or not os.path.isfile(skill_file):
        return ""

    with open(skill_file, "r", encoding="utf-8-sig") as file:
        content = file.read().strip()
    if not content:
        raise ValueError(f"技能文件为空：{skill_file}")

    skill_dir = os.path.dirname(skill_file)
    return (
        f"本轮用户显式调用了工作区技能 `/{skill_name}`，必须遵循下面完整的 SKILL.md 指令。\n"
        f"技能目录：{skill_dir}\n"
        "SKILL.md 中的相对路径均相对于上述技能目录。"
        "如果技能引用了其他文件，只在当前任务需要时使用 read_skill 读取；不要猜测其内容。"
        "该技能只作用于本轮请求，后续轮次需要用户再次显式调用。\n\n"
        f"--- BEGIN WORKSPACE SKILL: {skill_name} ---\n"
        f"{content}\n"
        f"--- END WORKSPACE SKILL: {skill_name} ---"
    )


def discover_skills(workspace: str) -> dict[str, Path]:
    roots = [Path(workspace) / "skills", Path(workspace) / ".agents" / "skills", Path.home() / ".agents" / "skills"]
    skills = {}
    for index, root in enumerate(roots):
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            name = directory.name
            if index == 2 and not name.startswith("lark-"):
                continue
            path = directory / "SKILL.md"
            if SKILL_NAME_PATTERN.fullmatch(name) and path.is_file():
                skills.setdefault(name, path.resolve())
    return skills


def read_skill(workspace: str, name: str, relative_path: str = "SKILL.md") -> str:
    skills = discover_skills(workspace)
    if name not in skills:
        raise FileNotFoundError(f"未找到技能：{name}")
    if Path(relative_path).is_absolute():
        raise ValueError("技能参考文件必须使用相对路径")
    target = (skills[name].parent / relative_path).resolve()
    if not any(target.is_relative_to(path.parent) for path in skills.values()):
        raise ValueError("参考文件超出已发现的技能目录")
    content = target.read_text(encoding="utf-8-sig").strip()
    if not content:
        raise ValueError(f"技能文件为空：{target}")
    return f"文件：{target}\n相对路径基于：{target.parent}\n\n{content}"


def build_skill_catalog_prompt(workspace: str) -> str:
    lines = [
        "可用技能目录（按需加载）：",
        "涉及飞书能力时必须先用 read_skill 读取匹配的 lark-* SKILL.md，再按其路由读取必要参考文件。",
        "技能是操作指南，实际动作使用通用 run_shell；不得凭记忆猜测命令、参数或绕过技能。",
        "read_skill 的 path 相对于所选技能根目录，支持访问其他已发现技能的相对引用。",
        "没有匹配技能或读取失败时应报告缺失，不要回退到硬编码命令。",
    ]
    for name, path in discover_skills(workspace).items():
        content = path.read_text(encoding="utf-8-sig")
        match = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
        description = match.group(1).strip() if match else name
        lines.append(f"- {name}: {description}")
    return "\n".join(lines)
