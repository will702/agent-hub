from agent_hub.orchestrator import AgentSpec, Spawn, State, build_command, load_specs, plan, worker_env


def spec(name, roles, n=1, runner="claude"):
    return AgentSpec(name=name, runner=runner, roles=roles, max_parallel=n)


def test_plan_respects_max_parallel_and_roles():
    specs = [spec("dev", ["developer"], 2), spec("rev", ["reviewer"])]
    out = plan(specs, {("p", "developer"): 5, ("p", "reviewer"): 1, ("p", "manager"): 3}, State())
    assert [(o.spec.name, o.role) for o in out] == [("dev", "developer")] * 2 + [("rev", "reviewer")]


def test_plan_subtracts_running():
    out = plan([spec("dev", ["developer"], 2)], {("p", "developer"): 5}, State(running={"dev": 1}))
    assert len(out) == 1


def test_plan_does_not_double_assign_tasks():
    specs = [spec("a", ["developer"], 3), spec("b", ["developer"], 3)]
    assert len(plan(specs, {("p", "developer"): 2}, State())) == 2


def test_build_command_and_unknown_runner():
    sp = Spawn(spec("dev", ["developer"]), "developer", "proj")
    cmd = build_command(sp)
    assert cmd[:2] == ["claude", "-p"] and "task_claim(project='proj'" in cmd[2]
    import pytest

    with pytest.raises(ValueError):
        build_command(Spawn(spec("x", ["developer"], runner="mlx"), "developer", "p"))


def test_worker_env_swaps_token(monkeypatch):
    monkeypatch.setenv("AGENT_HUB_TOKEN_DEV", "tok")
    monkeypatch.setenv("AGENT_HUB_TOKEN_CLAUDE", "interactive")
    env = worker_env(spec("dev", ["developer"]))
    assert env["AGENT_HUB_TOKEN_CLAUDE"] == "tok" and "AGENT_HUB_TOKEN_DEV" not in env
    monkeypatch.delenv("AGENT_HUB_TOKEN_DEV")
    assert worker_env(spec("dev", ["developer"])) is None


def test_load_specs_reads_repo_agents():
    assert {s.name for s in load_specs()} >= {"developer", "reviewer", "manager"}
