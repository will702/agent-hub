"""Register the hub's MCP server with local AI clients.

Uses each client's own `mcp add` CLI rather than editing its config files, so we never guess
at schemas. Tokens stay in environment variables wherever the client supports it.
"""
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

NAME = "agent-hub"
BEGIN = "<!-- agent-hub:begin -->"
END = "<!-- agent-hub:end -->"

SNIPPET = f"""{BEGIN}
## agent-hub (shared memory and tasks)
When the `agent-hub` MCP server is connected:
- Before planning, call `project_context` and `memory_search` for the topic.
- Take work with `task_claim`; send `task_heartbeat` on long tasks; finish with `task_update` and a concrete result.
- Store durable facts and decisions with `memory_write` (never secrets). Read the `agent-hub-usage` skill first.
{END}
"""


@dataclass
class Plan:
    commands: list[list[str]] = field(default_factory=list)
    file_edits: list[tuple[Path, str]] = field(default_factory=list)  # (path, snippet to merge)
    notes: list[str] = field(default_factory=list)


def token_env(client: str) -> str:
    return f"AGENT_HUB_TOKEN_{client.upper().replace('-', '_')}"


def merge_snippet(existing: str, snippet: str = SNIPPET) -> str:
    """Insert or replace the marked block. Idempotent."""
    if BEGIN in existing and END in existing:
        head, rest = existing.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        return head + snippet.rstrip("\n") + tail
    sep = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + snippet


def build(client: str, url: str, token: str | None) -> Plan:
    env = token_env(client)
    home = Path.home()
    p = Plan()
    if client == "claude":
        p.commands = [
            ["claude", "mcp", "remove", NAME, "--scope", "user"],
            ["claude", "mcp", "add", "--transport", "http", "--scope", "user", "--header",
             f"Authorization: Bearer ${{{env}}}", NAME, url],
        ]
        p.file_edits = [(home / ".claude" / "CLAUDE.md", SNIPPET)]
        p.notes.append(f"export {env}=<token> in the shell that launches claude")
    elif client == "codex":
        p.commands = [
            ["codex", "mcp", "remove", NAME],
            ["codex", "mcp", "add", NAME, "--url", url, "--bearer-token-env-var", env],
        ]
        p.file_edits = [(home / ".codex" / "AGENTS.md", SNIPPET)]
        p.notes.append(f"export {env}=<token> in the shell that launches codex")
    elif client == "agy":
        if not token:
            raise ValueError(f"agy has no env-var auth; set {env} so the token can be written to its config")
        p.commands = [
            ["agy", "mcp", "remove", NAME],
            ["agy", "mcp", "add", "--header", f"Authorization: Bearer {token}", NAME, url],
        ]
        p.notes.append("agy stores the token in its own config file in plain text")
    elif client == "kiro":
        # kiro-cli cannot set HTTP headers, so go through the stdio shim.
        p.commands = [
            ["kiro-cli", "mcp", "remove", "--name", NAME],
            ["kiro-cli", "mcp", "add", "--name", NAME, "--command", "agent-hub", "--args", "stdio",
             "--args", "--url", "--args", url, "--env", f"AGENT_HUB_TOKEN={token or '<set-me>'}"],
        ]
        p.file_edits = [(home / ".kiro" / "steering" / "agent-hub.md", SNIPPET)]
        p.notes.append("kiro uses the stdio shim; `agent-hub` must be on PATH (e.g. `uv tool install .`)")
    else:
        raise ValueError(f"unknown client {client!r}")
    return p


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.bak-{int(time.time())}")
    shutil.copy2(path, dest)
    return dest


def apply_edit(path: Path, snippet: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new = merge_snippet(existing, snippet)
    if new == existing:
        return
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")


def run(plan: Plan) -> list[str]:
    """Execute commands (a failing `remove` is ignored) and file edits. Returns log lines."""
    log = []
    for cmd in plan.commands:
        if shutil.which(cmd[0]) is None:
            raise RuntimeError(f"{cmd[0]} not found on PATH")
        r = subprocess.run(cmd, capture_output=True, text=True)
        is_remove = "remove" in cmd[:4]
        if r.returncode != 0 and not is_remove:
            raise RuntimeError(f"{cmd[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
        log.append(f"ok  {' '.join(cmd[:4])} ...")
    for path, snip in plan.file_edits:
        apply_edit(path, snip)
        log.append(f"ok  instructions -> {path}")
    return log


def redact(cmd: list[str], token: str | None) -> str:
    s = " ".join(cmd)
    return s.replace(token, "<token>") if token else s
