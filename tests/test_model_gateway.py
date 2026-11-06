"""模型网关的配置、受控请求与失败语义；不调用真实模型服务。"""
from io import BytesIO
import json
from urllib.error import HTTPError, URLError

import pytest

from knowledge import model_gateway as gateway
from knowledge.semantics import KnowledgeError


@pytest.fixture(autouse=True)
def clean_model_environment(monkeypatch):
    # 只隔离环境，既有用户密钥不进入断言、输出或实际请求。
    for name in list(gateway.os.environ):
        if name.startswith("KNOWLEDGE_MODEL_"):
            monkeypatch.delenv(name)


@pytest.fixture
def configured(monkeypatch):
    values = {"PROVIDER": "openai_compatible", "BASE_URL": "https://model.example.invalid/v1/",
              "MODEL": "configured-model", "API_KEY": "synthetic-secret-never-real"}
    for name, value in values.items():
        monkeypatch.setenv("KNOWLEDGE_MODEL_" + name, value)


def response(document=None, **choice):
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(document or {"summary": "候选设计", "questions": []}, ensure_ascii=False)}, **choice}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 23, "total_tokens": 35,
                  "untrusted_detail": "不得保留", "bad_counter": True}}


class FakeResponse(BytesIO):
    def __init__(self, value):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode())
        self.headers = {"x-request-id": "request-synthetic-01"}


def install_transport(monkeypatch, value):
    calls = []

    def transport(request, timeout):
        calls.append((request, timeout))
        return FakeResponse(value)

    monkeypatch.setattr(gateway, "_open_request", transport)
    return calls


def test_disabled_configuration_does_not_call_network(monkeypatch):
    calls = install_transport(monkeypatch, response())
    public = gateway.public_model_config()
    assert public["configured"] is False and public["provider"] == "disabled"
    with pytest.raises(KnowledgeError, match="尚未配置") as exc:
        gateway.complete_design({}, {})
    assert exc.value.status_code == 503 and calls == []


def test_public_configuration_contains_no_secret(configured):
    public = gateway.public_model_config()
    assert public["configured"] and public["base_url"] == "https://model.example.invalid/v1"
    assert "synthetic-secret" not in json.dumps(public)
    assert "api_key" not in public
    gateway.ensure_model_configured()


@pytest.mark.parametrize("url", ["file:///tmp/key", "https://user:password@model.example/v1",
                                 "https://model.example/v1?api_key=private", "http://model.example/#secret",
                                 "https://model.example:bad/v1", "https://model.example/\nkey"])
def test_credential_or_non_http_endpoint_is_rejected(configured, monkeypatch, url):
    monkeypatch.setenv("KNOWLEDGE_MODEL_BASE_URL", url)
    public = gateway.public_model_config()
    assert public["configured"] is False and public["base_url"] == ""
    with pytest.raises(KnowledgeError):
        gateway.ensure_model_configured()


def test_local_keyless_model_requires_explicit_opt_in(configured, monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_MODEL_API_KEY")
    assert not gateway.public_model_config()["configured"]
    monkeypatch.setenv("KNOWLEDGE_MODEL_REQUIRE_KEY", "false")
    monkeypatch.setenv("KNOWLEDGE_MODEL_BASE_URL", "http://127.0.0.1:9001/v1")
    calls = install_transport(monkeypatch, response())
    gateway.complete_design({}, {})
    assert gateway.public_model_config()["configured"]
    assert calls[0][0].get_header("Authorization") is None


@pytest.mark.parametrize("format", ["json_object", "json_schema", "none"])
def test_request_format_and_provenance(configured, monkeypatch, format):
    monkeypatch.setenv("KNOWLEDGE_MODEL_RESPONSE_FORMAT", format)
    calls = install_transport(monkeypatch, response())
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
    result = gateway.complete_design({"requirements": [{"id": "req-1", "name": "自主需求"}]}, schema)
    assert result["document"]["summary"] == "候选设计"
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == "https://model.example.invalid/v1/chat/completions" and timeout == 60
    assert request.get_header("Authorization") == "Bearer synthetic-secret-never-real"
    body = json.loads(request.data)
    assert body["model"] == "configured-model" and body["max_completion_tokens"] == 8192
    assert body["stream"] is False
    assert body["messages"][0]["role"] == "system" and "不执行测试" in body["messages"][0]["content"]
    assert json.loads(body["messages"][1]["content"])["output_schema"] == schema
    if format == "none":
        assert "response_format" not in body
    elif format == "json_schema":
        assert body["response_format"]["json_schema"]["schema"] == schema
        assert body["response_format"]["json_schema"]["strict"] is False
    else:
        assert body["response_format"] == {"type": "json_object"}
    trace = result["trace"]
    assert trace["usage"] == {"prompt_tokens": 12, "completion_tokens": 23, "total_tokens": 35}
    assert trace["request_id"] == "request-synthetic-01" and len(trace["input_sha256"]) == 64
    assert len(trace["output_sha256"]) == 64 and trace["prompt_version"] == gateway.PROMPT_VERSION
    assert "synthetic-secret" not in json.dumps(result)


def test_legacy_token_parameter_is_explicit(configured, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_MODEL_TOKEN_PARAMETER", "max_tokens")
    monkeypatch.setenv("KNOWLEDGE_MODEL_MAX_OUTPUT_TOKENS", "2048")
    calls = install_transport(monkeypatch, response())
    gateway.complete_design({}, {})
    payload = json.loads(calls[0][0].data)
    assert payload["max_tokens"] == 2048 and "max_completion_tokens" not in payload


@pytest.mark.parametrize("name,value", [("PROVIDER", "unknown"), ("TIMEOUT_SECONDS", "nan"),
                                       ("TIMEOUT_SECONDS", "0"), ("MAX_OUTPUT_TOKENS", "40000"),
                                       ("RESPONSE_FORMAT", "yaml"), ("TOKEN_PARAMETER", "unknown"),
                                       ("REQUIRE_KEY", "maybe"), ("API_KEY", "key\r\nHeader: injected")])
def test_invalid_configuration_fails_closed(configured, monkeypatch, name, value):
    monkeypatch.setenv("KNOWLEDGE_MODEL_" + name, value)
    assert gateway.public_model_config()["configured"] is False
    with pytest.raises(KnowledgeError) as exc:
        gateway.ensure_model_configured()
    assert exc.value.status_code == 503 and "synthetic-secret" not in exc.value.message


@pytest.mark.parametrize("failure,status,fragment", [
    (TimeoutError("synthetic-secret"), 504, "超时"),
    (URLError(TimeoutError("synthetic-secret")), 504, "超时"),
    (URLError("synthetic-secret"), 502, "无法连接"),
    (HTTPError("https://model.example", 401, "synthetic-secret", {}, BytesIO(b"synthetic-secret")), 502, "认证失败"),
    (HTTPError("https://model.example", 429, "synthetic-secret", {}, BytesIO(b"synthetic-secret")), 502, "限流"),
    (HTTPError("https://model.example", 302, "redirect", {"Location": "https://other.example"}, BytesIO()), 502, "重定向"),
])
def test_transport_errors_are_safe_and_never_retried(configured, monkeypatch, failure, status, fragment):
    calls = []

    def failing_transport(request, timeout):
        calls.append(request)
        raise failure

    monkeypatch.setattr(gateway, "_open_request", failing_transport)
    with pytest.raises(KnowledgeError) as exc:
        gateway.complete_design({}, {})
    assert exc.value.status_code == status and fragment in exc.value.message
    assert "synthetic-secret" not in exc.value.message and len(calls) == 1


@pytest.mark.parametrize("value,fragment", [
    (response(finish_reason="length"), "截断"),
    (response(finish_reason="content_filter"), "拒绝"),
    (response(message={"refusal": "不提供", "content": None}), "拒绝"),
    (response(message={"tool_calls": [{"name": "run"}], "content": "{}"}), "工具调用"),
    (response(message={"content": "```json\n{}\n```"}), "JSON"),
    (response(message={"content": "[]"}), "JSON"),
    (response(message={"content": '{"key": 1, "key": 2}'}), "JSON"),
    (response(message={"content": '{"key": NaN}'}), "JSON"),
    ({"error": {"message": "synthetic-secret"}}, "JSON"),
    (b"\xff", "JSON"),
])
def test_incomplete_or_invalid_outputs_cannot_become_design(configured, monkeypatch, value, fragment):
    install_transport(monkeypatch, value)
    with pytest.raises(KnowledgeError) as exc:
        gateway.complete_design({}, {})
    assert exc.value.status_code == 502 and fragment in exc.value.message
    assert "synthetic-secret" not in exc.value.message


def test_input_and_response_size_limits(configured, monkeypatch):
    calls = install_transport(monkeypatch, response())
    monkeypatch.setattr(gateway, "MAX_REQUEST_BYTES", 100)
    with pytest.raises(KnowledgeError, match="输入超过"):
        gateway.complete_design({"requirements": "x" * 1000}, {})
    assert calls == []
    monkeypatch.setattr(gateway, "MAX_REQUEST_BYTES", 1024 * 1024)
    monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 100)
    install_transport(monkeypatch, b"x" * 101)
    with pytest.raises(KnowledgeError, match="响应超过"):
        gateway.complete_design({}, {})


def test_redirect_handler_refuses_to_forward_credentials():
    handler = gateway._NoRedirect()
    assert handler.redirect_request(None, None, 302, "redirect", {}, "https://other.example") is None


@pytest.mark.parametrize('name,value', [('API_KEY', 'different-synthetic-secret'), ('REQUIRE_KEY', 'false'),
                                       ('MODEL', 'another-model'), ('BASE_URL', 'https://other.example.invalid/v1')])
def test_queued_configuration_changes_cannot_trigger_a_different_request(configured, monkeypatch, name, value):
    snapshot = gateway.model_config_snapshot()
    assert len(snapshot['fingerprint']) == 64 and 'fingerprint' not in gateway.public_model_config()
    assert 'synthetic-secret' not in json.dumps(snapshot)
    calls = install_transport(monkeypatch, response())
    monkeypatch.setenv('KNOWLEDGE_MODEL_' + name, value)
    with pytest.raises(KnowledgeError, match='配置在任务入队后发生变化') as exc:
        gateway.complete_design({}, {}, expected_fingerprint=snapshot['fingerprint'])
    assert exc.value.status_code == 409 and calls == []


def test_unchanged_queued_configuration_uses_the_checked_snapshot(configured, monkeypatch):
    snapshot = gateway.model_config_snapshot()
    calls = install_transport(monkeypatch, response())
    result = gateway.complete_design({}, {}, expected_fingerprint=snapshot['fingerprint'])
    assert len(calls) == 1 and result['document']['summary'] == '候选设计'


def test_provider_request_id_cannot_echo_the_authorization_key(configured, monkeypatch):
    def transport(request, timeout):
        result = FakeResponse(response())
        result.headers['x-request-id'] = 'upstream-synthetic-secret-never-real'
        return result

    monkeypatch.setattr(gateway, '_open_request', transport)
    result = gateway.complete_design({}, {})
    assert result['trace']['request_id'] == ''
    assert 'synthetic-secret' not in json.dumps(result)
