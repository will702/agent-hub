import plistlib

from agent_hub import launchd


def test_render_roundtrips_and_keeps_tokens_out():
    d = launchd.render("server", ["serve"], exe="/x/agent-hub")
    assert plistlib.loads(plistlib.dumps(d))["Label"] == "com.agenthub.server"
    cmd = d["ProgramArguments"][2]
    assert "exec /x/agent-hub serve" in cmd and "tokens.env" in cmd and "ah_" not in cmd


def test_orchestrator_is_plan_only_unless_live():
    assert "--live" not in launchd.services(False)["orchestrator"]
    assert "--live" in launchd.services(True)["orchestrator"]
