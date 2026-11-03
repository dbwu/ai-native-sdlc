"""运行时输入在持久化之前拒绝不兼容的 JSON 值。"""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from knowledge.runtime_models import TaskReport


@pytest.mark.parametrize("artifacts,message", [
    ({"说明": "不能保存\x00字符"}, "NUL"),
    ({"指标": float("inf")}, "有限数值"),
    ({"说明": "\ud800"}, "Unicode"),
])
def test_runtime_artifacts_reject_invalid_json_values_before_database_access(artifacts, message):
    with pytest.raises(ValidationError, match=message):
        TaskReport(agent_id=uuid4(), lease_token=uuid4(), expected_version=1, outcome="succeeded",
                   summary="检查结构化产物输入", artifacts=artifacts)
