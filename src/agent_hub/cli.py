import asyncio
import os
import shutil
import sys

import typer
from sqlalchemy import select, text

from agent_hub.config import ROOT, settings

app = typer.Typer(help="Agent Hub: shared memory, tasks, skills and events for coding agents.", no_args_is_help=True)
token_app = typer.Typer(help="Agent tokens.", no_args_is_help=True)
project_app = typer.Typer(help="Projects.", no_args_is_help=True)
agents_app = typer.Typer(help="Agents.", no_args_is_help=True)
app.add_typer(token_app, name="token")
app.add_typer(project_app, name="project")
app.add_typer(agents_app, name="agents")


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def _run(coro_fn):
    """Run an async fn in a fresh loop, disposing the shared engine afterwards."""

    async def main():
        from agent_hub.db.session import engine

        try:
            return await coro_fn()
        finally:
            await engine.dispose()

    return asyncio.run(main())


@app.command()
def migrate() -> None:
    """Apply database migrations (alembic upgrade head)."""
    from alembic import command

    command.upgrade(_alembic_cfg(), "head")
    typer.echo("migrated to head")


@app.command()
def serve() -> None:
    """Run the MCP server (streamable HTTP) on MCP_HOST:MCP_PORT at /mcp."""
    from agent_hub.mcp_server import serve as run

    run()


@token_app.command("create")
def token_create(
    name: str = typer.Argument(..., help="Unique agent name"),
    kind: str = typer.Option(..., "--kind", help="claude-code|codex|agy|kiro|zcode|mlx|custom|human"),
    roles: str = typer.Option(..., "--roles", help="Comma-separated roles, e.g. developer,reviewer"),
    project: list[str] = typer.Option([], "--project", help="Project slug to grant read+write (repeatable)"),
    model: str = typer.Option(None, "--model"),
) -> None:
    """Create an agent and print its bearer token (shown only once)."""
    from agent_hub.db.session import Session
    from agent_hub.services import auth

    role_list = [r.strip() for r in roles.split(",") if r.strip()]
    if not role_list:
        raise typer.BadParameter("at least one role is required", param_hint="--roles")

    async def go() -> str:
        async with Session() as s:
            agent, token = await auth.create_agent(s, name, kind, role_list, model=model)
            for slug in project:
                p = await auth.get_or_create_project(s, slug)
                await auth.grant(s, agent.id, p.id, ["write", "read"])
            await s.commit()
            return token

    try:
        token = _run(go)
    except Exception as e:  # noqa: BLE001
        typer.echo(f"error: could not create agent ({type(e).__name__}: {str(e).splitlines()[0][:200]})", err=True)
        raise typer.Exit(1)
    typer.echo(f"agent:   {name}")
    typer.echo(f"token:   {token}")
    typer.echo("Store this token now; it cannot be shown again.")


@project_app.command("add")
def project_add(
    slug: str = typer.Argument(...),
    path: str = typer.Option(..., "--path", help="Project root directory"),
) -> None:
    """Create a project (or set its root_path if unset)."""
    from agent_hub.db.session import Session
    from agent_hub.services import auth

    root = str(os.path.abspath(os.path.expanduser(path)))

    async def go():
        async with Session() as s:
            p = await auth.get_or_create_project(s, slug, root_path=root)
            await s.commit()
            return p.slug, p.root_path

    slug_, rp = _run(go)
    typer.echo(f"project {slug_} -> {rp}")


@agents_app.command("list")
def agents_list() -> None:
    """List agents, roles and project access."""
    from agent_hub.db.models import Agent, AgentProjectAccess, Project
    from agent_hub.db.session import Session

    async def go():
        async with Session() as s:
            agents = (await s.execute(select(Agent).order_by(Agent.name))).scalars().all()
            access = (
                await s.execute(
                    select(AgentProjectAccess.agent_id, Project.slug, AgentProjectAccess.perms).join(
                        Project, Project.id == AgentProjectAccess.project_id
                    )
                )
            ).all()
            return agents, access

    agents, access = _run(go)
    by_agent: dict = {}
    for aid, slug, perms in access:
        by_agent.setdefault(aid, []).append(f"{slug}({'+'.join(perms)})")
    for a in agents:
        typer.echo(
            f"{a.name}\tkind={a.kind}\troles={','.join(a.roles or [])}\tprojects={' '.join(by_agent.get(a.id, [])) or '-'}"
        )


@app.command()
def doctor() -> None:
    """Check DB, migrations, embedder and skills index. Exits non-zero on any FAIL."""
    ok_all = True

    def report(ok: bool, msg: str) -> None:
        nonlocal ok_all
        ok_all &= ok
        typer.echo(f"{'PASS' if ok else 'FAIL'}  {msg}")

    async def go():
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        from agent_hub.db.session import Session, engine
        from agent_hub.services import skills

        try:
            async with Session() as s:
                await s.execute(text("SELECT 1"))
            report(True, "database connection")
        except Exception as e:  # noqa: BLE001
            report(False, f"database connection ({type(e).__name__})")
            return
        try:
            head = ScriptDirectory.from_config(_alembic_cfg()).get_current_head()
            async with engine.connect() as conn:
                cur = await conn.run_sync(lambda c: MigrationContext.configure(c).get_current_revision())
            report(cur == head, f"alembic at head (current={cur}, head={head})")
        except Exception as e:  # noqa: BLE001
            report(False, f"alembic at head ({type(e).__name__})")
        try:
            from agent_hub.services.embeddings import embed

            [v] = await embed(["doctor check"])
            report(len(v) == settings.embed_dim, f"embedder '{settings.embedder}' loads (dim={len(v)}, expected {settings.embed_dim})")
        except Exception as e:  # noqa: BLE001
            report(False, f"embedder '{settings.embedder}' loads ({type(e).__name__}: {str(e)[:120]})")
        try:
            on_disk = len(skills.scan())
            async with Session() as s:
                n = (await s.execute(text("SELECT count(*) FROM skills"))).scalar_one()
            report(n == on_disk and n > 0, f"skills indexed: {n} in db, {on_disk} on disk")
        except Exception as e:  # noqa: BLE001
            report(False, f"skills indexed ({type(e).__name__})")

    _run(go)
    if not ok_all:
        raise typer.Exit(1)


@app.command("install-clients")
def install_clients(
    client: list[str] = typer.Argument(None, help="claude, codex, agy, kiro (default: all found on PATH)"),
    url: str = typer.Option(None, "--url", help="Hub URL (default http://MCP_HOST:MCP_PORT/mcp)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would change and exit"),
) -> None:
    """Register the hub with local AI clients and add the shared instructions snippet."""
    from agent_hub import installers

    target = url or f"http://{settings.mcp_host}:{settings.mcp_port}/mcp"
    binaries = {"claude": "claude", "codex": "codex", "agy": "agy", "kiro": "kiro-cli"}
    clients = client or [c for c, b in binaries.items() if shutil.which(b)]
    failed = False
    for c in clients:
        token = os.environ.get(installers.token_env(c), "").strip() or None
        try:
            plan = installers.build(c, target, token)
        except ValueError as e:
            typer.echo(f"[{c}] skipped: {e}", err=True)
            failed = True
            continue
        typer.echo(f"[{c}]")
        for cmd in plan.commands:
            typer.echo(f"  $ {installers.redact(cmd, token)}")
        for path, _ in plan.file_edits:
            typer.echo(f"  edit {path} (backup first, marked block)")
        for n in plan.notes:
            typer.echo(f"  note: {n}")
        if dry_run:
            continue
        try:
            for line in installers.run(plan):
                typer.echo(f"  {line}")
        except RuntimeError as e:
            typer.echo(f"  error: {e}", err=True)
            failed = True
    if failed:
        raise typer.Exit(1)


async def _bridge(url: str, token: str) -> None:
    import anyio
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.stdio import stdio_server
    from mcp.shared._httpx_utils import create_mcp_http_client

    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    assert isinstance(http, httpx2.AsyncClient)

    async with http, stdio_server() as (stdin_r, stdout_w), streamable_http_client(url, http_client=http) as (
        http_r,
        http_w,
    ):

        async def up() -> None:  # local stdio client -> HTTP server
            async for msg in stdin_r:
                if isinstance(msg, Exception):
                    print(f"agent-hub stdio: bad input: {type(msg).__name__}", file=sys.stderr)
                    continue
                await http_w.send(msg)
            tg.cancel_scope.cancel()  # stdin closed

        async def down() -> None:  # HTTP server -> local stdio client
            async for msg in http_r:
                if isinstance(msg, Exception):
                    print(f"agent-hub stdio: upstream error: {type(msg).__name__}: {msg}", file=sys.stderr)
                    continue
                await stdout_w.send(msg)

        async with anyio.create_task_group() as tg:
            tg.start_soon(up)
            tg.start_soon(down)


@app.command()
def stdio(
    url: str = typer.Option(None, "--url", help="Hub URL (default http://MCP_HOST:MCP_PORT/mcp)"),
) -> None:
    """stdio -> HTTP shim for clients that only speak stdio. Reads AGENT_HUB_TOKEN from the environment."""
    token = os.environ.get("AGENT_HUB_TOKEN", "").strip()
    if not token:
        typer.echo("error: AGENT_HUB_TOKEN is not set", err=True)
        raise typer.Exit(2)
    target = url or f"http://{settings.mcp_host}:{settings.mcp_port}/mcp"
    try:
        asyncio.run(_bridge(target, token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    app()
