# agent-hub

Shared memory, a task queue, skills and an event log for multiple AI coding agents
(Claude Code, Codex, Kiro, Antigravity, local MLX models, ...), exposed over MCP.
Agents share one Postgres + pgvector backend instead of each keeping its own notes.

## What it provides
- **Memory**: hybrid (vector + full-text) search; writes land as `draft`, only a reviewer/human promotes to `verified`.
- **Tasks**: role-based claiming (`FOR UPDATE SKIP LOCKED`), leases + heartbeats, dependencies, approval gate.
- **Skills**: shared `SKILL.md` files with per-agent allowlists (`agents/<name>.yaml`).
- **Events**: append-only audit log.
- **Auth**: per-agent bearer tokens (SHA-256 at rest), per-project read/write grants.

## Quick start
Requires Docker, Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
cp .env.example .env
docker compose up -d
uv sync
uv run agent-hub migrate
uv run agent-hub project add myproj --path ~/code/myproj
uv run agent-hub token create dev1 --kind codex --roles developer --project myproj
uv run agent-hub serve          # MCP at http://127.0.0.1:8765/mcp
uv run agent-hub doctor
uv run pytest -q
```

Clients authenticate with `Authorization: Bearer <token>`. For stdio-only clients use
`AGENT_HUB_TOKEN=... agent-hub stdio`.

## Security notes
The server binds to 127.0.0.1 by default and the compose file uses a development-only
database password. Change both before exposing it beyond your machine.

## Status
Phases 1-4 (core services, MCP server, CLI, skills) and `install-clients` (claude, codex, agy, kiro; `--dry-run` supported) are done. The
orchestrator (`agent-hub orchestrate`, plan-only unless `--live`) and the dashboard (`agent-hub dashboard`, localhost only) are done.

## License
MIT
