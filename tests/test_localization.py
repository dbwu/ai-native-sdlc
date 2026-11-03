"""验证中文展示和错误提示，同时保护稳定的接口标识。"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from knowledge.api import create_app
from knowledge.localization import localize_validation_errors
from knowledge.models import ATTRIBUTE_MODELS, Kind, RelationType
from knowledge.service import KnowledgeService


def test_chinese_api_docs_and_metamodel_keep_protocol_identifiers():
    with TestClient(create_app("unused")) as client:
        schema = client.get("/openapi.json").json()
        assert schema["info"]["title"] == "工程知识模型 API"
        for route in schema["paths"].values():
            for operation in route.values():
                assert any("\u4e00" <= char <= "\u9fff" for char in operation["summary"])
        metamodel = client.get("/metamodel").json()
        assert set(metamodel["labels"]["entity_kinds"]) == set(Kind)
        assert set(metamodel["labels"]["relationship_types"]) == set(RelationType)
        assert metamodel["labels"]["entity_kinds"]["Requirement"] == "需求"
        assert metamodel["entity_attributes"]["Requirement"]["properties"]["acceptance_criteria"]["title"] == "验收标准"
        assert schema["components"]["schemas"]["EntityCreate"]["properties"]["name"]["title"] == "名称"


@pytest.mark.parametrize("payload,error_type,message", [
    ({"kind": "Project", "name": "示例项目"}, "missing", "缺少必填字段"),
    ({"kind": "Project", "name": ""}, "string_too_short", "字符串长度不能小于 1 个字符"),
    ({"kind": "不存在的类型", "name": "示例"}, "enum", "应为以下枚举值之一"),
])
def test_request_validation_is_chinese_and_keeps_error_structure(payload, error_type, message):
    with TestClient(create_app("unused")) as client:
        response = client.post("/entities", json=payload)
        assert response.status_code == 422
        errors = response.json()["detail"]
        error = next(error for error in errors if error["type"] == error_type)
        assert message in error["msg"]
        assert error["loc"][0] == "body"
        assert "input" in error


def test_custom_chinese_validation_message_survives_framework_prefix(provenance):
    with TestClient(create_app("unused")) as client:
        response = client.post("/entities", json={"kind": "Requirement", "name": "示例需求", "provenance": provenance})
        assert response.status_code == 422
        error = response.json()["detail"][0]
        assert error["type"] == "value_error"
        assert error["msg"] == "创建非 Project 实体时必须提供 project_id"
        assert "Value error" not in error["msg"]


def test_service_attribute_validation_uses_context_for_chinese_messages(monkeypatch, provenance):
    def invalid_attributes(self, record_id, request):
        ATTRIBUTE_MODELS[Kind.REQUIREMENT].model_validate({"acceptance_criteria": [""]})

    monkeypatch.setattr(KnowledgeService, "update_entity", invalid_attributes)
    with TestClient(create_app("unused")) as client:
        response = client.put(f"/entities/{uuid4()}", json={
            "name": "示例需求", "expected_version": 1, "provenance": provenance,
        })
        assert response.status_code == 422
        error = response.json()["detail"][0]
        assert error["msg"] == "字符串长度不能小于 1 个字符"
        assert error["loc"] == ["acceptance_criteria", 0]
        assert "input" not in error
        assert "ctx" not in error


def test_invalid_query_and_json_have_chinese_errors():
    with TestClient(create_app("unused")) as client:
        query = client.get("/entities", params={"project_id": "不是UUID", "limit": 0})
        assert query.status_code == 422
        errors = query.json()["detail"]
        assert any(error["msg"] == "UUID 格式无效" for error in errors)
        assert any(error["msg"] == "数值必须大于或等于 1" for error in errors)
        body = client.post("/entities", content="{", headers={"content-type": "application/json"})
        assert body.status_code == 422
        assert body.json()["detail"][0]["msg"] == "请求内容不是有效的 JSON"


def test_unrecognized_error_is_preserved_without_inventing_a_translation():
    error = {"type": "future_type", "loc": ("field",), "msg": "upstream message", "input": 123}
    assert localize_validation_errors([error]) == [error]
