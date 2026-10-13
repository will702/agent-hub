"""Human dashboard: task board, approvals, memory promotion, events, agents.

Runs as a `human` agent (token from AGENT_HUB_TOKEN_HUMAN), binds to localhost, and protects
state-changing POSTs with a per-process CSRF token plus a Host check.
"""
import asyncio
import json
import secrets
import uuid

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from jinja2 import DictLoader, Environment, select_autoescape
from sqlalchemy import text

from agent_hub.db.models import Agent
from agent_hub.db.session import Session
from agent_hub.services import auth, memory, tasks

STATUSES = ["pending", "ready", "claimed", "in_progress", "review", "awaiting_approval", "blocked", "failed", "done"]

TEMPLATES = {
    "base.html": """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-hub</title><style>
:root{color-scheme:light dark;--bg:#fff;--fg:#1a1a1a;--mut:#666;--card:#f4f4f5;--bd:#d4d4d8;--ac:#2563eb}
@media(prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#999;--card:#1c1c1f;--bd:#333;--ac:#60a5fa}}
body{font:14px system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:0 16px 32px}
nav{display:flex;gap:16px;padding:12px 0;border-bottom:1px solid var(--bd);margin-bottom:16px;flex-wrap:wrap}
a{color:var(--ac)}h1{font-size:18px}.cols{display:flex;gap:12px;overflow-x:auto;align-items:flex-start}
.col{min-width:220px;flex:1}.col h2{font-size:12px;text-transform:uppercase;color:var(--mut)}
.card{background:var(--card);border:1px solid var(--bd);border-radius:6px;padding:8px;margin-bottom:8px}
.mut{color:var(--mut);font-size:12px}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:4px 8px;border-bottom:1px solid var(--bd);vertical-align:top}
button{cursor:pointer}pre{white-space:pre-wrap;margin:4px 0}
</style></head><body><nav><b>agent-hub</b><a href="/">Board</a><a href="/approvals">Approvals ({{ pending }})</a>
<a href="/memory">Memory</a><a href="/events">Events</a><a href="/agents">Agents</a></nav>{% block body %}{% endblock %}</body></html>""",
    "board.html": """{% extends "base.html" %}{% block body %}<h1>Task board</h1>
<div class="cols">{% for st in statuses %}<div class="col"><h2>{{ st }} ({{ by[st]|length }})</h2>
{% for t in by[st] %}<div class="card"><b>{{ t.title }}</b><div class="mut">{{ t.slug }} · {{ t.role_required }} · attempts {{ t.attempts }}/{{ t.max_attempts }}
{% if t.deps %}<br>waiting on {{ t.deps }}{% endif %}</div></div>{% endfor %}</div>{% endfor %}</div>{% endblock %}""",
    "approvals.html": """{% extends "base.html" %}{% block body %}<h1>Approvals</h1>
{% for t in rows %}<div class="card"><b>{{ t.title }}</b> <span class="mut">{{ t.slug }} · branch {{ t.branch or '-' }}</span>
<pre>{{ t.result or '(no result)' }}</pre>
<form method="post" action="/tasks/{{ t.id }}/approve"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Approve</button></form></div>
{% else %}<p class="mut">Nothing waiting.</p>{% endfor %}{% endblock %}""",
    "memory.html": """{% extends "base.html" %}{% block body %}<h1>Draft memories</h1>
{% for m in rows %}<div class="card"><span class="mut">{{ m.slug }} · {{ m.kind }} · {{ m.source_agent }}</span><pre>{{ m.content }}</pre>
<form method="post" action="/memory/{{ m.id }}/promote"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Promote to verified</button></form></div>
{% else %}<p class="mut">No drafts.</p>{% endfor %}{% endblock %}""",
    "events.html": """{% extends "base.html" %}{% block body %}<h1>Events</h1>
<form method="get"><input name="type" placeholder="type prefix" value="{{ q }}"> <button>Filter</button></form>
<p class="mut" id="live">live: connecting…</p>
<table><thead><tr><th>id</th><th>time</th><th>type</th><th>agent</th><th>payload</th></tr></thead><tbody id="rows">
{% for e in rows %}<tr><td>{{ e.id }}</td><td>{{ e.ts.strftime('%m-%d %H:%M:%S') }}</td><td>{{ e.type }}</td><td>{{ e.agent_name }}</td><td class="mut">{{ e.payload }}</td></tr>{% endfor %}</tbody></table>
<script>
(function(){
  var rows=document.getElementById('rows'), live=document.getElementById('live');
  var after={{ rows[0].id if rows else 0 }}, prefix={{ q|tojson }};
  var es=new EventSource('/events/stream?after='+after+'&type='+encodeURIComponent(prefix));
  es.onopen=function(){live.textContent='live';};
  es.onerror=function(){live.textContent='live: reconnecting…';};
  es.onmessage=function(m){
    var e=JSON.parse(m.data), tr=document.createElement('tr');
    [e.id,e.ts,e.type,e.agent_name||'',JSON.stringify(e.payload)].forEach(function(v,i){
      var td=document.createElement('td'); td.textContent=v; if(i==4) td.className='mut'; tr.appendChild(td);});
    rows.insertBefore(tr, rows.firstChild);
    while(rows.children.length>200) rows.removeChild(rows.lastChild);
  };
})();
</script>{% endblock %}""",
    "agents.html": """{% extends "base.html" %}{% block body %}<h1>Agents</h1>
<table><tr><th>name</th><th>kind</th><th>roles</th><th>projects</th></tr>
{% for a in rows %}<tr><td>{{ a.name }}</td><td>{{ a.kind }}</td><td>{{ a.roles|join(', ') }}</td><td>{{ a.projects }}</td></tr>{% endfor %}</table>{% endblock %}""",
}
_env = Environment(loader=DictLoader(TEMPLATES), autoescape=select_autoescape(default=True, default_for_string=True))


async def event_stream(prefix: str, after: int, interval: float = 1.0, max_ticks: int | None = None, is_gone=None):
    """Yield SSE frames for events newer than `after`. Polls the table; a comment keeps idle links open."""
    like = prefix.replace("\\", "").replace("%", "").replace("_", r"\_") + "%"
    ticks = idle = 0
    while max_ticks is None or ticks < max_ticks:
        if is_gone is not None and await is_gone():
            return
        async with Session() as s:
            rows = (await s.execute(text("""SELECT id, ts, type, agent_name, payload FROM events
                WHERE id > :a AND type LIKE :q ORDER BY id LIMIT 100"""), {"a": after, "q": like})).mappings().all()
        for r in rows:
            after = r["id"]
            data = {"id": r["id"], "ts": r["ts"].strftime("%m-%d %H:%M:%S"), "type": r["type"],
                    "agent_name": r["agent_name"], "payload": r["payload"]}
            yield f"data: {json.dumps(data, default=str)}\n\n"
        idle = 0 if rows else idle + 1
        if idle and idle % 15 == 0:
            yield ": keepalive\n\n"
        ticks += 1
        await asyncio.sleep(interval)


def create_app(token: str, allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")) -> FastAPI:
    app = FastAPI(title="agent-hub dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    csrf_token = secrets.token_urlsafe(24)

    @app.middleware("http")
    async def host_check(request: Request, call_next):
        if (request.headers.get("host") or "").split(":")[0] not in allowed_hosts:
            return HTMLResponse("forbidden host", status_code=403)
        return await call_next(request)

    async def human() -> Agent:
        async with Session() as s:
            a = await auth.authenticate(s, token)
        if a is None or "human" not in (a.roles or []):
            raise HTTPException(500, "dashboard token must belong to an agent with the human role")
        return a

    async def check_csrf(csrf: str = Form("")) -> None:
        if not secrets.compare_digest(csrf, csrf_token):
            raise HTTPException(403, "bad csrf token")

    async def page(name: str, **ctx) -> HTMLResponse:
        async with Session() as s:
            pending = (await s.execute(text("SELECT count(*) FROM tasks WHERE status='awaiting_approval'"))).scalar_one()
        return HTMLResponse(_env.get_template(name).render(pending=pending, csrf=csrf_token, **ctx))

    @app.get("/", response_class=HTMLResponse)
    async def board(_: Agent = Depends(human)):
        async with Session() as s:
            rows = (await s.execute(text("""
                SELECT t.id, t.title, t.status, t.role_required, t.attempts, t.max_attempts, p.slug,
                  (SELECT count(*) FROM task_deps d JOIN tasks x ON x.id=d.depends_on
                   WHERE d.task_id=t.id AND x.status<>'done') AS deps
                FROM tasks t JOIN projects p ON p.id=t.project_id
                ORDER BY t.priority DESC, t.created_at DESC LIMIT 500"""))).mappings().all()
        by = {st: [] for st in STATUSES}
        for r in rows:
            by.setdefault(r["status"], []).append(r)
        return await page("board.html", statuses=list(by), by=by)

    @app.get("/approvals", response_class=HTMLResponse)
    async def approvals(_: Agent = Depends(human)):
        async with Session() as s:
            rows = (await s.execute(text("""SELECT t.id, t.title, t.result, t.branch, p.slug FROM tasks t
                JOIN projects p ON p.id=t.project_id WHERE t.status='awaiting_approval' ORDER BY t.updated_at"""))).mappings().all()
        return await page("approvals.html", rows=rows)

    @app.post("/tasks/{task_id}/approve", dependencies=[Depends(check_csrf)])
    async def approve(task_id: uuid.UUID, agent: Agent = Depends(human)):
        async with Session() as s:
            try:
                await tasks.approve(s, agent, task_id)
            except (LookupError, ValueError) as e:
                raise HTTPException(409, str(e)) from None
            await s.commit()
        return RedirectResponse("/approvals", status_code=303)

    @app.get("/memory", response_class=HTMLResponse)
    async def memory_page(_: Agent = Depends(human)):
        async with Session() as s:
            rows = (await s.execute(text("""SELECT m.id, m.kind, m.content, m.source_agent, p.slug FROM memories m
                JOIN projects p ON p.id=m.project_id WHERE m.status='draft' ORDER BY m.created_at DESC LIMIT 200"""))).mappings().all()
        return await page("memory.html", rows=rows)

    @app.post("/memory/{memory_id}/promote", dependencies=[Depends(check_csrf)])
    async def promote(memory_id: uuid.UUID, agent: Agent = Depends(human)):
        async with Session() as s:
            try:
                await memory.promote(s, agent, memory_id)
            except (LookupError, ValueError) as e:
                raise HTTPException(409, str(e)) from None
            await s.commit()
        return RedirectResponse("/memory", status_code=303)

    @app.get("/events", response_class=HTMLResponse)
    async def events_page(type: str = "", _: Agent = Depends(human)):
        async with Session() as s:
            rows = (await s.execute(text("""SELECT id, ts, type, agent_name, payload FROM events
                WHERE type LIKE :q ORDER BY id DESC LIMIT 200"""), {"q": type.replace("%", "").replace("_", r"\_") + "%"})).mappings().all()
        return await page("events.html", rows=rows, q=type)

    @app.get("/events/stream")
    async def events_stream(request: Request, after: int = 0, type: str = "", _: Agent = Depends(human)):
        return StreamingResponse(
            event_stream(type, after, is_gone=request.is_disconnected), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/agents", response_class=HTMLResponse)
    async def agents_page(_: Agent = Depends(human)):
        async with Session() as s:
            rows = (await s.execute(text("""SELECT a.name, a.kind, a.roles,
                coalesce((SELECT string_agg(p.slug, ', ') FROM agent_project_access x JOIN projects p ON p.id=x.project_id
                          WHERE x.agent_id=a.id), '-') AS projects FROM agents a ORDER BY a.name"""))).mappings().all()
        return await page("agents.html", rows=rows)

    app.state.csrf = csrf_token
    return app
