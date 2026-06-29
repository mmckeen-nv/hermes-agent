from pathlib import Path

from agent.dml_status import build_dml_status_report
from hermes_cli.commands import gateway_help_lines, is_gateway_known_command, resolve_command


class DummyProvider:
    name = "daystrom_dml"

    def decide_iteration_extension(self, run_state):
        return {"extend": True, "reason": "test"}


class DummyMemoryManager:
    def __init__(self):
        self.providers = [DummyProvider()]

    def decide_iteration_extension(self, run_state):
        return {"extend": True, "reason": "test"}


class DummyAgent:
    def __init__(self):
        self._memory_manager = DummyMemoryManager()


def _minimal_dml_config(tmp_path: Path):
    for name in ("integration", "source", "storage"):
        (tmp_path / name).mkdir()
    for name in ("launcher", "python", "config.yaml"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    return {
        "memory": {
            "provider": "daystrom_dml",
            "daystrom_dml": {
                "retrieval_policy": "always",
                "sync_turns": True,
                "enable_memory": True,
                "dcn": {"mode": "active_read"},
                "integration_dir": str(tmp_path / "integration"),
                "launcher": str(tmp_path / "launcher"),
                "venv_python": str(tmp_path / "python"),
                "source_dir": str(tmp_path / "source"),
                "storage_dir": str(tmp_path / "storage"),
                "config_path": str(tmp_path / "config.yaml"),
            },
        },
        "agent": {
            "max_turns": 90,
            "max_turns_auto_extend": True,
            "max_turns_extension_policy": "cognition",
            "max_turns_extension": 30,
            "max_turns_hard_cap": 180,
        },
    }


def test_dml_help_registered_as_gateway_command():
    cmd = resolve_command("/dml-help")
    assert cmd is not None
    assert cmd.name == "dml-help"
    assert is_gateway_known_command("dml-help") is True
    assert any("/dml-help" in line for line in gateway_help_lines())


def test_dml_status_report_shows_clean_preflight_and_runtime_hooks(tmp_path):
    report = build_dml_status_report(
        config=_minimal_dml_config(tmp_path),
        hermes_home=tmp_path,
        agent=DummyAgent(),
    )

    assert "Daystrom DML help / status" in report
    assert "✅ Memory provider is Daystrom DML" in report
    assert "✅ Auto-extension enabled" in report
    assert "✅ Extension policy uses cognition" in report
    assert "✅ daystrom_dml provider is active at runtime" in report
    assert "✅ DML provider exposes iteration-extension hook" in report
    assert "✅ Preflight clean" in report
    assert "secret-safe" in report


def test_dml_status_report_skips_preflight_when_not_dml(tmp_path):
    report = build_dml_status_report(
        config={"memory": {"provider": "builtin"}, "agent": {}},
        hermes_home=tmp_path,
    )

    assert "❌ Memory provider is Daystrom DML" in report
    assert "Skipped because this profile is not configured" in report
