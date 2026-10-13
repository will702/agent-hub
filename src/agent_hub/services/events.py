from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.db.models import Event


async def publish(
    s: AsyncSession, *, type: str, project_id=None, task_id=None, agent_name=None, payload=None
) -> Event:
    ev = Event(type=type, project_id=project_id, task_id=task_id, agent_name=agent_name, payload=payload or {})
    s.add(ev)
    await s.flush()
    return ev


async def tail(s: AsyncSession, project_id=None, after_id: int = 0, limit: int = 100) -> list[dict]:
    q = select(Event).where(Event.id > after_id)
    if project_id is not None:
        q = q.where(Event.project_id == project_id)
    rows = (await s.execute(q.order_by(Event.id).limit(limit))).scalars().all()
    return [
        {
            "id": e.id, "ts": e.ts, "project_id": e.project_id, "task_id": e.task_id,
            "agent_name": e.agent_name, "type": e.type, "payload": e.payload,
        }
        for e in rows
    ]
