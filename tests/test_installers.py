from agent_hub import installers


def test_merge_snippet_idempotent_and_preserves_content():
    base = "# My rules\nkeep me\n"
    once = installers.merge_snippet(base)
    twice = installers.merge_snippet(once)
    assert once == twice
    assert once.startswith(base) and once.count(installers.BEGIN) == 1


def test_merge_snippet_replaces_stale_block():
    old = f"top\n{installers.BEGIN}\nold text\n{installers.END}\nbottom\n"
    new = installers.merge_snippet(old)
    assert "old text" not in new and new.startswith("top\n") and new.endswith("bottom\n")


def test_build_plans_keep_tokens_out_of_commands():
    for c in ("claude", "codex"):
        plan = installers.build(c, "http://x/mcp", "SECRET")
        assert all("SECRET" not in " ".join(cmd) for cmd in plan.commands)
    assert installers.build("agy", "http://x/mcp", "SECRET").commands


def test_agy_requires_token_and_unknown_client():
    import pytest

    with pytest.raises(ValueError):
        installers.build("agy", "http://x/mcp", None)
    with pytest.raises(ValueError):
        installers.build("nope", "http://x/mcp", None)


def test_claude_name_and_url_precede_variadic_header():
    cmd = installers.build("claude", "http://x/mcp", None).commands[1]
    assert cmd.index("agent-hub") < cmd.index("--header") and cmd.index("http://x/mcp") < cmd.index("--header")
