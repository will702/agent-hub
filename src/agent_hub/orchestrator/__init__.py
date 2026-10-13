"""Dispatcher: reaps expired leases and spawns one runner process per ready task.

Runners claim their own task over MCP (task_claim with worktree=true) using the worker's
token, which is injected through the env var the installed client already reads
(AGENT_HUB_TOKEN_<RUNNER>). The orchestrator never claims tasks itself.
"""
import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import text

from agent_hub.config import settings

DEFAULT_COMMANDS = {
    "claude": ["claude", "-p", "{prompt}"],
    "codex": ["codex", "exec", "{prompt}"],
}


@dataclass
class AgentSpec:
    name: str
    runner: str
    roles: list[str]
    max_parallel: int = 1
    prompt: str = ""
    command: list[str] | None = None


@dataclass
class Spawn:
    spec: AgentSpec
    role: str
    project_slug: str


@dataclass
class State:
    running: dict[str, int] = field(default_factory=dict)  # worker name -> live processes


def load_specs(agents_dir: Path | None = None) -> list[AgentSpec]:
    specs = []
    for f in sorted(Path(agents_dir or settings.agents_dir).glob("*.yaml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        specs.append(
            AgentSpec(
                name=d["name"], runner=d.get("runner", ""), roles=list(d.get("roles", [])),
                max_parallel=int(d.get("max_parallel", 1)), prompt=d.get("prompt", "").strip(),
                command=d.get("command"),
            )
        )
    return specs


def plan(specs: list[AgentSpec], ready: dict[tuple[str, str], int], state: State) -> list[Spawn]:
    """ready maps (project_slug, role) -> number of ready tasks. Pure; no I/O."""
    left = dict(ready)
    out: list[Spawn] = []
    free = {s.name: s.max_parallel - state.running.get(s.name, 0) for s in specs}
    for s in specs:
        for (slug, role), n in sorted(left.items()):
            while role in s.roles and n > 0 and free[s.name] > 0:
                out.append(Spawn(s, role, slug))
                n -= 1
                free[s.name] -= 1
            left[(slug, role)] = n
    return out


def build_command(sp: Spawn) -> list[str]:
    tmpl = sp.spec.command or DEFAULT_COMMANDS.get(sp.spec.runner)
    if not tmpl:
        raise ValueError(f"no command for runner {sp.spec.runner!r}; set `command:` in agents/{sp.spec.name}.yaml")
    prompt = (
        f"{sp.spec.prompt}\n\nUse the agent-hub MCP server. Call task_claim(project='{sp.project_slug}', "
        f"role='{sp.role}', worktree=true) to take exactly one task, do it, send task_heartbeat on long "
        "work, and finish with task_update and a concrete result."
    ).strip()
    return [a.replace("{prompt}", prompt) for a in tmpl]


def worker_env(spec: AgentSpec) -> dict[str, str] | None:
    """Child env with the worker's token under the runner's client env var, or None if no token."""
    token = os.environ.get(f"AGENT_HUB_TOKEN_{spec.name.upper().replace('-', '_')}", "").strip()
    if not token:
        return None
    env = dict(os.environ)
    for k in [k for k in env if k.startswith("AGENT_HUB_TOKEN_")]:
        del env[k]
    env[f"AGENT_HUB_TOKEN_{spec.runner.upper()}"] = token
    return env


async def ready_counts() -> dict[tuple[str, str], int]:
    from agent_hub.db.session import Session

    async with Session() as s:
        rows = (
            await s.execute(
                text("""SELECT p.slug, t.role_required, count(*) FROM tasks t
                        JOIN projects p ON p.id = t.project_id
                        WHERE t.status = 'ready' GROUP BY 1, 2""")
            )
        ).all()
    return {(r[0], r[1]): r[2] for r in rows}


async def _run_one(sp: Spawn, state: State, timeout: int, log_dir: Path) -> None:
    name = sp.spec.name
    env = worker_env(sp.spec)
    try:
        cmd = build_command(sp)
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / f"{name}-{int(time.time())}.log", "wb") as log:
            proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdout=log, stderr=log)
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
    finally:
        state.running[name] = max(0, state.running.get(name, 1) - 1)


async def run(live: bool, interval: int = 15, timeout: int = 3600, once: bool = False, echo=print) -> None:
    from agent_hub.db.session import Session
    from agent_hub.services import tasks

    specs = load_specs()
    state = State()
    log_dir = Path.home() / ".agent-hub" / "logs"
    jobs: set[asyncio.Task] = set()
    while True:
        async with Session() as s:
            n = await tasks.reap_expired(s)
            await s.commit()
        if n:
            echo(f"reaped {n} expired task(s)")
        for sp in plan(specs, await ready_counts(), state):
            problem = None
            if shutil.which((sp.spec.command or DEFAULT_COMMANDS.get(sp.spec.runner) or [""])[0]) is None:
                problem = f"runner binary for {sp.spec.runner!r} not found"
            elif worker_env(sp.spec) is None:
                problem = f"no AGENT_HUB_TOKEN_{sp.spec.name.upper().replace('-', '_')} in environment"
            if problem:
                echo(f"skip   {sp.spec.name} <- {sp.project_slug}/{sp.role}: {problem}")
                continue
            echo(f"{'spawn ' if live else 'would '} {sp.spec.name} <- {sp.project_slug}/{sp.role}")
            if live:
                state.running[sp.spec.name] = state.running.get(sp.spec.name, 0) + 1
                t = asyncio.create_task(_run_one(sp, state, timeout, log_dir))
                jobs.add(t)
                t.add_done_callback(jobs.discard)
        if once:
            break
        await asyncio.sleep(interval)
    if jobs:
        await asyncio.gather(*jobs)
