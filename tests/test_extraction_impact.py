"""M2 固定影响路径的方向、边界、历史引用与模型粒度。"""
from copy import deepcopy

import pytest

from knowledge.db import connect
from knowledge.extraction_impact import build_impact, capture_impact_snapshot
from knowledge.semantics import KnowledgeError


def entity(identity, kind='Requirement', version=1):
    return {'id': identity, 'name': '知识 ' + identity, 'kind': kind, 'version': version, 'status': 'active'}


def link(identity, start, end, kind, *, version=1, source_version=1, target_version=1, status='proposed'):
    return {'id': identity, 'version': version, 'type': kind, 'status': status,
            'source_id': start, 'target_id': end, 'source_version': source_version, 'target_version': target_version}


def snapshot(entities, relationships=(), baseline=None):
    return {'project_id': 'project', 'sha256': 'fixed-snapshot-hash', 'captured_at': '2026-11-12T01:00:00Z',
            'entities': entities, 'relationships': list(relationships), 'baseline': baseline,
            'truncated': False, 'truncation_reasons': []}


def root(identity, version=1):
    return {'id': identity, 'version': version}


def test_fixed_impact_follows_semantic_directions_and_keeps_full_version_path():
    data = snapshot([entity('rule', 'BusinessRule', 2), entity('requirement', version=3),
                     entity('component', 'ArchitectureComponent'), entity('interface', 'Interface'),
                     entity('code', 'CodeArtifact'), entity('test', 'TestCase'), entity('unrelated', 'DataModel')], [
        link('constraint', 'requirement', 'rule', 'CONSTRAINED_BY', version=4, source_version=2),
        link('implementation', 'requirement', 'component', 'IMPLEMENTED_BY', source_version=3),
        link('code', 'component', 'code', 'IMPLEMENTED_BY'),
        link('verification', 'requirement', 'test', 'VERIFIED_BY', source_version=3),
        link('api', 'interface', 'requirement', 'DERIVED_FROM', target_version=3),
        link('dependency', 'component', 'unrelated', 'DEPENDS_ON'),
    ])
    before = deepcopy(data)
    result = build_impact(data, [root('rule', 2)])
    by_id = {item['id']: item for item in result['affected']}
    assert set(by_id) == {'requirement', 'component', 'code', 'test', 'interface'}
    assert by_id['requirement']['depth'] == 1 and by_id['requirement']['level'] == 'direct'
    assert by_id['code']['depth'] == 3 and by_id['code']['level'] == 'indirect'
    assert [edge['direction'] for edge in by_id['code']['path']] == ['reverse', 'forward', 'forward']
    edge = by_id['code']['path'][0]
    assert edge['relationship_id'] == 'constraint' and edge['relationship_version'] == 4
    assert (edge['source_version'], edge['current_source_version']) == (2, 3)
    assert (edge['target_version'], edge['current_target_version']) == (1, 2)
    assert edge['stale_endpoints'] is True
    assert edge['from'] == entity('rule', 'BusinessRule', 2)
    assert edge['to'] == entity('requirement', version=3)
    assert all(item['code'] == 'POTENTIAL_IMPACT' for item in result['affected'])
    assert result['truncated'] is False
    assert data == before
    # 从依赖者出发不能把其依赖项倒推为受影响对象。
    assert 'unrelated' not in {item['id'] for item in build_impact(data, [root('component')])['affected']}


def test_fixed_impact_has_deterministic_shortest_paths_handles_cycles_and_statuses():
    records = [entity(identity) for identity in ('root', 'a', 'b', 'c', 'ignored')]
    relationships = [link('root-b', 'root', 'b', 'IMPACTS'), link('root-a', 'root', 'a', 'IMPACTS'),
                     link('a-c', 'a', 'c', 'IMPACTS'), link('b-c', 'b', 'c', 'IMPACTS'),
                     link('c-root', 'c', 'root', 'IMPACTS'),
                     link('retired', 'root', 'ignored', 'IMPACTS', status='retired'),
                     link('rejected', 'root', 'ignored', 'IMPACTS', status='rejected')]
    result = build_impact(snapshot(records, relationships), [root('root')])
    reversed_result = build_impact(snapshot(records[::-1], relationships[::-1]), [root('root')])
    assert result == reversed_result
    assert [item['id'] for item in result['affected']] == ['a', 'b', 'c']
    assert [edge['relationship_id'] for edge in result['affected'][-1]['path']] == ['root-a', 'a-c']
    assert result['truncated'] is False
    assert build_impact(snapshot(records, relationships), [root('root')], max_depth=1)['truncated']
    assert build_impact(snapshot(records, relationships), [root('root')], max_depth=0)['affected'] == []
    assert build_impact(snapshot(records, relationships), [root('root')], max_depth=0)['truncated']
    bounded = build_impact(snapshot(records, relationships), [root('root')], max_nodes=2)
    assert len(bounded['affected']) == 1 and bounded['truncated']
    assert not build_impact(snapshot(records, relationships), [root('ignored')], max_depth=0)['truncated']


def test_fixed_impact_conflicts_are_bidirectional_supersedes_is_reverse_only():
    data = snapshot([entity('a'), entity('b'), entity('replacement')], [
        link('conflict', 'a', 'b', 'CONFLICTS_WITH'),
        link('replacement', 'replacement', 'a', 'SUPERSEDES'),
    ])
    assert {item['id'] for item in build_impact(data, [root('a')])['affected']} == {'b', 'replacement'}
    assert {item['id'] for item in build_impact(data, [root('b')])['affected']} == {'a', 'replacement'}
    assert build_impact(data, [root('replacement')])['affected'] == []


def model_baseline():
    return {
        'project_id': 'project', 'version': 3, 'proposal_id': 'proposal',
        'nodes': [{'key': 'selection', 'kind': 'module', 'name': '选课模块', 'entity_id': 'component', 'entity_version': 1},
                  {'key': 'same-name', 'kind': 'module', 'name': '知识 rule', 'entity_id': None, 'entity_version': None}],
        'processes': [{
            'key': 'enroll', 'name': '提交选课', 'rule_refs': [root('rule', 1)],
            'requirement_refs': [root('requirement')], 'module_keys': ['selection'],
            'scenarios': [{'key': 'success', 'name': '成功', 'module_keys': ['student', 'selection']},
                          {'key': 'other', 'name': '其他模块', 'module_keys': ['unrelated']}],
        }],
    }


def test_model_impact_uses_fixed_refs_and_does_not_guess_message_impact_from_process_rules():
    data = snapshot([entity('rule', 'BusinessRule', 2), entity('requirement'),
                     entity('component', 'ArchitectureComponent', 3)], baseline=model_baseline())
    result = build_impact(data, [root('rule', 2)])
    assert len(result['model_affected']) == 1
    process = result['model_affected'][0]
    assert process['subject'] == {'kind': 'process', 'key': 'enroll'}
    assert process['depth'] == 1 and process['level'] == 'direct'
    edge = process['path'][-1]
    assert edge['reference_role'] == 'rule_refs'
    assert edge['referenced_version'] == 1 and edge['current_version'] == 2 and edge['stale_reference']
    assert process['baseline_ref'] == {'project_id': 'project', 'version': 3, 'proposal_id': 'proposal'}
    # 流程依据只定位到流程，不凭全流程规则推断哪些消息或场景已经变化。
    assert not any(item['subject']['kind'] == 'scenario' for item in result['model_affected'])
    assert not any(item['subject'].get('key') == 'same-name' for item in result['model_affected'])


def test_module_impact_finds_explicit_architecture_process_and_scenario_refs():
    data = snapshot([entity('rule', 'BusinessRule', 2), entity('requirement'),
                     entity('component', 'ArchitectureComponent', 3)], [
                         link('implemented', 'requirement', 'component', 'IMPLEMENTED_BY', target_version=3),
                     ], model_baseline())
    result = build_impact(data, [root('requirement')])
    found = {(item['subject']['kind'], item['subject']['key']): item for item in result['model_affected']}
    assert set(found) == {('architecture_node', 'selection'), ('process', 'enroll'), ('scenario', 'success')}
    assert found[('architecture_node', 'selection')]['depth'] == 2
    assert found[('scenario', 'success')]['path'][-1]['reference_role'] == 'module_usage'
    assert found[('scenario', 'success')]['path'][-1]['module_key'] == 'selection'
    process = found[('process', 'enroll')]
    assert process['depth'] == 1
    assert {ref['reference_role'] for ref in process['supporting_references']} == {'requirement_refs', 'module_usage'}
    assert all(item['code'] == 'POTENTIAL_MODEL_IMPACT' for item in result['model_affected'])
    limited = build_impact(data, [root('requirement')], max_nodes=3)
    assert len(limited['roots']) + len(limited['affected']) + len(limited['model_affected']) == 3
    assert limited['truncated']
    assert build_impact(data, [root('rule', 2)], max_depth=0)['truncated']


def test_legacy_unavailable_roots_truncation_and_invalid_versions_are_explicit():
    absent = build_impact(None, [root('rule')])
    assert absent['available'] is False and absent['affected'] == []
    data = snapshot([entity('rule')])
    data.update(truncated=True, truncation_reasons=['实体快照达到数量上限，影响结果可能不完整。'])
    result = build_impact(data, [root('missing'), root('rule')])
    assert result['truncated'] and result['unavailable_roots'][0]['id'] == 'missing'
    assert len(result['truncation_reasons']) == 2
    with pytest.raises(KnowledgeError, match='版本与固定'):
        build_impact(data, [root('rule', 2)])
    with pytest.raises(KnowledgeError, match='多个版本'):
        build_impact(data, [root('rule'), root('rule', 2)])
    assert len(build_impact(data, [root('rule'), root('rule')])['roots']) == 1
    for depth, nodes in [(-1, 5), (13, 5), (1, 0), (1, 1001)]:
        with pytest.raises(KnowledgeError):
            build_impact(data, [], max_depth=depth, max_nodes=nodes)


@pytest.mark.integration
def test_capture_impact_is_project_scoped_lightweight_and_stable_after_new_revision(client, project, entity, relationship, provenance, db_url):
    rule = entity('BusinessRule', '容量规则')
    requirement = entity('Requirement', '容量需求')
    edge = relationship(requirement, rule, type='CONSTRAINED_BY')
    entity('Evidence', attributes={'category': 'document', 'uri': 'urn:impact:doc', 'revision': 'v1'})
    other = client.post('/entities', json={'kind': 'Project', 'name': '其他项目', 'provenance': provenance}).json()
    response = client.post('/entities', json={'kind': 'Requirement', 'project_id': other['id'],
        'name': '其他需求', 'provenance': provenance})
    assert response.status_code == 201
    with connect(db_url) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        fixed = capture_impact_snapshot(conn, project['id'])
    assert {item['id'] for item in fixed['entities']} == {rule['id'], requirement['id']}
    assert {item['id'] for item in fixed['relationships']} == {edge['id']}
    assert all(set(item) == {'id', 'version', 'kind', 'status', 'name'} for item in fixed['entities'])
    before = build_impact(fixed, [root(rule['id'])])
    body = {key: rule[key] for key in ('name', 'description', 'attributes', 'evidence', 'status')}
    updated = client.put('/entities/' + rule['id'], json={**body, 'name': '新容量规则', 'expected_version': 1,
                                                        'provenance': provenance})
    assert updated.status_code == 200
    assert build_impact(fixed, [root(rule['id'])]) == before
    with connect(db_url) as conn:
        latest = capture_impact_snapshot(conn, project['id'])
        limited = capture_impact_snapshot(conn, project['id'], max_entities=1)
    assert latest['sha256'] != fixed['sha256']
    assert build_impact(latest, [root(rule['id'], 2)])['affected'][0]['path'][0]['stale_endpoints']
    assert limited['truncated'] and len(limited['entities']) == 1
    assert len(client.get('/relationships/' + edge['id'] + '/history').json()) == 1
