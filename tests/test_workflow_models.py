"""第四阶段输入必须明确选择示例，并保护版本及有效 JSON 边界。"""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from knowledge.workflow_models import ReferenceExecutionRequest, WorkflowControl, WorkflowCreate
from knowledge.models import ATTRIBUTE_MODELS, Kind
from knowledge.reference_runner import build_reference


def payload(**changes):
    return {"project_id": str(uuid4()), "title": "参考流程", "goal": "验证显式选定的合成场景",
            "scenario": "order_cancellation_v1", "actor": "本地维护者", **changes}


def test_reference_scenario_is_explicit_and_not_an_arbitrary_execution_entry():
    request = WorkflowCreate(**payload())
    assert request.max_stage_retries == 2
    values = payload()
    values.pop("scenario")
    with pytest.raises(ValidationError):
        WorkflowCreate(**values)
    with pytest.raises(ValidationError):
        WorkflowCreate(**payload(scenario="arbitrary_python_command"))
    with pytest.raises(ValidationError):
        WorkflowCreate(**payload(command="python unknown.py"))


@pytest.mark.parametrize("retries", [-1, 6])
def test_stage_retry_budget_is_bounded(retries):
    with pytest.raises(ValidationError):
        WorkflowCreate(**payload(max_stage_retries=retries))


def test_workflow_and_execution_requests_require_current_versions_and_lease():
    with pytest.raises(ValidationError):
        WorkflowControl(expected_version=0, actor="维护者", reason="不能忽略版本")
    with pytest.raises(ValidationError):
        ReferenceExecutionRequest(agent_id=uuid4(), expected_version=1)
    with pytest.raises(ValidationError):
        WorkflowCreate(**payload(goal="不能保存的\x00输入"))


def test_source_and_test_content_keep_exact_bytes_for_execution_evidence():
    files = build_reference()["files"]
    code = ATTRIBUTE_MODELS[Kind.CODE].model_validate({"extensions": {"source": files["order_service.py"]}})
    test = ATTRIBUTE_MODELS[Kind.TEST].model_validate({"extensions": {"test_source": files["test_order_service.py"]}})
    assert code.extensions["source"] == files["order_service.py"]
    assert test.extensions["test_source"] == files["test_order_service.py"]
