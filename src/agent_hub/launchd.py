"""Render and install launchd agents for the server, dashboard and orchestrator."""
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from agent_hub.config import ROOT

PREFIX = "com.agenthub"
TOKENS = Path.home() / ".agent-hub" / "tokens.env"
LOG_DIR = Path.home() / ".agent-hub" / "logs"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def binary() -> str:
    return str(Path(sys.argv[0]).resolve()) if Path(sys.argv[0]).name == "agent-hub" else str(ROOT / ".venv" / "bin" / "agent-hub")


def services(live: bool) -> dict[str, list[str]]:
    orch = ["orchestrate"] + (["--live"] if live else [])
    return {"server": ["serve"], "dashboard": ["dashboard"], "orchestrator": orch}


def render(name: str, args: list[str], exe: str | None = None) -> dict:
    """The tokens file is sourced by a shell wrapper so tokens never appear in the plist."""
    exe = exe or binary()
    cmd = f"set -a; [ -f {shlex.quote(str(TOKENS))} ] && . {shlex.quote(str(TOKENS))}; set +a; exec {shlex.quote(exe)} {' '.join(map(shlex.quote, args))}"
    return {
        "Label": f"{PREFIX}.{name}",
        "ProgramArguments": ["/bin/sh", "-c", cmd],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(LOG_DIR / f"{name}.log"),
        "StandardErrorPath": str(LOG_DIR / f"{name}.err.log"),
        "EnvironmentVariables": {"PATH": f"{Path(exe).parent}:{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    }


def install(live: bool, uid: int) -> list[str]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    log = []
    for name, args in services(live).items():
        path = AGENTS_DIR / f"{PREFIX}.{name}.plist"
        path.write_bytes(plistlib.dumps(render(name, args)))
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True, text=True)
        log.append(f"{'ok  ' if r.returncode == 0 else 'FAIL'} {name} -> {path}" + ("" if r.returncode == 0 else f" ({r.stderr.strip()[:200]})"))
    return log


def uninstall(uid: int) -> list[str]:
    log = []
    for name in services(False):
        path = AGENTS_DIR / f"{PREFIX}.{name}.plist"
        if path.exists():
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
            path.unlink()
            log.append(f"removed {path}")
    return log
