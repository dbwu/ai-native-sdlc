"""M1 选课文档抽取的真实数据库、证据与人工审阅闭环。"""
from copy import deepcopy
import hashlib
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from knowledge import model_gateway
from knowledge.db import connect
from knowledge.extraction import ExtractionService, acceptance_location_key, locate_quotes
from knowledge.extraction_models import ExtractionCandidate, ExtractionQuote
from knowledge.semantics import KnowledgeError


ACTOR = '选课知识审阅人'
TEXT = '# 选课 📚\n学生提交选课后生成一条选课记录。\n重复提交同一课程返回原记录，不重复占用名额。\n容量已满时拒绝新增选课。\n'
QUOTE = '学生提交选课后生成一条选课记录。'
RULE = '容量已满时拒绝新增选课。'


@pytest.fixture(autouse=True)
def extraction_environment(monkeypatch):
    monkeypatch.setenv('KNOWLEDGE_DESIGN_WORKER', 'off')
    monkeypatch.setenv('KNOWLEDGE_MODEL_PROVIDER', 'openai_compatible')
    monkeypatch.setenv('KNOWLEDGE_MODEL_BASE_URL', 'https://m1-model.example.invalid/v1')
    monkeypatch.setenv('KNOWLEDGE_MODEL_MODEL', 'test-model')
    monkeypatch.setenv('KNOWLEDGE_MODEL_API_KEY', 'synthetic-test-key')


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def register(client, project, text=TEXT, revision='v1', uri='urn:m1:course'):
    return ok(client.post('/operations/source-snapshots', json={'project_id': project['id'], 'source_uri': uri,
        'source_revision': revision, 'content': text, 'actor': ACTOR}), 201)


def candidate():
    return {'summary': '选课与容量约束', 'entities': [
        {'key': 'REQ-ENROLL', 'kind': 'Requirement', 'name': '基础选课', 'description': QUOTE,
         'attributes': {'requirement_type': 'functional'}, 'evidence': [{'quote': QUOTE}],
         'acceptance_items': [{'key': 'AC-RECORD', 'text': QUOTE, 'evidence': [{'quote': QUOTE}]}]},
        {'key': 'RULE-CAPACITY', 'kind': 'BusinessRule', 'name': '容量限制', 'description': RULE,
         'attributes': {}, 'evidence': [{'quote': RULE}]}],
        'relationships': [{'key': 'REL-CAPACITY', 'type': 'CONSTRAINED_BY', 'source_key': 'REQ-ENROLL',
                           'target_key': 'RULE-CAPACITY', 'rationale': RULE, 'evidence': [{'quote': RULE}]}],
        'warnings': ['引文匹配不等于语义已通过审阅']}


def complete(client, project, db_url, monkeypatch, document=None, snapshot=None):
    snapshot = snapshot or register(client, project)
    body = {'source_id': snapshot['source_id'], 'source_version': snapshot['version'], 'actor': ACTOR}
    job = ok(client.post(f'/extractions/projects/{project["id"]}/jobs', json=body), 201)

    def model(context, schema, **kwargs):
        kwargs['on_exchange']({'request': {'messages': [{'role': 'system', 'content': '测试提示'}]},
                               'response': {'content': '测试原始答复', 'finish_reason': 'stop'}})
        return {'document': deepcopy(document or candidate()), 'trace': {'prompt_version': 'synthetic-v1'}}

    monkeypatch.setattr(model_gateway, 'complete_extraction', model)
    ExtractionService(db_url).run_job(UUID(job['id']))
    return ok(client.get('/extractions/jobs/'+job['id']))


def propose(client, job, document=None):
    candidate = document or job['result']['candidate']
    report = client.post(f'/extractions/jobs/{job["id"]}/review', json={'candidate': candidate})
    if report.status_code != 200:
        return report
    review = report.json()
    decisions = [{'finding_id': finding['id'], 'action': finding['allowed_actions'][0]['value'],
                  'reason': '测试逐项核对本轮依据和适用范围'} for finding in review['findings']]
    return client.post(f'/extractions/jobs/{job["id"]}/proposals', json={
        'actor': ACTOR, 'reason': '逐条核对选课原文与身份', 'candidate': candidate,
        'review_digest': review['review_digest'], 'decisions': decisions})


def approve(client, proposal):
    path = '/operations/proposals/'+proposal['id']
    valid = ok(client.post(path+'/validate', json={'expected_version': proposal['version'], 'actor': ACTOR,
                                                  'reason': '核对原文和关系端点'}))['proposal']
    assert valid['status'] == 'validated', valid
    return ok(client.post(path+'/commit', json={'expected_version': valid['version'], 'actor': ACTOR,
                                               'reason': '确认候选语义及历史关联'}))['proposal']


def test_quotes_preserve_unicode_and_reject_ambiguous_or_fabricated_sources():
    source = {'source_id': str(uuid4()), 'version': 1, 'content': '📚\n  规则  \n规则',
              'sha256': hashlib.sha256('📚\n  规则  \n规则'.encode()).hexdigest()}
    spans = locate_quotes(source, [ExtractionQuote(quote='  规则  ')])
    assert spans[0]['start'] == 2 and spans[0]['end'] == 8
    with pytest.raises(KnowledgeError, match='出现多次'):
        locate_quotes(source, [ExtractionQuote(quote='规则')])
    with pytest.raises(KnowledgeError, match='未出现在'):
        locate_quotes(source, [ExtractionQuote(quote='伪造')])
    with pytest.raises(KnowledgeError, match='不一致'):
        locate_quotes(source, [ExtractionQuote(quote='规则', start=1)])


@pytest.mark.parametrize('criteria', [[], [QUOTE]])
def test_legacy_acceptance_field_cannot_bypass_stable_acceptance_items(criteria):
    body = candidate()
    body['entities'][0]['attributes']['acceptance_criteria'] = criteria
    body['entities'][0]['acceptance_items'] = None
    with pytest.raises(ValidationError, match='acceptance_items'):
        ExtractionCandidate.model_validate(body)


def test_acceptance_items_only_apply_to_requirement_even_when_empty():
    body = candidate()
    body['entities'][1]['acceptance_items'] = []
    with pytest.raises(ValidationError, match='只有需求'):
        ExtractionCandidate.model_validate(body)


def colliding_acceptance_candidate():
    return {'summary': '验收条件的父项和子项身份不能混淆', 'entities': [
        {'key': entity_key, 'kind': 'Requirement', 'name': quote, 'evidence': [{'quote': quote}],
         'acceptance_items': [{'key': condition_key, 'text': quote, 'evidence': [{'quote': quote}]}]}
        for entity_key, condition_key, quote in [('A:B', 'C', QUOTE), ('A', 'B:C', RULE)]],
        'relationships': [], 'warnings': []}


def test_composite_acceptance_keys_do_not_overwrite_other_requirements_quotes():
    source = {'source_id': str(uuid4()), 'version': 1, 'content': TEXT,
              'sha256': hashlib.sha256(TEXT.encode()).hexdigest()}
    job = {'request': {'context': {'source': source, 'existing_entities': [], 'bindings': []}}}
    locations = ExtractionService._check_candidate(job, ExtractionCandidate.model_validate(colliding_acceptance_candidate()))
    first, second = acceptance_location_key('A:B', 'C'), acceptance_location_key('A', 'B:C')
    assert first != second
    assert locations[first][0]['quote'] == QUOTE
    assert locations[second][0]['quote'] == RULE
    assert locations[first][0]['start'] != locations[second][0]['start']


@pytest.mark.integration
def test_composite_acceptance_keys_keep_separate_evidence_after_commit(client, project, db_url, monkeypatch):
    job = complete(client, project, db_url, monkeypatch, colliding_acceptance_candidate())
    assert job['status'] == 'SUCCEEDED'
    committed = approve(client, ok(propose(client, job), 201))
    refs = {item['key']: item for item in committed['result']['entity_mapping']}
    for entity_key, condition_key, quote in [('A:B', 'C', QUOTE), ('A', 'B:C', RULE)]:
        record = ok(client.get('/entities/'+refs[entity_key]['entity_id']))
        condition = record['attributes']['extensions']['acceptance_items'][0]
        assert condition['key'] == condition_key
        assert condition['source_spans'][0]['quote'] == quote
        evidence = ok(client.get('/entities/'+condition['evidence'][0]['id']))
        assert evidence['attributes']['extensions']['source_span']['quote'] == quote


@pytest.mark.integration
def test_extraction_review_commit_and_fixed_source_history(client, project, db_url, monkeypatch):
    job = complete(client, project, db_url, monkeypatch)
    assert job['status'] == 'SUCCEEDED'
    assert job['result']['model_exchange']['response']['content'] == '测试原始答复'
    assert 'config_fingerprint' not in job
    assert ok(client.get('/entities', params={'project_id': project['id'], 'kind': 'Requirement'})) == []
    proposal = ok(propose(client, job), 201)
    assert client.post(f'/operations/proposals/{proposal["id"]}/commit', json={
        'expected_version': 1, 'actor': ACTOR, 'reason': '提前提交'}).status_code == 409
    committed = approve(client, proposal)
    refs = {item['key']: item for item in committed['result']['entity_mapping']}
    req = ok(client.get('/entities/'+refs['REQ-ENROLL']['entity_id']))
    assert req['version'] == 1 and req['attributes']['acceptance_criteria'] == [QUOTE]
    assert req['attributes']['extensions']['acceptance_items'][0]['key'] == 'AC-RECORD'
    evidence = ok(client.get(f'/operations/projects/{project["id"]}/source-evidence', params={
        'table': 'entity', 'id': req['id'], 'version': 1}))['items']
    assert evidence[0]['span']['quote'] == QUOTE
    old = deepcopy(req)
    snapshot = register(client, project, TEXT.replace('基础', '普通')+'\n时间冲突时拒绝新增选课。', revision='v2')
    listed = ok(client.get(f'/extractions/projects/{project["id"]}/jobs'))['items']
    first_listed = next(item for item in listed if item['id'] == job['id'])
    assert first_listed['source_uri'] == 'urn:m1:course' and first_listed['external_revision'] == 'v1'
    second_doc = candidate()
    second_doc['entities'][0]['name'] = '提交选课'
    second_doc['entities'][0]['acceptance_items'] = None
    second_doc['entities'][0]['attributes'] = {}
    second = complete(client, project, db_url, monkeypatch, second_doc, snapshot)
    assert second['result']['candidate']['entities'][0]['existing'] == {'id': req['id'], 'version': 1}
    update = approve(client, ok(propose(client, second), 201))
    new_req = ok(client.get('/entities/'+req['id']))
    assert new_req['version'] == 2 and new_req['name'] == '提交选课'
    assert new_req['attributes']['requirement_type'] == 'functional'
    assert new_req['attributes']['acceptance_criteria'] == [QUOTE]
    assert all(ref in new_req['evidence'] for ref in old['evidence'])
    assert ok(client.get(f'/entities/{req["id"]}', params={'version': 1})) == old
    new_ref = next(item for item in update['result']['entity_mapping'] if item['key'] == 'REQ-ENROLL')
    assert new_ref['entity_id'] == req['id']
    with connect(db_url) as conn:
        bindings = conn.execute('SELECT * FROM source_relationship_binding WHERE source_id=%s',
                                (snapshot['source_id'],)).fetchall()
        assert len(bindings) == 1
    old_evidence = ok(client.get(f'/operations/projects/{project["id"]}/source-evidence', params={
        'table': 'entity', 'id': req['id'], 'version': 1}))['items']
    assert old_evidence == evidence


@pytest.mark.integration
def test_failed_quotes_are_auditable_and_can_retry_without_limit(client, project, db_url, monkeypatch):
    document = candidate()
    document['entities'][0]['evidence'] = [{'quote': '允许超额选课'}]
    job = complete(client, project, db_url, monkeypatch, document)
    assert job['status'] == 'FAILED'
    assert '未出现在' in job['result']['diagnostic']
    assert job['result']['model_exchange']['response']['content'] == '测试原始答复'
    assert propose(client, job, candidate()).status_code == 409
    for _ in range(6):
        retried = complete(client, project, db_url, monkeypatch)
        assert retried['status'] == 'SUCCEEDED'
    assert ok(client.get('/entities', params={'project_id': project['id'], 'kind': 'Requirement'})) == []


@pytest.mark.integration
def test_review_rejects_fabricated_binding_relation_or_quote(client, project, db_url, monkeypatch, entity):
    job = complete(client, project, db_url, monkeypatch)
    body = deepcopy(job['result']['candidate'])
    body['entities'][0]['existing'] = {'id': str(uuid4()), 'version': 1}
    assert propose(client, job, body).status_code == 422
    body = deepcopy(job['result']['candidate'])
    body['relationships'][0]['source_key'] = 'missing'
    assert propose(client, job, body).status_code == 422
    body = deepcopy(job['result']['candidate'])
    body['entities'][0]['evidence'] = [{'quote': '原文没有此句'}]
    assert propose(client, job, body).status_code == 422
    body = deepcopy(job['result']['candidate'])
    body['entities'][0]['key'] = 'NEW-KEY'
    assert propose(client, job, body).status_code == 422
    # 人工修改摘要并选择部分候选。未选中的关系不会自动保留。
    body = deepcopy(job['result']['candidate'])
    body['entities'] = body['entities'][:1]
    body['relationships'] = []
    body['entities'][0]['description'] = '经人工复核的选课规则'
    proposal = ok(propose(client, job, body), 201)
    assert len(proposal['plan']['entity_mapping']) == 1


@pytest.mark.integration
def test_stale_source_and_entity_block_commit_and_rejected_draft_can_be_revised(client, project, db_url, monkeypatch):
    job = complete(client, project, db_url, monkeypatch)
    proposal = ok(propose(client, job), 201)
    rejected = ok(client.post(f'/operations/proposals/{proposal["id"]}/reject', json={
        'expected_version': 1, 'actor': ACTOR, 'reason': '先修订描述'}))['proposal']
    assert rejected['status'] == 'rejected'
    replacement = ok(propose(client, job), 201)
    assert replacement['id'] != proposal['id']
    register(client, project, TEXT+'新增文档修订。', revision='v2')
    checked = ok(client.post(f'/operations/proposals/{replacement["id"]}/validate', json={
        'expected_version': 1, 'actor': ACTOR, 'reason': '检查过期来源'}))['proposal']
    assert checked['result']['valid'] is False and '原文已更新' in checked['result']['errors'][0]['message']
    assert ok(client.get('/entities', params={'project_id': project['id'], 'kind': 'Requirement'})) == []


@pytest.mark.integration
def test_queue_single_claim_and_recover_expired_without_replay(client, project, db_url, monkeypatch):
    source = register(client, project)
    path = f'/extractions/projects/{project["id"]}/jobs'
    body = {'source_id': source['source_id'], 'source_version': 1, 'actor': ACTOR}
    job = ok(client.post(path, json=body), 201)
    assert client.post(path, json=body).status_code == 409
    service = ExtractionService(db_url)
    exchange = {'request': {'messages': [{'role': 'system', 'content': '中断前已发送'}]}, 'response': None}
    with connect(db_url) as conn:
        service._event(conn, UUID(job['id']), 2, 'RUNNING', {'deadline_at': '2000-01-01T00:00:00+00:00',
                                                         'model_exchange': exchange})
    service.recover_expired()
    recovered = ok(client.get('/extractions/jobs/'+job['id']))
    assert recovered['status'] == 'FAILED'
    assert recovered['result']['model_exchange'] == exchange
    assert service.run_next() is False


@pytest.mark.integration
def test_model_request_audit_is_durable_before_model_returns(client, project, db_url, monkeypatch):
    source = register(client, project)
    job = ok(client.post(f'/extractions/projects/{project["id"]}/jobs', json={
        'source_id': source['source_id'], 'source_version': 1, 'actor': ACTOR}), 201)
    request_audit = {'request': {'messages': [{'role': 'system', 'content': '已持久化的请求'}]}, 'response': None}

    def interrupt_after_request(context, schema, **kwargs):
        kwargs['on_exchange'](request_audit)
        during = ExtractionService(db_url).get_job(UUID(job['id']))
        assert during['status'] == 'RUNNING'
        assert during['result']['model_exchange'] == request_audit
        assert 'deadline_at' in during['result']
        raise KnowledgeError('测试连接中断', 502)

    monkeypatch.setattr(model_gateway, 'complete_extraction', interrupt_after_request)
    ExtractionService(db_url).run_job(UUID(job['id']))
    failed = ok(client.get('/extractions/jobs/'+job['id']))
    assert failed['status'] == 'FAILED'
    assert failed['result']['model_exchange'] == request_audit
