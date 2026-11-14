from pathlib import Path

import pytest

from agent_harness import engines


def test_cooldown_parsing():
    assert engines.cooldown_seconds("Usage limit reached, try again in 2h 5m") == 2 * 3600 + 5 * 60 + 30
    assert engines.cooldown_seconds("limit; reset after 28m") == 28 * 60 + 30
    assert engines.cooldown_seconds("Fable requires usage credits") == engines.NO_CREDITS_COOLDOWN_SECONDS
    assert engines.cooldown_seconds("something else") == engines.DEFAULT_COOLDOWN_SECONDS


def test_codex_web_flag_precedes_exec(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "binary", lambda s, name: f"/bin/{name}")
    command = engines.codex_command(settings, settings.role("researcher"), cwd=tmp_path, out_file=tmp_path / "o",
                                    schema=None, add_dirs=[])
    assert command[:3] == ["/bin/codex", "--search", "exec"]
    plain = engines.codex_command(settings, settings.role("reviewer"), cwd=tmp_path, out_file=tmp_path / "o",
                                  schema=None, add_dirs=[])
    assert "--search" not in plain and "--ephemeral" in plain


def test_claude_web_role_allows_web_tools(settings, monkeypatch):
    monkeypatch.setattr(engines, "binary", lambda s, name: f"/bin/{name}")
    command = engines.claude_command(settings, settings.role("researcher_claude"))
    assert "WebSearch" in command[command.index("--allowedTools") + 1]


def test_fallback_runs_when_engine_unavailable(settings, monkeypatch, tmp_path):
    calls = []

    def missing_codex(*args, **kwargs):
        calls.append("codex")
        raise FileNotFoundError("codex CLI not found")

    def fake_claude(s, role, prompt, **kwargs):
        calls.append(role)
        return engines.CallResult(ok=True, text='{"verdict": "approve"} trailing', role=role, model="m",
                                  account="claude", seconds=0, log_path=None)

    monkeypatch.setattr(engines, "run_codex", missing_codex)
    monkeypatch.setattr(engines, "run_claude", fake_claude)
    schema = Path(engines.__file__).parent / "schemas" / "verdict.json"
    result = engines.run_role(settings, "reviewer", "review", log_dir=tmp_path, schema=schema)
    assert calls == ["codex", "reviewer_claude"]
    assert result.text == '{"verdict": "approve"}'


def test_no_fallback_raises(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "run_claude", lambda *a, **k: (_ for _ in ()).throw(engines.QuotaExhausted("x")))
    with pytest.raises(engines.QuotaExhausted):
        engines.run_role(settings, "scout", "hi", log_dir=tmp_path)


def test_default_account_is_codex_home(settings, monkeypatch, tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert engines.accounts(settings) == [{"name": "default", "codex_home": str(home)}]
