"""模型网关的配置、受控请求与失败语义；不调用真实模型服务。"""
from io import BytesIO
import json
from urllib.error import HTTPError, URLError

import pytest
from pydantic import ValidationError

from knowledge import model_gateway as gateway
from knowledge.design_models import ConfirmedScopeDesign, DesignDocument, SequenceDiagram, UseCaseDiagram
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


def test_confirmed_scope_design_uses_saved_answers_and_a_distinct_prompt(configured, monkeypatch):
    context = {
        "requirements": [{"id": "req-1", "acceptance_criteria": ["满员时拒绝选课"]}],
        "clarifications": [{"key": "capacity", "answer": "按教学班计算容量"}],
        "pending_questions": [{"key": "notification", "question": "是否通知学生？"}],
        "generation_guidance": "先形成容量限制设计",
    }
    schema = {"type": "object", "properties": {"questions": {"type": "array"}}}
    document = {"summary": "教学班容量限制设计", "questions": [], "assumptions": ["接口命名待审阅"],
                "deferred_items": ["满员通知另行设计"]}
    calls = install_transport(monkeypatch, response(document))

    result = gateway.complete_design(context, schema, confirmed_scope=True)

    assert len(calls) == 1
    body = json.loads(calls[0][0].data)
    prompt = body["messages"][0]["content"]
    assert "已明确回答的问题不要重复追问" in prompt
    assert "未决业务规则会改变本轮某项具体验收结果" in prompt
    assert "acceptance_criterion 逐字复制" in prompt and "options 列出 2 至 5 个具体选项" in prompt
    assert "非阻塞的实现选择列入 assumptions" in prompt and "范围外的问题列入 deferred_items" in prompt
    assert "返回空的 questions，直接产出完整的候选设计" in prompt
    assert json.loads(body["messages"][1]["content"]) == {"context": context, "output_schema": schema}
    assert result["document"] == document
    assert result["trace"]["prompt_version"] == gateway.CONFIRMED_SCOPE_PROMPT_VERSION != gateway.PROMPT_VERSION


@pytest.mark.parametrize("confirmed_scope,model", [(False, DesignDocument), (True, ConfirmedScopeDesign)])
def test_model_can_return_structured_business_diagrams(configured, monkeypatch, confirmed_scope, model):
    sequence = {
        "title": "满员时拒绝新增选课",
        "requirement_ids": ["REQ-CAPACITY"],
        "participants": [{"key": "Student", "name": "学生"}, {"key": "Enrollment", "name": "选课服务"}],
        "messages": [{"source": "Student", "target": "Enrollment", "label": "提交选课"},
                     {"source": "Enrollment", "target": "Student", "label": "拒绝满员请求"}],
    }
    use_case = {
        "title": "学生选课用例",
        "requirement_ids": ["REQ-CAPACITY"],
        "actors": [{"key": "Student", "name": "学生"}],
        "use_cases": [{"key": "Enroll", "name": "提交选课"}],
        "associations": [{"actor": "Student", "use_case": "Enroll"}],
    }
    design = {
        "summary": "容量限制设计",
        "components": [{"key": "Enrollment", "name": "选课服务", "requirement_ids": ["REQ-CAPACITY"],
                        "responsibility": "检查教学班容量"}],
        "test_plan": [{"key": "Capacity", "name": "满员时拒绝新增", "requirement_ids": ["REQ-CAPACITY"],
                       "steps": ["提交选课"], "expected_result": "拒绝满员请求"}],
    }
    calls = install_transport(monkeypatch, response(design | {"sequence_diagrams": [sequence], "use_case_diagrams": [use_case]}))

    result = gateway.complete_design({"requirements": [{"id": "REQ-CAPACITY"}]}, model.model_json_schema(),
                                     confirmed_scope=confirmed_scope)

    prompt = json.loads(calls[0][0].data)["messages"][0]["content"]
    assert "每张图只说明一条路径" in prompt and "不专门为画图提出澄清问题" in prompt
    assert "系统边界外已有依据的业务角色" in prompt and "不得虚构业务角色、职责或操作" in prompt
    parsed = model.model_validate(result["document"])
    assert parsed.sequence_diagrams[0].model_dump() == sequence
    assert parsed.use_case_diagrams[0].model_dump() == use_case
    previous = model.model_validate(design)
    assert previous.sequence_diagrams == [] and previous.use_case_diagrams == []


@pytest.mark.parametrize("scenario,message", [
    ("duplicate participant", "参与者标识不能重复"),
    ("unknown receiver", "必须引用本图定义的参与者"),
    ("duplicate requirement", "不能重复引用需求"),
    ("diagram code", "Extra inputs are not permitted"),
    ("multiline message", "String should match pattern"),
])
def test_sequence_diagram_rejects_invalid_structure(scenario, message):
    diagram = {
        "title": "发起选课",
        "requirement_ids": ["REQ-CAPACITY"],
        "participants": [{"key": "Student", "name": "学生"}, {"key": "Enrollment", "name": "选课服务"}],
        "messages": [{"source": "Student", "target": "Enrollment", "label": "提交选课"}],
    }
    if scenario == "duplicate participant":
        diagram["participants"][1]["key"] = "Student"
    elif scenario == "unknown receiver":
        diagram["messages"][0]["target"] = "Unknown"
    elif scenario == "duplicate requirement":
        diagram["requirement_ids"].append("REQ-CAPACITY")
    elif scenario == "diagram code":
        diagram["mermaid"] = "sequenceDiagram\nStudent->>Enrollment: 提交选课"
    else:
        diagram["messages"][0]["label"] = "提交选课\n等待确认"

    with pytest.raises(ValidationError, match=message):
        SequenceDiagram.model_validate(diagram)


@pytest.mark.parametrize("scenario,message", [
    ("unknown action", "必须引用本图的参与者和用例"),
    ("unlinked actor", "都必须出现在关联中"),
    ("duplicate association", "不能重复声明同一关联"),
])
def test_use_case_diagram_validates_associations(scenario, message):
    diagram = {
        "title": "学生选课用例",
        "requirement_ids": ["REQ-CAPACITY"],
        "actors": [{"key": "Student", "name": "学生"}],
        "use_cases": [{"key": "Enroll", "name": "提交选课"}],
        "associations": [{"actor": "Student", "use_case": "Enroll"}],
    }
    if scenario == "unknown action":
        diagram["associations"][0]["use_case"] = "Unknown"
    elif scenario == "unlinked actor":
        diagram["actors"].append({"key": "Advisor", "name": "导师"})
    else:
        diagram["associations"] *= 2

    with pytest.raises(ValidationError, match=message):
        UseCaseDiagram.model_validate(diagram)


def test_design_exchange_records_actual_request_and_reply_without_http_metadata(configured, monkeypatch):
    context = {"requirements": [{"name": "选课人数上限"}]}
    reply = response({"summary": "选课设计", "questions": []})
    reply["choices"][0]["message"]["reasoning_content"] = "提供商额外字段"
    calls = install_transport(monkeypatch, reply)
    exchanges = []

    result = gateway.complete_design(context, {}, confirmed_scope=True, on_exchange=exchanges.append)

    payload = json.loads(calls[0][0].data)
    assert exchanges == [
        {"request": payload, "response": None},
        {"request": payload, "response": {"content": reply["choices"][0]["message"]["content"],
                                          "finish_reason": "stop", "refusal": None}},
    ]
    assert calls[0][0].get_header("Authorization") and "Authorization" not in json.dumps(exchanges)
    assert "提供商额外字段" not in json.dumps(exchanges, ensure_ascii=False)
    assert result["document"]["summary"] == "选课设计"


def test_design_exchange_redacts_keys_in_both_plain_and_json_escaped_values(configured, monkeypatch):
    secret = 'audit-secret"with\\escape'
    encoded = json.dumps(secret)[1:-1]
    monkeypatch.setenv("KNOWLEDGE_MODEL_API_KEY", secret)
    monkeypatch.setenv("KNOWLEDGE_MODEL_RESPONSE_FORMAT", "json_schema")
    reply = response({"summary": secret})
    calls = install_transport(monkeypatch, reply)
    exchanges = []

    gateway.complete_design({"title": secret}, {"title": secret}, on_exchange=exchanges.append)

    payload = json.loads(calls[0][0].data)
    assert json.loads(payload["messages"][1]["content"])["context"]["title"] == secret
    audit = exchanges[-1]
    assert json.loads(audit["request"]["messages"][1]["content"])["context"]["title"] == "＊"
    assert audit["request"]["response_format"]["json_schema"]["schema"]["title"] == "＊"
    assert json.loads(audit["response"]["content"])["summary"] == "＊"
    assert secret not in repr(exchanges) and encoded not in repr(exchanges)


@pytest.mark.parametrize("reply,error,expected", [
    (response(finish_reason="length", message={"content": "{未完成"}), "截断",
     {"content": "{未完成", "finish_reason": "length", "refusal": None}),
    (response(message={"content": None, "refusal": "需要明确范围"}), "拒绝",
     {"content": None, "finish_reason": "stop", "refusal": "需要明确范围"}),
    ({"error": {"message": "服务商错误详细信息"}}, "JSON", None),
])
def test_design_exchange_preserves_visible_reply_even_if_validation_fails(configured, monkeypatch, reply, error, expected):
    install_transport(monkeypatch, reply)
    exchanges = []
    with pytest.raises(KnowledgeError, match=error):
        gateway.complete_design({}, {}, on_exchange=exchanges.append)
    assert exchanges[-1]["response"] == expected


def test_design_exchange_removes_unstorable_characters_from_invalid_reply(configured, monkeypatch):
    invalid_reply = json.dumps(response(message={"content": "候选\x00\ud800"})).encode()
    install_transport(monkeypatch, invalid_reply)
    exchanges = []
    with pytest.raises(KnowledgeError, match="JSON"):
        gateway.complete_design({}, {}, on_exchange=exchanges.append)
    assert exchanges[-1]["response"]["content"] == "候选��"
    json.dumps(exchanges, ensure_ascii=False).encode("utf-8")


def test_design_exchange_keeps_only_request_when_transport_fails(configured, monkeypatch):
    def transport(request, timeout):
        raise URLError("未公开的网络细节")

    monkeypatch.setattr(gateway, "_open_request", transport)
    exchanges = []
    with pytest.raises(KnowledgeError, match="无法连接"):
        gateway.complete_design({}, {}, on_exchange=exchanges.append)
    assert len(exchanges) == 1 and exchanges[0]["response"] is None
    assert exchanges[0]["request"]["messages"][0]["role"] == "system"


def test_code_uses_a_separate_prompt_and_schema_for_the_fixed_workspace_context(configured, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_MODEL_RESPONSE_FORMAT", "json_schema")
    context = {"design": {"summary": "选课容量校验"}, "workspace_files": [{"path": "enrollment.py", "content": ""}]}
    schema = {"type": "object", "required": ["summary", "files"]}
    document = {"summary": "添加容量校验", "files": [{"path": "enrollment.py", "content": "LIMIT = 10\n"}]}
    calls = install_transport(monkeypatch, response(document))

    result = gateway.complete_code(context, schema)

    assert len(calls) == 1
    body = json.loads(calls[0][0].data)
    assert body["response_format"]["json_schema"] == {
        "name": "engineering_code_changes", "strict": False, "schema": schema,
    }
    assert "unittest" in body["messages"][0]["content"]
    assert "已确认设计" in body["messages"][0]["content"]
    assert "deferred_items 的后续事项不属于本轮代码和测试范围" in body["messages"][0]["content"]
    assert json.loads(body["messages"][1]["content"]) == {"context": context, "output_schema": schema}
    assert result["document"] == document
    assert result["trace"]["prompt_version"] == gateway.CODE_PROMPT_VERSION != gateway.PROMPT_VERSION
    assert "synthetic-secret" not in json.dumps(result)


@pytest.mark.parametrize("language,filename,standard,test_entry,test_layout", [
    ("c", "tests/test_enrollment.c", "C11", "main", "tests/test_*.c"),
    ("cpp", "tests/test_enrollment.cpp", "C++17", "main", "tests/test_*.cpp"),
    ("java", "src/test/java/EnrollmentTest.java", "Java 17", "AssertionError", "src/test/java/"),
])
def test_code_uses_language_specific_source_rules(configured, monkeypatch, language, filename, standard, test_entry, test_layout):
    context = {"language": language, "design": {"summary": "选课容量校验"}}
    document = {"summary": "添加容量校验", "files": [{"path": filename, "content": "source"}]}
    calls = install_transport(monkeypatch, response(document))

    result = gateway.complete_code(context, {}, language=language)

    request = json.loads(calls[0][0].data)
    prompt = request["messages"][0]["content"]
    assert standard in prompt and filename[filename.rfind("."):] in prompt and test_entry in prompt
    assert test_layout in prompt
    assert "unittest" not in prompt and "不要声称测试已运行或通过" in prompt
    assert result["document"] == document
    assert "deferred_items 的后续事项不属于本轮代码和测试范围" in prompt
    assert result["trace"]["prompt_version"] == f"confirmed-design-code-{language}-v3"


def test_unsupported_code_language_is_rejected_before_contacting_model(configured, monkeypatch):
    calls = install_transport(monkeypatch, response())
    with pytest.raises(KnowledgeError, match="代码生成仅支持"):
        gateway.complete_code({}, {}, language="go")
    assert calls == []


def test_truncated_code_output_is_not_a_candidate_or_retried(configured, monkeypatch):
    calls = install_transport(monkeypatch, response(finish_reason="length"))
    with pytest.raises(KnowledgeError, match="长度上限截断"):
        gateway.complete_code({}, {})
    assert len(calls) == 1


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
    (URLError("synthetic-secret"), 502, "无法连接"),
    (HTTPError("https://model.example", 401, "synthetic-secret", {}, BytesIO(b"synthetic-secret")), 502, "认证失败"),
    (HTTPError("https://model.example", 429, "synthetic-secret", {}, BytesIO(b"synthetic-secret")), 502, "限流"),
    (HTTPError("https://model.example", 400, "synthetic-secret", {}, BytesIO(b"synthetic-secret")), 502, "请求失败"),
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


@pytest.mark.parametrize("failure", [
    lambda: TimeoutError("synthetic-secret"),
    lambda: URLError(TimeoutError("synthetic-secret")),
    lambda: HTTPError("https://model.example", 503, "synthetic-secret", {}, BytesIO()),
])
def test_transient_failure_retries_once_with_same_request(configured, monkeypatch, failure):
    events = []
    calls = []

    def transport(request, timeout):
        events.append('call')
        calls.append((request.data, timeout))
        if len(calls) == 1:
            raise failure()
        return FakeResponse(response())

    monkeypatch.setenv('KNOWLEDGE_MODEL_TIMEOUT_SECONDS', '180')
    monkeypatch.setattr(gateway, '_open_request', transport)
    monkeypatch.setattr(gateway, 'sleep', events.append)
    result = gateway.complete_design({}, {}, before_retry=lambda: events.append('check'))
    assert events == ['call', 2, 'check', 'call']
    assert calls[0] == calls[1] and calls[1][1] == 180
    assert result['document']['summary'] == '候选设计' and result['trace']['attempt_count'] == 2


def test_repeated_read_timeout_reports_exhausted_retry(configured, monkeypatch):
    calls = []

    class SlowResponse(FakeResponse):
        def read(self, size):
            raise TimeoutError('synthetic-secret')

    def transport(request, timeout):
        reply = SlowResponse(response())
        calls.append(reply)
        return reply

    monkeypatch.setattr(gateway, '_open_request', transport)
    monkeypatch.setattr(gateway, 'sleep', lambda seconds: None)
    with pytest.raises(KnowledgeError, match='超时。已尝试2次') as exc:
        gateway.complete_design({}, {})
    assert exc.value.status_code == 504 and 'synthetic-secret' not in exc.value.message
    assert len(calls) == 2 and all(reply.closed for reply in calls)


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
