import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.db.models import Agent, AgentProjectAccess, Project

BYPASS_ROLES = {"human", "admin"}


def hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


async def create_agent(
    s: AsyncSession, name: str, kind: str, roles: list[str], model: str | None = None
) -> tuple[Agent, str]:
    token = "ah_" + secrets.token_urlsafe(32)
    agent = Agent(name=name, kind=kind, roles=list(roles), model=model, token_hash=hash_token(token))
    s.add(agent)
    await s.flush()
    return agent, token


async def authenticate(s: AsyncSession, token: str) -> Agent | None:
    return (
        await s.execute(select(Agent).where(Agent.token_hash == hash_token(token)))
    ).scalar_one_or_none()


async def grant(s: AsyncSession, agent_id, project_id, perms: list[str]) -> None:
    stmt = pg_insert(AgentProjectAccess).values(
        agent_id=agent_id, project_id=project_id, perms=list(perms)
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["agent_id", "project_id"], set_={"perms": stmt.excluded.perms}
    )
    await s.execute(stmt)


async def check_access(s: AsyncSession, agent: Agent, project_id, perm: str) -> bool:
    if BYPASS_ROLES & set(agent.roles or []):
        return True
    row = (
        await s.execute(
            select(AgentProjectAccess.perms).where(
                AgentProjectAccess.agent_id == agent.id,
                AgentProjectAccess.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    return row is not None and perm in row


async def require_access(s: AsyncSession, agent: Agent, project_id, perm: str) -> None:
    if not await check_access(s, agent, project_id, perm):
        raise PermissionError(f"agent {agent.name!r} lacks {perm!r} on project {project_id}")


async def get_or_create_project(s: AsyncSession, slug: str, root_path: str | None = None) -> Project:
    p = (await s.execute(select(Project).where(Project.slug == slug))).scalar_one_or_none()
    if p is None:
        p = Project(slug=slug, root_path=root_path)
        s.add(p)
        await s.flush()
    elif root_path and not p.root_path:
        p.root_path = root_path
        await s.flush()
    return p


async def resolve_project(s: AsyncSession, slug_or_none: str | None, cwd_or_none: str | None) -> Project:
    if slug_or_none:
        p = (await s.execute(select(Project).where(Project.slug == slug_or_none))).scalar_one_or_none()
        if p is None:
            raise LookupError(f"unknown project slug {slug_or_none!r}")
        return p
    if cwd_or_none:
        cwd = cwd_or_none.rstrip("/") or "/"
        rows = (await s.execute(select(Project).where(Project.root_path.is_not(None)))).scalars().all()
        best = None
        for p in rows:
            root = p.root_path.rstrip("/") or "/"
            if cwd == root or cwd.startswith(root.rstrip("/") + "/"):
                if best is None or len(root) > len(best.root_path.rstrip("/") or "/"):
                    best = p
        if best is not None:
            return best
    raise LookupError("could not resolve project from slug or cwd")
