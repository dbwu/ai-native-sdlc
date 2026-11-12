"""M2 固定上下文的纯逻辑增量审阅，不依赖数据库或真实模型。"""
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from knowledge.extraction_review import build_review, validate_decisions
from knowledge.semantics import KnowledgeError


SOURCE = '32631a0e-7f12-4cd3-8c1b-575b2108aed0'
REQ = '1d52cf72-cc0c-4a8b-af9d-a2ac80fc98aa'
RULE = '087af9b8-70ac-4a3b-8b0c-e844b800f52d'
QUOTE = '容量已满时拒绝新增选课。'


def entity(identity=REQ, kind='Requirement', name='教学班容量限制', version=2, **kwargs):
    attrs = {'extensions': {'extraction': {'job_id': str(uuid4())}}, 'requirement_type': 'functional',
             'acceptance_criteria': [QUOTE]}
    if kind == 'Requirement':
        attrs['extensions']['acceptance_items'] = [
            {'key': 'AC-FULL', 'text': QUOTE, 'evidence': [{'id': str(uuid4()), 'version': 1}]}]
    else:
        attrs = {'extensions': {}}
    return {'id': identity, 'version': version, 'kind': kind, 'name': name, 'description': QUOTE,
            'attributes': attrs, 'status': 'active', 'evidence': [], **kwargs}


def item(key='REQ-CAP', kind='Requirement', name='教学班容量限制', **kwargs):
    return {'key': key, 'kind': kind, 'name': name, 'description': QUOTE, 'attributes': {},
            'existing': None, 'acceptance_items': None, 'evidence': [{'quote': QUOTE, 'start': None}], **kwargs}


def context(records=(), bindings=(), relations=(), relation_bindings=(), version=2):
    return {'source': {'source_id': SOURCE, 'version': 3}, 'existing_entities': list(records),
            'bindings': list(bindings), 'existing_relationships': list(relations),
            'relationship_bindings': list(relation_bindings), 'review_contract_version': version}


def candidate(items=(), links=(), hints=()):
    return {'summary': '选课增量审阅', 'entities': list(items), 'relationships': list(links),
            'review_hints': list(hints), 'warnings': []}


def binding(key='REQ-CAP', identity=REQ, version=2):
    return {'key': key, 'entity_id': identity, 'version': version}


def relationship(kind='CONSTRAINED_BY', identity=None, **kwargs):
    return {'id': identity or str(uuid4()), 'version': 2, 'type': kind, 'source_id': REQ, 'source_version': 1,
            'target_id': RULE, 'target_version': 1, 'rationale': QUOTE, 'status': 'verified', 'evidence': [], **kwargs}


def hint(kind='possible_conflict', key='REQ-CAP', identity=REQ, **kwargs):
    return {'kind': kind, 'candidate_key': key, 'existing': {'id': identity, 'version': 2},
            'rationale': '新旧规则可能适用于相同场景但处理结果不同。',
            'evidence': [{'quote': '容量已满时允许新增选课。', 'start': None}], 'existing_quote': QUOTE, **kwargs}


def decisions(report):
    return [{'finding_id': value['id'], 'action': value['allowed_actions'][0]['value'],
             'reason': '已核对新旧原文及适用范围。'} for value in report['findings']]


def test_new_candidates_show_addition_and_keep_input_immutable():
    source, document = context(), candidate([item()])
    before = deepcopy((source, document))
    report = build_review(source, document)
    change = report['changes'][0]
    assert change['operation'] == 'added'
    assert change['before'] is None and change['after']['version'] == 1
    assert report['summary']['semantic_checked'] is True
    assert report['findings'] == []
    assert (source, document) == before
    assert build_review({**source, 'review_contract_version': 1}, document)['summary']['semantic_checked'] is False
    source.pop('review_contract_version')
    assert build_review(source, document)['summary']['semantic_checked'] is False


def test_confirmed_binding_and_omitted_attributes_preserve_identity_and_content():
    old = entity()
    old['attributes']['extensions']['team:owner'] = '选课组'
    source = context([old], [binding()])
    report = build_review(source, candidate([item()]))
    change = report['changes'][0]
    assert change['operation'] == 'evidence_only'
    assert change['changed_fields'] == []
    assert change['after']['id'] == REQ and change['after']['version'] == 3
    assert change['after']['attributes']['requirement_type'] == 'functional'
    assert change['after']['attributes']['extensions'] == {'team:owner': '选课组'}
    assert change['after']['acceptance_items'] == [{'key': 'AC-FULL', 'text': QUOTE}]
    assert report['findings'] == []


def test_rename_is_update_without_inventing_semantic_conflict():
    report = build_review(context([entity()], [binding()]), candidate([item(name='班级容量上限')]))
    assert report['changes'][0]['operation'] == 'updated'
    assert report['changes'][0]['changed_fields'] == ['名称']
    assert report['findings'] == []


def test_explicit_attribute_revision_and_extension_merge_match_storage_semantics():
    old = entity()
    old['attributes']['extensions']['preserved'] = True
    report = build_review(context([old], [binding()]), candidate([item(attributes={
        'requirement_type': 'nonfunctional', 'extensions': {'new': '说明'}})]))
    attrs = report['changes'][0]['after']['attributes']
    assert attrs == {'requirement_type': 'nonfunctional', 'extensions': {'preserved': True, 'new': '说明'}}
    assert report['changes'][0]['changed_fields'] == ['知识属性']


@pytest.mark.parametrize('items, expected', [
    ([{'key': 'AC-FULL', 'text': QUOTE, 'evidence': [{'quote': '新证据'}]}], False),
    ([{'key': 'AC-FULL', 'text': QUOTE}, {'key': 'AC-ZERO', 'text': '零容量拒绝。'}], False),
    ([{'key': 'AC-FULL', 'text': '允许排队。'}], True),
    ([], True),
    ([{'key': 'AC-RENAMED', 'text': QUOTE}], True),
])
def test_acceptance_reviews_content_changes_and_removal_not_evidence_or_additions(items, expected):
    report = build_review(context([entity()], [binding()]), candidate([item(acceptance_items=items)]))
    changed = [finding for finding in report['findings'] if finding['kind'] == 'acceptance_changed']
    assert bool(changed) is expected
    if changed:
        assert changed[0]['existing']['id'] == REQ
        assert changed[0]['allowed_actions'] == [{'value': 'accept_change', 'label': '确认本轮修改'}]


def test_legacy_acceptance_text_receiving_keys_is_not_reported_as_removed():
    old = entity()
    old['attributes']['extensions'].pop('acceptance_items')
    report = build_review(context([old], [binding()]), candidate([item(acceptance_items=[{'key': 'AC-FULL', 'text': QUOTE}])]))
    assert report['findings'] == []


def test_lexical_duplicate_does_not_bind_and_explicit_selection_requires_confirmation():
    old = entity()
    report = build_review(context([old]), candidate([item()]))
    duplicate = report['findings'][0]
    assert duplicate['kind'] == 'possible_duplicate' and duplicate['match_basis'] == 'name_exact'
    assert report['changes'][0]['after']['id'] != REQ
    assert [action['value'] for action in duplicate['allowed_actions']] == ['keep_distinct']
    reused = build_review(context([old]), candidate([item(existing={'id': REQ, 'version': 2})]))
    assert [value['kind'] for value in reused['findings']] == ['cross_source_binding']
    assert reused['changes'][0]['after']['id'] == REQ
    assert reused['findings'][0]['allowed_actions'][0]['value'] == 'confirm_binding'


def test_near_names_are_lexical_suggestions_not_proven_conflicts():
    old = entity(name='限制教学班的选课容量')
    report = build_review(context([old]), candidate([item(name='限制教学班选课容量')]))
    assert [value['kind'] for value in report['findings']] == ['possible_duplicate']
    assert report['findings'][0]['match_basis'] == 'name_similar'


def test_type_boundary_blocks_bindings_and_does_not_match_names_across_types():
    old = entity(kind='BusinessRule')
    assert build_review(context([old]), candidate([item()]))['findings'] == []
    with pytest.raises(KnowledgeError, match='类型和版本'):
        build_review(context([old]), candidate([item(existing={'id': REQ, 'version': 2})]))


def test_stale_bindings_and_changed_source_identity_cannot_form_review():
    with pytest.raises(KnowledgeError, match='类型和版本'):
        build_review(context([entity()]), candidate([item(existing={'id': REQ, 'version': 1})]))
    with pytest.raises(KnowledgeError, match='来源知识身份'):
        build_review(context([entity(), entity(RULE)], [binding()]),
                     candidate([item(existing={'id': RULE, 'version': 2})]))


def test_same_named_candidates_are_reviewed_as_separate_identities():
    report = build_review(context(), candidate([item('REQ-1'), item('REQ-2')]))
    assert len(report['findings']) == 1
    assert report['findings'][0]['candidate_keys'] == ['REQ-1', 'REQ-2']
    assert report['changes'][0]['after']['id'] != report['changes'][1]['after']['id']


def test_missing_same_source_entities_and_relations_are_only_retirement_suggestions():
    old_relation = relationship()
    report = build_review(context([entity(), entity(RULE, 'BusinessRule')], [binding()], [old_relation],
                                  [{'key': 'REL-CAP', 'relationship_id': old_relation['id']}]), candidate())
    assert report['changes'] == []
    assert {value['kind'] for value in report['findings']} == {'missing_entity', 'missing_relationship'}
    assert {value['table'] for value in report['missing']} == {'entity', 'relationship'}
    assert all({action['value'] for action in value['allowed_actions']} == {'keep_existing', 'defer_retirement'}
               for value in report['findings'])
    assert all('operations' not in finding for finding in report['findings'])


def test_rejected_or_retired_relations_and_archived_entities_do_not_create_missing_review():
    old = entity(status='archived')
    rel = relationship(status='retired')
    source = context([old], [binding()], [rel], [{'key': 'REL', 'relationship_id': rel['id']}])
    assert build_review(source, candidate())['findings'] == []


def test_delta_scope_does_not_infer_retirement_from_omission_but_retains_explicit_hint():
    old, rel = entity(), relationship()
    source = {**context([old], [binding()], [rel], [{'key': 'REL', 'relationship_id': rel['id']}]),
              'source_scope': 'delta'}
    report = build_review(source, candidate())
    assert report['missing'] == [] and report['findings'] == []
    report = build_review(source, candidate(hints=[hint('retirement', key=None)]))
    assert [value['kind'] for value in report['findings']] == ['retirement_suggestion']


def test_relation_diffs_preserve_identity_and_fixed_before_endpoint_versions():
    rel = relationship()
    source = context([entity(), entity(RULE, 'BusinessRule')], [binding(), binding('RULE-CAP', RULE)],
                     [rel], [{'key': 'REL-CAP', 'relationship_id': rel['id']}])
    document = candidate([item(), item('RULE-CAP', 'BusinessRule')], [
        {'key': 'REL-CAP', 'type': 'CONSTRAINED_BY', 'source_key': 'REQ-CAP', 'target_key': 'RULE-CAP',
         'rationale': QUOTE, 'evidence': [{'quote': QUOTE}]}])
    report = build_review(source, document)
    change = report['changes'][-1]
    assert change['table'] == 'relationship'
    assert change['before']['source_version'] == 1
    assert change['after']['source_version'] == 3
    assert change['before']['id'] == change['after']['id'] == rel['id']
    assert set(change['changed_fields']) == {'起点版本', '终点版本', '关系状态'}


def test_explicit_conflict_relationship_is_reported_without_claiming_new_text_is_conflicting():
    rel = relationship(kind='CONFLICTS_WITH', status='proposed')
    report = build_review(context([entity(), entity(RULE, 'BusinessRule')], [binding()], [rel]), candidate([item()]))
    finding = report['findings'][0]
    assert finding['kind'] == 'recorded_conflict'
    assert finding['before']['status'] == 'proposed'
    assert finding['before']['source_version'] == 1
    assert finding['existing']['id'] == RULE


def test_new_explicit_conflict_relationship_requires_review_even_without_model_hint():
    document = candidate([item('RULE-1', 'BusinessRule', '满员拒绝'), item('RULE-2', 'BusinessRule', '满员接收')],
                         [{'key': 'CONFLICT-1', 'type': 'CONFLICTS_WITH', 'source_key': 'RULE-1', 'target_key': 'RULE-2',
                           'rationale': '同场景处理结果相反。', 'evidence': [{'quote': QUOTE}]}])
    report = build_review(context(), document)
    assert [value['kind'] for value in report['findings']] == ['proposed_conflict']
    assert report['findings'][0]['allowed_actions'][0]['value'] == 'keep_both'
    with pytest.raises(KnowledgeError, match='未处理'):
        validate_decisions(report, [])


def test_relation_endpoint_only_revision_is_explained_as_version_refresh():
    rel = relationship(status='proposed')
    source = context([entity(), entity(RULE, 'BusinessRule')], [binding(), binding('RULE-CAP', RULE)], [rel])
    document = candidate([item(), item('RULE-CAP', 'BusinessRule')], [
        {'key': 'REL', 'type': 'CONSTRAINED_BY', 'source_key': 'REQ-CAP', 'target_key': 'RULE-CAP',
         'rationale': QUOTE, 'evidence': [{'quote': QUOTE}]}])
    change = build_review(source, document)['changes'][-1]
    assert change['version_refresh_only'] is True
    assert '不表示关系含义改变' in change['change_note']


def test_model_conflict_retains_exact_old_reference_and_actions_depend_on_identity():
    old = entity()
    document = candidate([item(name='满员后允许排队')], hints=[hint()])
    report = build_review(context([old]), document)
    finding = report['findings'][0]
    assert finding['kind'] == 'possible_conflict' and finding['origin'] == 'model'
    assert finding['existing']['version'] == 2 and finding['existing_quote'] == QUOTE
    assert finding['allowed_actions'][0]['value'] == 'keep_both'
    document['entities'][0]['existing'] = {'id': REQ, 'version': 2}
    report = build_review(context([old], [binding()]), document)
    assert report['findings'][0]['allowed_actions'][0]['value'] == 'accept_change'


def test_model_duplicate_merges_with_name_hint_and_deselected_candidates_cannot_add_conflict():
    report = build_review(context([entity()]), candidate([item()], hints=[hint('possible_duplicate')]))
    assert len(report['findings']) == 1
    finding = report['findings'][0]
    assert finding['origins'] == ['rule', 'model']
    assert finding['existing_quote'] == QUOTE
    assert len(finding['evidence']) == 2
    assert build_review(context([entity()]), candidate(hints=[hint()]))['findings'] == []


def test_model_retirement_is_retained_without_deletion_operation():
    report = build_review(context([entity()]), candidate(hints=[hint('retirement', key=None)]))
    assert report['changes'] == []
    assert report['findings'][0]['kind'] == 'retirement_suggestion'
    assert report['findings'][0]['allowed_actions'][1]['value'] == 'defer_retirement'


def test_many_name_matches_are_bounded_with_explicit_truncation_count():
    records = [entity(str(uuid4())) for _ in range(8)]
    report = build_review(context(records), candidate([item()]))
    assert len(report['findings']) == 5 and report['summary']['matching_truncated'] == 3
    assert any('未完整显示' in note for note in report['notes'])


def test_candidate_duplicate_clustering_avoids_quadratic_review_questions():
    report = build_review(context(), candidate([item(f'REQ-{number}') for number in range(40)]))
    assert len(report['findings']) == 1
    assert len(report['findings'][0]['candidate_keys']) == 40


def test_review_limit_refuses_extra_findings_instead_of_silently_truncating(monkeypatch):
    monkeypatch.setattr('knowledge.extraction_review.MAX_FINDINGS', 1)
    with pytest.raises(KnowledgeError, match='不会静默丢弃'):
        build_review(context([entity(), entity(RULE)]), candidate([item()]))


def test_finding_ids_are_unambiguous_and_stable_after_input_order_changes():
    first, second = entity(REQ), entity(RULE)
    document = candidate([item('A:B'), item('A')])
    initial = build_review(context([first, second]), document)
    reordered = build_review(context([second, first]), {**document, 'entities': list(reversed(document['entities']))})
    assert [value['id'] for value in initial['findings']] == [value['id'] for value in reordered['findings']]
    assert len({value['id'] for value in initial['findings']}) == len(initial['findings'])


def test_uuid_inputs_are_serialized_into_plain_json_records():
    source = context([entity(UUID(REQ))], [binding(identity=UUID(REQ))])
    source['source']['source_id'] = UUID(SOURCE)
    report = build_review(source, candidate([item()]))
    assert report['changes'][0]['before']['id'] == REQ


def test_decisions_require_complete_explicit_supported_answers_and_nonempty_reasons():
    report = build_review(context([entity()]), candidate([item()]))
    valid = decisions(report)
    assert validate_decisions(report, valid) == valid
    with pytest.raises(KnowledgeError, match='未处理'):
        validate_decisions(report, [])
    with pytest.raises(KnowledgeError, match='审阅依据'):
        validate_decisions(report, [{**valid[0], 'reason': '  '}])
    with pytest.raises(KnowledgeError, match='明确处理'):
        validate_decisions(report, [{**valid[0], 'action': 'pending'}])
    with pytest.raises(KnowledgeError, match='不存在'):
        validate_decisions(report, [{**valid[0], 'finding_id': 'outdated'}])
    with pytest.raises(KnowledgeError, match='一个审阅结论'):
        validate_decisions(report, [*valid, *valid])
    with pytest.raises(KnowledgeError, match='明确处理'):
        validate_decisions(report, [{**valid[0], 'action': 'delete'}])


def test_no_findings_allow_empty_decisions_and_unknown_extras_still_fail():
    report = build_review(context(), candidate([item()]))
    assert validate_decisions(report, []) == []
    with pytest.raises(KnowledgeError, match='不存在'):
        validate_decisions(report, [{'finding_id': 'fabricated', 'action': 'keep_distinct', 'reason': '无依据'}])
