"""M2 增量审阅的真实数据库闭环：显式裁决、版本保护与历史兼容。"""
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from knowledge import model_gateway
from knowledge.db import connect
from knowledge.extraction import ExtractionService, _digest
from test_extraction import (ACTOR, TEXT, QUOTE, RULE, approve, candidate, complete,
                             extraction_environment, ok, propose, register)


pytestmark = pytest.mark.integration


def review(client, job, document=None):
    return client.post(f'/extractions/jobs/{job["id"]}/review',
                       json={'candidate': document if document is not None else job['result']['candidate']})


def decisions(report):
    return [{'finding_id': finding['id'], 'action': finding['allowed_actions'][0]['value'],
             'reason': '逐条核对两侧内容、固定原文及本轮适用范围'} for finding in report['findings']]


def submission(job, report=None, document=None, choices=None):
    body = {'actor': ACTOR, 'reason': '核对本轮增量与可能影响后形成待审提案',
            'candidate': document if document is not None else job['result']['candidate']}
    if report:
        body.update(review_digest=report['review_digest'],
                    decisions=choices if choices is not None else decisions(report))
    return body


def records(db_url, project_id):
    queries = {
        'entities': 'SELECT count(*) AS n FROM entity_revision r JOIN entity e ON e.id=r.entity_id WHERE e.project_id=%s',
        'relationships': 'SELECT count(*) AS n FROM relationship_revision r JOIN relationship e ON e.id=r.relationship_id WHERE e.project_id=%s',
        'proposals': 'SELECT count(*) AS n FROM change_proposal WHERE project_id=%s',
        'job_events': 'SELECT count(*) AS n FROM extraction_job_event e JOIN extraction_job j ON j.id=e.job_id WHERE j.project_id=%s',
        'sources': 'SELECT count(*) AS n FROM source_revision r JOIN knowledge_source s ON s.id=r.source_id WHERE s.project_id=%s',
    }
    with connect(db_url) as conn:
        return {key: conn.execute(query, (project_id,)).fetchone()['n'] for key, query in queries.items()}


def complete_scoped(client, project, db_url, monkeypatch, document, source, source_scope):
    job = ok(client.post(f'/extractions/projects/{project["id"]}/jobs', json={
        'source_id': source['source_id'], 'source_version': source['version'], 'actor': ACTOR,
        'source_scope': source_scope}), 201)

    def model(context, schema, **kwargs):
        kwargs['on_exchange']({'request': {'messages': [{'role': 'system', 'content': 'M2 测试协议'}]},
                               'response': {'content': 'M2 测试原始答复', 'finish_reason': 'stop'}})
        return {'document': deepcopy(document), 'trace': {'prompt_version': 'synthetic-m2'}}

    monkeypatch.setattr(model_gateway, 'complete_extraction', model)
    ExtractionService(db_url).run_job(UUID(job['id']))
    result = ok(client.get('/extractions/jobs/' + job['id']))
    assert result['status'] == 'SUCCEEDED', result['result']
    return result


def test_review_is_read_only_and_proposals_require_digest_of_current_candidate(client, project, db_url, monkeypatch):
    job = complete(client, project, db_url, monkeypatch)
    before = records(db_url, project['id'])
    report = ok(review(client, job))
    assert ok(review(client, job)) == report
    assert records(db_url, project['id']) == before
    assert report['summary']['semantic_checked'] is True
    assert report['impact']['available'] is True
    assert all(item['operation'] == 'added' for item in report['changes'])
    endpoint = f'/extractions/jobs/{job["id"]}/proposals'
    absent = client.post(endpoint, json=submission(job))
    assert absent.status_code == 409 and '先分析' in absent.text
    edited = deepcopy(job['result']['candidate'])
    edited['entities'][0]['name'] = '学生提交基础选课'
    stale = client.post(endpoint, json=submission(job, report, edited))
    assert stale.status_code == 409 and '重新分析' in stale.text
    assert records(db_url, project['id']) == before
    fresh = ok(review(client, job, edited))
    assert fresh['review_digest'] != report['review_digest']
    proposal = ok(client.post(endpoint, json=submission(job, fresh, edited)), 201)
    assert proposal['plan']['extraction']['review']['review_digest'] == fresh['review_digest']


def test_duplicate_requires_decision_and_explicit_binding_reuses_same_identity(client, project, db_url, monkeypatch, entity):
    original = entity('Requirement', '基础选课', attributes={'requirement_type': 'functional'})
    job = complete(client, project, db_url, monkeypatch)
    report = ok(review(client, job))
    duplicate = next(item for item in report['findings'] if item['kind'] == 'possible_duplicate')
    assert duplicate['existing']['id'] == original['id']
    endpoint = f'/extractions/jobs/{job["id"]}/proposals'
    unhandled = client.post(endpoint, json=submission(job, report, choices=[]))
    assert unhandled.status_code == 409 and '未处理' in unhandled.text
    chosen = deepcopy(job['result']['candidate'])
    chosen['entities'][0]['existing'] = {'id': original['id'], 'version': original['version']}
    rebound = ok(review(client, job, chosen))
    assert not any(item['kind'] == 'possible_duplicate' and item.get('existing', {}).get('id') == original['id']
                   for item in rebound['findings'])
    binding = next(item for item in rebound['findings'] if item['kind'] == 'cross_source_binding')
    assert binding['allowed_actions'] == [{'value': 'confirm_binding', 'label': '确认复用该知识身份'}]
    old_digest = client.post(endpoint, json=submission(job, report, chosen))
    assert old_digest.status_code == 409
    proposal = ok(client.post(endpoint, json=submission(job, rebound, chosen)), 201)
    frozen = proposal['plan']['extraction']['review']
    assert frozen['decisions'] == decisions(rebound)
    diff = ok(client.get(f'/workbench/proposals/{proposal["id"]}/diff'))
    assert diff['extraction_review'] == frozen
    committed = approve(client, proposal)
    mapping = next(item for item in committed['result']['entity_mapping'] if item['key'] == 'REQ-ENROLL')
    assert mapping['entity_id'] == original['id'] and mapping['version'] == 2
    assert len(ok(client.get('/entities', params={'project_id': project['id'], 'kind': 'Requirement'}))) == 1
    assert ok(client.get('/entities/' + original['id'], params={'version': 1})) == original
    assert ok(client.get(f'/workbench/proposals/{proposal["id"]}/diff'))['extraction_review'] == frozen


def hint_document(original):
    document = candidate()
    document['review_hints'] = [{
        'kind': 'possible_conflict', 'candidate_key': 'RULE-CAPACITY',
        'existing': {'id': original['id'], 'version': original['version']},
        'rationale': '已有规则允许满员后新增，本轮规则拒绝新增，需确认适用范围。',
        'evidence': [{'quote': RULE}], 'existing_quote': original['description'],
    }]
    return document


def test_model_conflict_cannot_be_hidden_by_removing_client_hints(client, project, db_url, monkeypatch, entity, provenance):
    original = entity('BusinessRule', '超额选课策略', description='容量已满也允许新增选课。')
    job = complete(client, project, db_url, monkeypatch, hint_document(original))
    assert job['status'] == 'SUCCEEDED'
    normal = ok(review(client, job))
    hidden = deepcopy(job['result']['candidate'])
    hidden['review_hints'] = []
    report = ok(review(client, job, hidden))
    assert report == normal
    conflict = next(item for item in report['findings'] if item['kind'] == 'possible_conflict')
    assert conflict['origin'] == 'model' and conflict['existing_quote'] == original['description']
    assert conflict['evidence'][0]['quote'] == RULE
    endpoint = f'/extractions/jobs/{job["id"]}/proposals'
    response = client.post(endpoint, json=submission(job, report, hidden, choices=[]))
    assert response.status_code == 409 and '未处理' in response.text
    proposal = ok(client.post(endpoint, json=submission(job, report, hidden)), 201)
    stored = proposal['plan']['extraction']
    assert stored['candidate']['review_hints'][0]['existing_quote'] == original['description']
    assert any(item['kind'] == 'possible_conflict' for item in stored['review']['findings'])
    # 模型提示和人审决定保留在提案中，不偷偷建立正式冲突关系。
    assert not any(item['op'] == 'create_relationship' and item['data']['type'] == 'CONFLICTS_WITH'
                   for item in proposal['plan']['operations'])


def test_new_explicit_conflict_relation_requires_review_before_it_can_be_recorded(client, project, db_url, monkeypatch):
    opposite = '容量已满时仍允许新增选课。'
    source = register(client, project, TEXT + opposite)
    document = {'summary': '两条满员处理要求互相冲突，等待审阅', 'entities': [
        {'key': 'REJECT-FULL', 'kind': 'BusinessRule', 'name': '满员拒绝', 'description': RULE,
         'evidence': [{'quote': RULE}]},
        {'key': 'ACCEPT-FULL', 'kind': 'BusinessRule', 'name': '满员仍放行', 'description': opposite,
         'evidence': [{'quote': opposite}]},
    ], 'relationships': [{'key': 'FULL-CONFLICT', 'type': 'CONFLICTS_WITH',
        'source_key': 'REJECT-FULL', 'target_key': 'ACCEPT-FULL',
        'rationale': '两个满员处理规则相反，未说明例外适用范围。',
        'evidence': [{'quote': RULE}, {'quote': opposite}]}], 'warnings': []}
    job = complete(client, project, db_url, monkeypatch, document, source)
    assert job['status'] == 'SUCCEEDED'
    report = ok(review(client, job))
    conflict = next(item for item in report['findings'] if item['kind'] == 'proposed_conflict')
    assert conflict['allowed_actions'][0]['value'] == 'keep_both'
    endpoint = f'/extractions/jobs/{job["id"]}/proposals'
    assert client.post(endpoint, json=submission(job, report, choices=[])).status_code == 409
    proposal = ok(client.post(endpoint, json=submission(job, report)), 201)
    committed = approve(client, proposal)
    link_id = committed['plan']['extraction']['relationship_mapping'][0]['relationship_id']
    relation = ok(client.get('/relationships/' + link_id))
    assert relation['type'] == 'CONFLICTS_WITH' and relation['status'] == 'proposed'


@pytest.mark.parametrize('field,value,diagnostic', [
    ('existing_quote', '旧知识没有这句话', '已有知识引文不匹配'),
    ('evidence', [{'quote': '当前文档没有这句话'}], '未出现在固定原文'),
])
def test_model_hint_evidence_on_both_sides_must_be_exact(client, project, db_url, monkeypatch, entity, field, value, diagnostic):
    original = entity('BusinessRule', '旧满员规则', description='容量已满也允许新增选课。')
    document = hint_document(original)
    document['review_hints'][0][field] = value
    job = complete(client, project, db_url, monkeypatch, document)
    assert job['status'] == 'FAILED'
    assert diagnostic in job['result']['diagnostic']
    assert job['result']['model_exchange']['response']['content'] == '测试原始答复'
    assert len(ok(client.get('/entities', params={'project_id': project['id'], 'kind': 'BusinessRule'}))) == 1
    assert review(client, job, candidate()).status_code == 409


@pytest.mark.parametrize('source_scope', ['full', 'delta'])
def test_removed_acceptance_and_missing_items_never_delete_history(client, project, db_url, monkeypatch, source_scope):
    initial = complete(client, project, db_url, monkeypatch)
    committed = approve(client, ok(propose(client, initial), 201))
    mapping = {item['key']: item for item in committed['result']['entity_mapping']}
    requirement_id, rule_id = mapping['REQ-ENROLL']['entity_id'], mapping['RULE-CAPACITY']['entity_id']
    old_requirement = ok(client.get('/entities/' + requirement_id))
    old_rule = ok(client.get('/entities/' + rule_id))
    old_relationship_id = committed['plan']['extraction']['relationship_mapping'][0]['relationship_id']
    old_relationship = ok(client.get('/relationships/' + old_relationship_id))
    source = register(client, project, TEXT, revision='v2')
    document = candidate()
    document['entities'] = document['entities'][:1]
    document['entities'][0]['acceptance_items'] = []
    document['relationships'] = []
    job = complete_scoped(client, project, db_url, monkeypatch, document, source, source_scope)
    report = ok(review(client, job))
    assert report['source_scope'] == source_scope
    changed = next(item for item in report['findings'] if item['kind'] == 'acceptance_changed')
    assert changed['removed_items'] == [{'key': 'AC-RECORD', 'text': QUOTE}]
    if source_scope == 'full':
        assert {item['kind'] for item in report['findings']} >= {'missing_entity', 'missing_relationship'}
    else:
        assert report['missing'] == []
        assert not any(item['kind'].startswith('missing_') for item in report['findings'])
    choices = decisions(report)
    missing_ids = {item['id'] for item in report['findings'] if item['kind'].startswith('missing_')}
    for choice in choices:
        if choice['finding_id'] in missing_ids:
            choice['action'] = 'defer_retirement'
            choice['reason'] = '记录待评估移除事项，本轮尚未授权归档或退役。'
    proposal = ok(client.post(f'/extractions/jobs/{job["id"]}/proposals', json=submission(job, report, choices=choices)), 201)
    assert proposal['plan']['removed_keys'] == []
    assert not any(item['id'] in {rule_id, old_relationship_id} for item in proposal['plan']['operations'])
    approve(client, proposal)
    assert ok(client.get('/entities/' + rule_id)) == old_rule
    assert ok(client.get('/relationships/' + old_relationship_id)) == old_relationship
    assert ok(client.get('/entities/' + requirement_id))['attributes']['acceptance_criteria'] == []
    assert ok(client.get('/entities/' + requirement_id, params={'version': 1})) == old_requirement


def test_new_project_knowledge_after_job_invalidates_analysis_and_proposal(client, project, db_url, monkeypatch, entity):
    job = complete(client, project, db_url, monkeypatch)
    report = ok(review(client, job))
    entity('BusinessRule', '任务开始之后新增的规则')
    response = review(client, job)
    assert response.status_code == 409 and '上下文重新抽取' in response.text
    response = client.post(f'/extractions/jobs/{job["id"]}/proposals', json=submission(job, report))
    assert response.status_code == 409
    assert not ok(client.get('/operations/proposals', params={'project_id': project['id']}))


def test_new_relationship_after_validating_proposal_blocks_commit(client, project, db_url, monkeypatch, entity, relationship):
    component = entity('ArchitectureComponent', '选课模块')
    interface = entity('Interface', '选课接口')
    job = complete(client, project, db_url, monkeypatch)
    proposal = ok(propose(client, job), 201)
    path = '/operations/proposals/' + proposal['id']
    validated = ok(client.post(path + '/validate', json={'expected_version': proposal['version'],
        'actor': ACTOR, 'reason': '当前固定上下文校验'}))['proposal']
    assert validated['status'] == 'validated'
    relationship(component, interface, type='DEPENDS_ON')
    assert ok(client.get(f'/workbench/proposals/{proposal["id"]}/diff'))['stale'] is True
    response = client.post(path + '/commit', json={'expected_version': validated['version'],
        'actor': ACTOR, 'reason': '尝试提交已经过期的影响审阅'})
    assert response.status_code == 409 and '不会沿用过期决定' in response.text
    assert ok(client.get(path))['status'] == 'validated'
    assert ok(client.get('/entities', params={'project_id': project['id'], 'kind': 'Requirement'})) == []


def test_evidence_only_revision_does_not_start_semantic_impact(client, project, db_url, monkeypatch):
    initial = complete(client, project, db_url, monkeypatch)
    approve(client, ok(propose(client, initial), 201))
    source = register(client, project, TEXT, revision='v2')
    second = complete(client, project, db_url, monkeypatch, candidate(), source)
    report = ok(review(client, second))
    entity_changes = [item for item in report['changes'] if item['table'] == 'entity']
    assert len(entity_changes) == 2
    assert all(item['operation'] == 'evidence_only' and item['changed_fields'] == [] for item in entity_changes)
    assert report['impact']['available'] is True
    assert report['impact']['roots'] == report['impact']['affected'] == report['impact']['model_affected'] == []
    proposal = ok(propose(client, second), 201)
    assert len([item for item in proposal['plan']['operations'] if item['op'] == 'update_entity']) == 2


def test_empty_full_candidate_can_review_missing_items_but_cannot_write_retirements(client, project, db_url, monkeypatch):
    initial = complete(client, project, db_url, monkeypatch)
    committed = approve(client, ok(propose(client, initial), 201))
    entity_ids = {item['entity_id'] for item in committed['result']['entity_mapping']}
    relationship_ids = {item['relationship_id'] for item in committed['plan']['extraction']['relationship_mapping']}
    original_entities = {identity: ok(client.get('/entities/' + identity)) for identity in entity_ids}
    original_relationships = {identity: ok(client.get('/relationships/' + identity)) for identity in relationship_ids}
    source = register(client, project, '本版资料不再列出此前的选课条目，移除仍需单独审阅。', revision='v2')
    empty = {'summary': '本版没有可写入知识，仅供审阅未出现的旧条目',
             'entities': [], 'relationships': [], 'warnings': [], 'review_hints': []}
    job = complete_scoped(client, project, db_url, monkeypatch, empty, source, 'full')
    before = records(db_url, project['id'])
    report = ok(review(client, job))
    assert report['changes'] == []
    assert {item['id'] for item in report['missing'] if item['table'] == 'entity'} == entity_ids
    assert {item['id'] for item in report['missing'] if item['table'] == 'relationship'} == relationship_ids
    assert {item['kind'] for item in report['findings']} == {'missing_entity', 'missing_relationship'}
    assert records(db_url, project['id']) == before
    choices = [{**item, 'action': 'defer_retirement', 'reason': '本次只记录移除建议，另行进行生命周期审阅。'}
               for item in decisions(report)]
    response = client.post(f'/extractions/jobs/{job["id"]}/proposals',
                           json=submission(job, report, choices=choices))
    assert response.status_code == 422
    assert '没有知识写入' in response.text and '生命周期修订提案' in response.text
    assert records(db_url, project['id']) == before
    assert {identity: ok(client.get('/entities/' + identity)) for identity in entity_ids} == original_entities
    assert {identity: ok(client.get('/relationships/' + identity)) for identity in relationship_ids} == original_relationships


def test_legacy_m1_task_can_be_reviewed_without_claiming_frozen_m2_impact(client, project, db_url, monkeypatch):
    current = complete(client, project, db_url, monkeypatch)
    legacy_id = uuid4()
    with connect(db_url) as conn:
        record = conn.execute('SELECT * FROM extraction_job WHERE id=%s', (current['id'],)).fetchone()
        request = deepcopy(record['request'])
        for key in ('project_state_token', 'impact_snapshot', 'review_contract_version', 'retrieval', 'source_scope'):
            request['context'].pop(key, None)
        request['input_sha256'] = _digest(request['context'])
        result = deepcopy(current['result'])
        result['candidate'].pop('review_hints', None)
        conn.execute('''INSERT INTO extraction_job(id,project_id,source_id,source_version,actor,request)
            VALUES(%s,%s,%s,%s,%s,%s)''', (legacy_id, record['project_id'], record['source_id'],
                                         record['source_version'], ACTOR, Jsonb(request)))
        ExtractionService._event(conn, legacy_id, 1, 'SUCCEEDED', result)
    legacy = ok(client.get('/extractions/jobs/' + str(legacy_id)))
    before = records(db_url, project['id'])
    report = ok(review(client, legacy))
    assert records(db_url, project['id']) == before
    assert report['summary']['semantic_checked'] is False
    assert report['impact']['available'] is False and report['impact']['affected'] == []
    assert '无法回溯' in report['impact']['message']
    proposal = ok(client.post(f'/extractions/jobs/{legacy_id}/proposals', json=submission(legacy, report)), 201)
    assert proposal['plan']['extraction']['review']['impact']['available'] is False
    assert ok(client.get('/extractions/jobs/' + current['id']))['context'] == current['context']
