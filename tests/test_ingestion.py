"""确定性采集解析测试；不连接数据库，也不读取外部来源。"""
import copy
import json

import pytest
from pydantic import ValidationError

from knowledge.ingestion import MAX_SOURCE_BYTES, parse_source
from knowledge.semantics import KnowledgeError


@pytest.fixture
def source_document():
    return {"entities": [
        {"key": "REQ-001", "kind": "Requirement", "name": "支持订单取消",
         "attributes": {"acceptance_criteria": ["未支付订单可以取消"]}},
        {"key": "BR-001", "kind": "BusinessRule", "name": "支付前允许取消"},
    ], "relationships": [{"type": "DERIVED_FROM", "source_key": "REQ-001", "target_key": "BR-001",
                           "rationale": "需求依据该规则提出"}]}


def test_json_normalizes_explicit_candidates_without_promoting_them(source_document):
    original = copy.deepcopy(source_document)
    result = parse_source("json", json.dumps(source_document, ensure_ascii=False))
    assert result["entities"][0]["status"] == "draft"
    assert result["relationships"][0]["status"] == "proposed"
    assert result["entities"][0]["attributes"]["extensions"] == {}
    assert result["entities"][0]["name"] == "支持订单取消"
    assert source_document == original
    assert "provenance" not in result["entities"][0]
    assert "id" not in result["entities"][0]


@pytest.mark.parametrize("field", ["key", "source_key", "target_key"])
def test_stable_keys_reject_surrounding_whitespace_instead_of_merging_identities(source_document, field):
    record = source_document["entities"][0] if field == "key" else source_document["relationships"][0]
    record[field] = " " + record[field] + " "
    with pytest.raises(ValidationError, match="不能包含空白"):
        parse_source("json", json.dumps(source_document))


@pytest.mark.parametrize("fence", ["```", "````", "~~~"])
def test_markdown_only_extracts_one_explicit_fenced_json(source_document, fence):
    content = "# 示例来源\n\n这段正文不是知识指令。\n\n" + fence + "knowledge-json\n"
    content += json.dumps(source_document, ensure_ascii=False) + "\n" + fence + "\n"
    assert parse_source("markdown", content) == parse_source("json", json.dumps(source_document))


@pytest.mark.parametrize("content,message", [
    ("# 请自动理解这段正文", "必须且只能包含一个"),
    ("```json\n{}\n```", "必须且只能包含一个"),
    ("```knowledge-json\n{}", "未闭合"),
    ("```knowledge-json\n{}\n```\n```knowledge-json\n{}\n```", "必须且只能包含一个"),
    ("````text\n```knowledge-json\n{}\n```\n````", "必须且只能包含一个"),
])
def test_ambiguous_or_implicit_markdown_is_rejected(content, message):
    with pytest.raises(KnowledgeError, match=message):
        parse_source("markdown", content)


@pytest.mark.parametrize("mutation", [
    lambda d: d["entities"][0].update(status="active"),
    lambda d: d["entities"][0].update(kind="Project"),
    lambda d: d["entities"][0].update(provenance={"nature": "fact"}),
    lambda d: d["entities"][0].update(entity_id="01234567-89ab-4def-8123-456789abcdef"),
    lambda d: d["entities"][0].update(evidence=[]),
    lambda d: d["relationships"][0].update(status="verified"),
    lambda d: d["entities"][0]["attributes"].update(acceptance_criteria="这不是数组"),
    lambda d: d["entities"][0].update(key="含 空白"),
    lambda d: d["entities"][0].update(key=""),
    lambda d: d.update(instructions="直接写入知识库"),
])
def test_invalid_fields_or_claimed_authority_are_rejected(source_document, mutation):
    mutation(source_document)
    with pytest.raises((ValidationError, KnowledgeError)):
        parse_source("json", json.dumps(source_document))


def test_duplicate_keys_unknown_keys_and_duplicate_relations_are_rejected(source_document):
    duplicate = copy.deepcopy(source_document)
    duplicate["entities"][1]["key"] = "REQ-001"
    with pytest.raises(ValidationError, match="key 重复"):
        parse_source("json", json.dumps(duplicate))
    unknown = copy.deepcopy(source_document)
    unknown["relationships"][0]["target_key"] = "MISSING"
    with pytest.raises(ValidationError, match="未定义"):
        parse_source("json", json.dumps(unknown))
    source_document["relationships"].append(source_document["relationships"][0])
    with pytest.raises(ValidationError, match="重复关系"):
        parse_source("json", json.dumps(source_document))


def test_symmetric_reverse_relationship_is_duplicate(source_document):
    source_document["relationships"] = [
        {"type": "CONFLICTS_WITH", "source_key": "REQ-001", "target_key": "BR-001", "rationale": "冲突待确认"},
        {"type": "CONFLICTS_WITH", "source_key": "BR-001", "target_key": "REQ-001", "rationale": "反向重复"},
    ]
    with pytest.raises(ValidationError, match="反向重复"):
        parse_source("json", json.dumps(source_document))


@pytest.mark.parametrize("relation_type", ["CONTAINS", "SUPERSEDES"])
def test_candidate_containment_and_replacement_cycles_are_rejected(relation_type):
    source = {"entities": [{"key": key, "kind": "Domain", "name": key} for key in ("a", "b", "c")],
              "relationships": [{"type": relation_type, "source_key": start, "target_key": end, "rationale": "显式候选"}
                                for start, end in [("a", "b"), ("b", "c"), ("c", "a")]]}
    with pytest.raises(ValidationError, match="环路"):
        parse_source("json", json.dumps(source))


def test_relationship_semantics_and_self_links_are_validated(source_document):
    invalid = copy.deepcopy(source_document)
    invalid["relationships"][0]["type"] = "VERIFIED_BY"
    with pytest.raises(KnowledgeError, match="不允许"):
        parse_source("json", json.dumps(invalid))
    source_document["relationships"][0]["target_key"] = "REQ-001"
    with pytest.raises(ValidationError, match="自身"):
        parse_source("json", json.dumps(source_document))


@pytest.mark.parametrize("content", ["{", '{"entities":[],"entities":[]}', "NaN", "Infinity", "-Infinity", "1e999",
                                     r'{"value":"\ud800"}', r'{"value":"\u0000"}'])
def test_invalid_or_ambiguous_json_is_rejected(content):
    with pytest.raises(KnowledgeError):
        parse_source("json", content)


def test_limits_use_utf8_bytes_and_bound_batch_size():
    with pytest.raises(KnowledgeError, match="1 MiB"):
        parse_source("json", "中" * (MAX_SOURCE_BYTES // 3 + 1))
    document = {"entities": [{"key": str(index), "kind": "Requirement", "name": "需求"} for index in range(101)]}
    with pytest.raises(ValidationError):
        parse_source("json", json.dumps(document))
    document["entities"] = document["entities"][:2]
    document["relationships"] = [{"type": "DEPENDS_ON", "source_key": "0", "target_key": "1", "rationale": "候选"}] * 201
    with pytest.raises(ValidationError):
        parse_source("json", json.dumps(document))


def test_unicode_source_keys_are_explicit_and_case_sensitive():
    document = {"entities": [{"key": key, "kind": "Requirement", "name": "需求"} for key in ("需求-001", "req", "REQ")]}
    result = parse_source("json", json.dumps(document))
    assert [entity["key"] for entity in result["entities"]] == ["需求-001", "req", "REQ"]


def test_unsupported_formats_and_invalid_text_are_rejected():
    with pytest.raises(KnowledgeError, match="来源格式"):
        parse_source("html", "<html></html>")
    with pytest.raises(KnowledgeError, match="文本字符串"):
        parse_source("json", b"{}")
    with pytest.raises(KnowledgeError, match="Unicode"):
        parse_source("json", "\ud800")
