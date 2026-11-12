"""增量召回保留来源身份，投影保留业务属性但不发送重复审计与图谱。"""
from copy import deepcopy
from uuid import uuid4

import pytest

from knowledge.extraction_context import existing_quote_supported, model_context, select_context_records
from knowledge.semantics import KnowledgeError


def record(name, description='', **extra):
    return {'id': str(uuid4()), 'version': 1, 'name': name, 'description': description,
            'kind': 'Requirement', 'status': 'active', 'attributes': {}, **extra}


def test_retrieval_keeps_source_bound_identity_even_without_text_match():
    bound = record('早期不同名称')
    capacity = record('容量限制', '教学班容量为非负整数')
    unrelated = record('开票', '财务开票业务')
    selected, reasons = select_context_records([unrelated, bound, capacity], '新增选课受教学班容量限制', [bound['id']], limit=2)
    assert [item['id'] for item in selected] == [bound['id'], capacity['id']]
    assert reasons[0]['reason'] == '同来源已确认身份'
    assert select_context_records([capacity, bound, unrelated], '新增选课受教学班容量限制', [bound['id']], limit=2) == (selected, reasons)
    selected, _ = select_context_records([bound, capacity], '无匹配词', [bound['id'], capacity['id']], limit=1)
    assert len(selected) == 2


def test_retrieval_rejects_excessive_mandatory_bindings():
    rows = [record('同来源') for _ in range(201)]
    with pytest.raises(KnowledgeError, match='200'):
        select_context_records(rows, '内容', [row['id'] for row in rows])


def test_model_projection_keeps_business_extensions_without_audit_or_impact_snapshot():
    old = record('容量', attributes={'extensions': {'business_scope': {'semester': '本学期'},
        'extraction': {'private_audit': '历史模型提示'}, 'source_span': {'content': '旧原文'},
        'acceptance_items': [{'key': 'cap', 'text': '不超额', 'evidence': [{'id': 'old'}]}]}})
    context = {'source': {'content': '新需求'}, 'guidance': '', 'bindings': [], 'relationship_bindings': [],
               'project_baseline_version': 1, 'existing_entities': [old], 'existing_relationships': [],
               'review_contract_version': 2, 'source_scope': 'delta', 'impact_snapshot': {'entities': ['不要发送']},
               'project_state_token': '不要发送'}
    saved = deepcopy(context)
    projected = model_context(context)
    assert context == saved
    assert 'impact_snapshot' not in projected and 'project_state_token' not in projected
    assert projected['source_scope'] == 'delta'
    assert projected['existing_entities'][0]['attributes']['extensions'] == {
        'business_scope': {'semester': '本学期'}, 'acceptance_items': [{'key': 'cap', 'text': '不超额'}]}


def test_existing_quote_can_locate_expression_string_leaves_but_not_audit():
    old = record('容量', attributes={'expression': {'conditions': [{'rule': '满员拒绝新增选课'}]},
                                   'extensions': {'extraction': {'text': '伪证据'}}})
    assert existing_quote_supported(old, '满员拒绝新增选课')
    assert not existing_quote_supported(old, '伪证据')
    assert not existing_quote_supported(old, '')
