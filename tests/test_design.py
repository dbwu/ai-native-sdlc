"""仅需求起步、模型澄清、设计候选与显式审核的真实数据库回归。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import json
from threading import Lock
from uuid import UUID, uuid4

import psycopg
import pytest

from knowledge import model_gateway
from knowledge.db import connect
from knowledge.design import DesignService, sha256
from knowledge.semantics import KnowledgeError
from test_generic_workflow import approve

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def model_environment(monkeypatch):
    monkeypatch.setenv('KNOWLEDGE_DESIGN_WORKER', 'disabled')
    monkeypatch.setenv('KNOWLEDGE_MODEL_PROVIDER', 'openai_compatible')
    monkeypatch.setenv('KNOWLEDGE_MODEL_BASE_URL', 'http://127.0.0.1:9999/v1')
    monkeypatch.setenv('KNOWLEDGE_MODEL_MODEL', 'integration-test-model')
    monkeypatch.setenv('KNOWLEDGE_MODEL_REQUIRE_KEY', 'false')
    monkeypatch.delenv('KNOWLEDGE_MODEL_API_KEY', raising=False)


@pytest.fixture
def requirement(entity):
    return entity(name='学生选课数量上限', attributes={'acceptance_criteria': ['第三个有效教学班被拒绝，重复请求不增加计数。']})


def document(context):
    ids = [item['id'] for item in context['requirements']]
    return {'summary': '依据用户需求形成数量限制设计，等待审核，不代表已实现。', 'questions': [],
            'assumptions': ['本轮不增加退课能力。'],
            'components': [{'key': 'Enrollment', 'name': '选课计数服务', 'description': '候选设计',
                'responsibility': '在事务中按已确认规则检查有效选课数量。', 'requirement_ids': ids}],
            'interfaces': [{'key': 'EnrollApi', 'name': '选课接口设计', 'description': '待实现接口',
                'protocol': 'http', 'component_key': 'Enrollment', 'contract': {'method': 'POST', 'path': '/enroll'}, 'requirement_ids': ids}],
            'data_models': [{'key': 'EnrollmentData', 'name': '选课记录结构', 'description': '待实现模型',
                'component_key': 'Enrollment', 'schema_definition': {'fields': ['student', 'offering']}, 'requirement_ids': ids}],
            'test_plan': [{'key': 'LimitTest', 'name': '数量限制验收计划', 'description': '尚未执行',
                'steps': ['准备三个互不冲突且有余量的教学班', '第三次选课应被拒绝'], 'expected_result': '只保留两条记录', 'requirement_ids': ids}]}


def returned(doc):
    return {'document': doc, 'trace': {'provider': 'test', 'model': '测试替身，非真实大模型', 'endpoint': 'urn:test:model',
        'response_format': 'json_object', 'request_id': 'test-request', 'usage': {}, 'prompt_version': 'test-v1',
        'input_sha256': 'a'*64, 'output_sha256': sha256(doc)}}


@pytest.fixture
def fake_model(monkeypatch):
    calls = []
    def complete(context, schema, **kwargs):
        calls.append(deepcopy(context))
        assert 'components' in schema['properties']
        return returned(document(context))
    monkeypatch.setattr(model_gateway, 'complete_design', complete)
    return calls


def detail(client, fid):
    response = client.get('/design/flows/'+str(fid))
    assert response.status_code == 200, response.text
    return response.json()


def create(client, project, requirement):
    response = client.post(f"/design/projects/{project['id']}/flows", json={
        'title': '从新需求生成设计', 'goal': '验证自己创建的数量限制需求', 'actor': '设计复核者',
        'requirements': [{'id': requirement['id'], 'version': requirement['version']}]})
    assert response.status_code == 201, response.text
    return detail(client, response.json()['id'])


def action(client, current, name, expected=200):
    response = client.post(f"/design/flows/{current['flow']['id']}/actions/{name}", json={
        'expected_version': current['flow']['version'], 'proposal_version': current['proposal']['version'] if current['proposal'] else None,
        'actor': '设计复核者', 'reason': '已核对本次输入与产物范围，明确执行此操作。'})
    assert response.status_code == expected, response.text
    return response.json()


def generate(client, db_url, current):
    queued = action(client, current, 'generate', 202)
    DesignService(db_url).run_job(UUID(queued['flow']['state']['active_job_id']))
    return detail(client, queued['flow']['id'])


def test_requirement_only_design_is_not_published_until_explicit_review(client, db_url, project, requirement, fake_model):
    current = create(client, project, requirement)
    assert current['flow']['status'] == 'DRAFT'
    assert len(current['requirements']) == 1 and current['proposal'] is None
    action(client, current, 'generate', 409)
    current = generate(client, db_url, action(client, current, 'start'))
    assert current['flow']['status'] == 'REVIEW_PENDING'
    assert current['proposal']['status'] == 'pending' and len(fake_model) == 1
    assert 'project_baseline' not in fake_model[0]
    assert current['jobs'][0]['trace']['context_sha256'] == sha256(fake_model[0])
    url = f"/entities?project_id={project['id']}"
    assert len(client.get(url).json()) == 2  # 只有项目和原需求。
    action(client, current, 'commit', 409)
    action(client, current, 'complete', 409)
    current = action(client, current, 'validate')
    assert current['proposal']['status'] == 'validated'
    assert len(client.get(url).json()) == 2  # 试运行回滚。
    current = action(client, current, 'commit')
    assert current['flow']['status'] == 'DESIGN_COMMITTED'
    current = action(client, current, 'complete')
    assert current['flow']['status'] == 'COMPLETED'
    assert len(current['flow']['state']['completed_stages']) == 2
    records = client.get(url).json()
    assert {'ArchitectureComponent', 'Interface', 'DataModel', 'TestCase', 'Evidence'} <= {item['kind'] for item in records}
    assert not {'CodeArtifact'} & {item['kind'] for item in records}
    assert all(item['provenance']['nature'] == 'inference' for item in records if item['kind'] not in {'Project', 'Requirement'})
    relations = client.get(f"/relationships?project_id={project['id']}").json()
    assert relations and all(item['status'] == 'proposed' for item in relations)
    assert all(item['attributes']['category'] == 'analysis' for item in records if item['kind'] == 'Evidence')


def test_new_requirement_design_reads_only_the_project_baseline_pinned_at_creation(
        client, db_url, project, requirement, fake_model):
    path = f'/workbench/projects/{project["id"]}/project-model'
    model = {'title': '基础选课', 'nodes': [
        {'key': 'Course', 'name': '选课领域', 'kind': 'domain'},
        {'key': 'Enrollment', 'name': '选课办理', 'kind': 'module', 'parent_key': 'Course'},
    ]}

    def publish(expected_version):
        response = client.post(path, json={'expected_version': expected_version, 'actor': '基线审阅者', 'model': model})
        assert response.status_code == 201, response.text
        approve(client, response.json())

    publish(0)
    first = create(client, project, requirement)
    assert first['flow']['definition']['project_model_version'] == 1
    model['nodes'].append({'key': 'Limit', 'name': '数量限制', 'kind': 'module', 'parent_key': 'Enrollment'})
    publish(1)
    second = create(client, project, requirement)
    assert second['flow']['definition']['project_model_version'] == 2

    first = generate(client, db_url, action(client, first, 'start'))
    assert first['flow']['status'] == 'REVIEW_PENDING'
    reference = fake_model[0]['project_baseline']
    assert reference['version'] == 1
    assert [node['key'] for node in reference['model']['nodes']] == ['Course', 'Enrollment']
    assert first['jobs'][0]['trace']['context_sha256'] == sha256(fake_model[0])
    assert client.get(path).json()['baseline']['version'] == 2


@pytest.mark.parametrize('outcome', ['success', 'truncated', 'timeout'])
def test_generation_records_platform_input_and_model_reply_separately(
        client, db_url, project, requirement, monkeypatch, outcome):
    requests = []
    received = []
    secret = 'synthetic-exchange-token'
    monkeypatch.setenv('KNOWLEDGE_MODEL_API_KEY', secret)
    monkeypatch.setattr(model_gateway, 'sleep', lambda _: None)

    def transport(request, timeout):
        payload = json.loads(request.data)
        requests.append(payload)
        assert request.get_header('Authorization') == 'Bearer ' + secret
        if outcome == 'timeout':
            raise TimeoutError()
        context = json.loads(payload['messages'][1]['content'])['context']
        content = (json.dumps(document(context), ensure_ascii=False, indent=2) if outcome == 'success'
                   else '{"summary":"尚未输出完毕 ' + secret)
        received.append(content)
        body = {'choices': [{'message': {'role': 'assistant', 'content': content},
                             'finish_reason': 'stop' if outcome == 'success' else 'length'}]}
        reply = BytesIO(json.dumps(body, ensure_ascii=False).encode())
        reply.headers = {'x-request-id': 'exchange-test'}
        return reply

    monkeypatch.setattr(model_gateway, '_open_request', transport)
    current = action(client, create(client, project, requirement), 'start')
    queued = action(client, current, 'generate_confirmed', 202)
    flow_id = current['flow']['id']
    job_id = queued['jobs'][0]['id']
    endpoint = f'/design/flows/{flow_id}/generations/{job_id}/model-exchange'
    assert queued['jobs'][0]['has_model_exchange'] is False
    assert client.get(endpoint).status_code == 404
    DesignService(db_url).run_job(UUID(job_id))

    current = detail(client, flow_id)
    job = current['jobs'][0]
    assert job['has_model_exchange'] is True and 'model_exchange' not in job['result']
    assert job['status'] == ('SUCCEEDED' if outcome == 'success' else 'FAILED')
    assert client.get(f'/design/flows/{uuid4()}/generations/{job_id}/model-exchange').status_code == 404
    response = client.get(endpoint)
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    exchange = response.json()
    assert exchange['request'] == requests[-1]
    assert exchange['request']['messages'][0]['role'] == 'system'
    assert exchange['request']['messages'][1]['role'] == 'user'
    assert secret not in response.text and 'Authorization' not in exchange['request']
    if outcome == 'timeout':
        assert len(requests) == 2 and requests[0] == requests[1]
        assert exchange['response'] is None and current['proposal'] is None
    elif outcome == 'truncated':
        assert exchange['response']['finish_reason'] == 'length' and current['proposal'] is None
        assert exchange['response']['content'].startswith('{"summary":"尚未输出完毕 ')
    else:
        assert exchange['response']['finish_reason'] == 'stop' and exchange['response']['content'] == received[0]
        assert current['proposal']['status'] == 'pending'


@pytest.mark.parametrize('legacy_history', [False, True])
def test_clarification_requires_all_answers_and_explicit_regeneration(client, db_url, project, requirement, monkeypatch, legacy_history):
    if legacy_history:
        record_job = DesignService._job_event

        def legacy_record(conn, job, status, result):
            return record_job(conn, job, status, {key: value for key, value in result.items() if key != 'questions'})

        monkeypatch.setattr(DesignService, '_job_event', staticmethod(legacy_record))
    calls = []
    def model(context, schema, **kwargs):
        calls.append(context)
        if not context['clarifications']:
            return returned({'summary': '计数口径尚不明确', 'questions': [{'key': 'CountUnit',
                'question': '数量上限按课程还是教学班计数？', 'requirement_ids': [requirement['id']]}]})
        return returned(document(context))
    monkeypatch.setattr(model_gateway, 'complete_design', model)
    current = generate(client, db_url, action(client, create(client, project, requirement), 'start'))
    assert current['flow']['status'] == 'NEEDS_CLARIFICATION' and current['proposal'] is None
    original_job = current['jobs'][0]['id']
    assert current['jobs'][0]['questions'] == current['questions'] and current['jobs'][0]['answers'] == []
    body = {'expected_version': current['flow']['version'], 'actor': '需求负责人', 'reason': '明确确认计数规则',
            'answers': [{'key': 'Unknown', 'answer': '按有效教学班计数'}]}
    endpoint = f"/design/flows/{current['flow']['id']}/answers"
    assert client.post(endpoint, json=body).status_code == 422
    body['answers'][0]['key'] = 'CountUnit'
    response = client.post(endpoint, json=body)
    assert response.status_code == 200, response.text
    current = response.json()
    assert current['flow']['status'] == 'READY' and len(calls) == 1
    current = generate(client, db_url, current)
    assert current['flow']['status'] == 'REVIEW_PENDING'
    assert calls[-1]['clarifications'][0]['answer'] == '按有效教学班计数'
    assert len(current['jobs']) == 2
    first = next(job for job in current['jobs'] if job['id'] == original_job)
    assert first['questions'][0]['question'] == '数量上限按课程还是教学班计数？'
    assert first['answers'][0]['answer'] == '按有效教学班计数'
    assert current['jobs'][0]['answers'] == []


def test_confirmed_scope_keeps_existing_answers_and_moves_unanswered_side_issue_to_deferred(
        client, db_url, project, requirement, monkeypatch):
    calls = []

    def model(context, schema, *, confirmed_scope=False, **kwargs):
        calls.append((context, confirmed_scope))
        if confirmed_scope:
            result = document(context)
            result['deferred_items'] = ['选课成功通知在后续流程设计。']
            return returned(result)
        key, question = ('Notification', '是否发送选课通知？') if context['clarifications'] else ('CountUnit', '如何计数？')
        return returned({'summary': '等待明确规则', 'questions': [
            {'key': key, 'question': question, 'requirement_ids': [requirement['id']]}]})

    monkeypatch.setattr(model_gateway, 'complete_design', model)
    current = generate(client, db_url, action(client, create(client, project, requirement), 'start'))
    flow_id = current['flow']['id']
    response = client.post(f'/design/flows/{flow_id}/answers', json={
        'expected_version': current['flow']['version'], 'actor': '需求负责人', 'reason': '确认业务规则',
        'answers': [{'key': 'CountUnit', 'answer': '按有效教学班计数'}],
    })
    assert response.status_code == 200, response.text
    current = generate(client, db_url, response.json())
    action(client, current, 'generate', 409)
    queued = action(client, current, 'generate_confirmed', 202)
    action(client, queued, 'generate_confirmed', 409)
    DesignService(db_url).run_job(UUID(queued['flow']['state']['active_job_id']))
    current = detail(client, flow_id)
    assert current['flow']['status'] == 'REVIEW_PENDING' and current['proposal']['status'] == 'pending'
    assert current['flow']['id'] == flow_id and current['design']['deferred_items'] == ['选课成功通知在后续流程设计。']
    context, confirmed_scope = calls[-1]
    assert confirmed_scope and context['generation_guidance']
    assert context['pending_questions'][0]['key'] == 'Notification'
    assert context['clarifications'][0]['answer'] == '按有效教学班计数'
    new, pending, answered = current['jobs']
    assert new['generation_mode'] == 'confirmed_scope' and new['questions'] == []
    assert pending['questions'][0]['question'] == '是否发送选课通知？' and pending['answers'] == []
    assert answered['questions'][0]['question'] == '如何计数？' and answered['answers'][0]['answer'] == '按有效教学班计数'


@pytest.mark.parametrize('invalid', [None, 'unbound_criterion', 'repeated_question'])
def test_confirmed_scope_validates_blockers_and_preserves_previous_unanswered_questions(
        client, db_url, project, requirement, monkeypatch, invalid):
    original_question = '此前确认过的计数单位是什么？'

    def model(context, schema, *, confirmed_scope=False, **kwargs):
        if confirmed_scope:
            question = {'key': 'Third', 'question': '第三次请求同时是重复请求时，是否仍允许幂等返回？',
                        'requirement_ids': [requirement['id']],
                        'acceptance_criterion': requirement['attributes']['acceptance_criteria'][0],
                        'acceptance_impact': '决定达到上限后的重复请求是返回旧记录，还是返回容量错误。',
                        'options': ['返回已有记录', '返回数量上限错误']}
            if invalid == 'unbound_criterion':
                question['acceptance_criterion'] = '本轮未绑定的验收条件。'
            if invalid == 'repeated_question':
                question['question'] = original_question.replace('？', '?')
        else:
            question = {'key': 'First' if not context['clarifications'] else 'Second',
                        'question': original_question if not context['clarifications'] else '需要通知吗？',
                        'requirement_ids': [requirement['id']]}
        return returned({'summary': '澄清', 'questions': [question]})

    def answer(current, key):
        response = client.post(f"/design/flows/{current['flow']['id']}/answers", json={
            'expected_version': current['flow']['version'], 'actor': '需求负责人', 'reason': '确认问题',
            'answers': [{'key': key, 'answer': '确认当前规则'}],
        })
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(model_gateway, 'complete_design', model)
    current = generate(client, db_url, action(client, create(client, project, requirement), 'start'))
    current = generate(client, db_url, answer(current, 'First'))
    previous_job = current['jobs'][0]['id']
    queued = action(client, current, 'generate_confirmed', 202)
    DesignService(db_url).run_job(UUID(queued['flow']['state']['active_job_id']))
    current = detail(client, current['flow']['id'])
    assert current['flow']['status'] == 'NEEDS_CLARIFICATION' and current['proposal'] is None
    if invalid:
        assert current['jobs'][0]['status'] == 'FAILED' and current['questions'][0]['key'] == 'Second'
        assert current['flow']['state']['last_error']
        current = answer(current, 'Second')
        assert current['jobs'][0]['answers'] == []
        assert current['jobs'][1]['id'] == previous_job and current['jobs'][1]['answers'][0]['key'] == 'Second'
    else:
        assert current['jobs'][0]['questions'][0]['acceptance_impact'] == current['questions'][0]['acceptance_impact']
        current = answer(current, 'Third')
        assert current['jobs'][0]['answers'][0]['key'] == 'Third' and current['jobs'][1]['answers'] == []


@pytest.mark.parametrize('invalid', ['unknown_requirement', 'missing_component', 'missing_test', 'foreign_component',
                                    'extra_code_field', 'empty_contract', 'empty_data_model', 'foreign_diagram'])
def test_invalid_model_output_never_becomes_a_proposal(client, db_url, project, requirement, monkeypatch, invalid):
    def model(context, schema, **kwargs):
        doc = document(context)
        if invalid == 'unknown_requirement': doc['components'][0]['requirement_ids'] = [str(uuid4())]
        elif invalid == 'missing_component': doc['components'] = []
        elif invalid == 'missing_test': doc['test_plan'] = []
        elif invalid == 'foreign_component': doc['interfaces'][0]['component_key'] = 'Other'
        elif invalid == 'empty_contract': doc['interfaces'][0]['contract'] = {}
        elif invalid == 'empty_data_model': doc['data_models'][0]['schema_definition'] = {}
        elif invalid == 'foreign_diagram':
            doc['use_case_diagrams'] = [{'title': '未选需求', 'requirement_ids': [str(uuid4())],
                'actors': [{'key': 'A', 'name': '学生'}], 'use_cases': [{'key': 'B', 'name': '选课'}],
                'associations': [{'actor': 'A', 'use_case': 'B'}]}]
        else: doc['source_code'] = 'print("已通过")'
        return returned(doc)
    monkeypatch.setattr(model_gateway, 'complete_design', model)
    current = generate(client, db_url, action(client, create(client, project, requirement), 'start'))
    assert current['flow']['status'] == 'BLOCKED'
    assert current['proposal'] is None and current['jobs'][0]['status'] == 'FAILED'
    assert len(client.get(f"/entities?project_id={project['id']}").json()) == 2


def revise_requirement(client, requirement):
    record = client.get('/entities/'+requirement['id']).json()
    response = client.put('/entities/'+requirement['id'], json={
        **{key: record[key] for key in ('name', 'description', 'status', 'attributes', 'evidence', 'provenance')},
        'expected_version': record['version'], 'description': '模型调用期间需求已有新修订'})
    assert response.status_code == 200, response.text


def test_model_call_releases_database_lock_and_stale_result_is_discarded(client, db_url, project, requirement, monkeypatch):
    def model(context, schema, **kwargs):
        # 模拟慢模型调用期间另一请求编辑需求；若主事务仍持有项目锁会阻塞。
        with connect(db_url) as conn:
            conn.execute("SET LOCAL lock_timeout='1s'")
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (project['id'],))
        revise_requirement(client, requirement)
        return returned(document(context))
    monkeypatch.setattr(model_gateway, 'complete_design', model)
    current = generate(client, db_url, action(client, create(client, project, requirement), 'start'))
    assert current['flow']['status'] == 'BLOCKED' and current['jobs'][0]['status'] == 'STALE'
    assert current['proposal'] is None


def test_stale_queued_requirement_or_config_never_calls_model(client, db_url, project, requirement, fake_model, monkeypatch):
    current = action(client, create(client, project, requirement), 'start')
    queued = action(client, current, 'generate', 202)
    monkeypatch.setenv('KNOWLEDGE_MODEL_MODEL', 'another-model')
    DesignService(db_url).run_job(UUID(queued['flow']['state']['active_job_id']))
    current = detail(client, current['flow']['id'])
    assert current['jobs'][0]['status'] == 'STALE' and not fake_model


def test_cancel_during_model_call_discards_result(client, db_url, project, requirement, monkeypatch):
    def model(context, schema, **kwargs):
        current = detail(client, fid)
        action(client, current, 'cancel')
        return returned(document(context))
    monkeypatch.setattr(model_gateway, 'complete_design', model)
    current = action(client, create(client, project, requirement), 'start')
    fid = current['flow']['id']
    current = generate(client, db_url, current)
    assert current['flow']['status'] == 'CANCELLED'
    assert current['proposal'] is None and current['jobs'][0]['status'] == 'CANCELLED'


def test_cancel_during_retry_wait_prevents_second_model_call(client, db_url, project, requirement, monkeypatch):
    calls = []
    current = action(client, create(client, project, requirement), 'start')
    flow_id = current['flow']['id']

    def timeout(request, seconds):
        calls.append(seconds)
        raise TimeoutError()

    monkeypatch.setattr(model_gateway, '_open_request', timeout)
    monkeypatch.setattr(model_gateway, 'sleep', lambda _: action(client, detail(client, flow_id), 'cancel'))
    current = generate(client, db_url, current)
    assert current['flow']['status'] == 'CANCELLED' and current['jobs'][0]['status'] == 'CANCELLED'
    assert current['proposal'] is None and len(calls) == 1


def test_concurrent_workers_claim_generation_only_once(client, db_url, project, requirement, monkeypatch):
    counter, lock = [], Lock()
    def model(context, schema, **kwargs):
        with lock: counter.append(1)
        return returned(document(context))
    monkeypatch.setattr(model_gateway, 'complete_design', model)
    current = action(client, create(client, project, requirement), 'start')
    queued = action(client, current, 'generate', 202)
    job_id = UUID(queued['flow']['state']['active_job_id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: DesignService(db_url).run_job(job_id), range(2)))
    assert len(counter) == 1
    assert detail(client, current['flow']['id'])['proposal']['status'] == 'pending'


def test_model_disabled_and_old_page_actions_are_rejected(client, project, requirement, monkeypatch):
    current = create(client, project, requirement)
    old = current
    current = action(client, current, 'start')
    action(client, old, 'start', 409)
    monkeypatch.setenv('KNOWLEDGE_MODEL_PROVIDER', 'disabled')
    assert client.get('/design/config').json()['configured'] is False
    action(client, current, 'generate', 503)
    assert detail(client, current['flow']['id'])['jobs'] == []


def test_review_reject_regeneration_continues_past_five_with_history(client, db_url, project, requirement, fake_model):
    current = generate(client, db_url, action(client, create(client, project, requirement), 'start'))
    flow_id = current['flow']['id']
    first = current['proposal']
    first_ids = {item['entity_id'] for item in first['plan']['entity_mapping']}
    for _ in range(5):
        previous_id = current['proposal']['id']
        current = action(client, current, 'reject')
        assert current['flow']['status'] == 'READY'
        current = generate(client, db_url, current)
        assert current['proposal']['id'] != previous_id
        assert {item['entity_id'] for item in current['proposal']['plan']['entity_mapping']} == first_ids
    assert current['flow']['id'] == flow_id
    assert current['flow']['state']['generation_count'] == len(current['jobs']) == len(fake_model) == 6
    assert client.get('/operations/proposals/'+first['id']).json()['status'] == 'rejected'
    current = action(client, action(client, current, 'validate'), 'commit')
    assert current['proposal']['status'] == 'committed'


def test_design_proposal_baseline_cannot_be_bypassed_via_project_review(client, db_url, project, requirement, fake_model):
    current = generate(client, db_url, action(client, create(client, project, requirement), 'start'))
    current = action(client, current, 'validate')
    revise_requirement(client, requirement)
    p = current['proposal']
    response = client.post('/operations/proposals/'+p['id']+'/commit', json={
        'expected_version': p['version'], 'actor': '另一个页面', 'reason': '旧提案应被基线校验拒绝'})
    assert response.status_code == 409
    action(client, current, 'commit', 409)


def test_design_records_are_append_only_and_wrong_project_inputs_rejected(client, db_url, project, requirement):
    current = create(client, project, requirement)
    with pytest.raises(psycopg.errors.RaiseException):
        with connect(db_url) as conn:
            conn.execute("UPDATE design_flow_event SET status='COMPLETED' WHERE flow_id=%s", (current['flow']['id'],))
    other = client.post('/entities', json={'kind': 'Project', 'name': '其他项目', 'provenance': requirement['provenance']}).json()
    response = client.post(f"/design/projects/{other['id']}/flows", json={
        'title': '跨项目输入', 'goal': '必须拒绝', 'actor': '测试者',
        'requirements': [{'id': requirement['id'], 'version': requirement['version']}]})
    assert response.status_code == 422


def test_queued_stale_requirement_is_rejected_before_model_call(client, db_url, project, requirement, fake_model):
    current = action(client, create(client, project, requirement), 'start')
    queued = action(client, current, 'generate', 202)
    revise_requirement(client, requirement)
    DesignService(db_url).run_job(UUID(queued['flow']['state']['active_job_id']))
    current = detail(client, current['flow']['id'])
    assert current['jobs'][0]['status'] == 'STALE' and not fake_model


def test_secret_rotation_between_enqueue_and_call_requires_explicit_retry(client, db_url, project, requirement, monkeypatch):
    current = action(client, create(client, project, requirement), 'start')
    queued = action(client, current, 'generate', 202)
    monkeypatch.setenv('KNOWLEDGE_MODEL_API_KEY', 'test-only-rotated-secret')
    def forbidden(*args, **kwargs):
        pytest.fail('配置漂移后不应访问模型网络')
    monkeypatch.setattr(model_gateway, '_open_request', forbidden)
    DesignService(db_url).run_job(UUID(queued['flow']['state']['active_job_id']))
    current = detail(client, current['flow']['id'])
    assert current['jobs'][0]['status'] == 'STALE'
    assert 'test-only-rotated-secret' not in json.dumps(current)


@pytest.mark.parametrize('recorded_deadline', [False, True])
def test_interrupted_generation_expires_without_automatically_recalling_model(client, db_url, project, requirement, fake_model, recorded_deadline):
    current = action(client, create(client, project, requirement), 'start')
    queued = action(client, current, 'generate', 202)
    job_id = UUID(queued['flow']['state']['active_job_id'])
    service = DesignService(db_url)
    with connect(db_url) as conn:
        conn.execute("""INSERT INTO design_generation_event(job_id,version,status,document,recorded_at)
            VALUES (%s,2,'RUNNING',CASE WHEN %s THEN jsonb_build_object('deadline_at',clock_timestamp()+interval '1 minute')
            ELSE '{}'::jsonb END,clock_timestamp()-interval '11 minutes')""", (job_id, recorded_deadline))
    if recorded_deadline:
        service.recover_expired()
        assert detail(client, queued['flow']['id'])['jobs'][0]['status'] == 'RUNNING'
        with connect(db_url) as conn:
            conn.execute("""INSERT INTO design_generation_event(job_id,version,status,document)
                VALUES (%s,3,'RUNNING',jsonb_build_object('deadline_at',clock_timestamp()-interval '1 second'))""", (job_id,))
    service.recover_expired()
    current = detail(client, current['flow']['id'])
    assert current['flow']['status'] == 'BLOCKED' and current['jobs'][0]['status'] == 'FAILED'
    service.run_job(job_id)
    assert not fake_model
    current = generate(client, db_url, current)
    assert len(current['jobs']) == 2 and len(fake_model) == 1


def test_every_selected_requirement_needs_component_and_test_plan_coverage(client, db_url, project, requirement, entity, monkeypatch):
    second = entity(name='其他验收目标', attributes={'acceptance_criteria': ['应有独立验收计划']})
    response = client.post(f"/design/projects/{project['id']}/flows", json={
        'title': '两条需求', 'goal': '每条都必须被设计覆盖', 'actor': '测试者',
        'requirements': [{'id': r['id'], 'version': r['version']} for r in (requirement, second)]})
    current = detail(client, response.json()['id'])
    def incomplete(context, schema, **kwargs):
        doc = document(context)
        doc['test_plan'][0]['requirement_ids'] = [requirement['id']]
        return returned(doc)
    monkeypatch.setattr(model_gateway, 'complete_design', incomplete)
    current = generate(client, db_url, action(client, current, 'start'))
    assert current['flow']['status'] == 'BLOCKED' and current['proposal'] is None
    assert '全部需求' in current['jobs'][0]['diagnostic']
