"""内置参考执行器：真实业务测试、固定命令和执行失败边界。"""
import hashlib
from pathlib import Path
import subprocess

import pytest

from knowledge.reference_runner import (
    EXPECTED_TESTS, MAX_OUTPUT_BYTES, RUN_TIMEOUT_SECONDS, build_reference, run_reference,
)
from knowledge.semantics import KnowledgeError


def test_reference_bundle_is_stable_and_has_independent_code_and_tests():
    first = build_reference()
    second = build_reference()
    assert first == second
    assert set(first["files"]) == {"order_service.py", "test_order_service.py"}
    for name, content in first["files"].items():
        assert hashlib.sha256(content.encode("utf-8")).hexdigest() == first["sha256"][name]
    assert "from order_service import" in first["files"]["test_order_service.py"]
    first["files"]["order_service.py"] = "不可污染后续生成"
    assert build_reference() == second


@pytest.mark.parametrize("scenario", ["../../outside.py", "https://example.invalid/code", "unknown", None])
def test_unknown_scenarios_never_launch_a_process(monkeypatch, scenario):
    def unexpected_process(*args, **kwargs):
        pytest.fail("不允许的场景不能启动子进程")
    monkeypatch.setattr(subprocess, "run", unexpected_process)
    with pytest.raises(KnowledgeError, match="只允许"):
        run_reference(scenario)


def test_builtin_reference_really_executes_its_acceptance_tests():
    result = run_reference()
    assert result["executed"] is True
    assert result["outcome"] == "passed", result["stderr"]
    assert result["passed"] is True
    assert result["returncode"] == 0
    assert result["tests_run"] == EXPECTED_TESTS
    assert "test_repeat_cancellation_is_idempotent" in result["stderr"]
    assert "test_paid_order_is_rejected_without_mutation" in result["stderr"]
    assert result["output_truncated"] is False


def test_runner_uses_only_the_fixed_command_and_removes_its_workspace(monkeypatch):
    observed = {}
    def process(command, **kwargs):
        observed.update(command=command, **kwargs)
        workspace = kwargs["cwd"]
        assert (workspace / "order_service.py").is_file()
        assert (workspace / "test_order_service.py").is_file()
        assert len(list(workspace.iterdir())) == 2
        kwargs["stderr"].write(b"Ran 4 tests in 0.001s\n\nOK\n")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, "run", process)
    result = run_reference()
    assert result["passed"] is True
    assert observed["command"][1:6] == ["-I", "-B", "-X", "utf8", "-m"]
    assert observed["command"][6:] == ["unittest", "discover", "-s", ".", "-p", "test_order_service.py", "-v"]
    assert observed["shell"] is False
    assert observed["stdin"] == subprocess.DEVNULL
    assert observed["timeout"] == RUN_TIMEOUT_SECONDS
    assert "PYTHONPATH" not in observed["env"]
    assert "PYTHONHOME" not in observed["env"]
    assert not Path(observed["cwd"]).exists()


def test_nonzero_process_status_is_not_reported_as_passed(monkeypatch):
    def process(command, **kwargs):
        kwargs["stderr"].write(b"Ran 4 tests in 0.001s\n\nFAILED (failures=1)\n")
        return subprocess.CompletedProcess(command, 1)
    monkeypatch.setattr(subprocess, "run", process)
    result = run_reference()
    assert result["executed"] is True
    assert result["outcome"] == "failed"
    assert result["passed"] is False
    assert result["returncode"] == 1
    assert result["tests_run"] == 4


def test_timeout_preserves_bounded_partial_output_and_cleans_up(monkeypatch):
    observed = {}
    def process(command, **kwargs):
        observed["cwd"] = kwargs["cwd"]
        kwargs["stdout"].write(b"partial output")
        kwargs["stderr"].write(b"running tests")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", process)
    result = run_reference()
    assert result["outcome"] == "timeout"
    assert result["executed"] is True
    assert result["passed"] is False
    assert result["returncode"] is None
    assert result["stdout"] == "partial output"
    assert "超时" in result["error"]
    assert not Path(observed["cwd"]).exists()


def test_captured_output_has_a_fixed_size_limit(monkeypatch):
    def process(command, **kwargs):
        kwargs["stdout"].write(b"x" * (MAX_OUTPUT_BYTES + 1000))
        kwargs["stderr"].write(b"Ran 4 tests in 0.001s\n\nOK\n")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, "run", process)
    result = run_reference()
    assert result["passed"] is True
    assert len(result["stdout"].encode()) == MAX_OUTPUT_BYTES
    assert result["output_truncated"] is True


def test_successful_exit_without_discovering_required_tests_is_a_failure(monkeypatch):
    def process(command, **kwargs):
        kwargs["stderr"].write(b"Ran 0 tests in 0.000s\n\nOK\n")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, "run", process)
    result = run_reference()
    assert result["outcome"] == "failed"
    assert result["tests_run"] == 0
    assert result["passed"] is False


def test_process_launch_failure_does_not_claim_execution(monkeypatch):
    def process(command, **kwargs):
        raise OSError("合成进程启动故障")
    monkeypatch.setattr(subprocess, "run", process)
    result = run_reference()
    assert result["outcome"] == "failed"
    assert result["executed"] is False
    assert result["passed"] is False
    assert result["returncode"] is None
    assert "无法启动" in result["error"]
