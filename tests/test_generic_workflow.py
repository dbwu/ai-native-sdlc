"""绑定已有知识、三轮选课和人工产物的真实数据库回归。"""
import json
from importlib.resources import files
from uuid import uuid4

import pytest

from knowledge.course_executor import build_course, run_course
from knowledge.semantics import KnowledgeError


@pytest.mark.parametrize('release,count',[('v1',5),('v1.1',8),('v1.2',13)])
def test_course_executes_independent_regression(release,count):
    build=build_course(release)
    report=run_course(release,build['files']['course_app.py'])
    assert report['passed'], report['stderr']
    assert report['tests_run']==count
    assert report['sha256']==build['sha256']


def approve(client,proposal):
    url='/operations/proposals/'+proposal['id']
    body={'expected_version':proposal['version'],'actor':'测试审阅者','reason':'明确审阅当前提案'}
    checked=client.post(url+'/validate',json=body)
    assert checked.status_code==200,checked.text
    p=checked.json()['proposal']
    assert p['result']['valid'], p['result']
    body['expected_version']=p['version']
    committed=client.post(url+'/commit',json=body)
    assert committed.status_code==200,committed.text
    return committed.json()['proposal']


def import_version(client,project,release):
    response=client.post(f"/workbench/projects/{project['id']}/course-import",json={'release':release,'actor':'测试审阅者'})
    assert response.status_code==201,response.text
    return approve(client,response.json()['proposal'])


def creation(client,project,release,executor='course_selection_v1'):
    batch=client.get(f"/workbench/projects/{project['id']}/knowledge").json()
    kinds={'Requirement':'requirement','ArchitectureComponent':'architecture','Interface':'interface','DataModel':'data','CodeArtifact':'code','TestCase':'test'}
    bindings={key:[] for key in kinds.values()}
    inputs=[]
    for e in batch['entities']:
        key=batch['keys'].get(e['id'])
        if e['kind']=='BusinessRule': inputs.append({'id':e['id'],'version':e['version']})
        if e['kind'] in kinds and (e['kind']!='TestCase' or key=='TEST-ENROLL'):
            bindings[kinds[e['kind']]].append({'id':e['id'],'version':e['version']})
    return {'project_id':project['id'],'title':'选课流程 '+release,'goal':'实际验证当前选课需求','scenario':'generic_v1',
            'executor':executor,'release':release,'bindings':bindings,'input_knowledge':inputs,'actor':'测试审阅者'}


def snap(client,fid):
    response=client.get('/workbench/workflows/'+fid+'/snapshot')
    assert response.status_code==200,response.text
    return response.json()


def act(client,current,name,expected=200):
    body={'expected_version':current['workflow']['version'],'task_version':current['task']['version'] if current['task'] else None,
          'proposal_version':current['proposal']['version'] if current['proposal'] else None,'actor':'测试审阅者','reason':'逐项核对后明确操作'}
    response=client.post(f"/workbench/workflows/{current['workflow']['id']}/actions/{name}",json=body)
    assert response.status_code==expected,response.text
    return response.json()['snapshot'] if expected==200 else response.json()


def finish(client,current):
    current=act(client,current,'validate_proposal');current=act(client,current,'commit_proposal');current=act(client,current,'start')
    for stage in range(4):
        current=act(client,current,'run_reference_stage')
        assert current['proposal']['status']=='pending'
        assert current['workflow']['state']['stage']==stage
        current=act(client,current,'validate_task');current=act(client,current,'commit_proposal');current=act(client,current,'complete_task')
        current=act(client,current,'prepare_gate')
        assert current['workflow']['status']=='AWAITING_GATE',current['workflow']['state']
        current=act(client,current,'validate_proposal');current=act(client,current,'commit_proposal');current=act(client,current,'advance')
    assert current['workflow']['status']=='COMPLETED'
    return current


@pytest.mark.integration
def test_three_course_rounds_reuse_knowledge_and_archive_versions(client,project,tmp_path,monkeypatch):
    monkeypatch.setenv('KNOWLEDGE_DEMO_DATA_DIR',str(tmp_path))
    identity=None; old=None
    for release,expected in [('v1',5),('v1.1',8),('v1.2',13)]:
        import_version(client,project,release)
        body=creation(client,project,release)
        ids={ref['id'] for refs in body['bindings'].values() for ref in refs}
        if identity: assert identity <= ids
        identity=ids
        response=client.post('/workflows',json=body)
        assert response.status_code==201,response.text
        current=finish(client,snap(client,response.json()['id']))
        assert current['executions'][0]['document']['tests_run']==expected
        targets={current['workflow']['state']['bindings'][key] for key in ('code','test')}
        assert all(r['status']=='verified' for r in current['relationships'] if r['type'] in ('IMPLEMENTED_BY','VERIFIED_BY') and str(r['target_id']) in targets)
        if old:
            archived=snap(client,old['workflow']['id'])
            assert archived['artifacts']==old['artifacts']
        old=current
        preview=client.get(f"/workbench/workflows/{current['workflow']['id']}/app")
        assert preview.status_code==200
        bundle=client.get(f"/workbench/workflows/{current['workflow']['id']}/bundle")
        assert bundle.status_code==200 and bundle.content.startswith(b'PK')
        api=f"/workbench/workflows/{current['workflow']['id']}/app/api"
        assert client.post(api,json={'student':'S001','offering':'O101'}).status_code==200
        duplicate=client.post(api,json={'student':'S001','offering':'O101'})
        assert duplicate.status_code==200 and duplicate.json()['created'] is False
        second=client.post(api,json={'student':'S002','offering':'O101'})
        assert second.status_code==(200 if release=='v1' else 409)
        overlap=client.post(api,json={'student':'S001','offering':'O102'})
        assert overlap.status_code==(409 if release=='v1.2' else 200)
        assert client.post(api,json={'student':'S001','offering':'O103'}).status_code==200


def test_modified_code_cannot_be_executed_as_registered_course():
    with pytest.raises(KnowledgeError): run_course('v1',build_course('v1')['files']['course_app.py']+'\n# changed')


@pytest.mark.integration
def test_generic_binding_staleness_and_scope_are_rejected(client,project,entity):
    import_version(client,project,'v1')
    body=creation(client,project,'v1')
    body['bindings']['requirement'][0]['version']=99
    assert client.post('/workflows',json=body).status_code==409
    body=creation(client,project,'v1')
    body['bindings']['requirement'][0]['id']=str(uuid4())
    assert client.post('/workflows',json=body).status_code==404


@pytest.mark.integration
def test_manual_submission_creates_pending_proposal_without_automatic_approval(client,project):
    import_version(client,project,'v1')
    created=client.post('/workflows',json=creation(client,project,'v1','manual'))
    assert created.status_code==201,created.text
    current=snap(client,created.json()['id'])
    for action in ('validate_proposal','commit_proposal','start'): current=act(client,current,action)
    act(client,current,'run_reference_stage',409)
    draft=client.get(f"/workbench/workflows/{current['workflow']['id']}/artifact-draft").json()
    data={'expected_version':current['workflow']['version'],'task_version':current['task']['version'],'actor':'人工提交者',
          'reason':'核对实际需求文本后提交','changes':draft['changes']}
    submitted=client.post(f"/workbench/workflows/{current['workflow']['id']}/artifacts",json=data)
    assert submitted.status_code==200,submitted.text
    result=submitted.json()
    assert result['proposal']['status']=='pending' and result['task']['status']=='PROPOSED'
    assert result['workflow']['state']['stage']==0
    current=result
    for stage in range(3):
        if stage:
            draft=client.get(f"/workbench/workflows/{current['workflow']['id']}/artifact-draft").json()
            data.update(expected_version=current['workflow']['version'],task_version=current['task']['version'],changes=draft['changes'])
            response=client.post(f"/workbench/workflows/{current['workflow']['id']}/artifacts",json=data)
            assert response.status_code==200,response.text
            current=response.json()
        for action in ('validate_task','commit_proposal','complete_task','prepare_gate'):
            current=act(client,current,action)
        if stage<2:
            assert current['workflow']['status']=='AWAITING_GATE'
            for action in ('validate_proposal','commit_proposal','advance'): current=act(client,current,action)
    assert current['workflow']['status']=='BLOCKED'
    assert '尚未配置可信代码执行适配器' in current['workflow']['state']['diagnostic']
    assert current['executions']==[]
    assert not any(r['status']=='verified' for r in current['relationships'])


@pytest.mark.integration
def test_course_adapter_cannot_verify_requirements_changed_during_manual_submission(client,project):
    import_version(client,project,'v1')
    created=client.post('/workflows',json=creation(client,project,'v1'))
    assert created.status_code==201,created.text
    current=snap(client,created.json()['id'])
    for action in ('validate_proposal','commit_proposal','start'): current=act(client,current,action)
    draft=client.get(f"/workbench/workflows/{current['workflow']['id']}/artifact-draft").json()
    draft['changes'][0]['data']['attributes']['acceptance_criteria'].append('新增：所有选课必须先完成缴费。')
    response=client.post(f"/workbench/workflows/{current['workflow']['id']}/artifacts",json={
        'expected_version':current['workflow']['version'],'task_version':current['task']['version'],
        'actor':'人工需求复核者','reason':'明确增加教学执行器未覆盖的新需求','changes':draft['changes']})
    assert response.status_code==200,response.text
    current=response.json()
    for action in ('validate_task','commit_proposal','complete_task','prepare_gate'):
        current=act(client,current,action)
    assert current['workflow']['status']=='BLOCKED'
    assert '超出所选教学适配器范围' in current['workflow']['state']['diagnostic']
    assert current['executions']==[]
    assert not any(r['status']=='verified' for r in current['relationships'])
