import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.config import settings
from agent_hub.db.models import SkillIndex

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)", re.DOTALL)


def parse_skill(text: str) -> tuple[dict, str]:
    """Split a SKILL.md into (frontmatter dict, body)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), m.group(2)


def scan() -> list[dict]:
    """Parse every shared/skills/*/SKILL.md into {name, description, path}."""
    out = []
    root = Path(settings.skills_dir)
    if not root.is_dir():
        return out
    for f in sorted(root.glob("*/SKILL.md")):
        meta, _ = parse_skill(f.read_text(encoding="utf-8"))
        out.append(
            {
                "name": str(meta.get("name") or f.parent.name),
                "description": str(meta.get("description") or ""),
                "path": str(f),
            }
        )
    return out


async def index(s: AsyncSession) -> int:
    """Upsert all skills on disk into the skills table; delete rows for removed skills."""
    found = {r["name"]: r for r in scan()}
    existing = {r.name: r for r in (await s.execute(select(SkillIndex))).scalars().all()}
    for name, r in found.items():
        row = existing.get(name)
        if row is None:
            s.add(SkillIndex(name=name, description=r["description"], path=r["path"]))
        else:
            row.description = r["description"]
            row.path = r["path"]
    stale = [n for n in existing if n not in found]
    if stale:
        await s.execute(delete(SkillIndex).where(SkillIndex.name.in_(stale)))
    await s.flush()
    return len(found)


def _agent_skill_filter(agent_name: str) -> list[str] | None:
    f = Path(settings.agents_dir) / f"{agent_name}.yaml"
    if "/" in agent_name or "\\" in agent_name or agent_name in ("", ".", "..") or not f.is_file():
        return None
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    skills = data.get("skills") if isinstance(data, dict) else None
    return [str(x) for x in skills] if isinstance(skills, list) else None


async def list_for(s: AsyncSession, agent_name: str) -> list[dict]:
    rows = (await s.execute(select(SkillIndex).order_by(SkillIndex.name))).scalars().all()
    allowed = _agent_skill_filter(agent_name)
    return [
        {"name": r.name, "description": r.description}
        for r in rows
        if allowed is None or r.name in allowed
    ]


def read(name: str, agent_name: str | None = None) -> str:
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or Path(name).name != name
    ):
        raise ValueError(f"invalid skill name {name!r}")
    if agent_name is not None:
        allowed = _agent_skill_filter(agent_name)
        if allowed is not None and name not in allowed:
            raise PermissionError(f"skill {name!r} is not available to this agent")
    root = Path(settings.skills_dir).resolve()
    f = (root / name / "SKILL.md").resolve()
    if f.parent.parent != root:
        raise ValueError(f"invalid skill name {name!r}")
    if not f.is_file():
        raise LookupError(f"skill {name!r} not found")
    return f.read_text(encoding="utf-8")


async def watch(callback: Callable[[], Awaitable[None]]) -> None:
    """Call `callback` (async, no args) whenever files under skills_dir change."""
    from watchfiles import awatch

    root = Path(settings.skills_dir)
    root.mkdir(parents=True, exist_ok=True)
    async for _changes in awatch(root):
        await callback()
