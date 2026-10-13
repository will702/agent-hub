import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.db.models import Agent, Memory
from agent_hub.services import auth, events
from agent_hub.services.embeddings import embed

RRF_K = 60
REVIEW_ROLES = {"reviewer", "human", "admin"}


def _vec(v: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


async def write(
    s: AsyncSession, agent: Agent, project_id, content: str, kind: str = "note", source_ref: str | None = None
) -> Memory:
    await auth.require_access(s, agent, project_id, "write")
    [vec] = await embed([content])
    m = Memory(
        project_id=project_id, kind=kind, content=content, embedding=vec,
        source_agent=agent.name, source_ref=source_ref, status="draft", version=1,
    )
    s.add(m)
    await s.flush()
    await events.publish(
        s, type="memory.written", project_id=project_id, agent_name=agent.name,
        payload={"memory_id": str(m.id), "kind": kind},
    )
    return m


_SEARCH_SQL = text("""
WITH vec AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> CAST(:emb AS vector)) AS r
    FROM memories
    WHERE project_id = :p AND status = ANY(:statuses)
    ORDER BY embedding <=> CAST(:emb AS vector)
    LIMIT :n
), fts AS (
    SELECT id, row_number() OVER (ORDER BY ts_rank(tsv, websearch_to_tsquery('english', :q)) DESC) AS r
    FROM memories
    WHERE project_id = :p AND status = ANY(:statuses)
      AND tsv @@ websearch_to_tsquery('english', :q)
    ORDER BY ts_rank(tsv, websearch_to_tsquery('english', :q)) DESC
    LIMIT :n
), fused AS (
    SELECT coalesce(vec.id, fts.id) AS id,
           coalesce(1.0 / (:k + vec.r), 0) + coalesce(1.0 / (:k + fts.r), 0) AS score
    FROM vec FULL OUTER JOIN fts ON vec.id = fts.id
)
SELECT m.id, m.kind, m.content, m.status, m.source_agent, f.score, m.created_at
FROM fused f JOIN memories m ON m.id = f.id
ORDER BY f.score DESC, m.created_at DESC
LIMIT :limit
""")


async def search(
    s: AsyncSession, agent: Agent, project_id, query: str, limit: int = 8, include_drafts: bool = False
) -> list[dict]:
    await auth.require_access(s, agent, project_id, "read")
    [vec] = await embed([query])
    statuses = ["verified", "draft"] if include_drafts else ["verified"]
    rows = (
        await s.execute(
            _SEARCH_SQL,
            {
                "emb": _vec(vec), "p": project_id, "statuses": statuses, "q": query,
                "n": max(limit * 4, 20), "k": RRF_K, "limit": limit,
            },
        )
    ).mappings().all()
    return [dict(r) | {"score": float(r["score"])} for r in rows]


async def _get(s: AsyncSession, memory_id) -> Memory:
    if isinstance(memory_id, str):
        memory_id = uuid.UUID(memory_id)
    m = (
        await s.execute(
            text("SELECT id FROM memories WHERE id = :i FOR UPDATE"), {"i": memory_id}
        )
    ).first()
    if m is None:
        raise LookupError(f"memory {memory_id} not found")
    return await s.get(Memory, memory_id, populate_existing=True)


async def promote(s: AsyncSession, agent: Agent, memory_id) -> Memory:
    if not REVIEW_ROLES & set(agent.roles or []):
        raise PermissionError("promote requires reviewer, human or admin role")
    m = await _get(s, memory_id)
    await auth.require_access(s, agent, m.project_id, "write")
    if m.status == "superseded":
        raise ValueError("cannot promote a superseded memory")
    m.status = "verified"
    m.promoted_by = agent.name
    await s.flush()
    await events.publish(
        s, type="memory.promoted", project_id=m.project_id, agent_name=agent.name,
        payload={"memory_id": str(m.id)},
    )
    return m


async def supersede(s: AsyncSession, agent: Agent, memory_id, new_content: str) -> Memory:
    old = await _get(s, memory_id)
    await auth.require_access(s, agent, old.project_id, "write")
    if old.status == "superseded":
        raise ValueError("memory already superseded")
    if old.status == "verified" and not REVIEW_ROLES & set(agent.roles or []):
        raise PermissionError("superseding a verified memory requires reviewer, human or admin role")
    [vec] = await embed([new_content])
    new = Memory(
        project_id=old.project_id, kind=old.kind, content=new_content, embedding=vec,
        source_agent=agent.name, source_ref=old.source_ref, status="draft",
        version=old.version + 1, supersedes_id=old.id,
    )
    old.status = "superseded"
    s.add(new)
    await s.flush()
    await events.publish(
        s, type="memory.superseded", project_id=old.project_id, agent_name=agent.name,
        payload={"old_id": str(old.id), "new_id": str(new.id)},
    )
    return new
