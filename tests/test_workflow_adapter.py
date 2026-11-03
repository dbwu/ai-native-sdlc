"""参考产物应描述已生成内容；审批状态独立记录，不继承初始化占位说明。"""
from copy import deepcopy
from uuid import uuid4

import pytest

from knowledge.reference_runner import build_reference
from knowledge.workflow_adapter import WorkflowReferenceAdapter


@pytest.mark.parametrize("stage, keys", [
    (1, {"architecture", "interface", "data"}),
    (2, {"code", "code-evidence"}),
    (3, {"test", "test-evidence"}),
])
def test_generated_reference_artifacts_replace_scaffold_copy_without_approving(stage, keys):
    bindings = {key: str(uuid4()) for key in (
        "requirement", "architecture", "interface", "data", "code", "test", "code-evidence", "test-evidence")}
    package = {"document": {"entities": [
        {"id": record_id, "version": 1, "name": "待完成产物（流程草稿）",
         "description": "尚未完成的流程产物，不代表已实现或验证。", "status": "draft",
         "attributes": {}, "evidence": [], "provenance": {"nature": "inference", "actor": "参考来源"}}
        for record_id in bindings.values()
    ]}}
    original = deepcopy(package)
    build = build_reference()
    execution = {"id": uuid4(), "document": {"passed": True, "tests_run": 4, "executed": True},
                 "code_sha256": build["sha256"]["order_service.py"],
                 "test_sha256": build["sha256"]["test_order_service.py"]}

    report = WorkflowReferenceAdapter().build_report(
        stage, {"definition": {"acceptance_criteria": ["形成可审阅产物"]}}, package, bindings,
        execution if stage == 3 else None)

    assert {change["id"] for change in report["changes"]} == {bindings[key] for key in keys}
    for change in report["changes"]:
        candidate = change["data"]
        assert "合成参考" in candidate["description"]
        assert "尚未完成" not in candidate["description"]
        assert "待人工确认" not in candidate["name"]
        assert candidate["status"] == "draft"
        assert candidate["provenance"]["nature"] == "inference"
        assert candidate["expected_version"] == 1
    assert report["artifacts"]["synthetic_business_scenario"] is True
    assert package == original
