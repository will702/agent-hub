import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.config import settings
from agent_hub.db.models import Agent
from agent_hub.services import auth, events

ALLOWED_STATUSES = {"in_progress", "review", "done", "failed", "blocked"}
ADMIN_ROLES = {"human", "admin"}
UPDATABLE_FROM = {"claimed", "in_progress", "review", "blocked"}


def _uid(v):
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


async def _promote_dependents(s: AsyncSession, task_id, project_id, agent_name) -> list[uuid.UUID]:
    rows = (
        await s.execute(
            text("""
            UPDATE tasks t SET status = 'ready', updated_at = now()
            WHERE t.status = 'pending'
              AND t.id IN (SELECT task_id FROM task_deps WHERE depends_on = :t)
              AND NOT EXISTS (
                  SELECT 1 FROM task_deps d JOIN tasks x ON x.id = d.depends_on
                  WHERE d.task_id = t.id AND x.status <> 'done')
            RETURNING t.id
            """),
            {"t": task_id},
        )
    ).all()
    ids = [r[0] for r in rows]
    for i in ids:
        await events.publish(
            s, type="task.ready", project_id=project_id, task_id=i, agent_name=agent_name,
            payload={"unblocked_by": str(task_id)},
        )
    return ids


async def create(
    s: AsyncSession, agent: Agent, project_id, title: str, spec: str = "",
    role_required: str = "developer", depends_on: list | None = None, priority: int = 0,
    requires_approval: bool = False, max_attempts: int = 3,
) -> dict:
    await auth.require_access(s, agent, project_id, "write")
    deps = [*dict.fromkeys(_uid(d) for d in (depends_on or []))]
    status = "ready"
    if deps:
        rows = (
            await s.execute(
                text("SELECT id, project_id, status FROM tasks WHERE id = ANY(:ids) ORDER BY id FOR SHARE"), {"ids": deps}
            )
        ).all()
        found = {r.id: r for r in rows}
        missing = [d for d in deps if d not in found]
        if missing:
            raise ValueError(f"dependency tasks not found: {[str(m) for m in missing]}")
        if any(r.project_id != project_id for r in rows):
            raise ValueError("dependencies must belong to the same project")
        if any(r.status != "done" for r in rows):
            status = "pending"
    tid = uuid.uuid4()
    row = (
        await s.execute(
            text("""
            INSERT INTO tasks (id, project_id, title, spec, role_required, status, priority,
                               requires_approval, max_attempts, attempts, created_by)
            VALUES (:id, :p, :title, :spec, :role, :status, :prio, :ra, :ma, 0, :by)
            RETURNING *
            """),
            {
                "id": tid, "p": project_id, "title": title, "spec": spec, "role": role_required,
                "status": status, "prio": priority, "ra": requires_approval, "ma": max_attempts,
                "by": agent.name,
            },
        )
    ).mappings().one()
    for d in deps:
        await s.execute(
            text("INSERT INTO task_deps (task_id, depends_on) VALUES (:t, :d)"), {"t": tid, "d": d}
        )
    await events.publish(
        s, type="task.created", project_id=project_id, task_id=tid, agent_name=agent.name,
        payload={"title": title, "status": status},
    )
    return dict(row)


async def claim(
    s: AsyncSession, agent: Agent, project_id, role: str | None = None, task_id=None
) -> dict | None:
    await auth.require_access(s, agent, project_id, "write")
    own = [*(agent.roles or [])]
    if role and role not in own and not (ADMIN_ROLES & set(own)):
        raise PermissionError(f"agent does not hold role {role!r}")
    roles = [role] if role else own
    sql = """
        UPDATE tasks SET status = 'claimed', claimed_by = :a,
               lease_expires_at = now() + make_interval(mins => :lease),
               attempts = attempts + 1, updated_at = now()
        WHERE id = (
            SELECT id FROM tasks
            WHERE project_id = :p AND status = 'ready' AND role_required = ANY(:roles)
            {tid}
            ORDER BY priority DESC, created_at
            FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING *
    """.format(tid="AND id = :tid" if task_id else "")
    params = {"a": agent.id, "p": project_id, "roles": roles, "lease": settings.lease_minutes}
    if task_id:
        params["tid"] = _uid(task_id)
    row = (await s.execute(text(sql), params)).mappings().first()
    if row is None:
        return None
    await events.publish(
        s, type="task.claimed", project_id=project_id, task_id=row["id"], agent_name=agent.name,
        payload={"attempt": row["attempts"]},
    )
    return dict(row)


async def _locked(s: AsyncSession, task_id) -> dict:
    row = (
        await s.execute(text("SELECT * FROM tasks WHERE id = :i FOR UPDATE"), {"i": _uid(task_id)})
    ).mappings().first()
    if row is None:
        raise LookupError(f"task {task_id} not found")
    return dict(row)


async def heartbeat(s: AsyncSession, agent: Agent, task_id) -> dict:
    t = await _locked(s, task_id)
    await auth.require_access(s, agent, t["project_id"], "write")
    if t["claimed_by"] != agent.id or t["status"] not in ("claimed", "in_progress"):
        raise PermissionError("task is not currently claimed by this agent")
    row = (
        await s.execute(
            text("""UPDATE tasks SET lease_expires_at = now() + make_interval(mins => :lease),
                    updated_at = now() WHERE id = :i RETURNING *"""),
            {"i": t["id"], "lease": settings.lease_minutes},
        )
    ).mappings().one()
    return dict(row)


async def update(
    s: AsyncSession, agent: Agent, task_id, status: str, result: str | None = None,
    branch: str | None = None, worktree_path: str | None = None,
) -> dict:
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"invalid status {status!r}; allowed: {sorted(ALLOWED_STATUSES)}")
    t = await _locked(s, task_id)
    await auth.require_access(s, agent, t["project_id"], "write")
    if t["claimed_by"] != agent.id and not (ADMIN_ROLES & set(agent.roles or [])):
        raise PermissionError("only the claimer (or human/admin) may update this task")
    if t["status"] in ("done", "awaiting_approval"):
        raise ValueError(f"task is already {t['status']}")
    if t["status"] not in UPDATABLE_FROM and not (ADMIN_ROLES & set(agent.roles or [])):
        raise ValueError(f"task is {t['status']}; only human/admin may update it")
    final = "awaiting_approval" if status == "done" and t["requires_approval"] else status
    row = (
        await s.execute(
            text("""
            UPDATE tasks SET status = :st,
                   result = coalesce(:result, result),
                   branch = coalesce(:branch, branch),
                   worktree_path = coalesce(:wt, worktree_path),
                   updated_at = now()
            WHERE id = :i RETURNING *
            """),
            {"st": final, "result": result, "branch": branch, "wt": worktree_path, "i": t["id"]},
        )
    ).mappings().one()
    await events.publish(
        s, type="task.updated", project_id=t["project_id"], task_id=t["id"], agent_name=agent.name,
        payload={"status": final, "requested": status},
    )
    if final == "done":
        await events.publish(
            s, type="task.done", project_id=t["project_id"], task_id=t["id"], agent_name=agent.name
        )
        await _promote_dependents(s, t["id"], t["project_id"], agent.name)
    return dict(row)


async def approve(s: AsyncSession, human_agent: Agent, task_id) -> dict:
    if not ADMIN_ROLES & set(human_agent.roles or []):
        raise PermissionError("only human/admin may approve")
    t = await _locked(s, task_id)
    await auth.require_access(s, human_agent, t["project_id"], "write")
    if t["status"] != "awaiting_approval":
        raise ValueError(f"task is {t['status']}, not awaiting_approval")
    row = (
        await s.execute(
            text("UPDATE tasks SET status = 'done', updated_at = now() WHERE id = :i RETURNING *"),
            {"i": t["id"]},
        )
    ).mappings().one()
    await events.publish(
        s, type="task.approved", project_id=t["project_id"], task_id=t["id"], agent_name=human_agent.name
    )
    await events.publish(
        s, type="task.done", project_id=t["project_id"], task_id=t["id"], agent_name=human_agent.name
    )
    await _promote_dependents(s, t["id"], t["project_id"], human_agent.name)
    return dict(row)


async def reap_expired(s: AsyncSession) -> int:
    rows = (
        await s.execute(
            text("""
            UPDATE tasks SET
                status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'ready' END,
                claimed_by = CASE WHEN attempts >= max_attempts THEN claimed_by ELSE NULL END,
                lease_expires_at = NULL, updated_at = now()
            WHERE id IN (
                SELECT id FROM tasks
                WHERE status IN ('claimed', 'in_progress') AND lease_expires_at < now()
                FOR UPDATE SKIP LOCKED)
            RETURNING id, project_id, status
            """)
        )
    ).all()
    for r in rows:
        await events.publish(
            s,
            type="task.failed_max_attempts" if r.status == "failed" else "task.requeued",
            project_id=r.project_id, task_id=r.id, agent_name="system",
        )
    return len(rows)


async def list(s: AsyncSession, agent: Agent, project_id, status: str | None = None) -> list[dict]:
    await auth.require_access(s, agent, project_id, "read")
    rows = (
        await s.execute(
            text("""SELECT * FROM tasks WHERE project_id = :p AND (CAST(:st AS text) IS NULL OR status = :st)
                    ORDER BY priority DESC, created_at"""),
            {"p": project_id, "st": status},
        )
    ).mappings().all()
    return [dict(r) for r in rows]
