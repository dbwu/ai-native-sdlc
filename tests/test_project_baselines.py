"""使用 PostgreSQL 和已有人工提案接口验收选课三轮架构与业务流程。"""
from copy import deepcopy
from uuid import uuid4

import pytest

from knowledge import model_gateway
from test_design import action, create, fake_model, generate, model_environment, requirement
from test_generic_workflow import approve


ACTOR = '选课项目架构审阅者'


def _read(client, path, version=None):
    result = client.get(path, params={} if version is None else {'version': version})
    assert result.status_code == 200, result.text
    return result.json()


def _propose_example(client, path, version, release, status=201):
    result = client.post(path + '/course-example', json={'expected_version': version, 'release': release, 'actor': ACTOR})
    assert result.status_code == status, result.text
    return result.json()


def _process(baseline, key):
    return next(item for item in baseline['processes'] if item['key'] == key)


@pytest.mark.integration
def test_model_project_baseline_preserves_confirmed_input_and_requires_review(
        client, db_url, project, requirement, fake_model, model_environment, monkeypatch):
    path = f'/workbench/projects/{project["id"]}/project-model'
    draft = create(client, project, requirement)
    request = {'design_id': draft['flow']['id'], 'expected_version': 0, 'actor': ACTOR}
    assert client.post(path + '/model-draft', json=request).status_code == 409
    detail = generate(client, db_url, action(client, draft, 'start'))
    for step in ('validate', 'commit', 'complete'):
        detail = action(client, detail, step)

    observed = []

    def complete(context, schema, *, on_exchange):
        observed.append(context)
        assert schema['title'] == 'ProjectModel'
        on_exchange({'request': {'model': '验收模型', 'messages': []}, 'response': {'content': '真实响应占位'}})
        component = next(row for row in context['confirmed_artifacts'] if row['kind'] == 'ArchitectureComponent')
        ref = {'id': requirement['id'], 'version': requirement['version']}
        return {'trace': {'provider': 'test', 'model': '验收模型', 'request_id': 'trace', 'prompt_version': 'v1',
                          'input_sha256': 'a' * 64, 'output_sha256': 'b' * 64},
                'document': {'title': '模型提出的选课架构', 'nodes': [
                    {'key': 'Course', 'name': '选课领域', 'kind': 'domain'},
                    {'key': 'Enrollment', 'name': '选课办理', 'kind': 'module', 'parent_key': 'Course',
                     'description': '办理选课并保护记录一致性。', 'entity_id': component['id'], 'entity_version': component['version']},
                ], 'processes': [{'key': 'Enroll', 'name': '办理选课', 'requirement_refs': [ref],
                                  'steps': [{'key': 'Start', 'name': '开始', 'kind': 'start', 'owner': 'Enrollment'},
                                            {'key': 'End', 'name': '完成', 'kind': 'end', 'owner': 'Enrollment'}],
                                  'transitions': [{'key': 'Finish', 'source': 'Start', 'target': 'End'}]}]}}

    monkeypatch.setattr(model_gateway, 'complete_project_model', complete)
    response = client.post(path + '/model-draft', json=request)
    assert response.status_code == 201, response.text
    proposal = response.json()
    assert proposal['status'] == 'pending' and _read(client, path)['baseline'] is None
    assert observed[0]['confirmed_design'] == detail['design']
    assert observed[0]['requirements'][0]['id'] == requirement['id']
    diff = _read(client, f'/workbench/proposals/{proposal["id"]}/diff')
    assert diff['project_model_origin']['design_id'] == request['design_id']
    assert [item['kind'] for item in diff['project_model_preview']] == ['logical_structure', 'activity']
    exchange = _read(client, f'/workbench/proposals/{proposal["id"]}/project-model-exchange')
    assert exchange['response']['content'] == '真实响应占位'
    approve(client, proposal)
    assert _read(client, path)['baseline']['model_origin'] == diff['project_model_origin']
    assert '模型分析 验收模型' in client.get(path + '/markdown?version=1').text
    assert client.post(path + '/model-draft', json={**request, 'expected_version': 1}).status_code == 409
    assert len(observed) == 1


@pytest.mark.integration
def test_course_project_architecture_processes_and_three_version_diffs(client, project):
    path = f'/workbench/projects/{project["id"]}/project-model'
    empty = _read(client, path)
    assert empty['baseline'] is None and empty['versions'] == []
    assert client.get(path.replace(project['id'], str(uuid4()))).status_code == 404
    _propose_example(client, path, 0, 'v1', 409)

    versions = []
    for index, release in enumerate(('v1', 'v1.1', 'v1.2')):
        imported = client.post(path.removesuffix('/project-model') + '/course-import', json={'release': release, 'actor': ACTOR})
        assert imported.status_code == 201, imported.text
        _propose_example(client, path, index, release, 409)
        import_proposal = approve(client, imported.json()['proposal'])
        source_refs = {row['key']: row for row in import_proposal['result']['entity_mapping']}

        _propose_example(client, path, index + 1, release, 409)
        candidate = _propose_example(client, path, index, release)
        assert candidate['status'] == 'pending'
        assert candidate['plan']['project_model']['before_version'] == (index or None)
        assert all(item['comparison'] == {'before_version': index or None, 'after_version': index + 1}
                   for item in candidate['plan']['project_model']['changes'])
        checked = client.post(f'/operations/proposals/{candidate["id"]}/validate', json={
            'expected_version': candidate['version'], 'actor': ACTOR, 'reason': '核对跨模块依赖和业务流程步骤',
        })
        assert checked.status_code == 200, checked.text
        assert checked.json()['proposal']['status'] == 'validated', checked.text
        assert _read(client, path)['baseline'] == (versions[-1] if versions else None)
        committed = client.post(f'/operations/proposals/{candidate["id"]}/commit', json={
            'expected_version': checked.json()['proposal']['version'], 'actor': ACTOR, 'reason': '确认发布本轮选课项目基线',
        })
        assert committed.status_code == 200, committed.text

        current = _read(client, path)
        assert [item['version'] for item in current['versions']] == list(range(index + 1, 0, -1))
        baseline = current['baseline']
        assert baseline['version'] == index + 1 and baseline['proposal_id'] == candidate['id']
        assert baseline['comparison'] == {'before_version': index or None, 'after_version': index + 1}
        assert baseline['architecture']['baseline_ref'] == {
            'project_id': project['id'], 'version': index + 1, 'proposal_id': candidate['id']}
        structure = baseline['logical_structure']['mermaid']
        assert 'subgraph' in structure and '包含' not in structure
        assert '余位校验' not in structure and '课表冲突校验' not in structure
        assert len(baseline['collaboration']['edges']) == (5, 8, 11)[index]
        ports = {item['key']: item for item in baseline['model']['architecture_ports']}
        assert ports['SubmitApi']['knowledge']['id'] == source_refs['IF-ENROLL']['entity_id']
        assert ports['OfferingData']['knowledge']['version'] == source_refs['DM-ENROLL']['version']
        assert baseline['title'].endswith(release)
        assert baseline['confirmed_at']
        nodes = {item['key']: item for item in baseline['model']['nodes']}
        assert nodes['CourseDomain']['entity_id'] == source_refs['DOMAIN-COURSE']['entity_id']
        assert nodes['CourseDomain']['entity_version'] == source_refs['DOMAIN-COURSE']['version']
        assert nodes['Enrollment']['entity_id'] == source_refs['COMP-ENROLL']['entity_id']
        assert nodes['Enrollment']['entity_version'] == source_refs['COMP-ENROLL']['version']
        assert nodes['Catalog']['parent_key'] == 'CatalogDomain'
        assert nodes['Offering']['parent_key'] == 'Catalog'
        assert nodes['Record']['parent_key'] == 'Enrollment'
        assert len({node['entity_id'] for node in nodes.values()}) == len(nodes)
        if versions:
            previous = {item['key']: item for item in versions[-1]['model']['nodes']}
            assert all(nodes[key]['entity_id'] == node['entity_id'] for key, node in previous.items())

        assert {process['key'] for process in baseline['processes']} == {'SubmitEnrollment', 'BrowseOfferings', 'ViewEnrollments'}
        submit = _process(baseline, 'SubmitEnrollment')
        diagrams = submit['diagrams']
        activity = next(diagram for diagram in diagrams if diagram['kind'] == 'activity')
        assert 'subgraph' in activity['mermaid'] and '教学班信息' in activity['mermaid'] and '选课记录' in activity['mermaid']
        assert any(diagram['kind'] == 'sequence' and '返回同一条记录' in diagram['mermaid'] for diagram in diagrams)
        source = next(item for item in baseline['model']['processes'] if item['key'] == 'SubmitEnrollment')
        assert activity['model_refs']['process_key'] == 'SubmitEnrollment'
        assert activity['model_refs']['requirement_refs'] == source['requirement_refs']
        transitions = {(row['source'], row['target']) for row in source['transitions']}
        assert ('CheckExisting', 'ReturnExisting') in transitions
        assert {'id': source_refs['REQ-ENROLL']['entity_id'], 'version': source_refs['REQ-ENROLL']['version']} in source['requirement_refs']
        if index == 0:
            assert ('CheckExisting', 'Persist') in transitions
            assert '检查余位' not in activity['mermaid']
        else:
            assert ('CheckCapacity', 'RejectCapacity') in transitions
            assert ('CheckCapacity', 'Persist') in transitions
            assert '检查余位' in activity['mermaid'] and '满员拒绝' in activity['mermaid']
            capacity_rule = source_refs['BR-CAPACITY']
            assert {'id': capacity_rule['entity_id'], 'version': capacity_rule['version']} in source['rule_refs']
            if index == 1:
                assert ('CheckExisting', 'CheckCapacity') in transitions
                assert any(item['op'] == 'added' and '检查余位' in item['label'] for item in baseline['changes'])
                capacity_change = next(item for item in baseline['changes'] if item['subject']['kind'] == 'step'
                                       and item['subject']['key'] == 'CheckCapacity')
                assert capacity_change['before'] is None
                assert any(ref['id'] == capacity_rule['entity_id'] for ref in capacity_change['rule_refs']['after'])
            else:
                assert ('CheckExisting', 'CheckTimetable') in transitions
                assert ('CheckTimetable', 'CheckCapacity') in transitions
                assert ('CheckTimetable', 'RejectTimetable') in transitions
                assert '检查课表冲突' in activity['mermaid']
                assert any(item['op'] == 'added' and '检查课表冲突' in item['label'] for item in baseline['changes'])

        md = client.get(baseline['download_url'])
        assert md.status_code == 200, md.text
        assert md.headers['Content-Type'].startswith('text/markdown')
        for diagram in [baseline['architecture'], *diagrams]:
            assert f'```mermaid\n{diagram["mermaid"]}\n```' in md.text
        for previous in versions:
            assert _read(client, path, previous['version'])['baseline'] == previous
            assert client.get(previous['download_url']).status_code == 200
        compare_path = path + '/comparison'
        params = {'before_version': index, 'after_version': index + 1, 'kind': 'sequence',
                  'process_key': 'SubmitEnrollment', 'scenario_key': 'Success'}
        comparison = client.get(compare_path, params=params)
        assert comparison.status_code == 200, comparison.text
        report = comparison.json()
        assert report['after_ref']['proposal_id'] == baseline['proposal_id']
        assert report['before'] is None if index == 0 else report['before_ref']['version'] == index
        assert not report['notice']
        if index:
            assert '【修改】' in report['overlay']['mermaid']
        export = client.get(compare_path + '/markdown', params=params)
        assert export.headers['Content-Type'].startswith('text/markdown')
        assert report['overlay']['mermaid'] in export.text
        assert client.get(compare_path, params={**params, 'before_version': index + 1}).status_code == 422
        assert client.get(compare_path, params={**params, 'after_version': index + 2}).status_code == 404
        collab = _read_collaboration(client, compare_path, index, index + 1)
        assert '【新增】' in collab['overlay']['mermaid']
        assert ('【修改】写入' in collab['overlay']['mermaid']) == bool(index)
        if index:
            expected = '读取容量上限' if index == 1 else '读取目标和已选教学班时段'
            assert f'【新增】读取：{expected}' in collab['overlay']['mermaid']
        versions.append(baseline)


def _read_collaboration(client, path, before, after):
    response = client.get(path, params={'before_version': before, 'after_version': after, 'kind': 'collaboration'})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.integration
def test_manual_project_baseline_is_atomic_and_rechecks_review_version(client, project):
    root = f'/workbench/projects/{project["id"]}'
    path = root + '/project-model'
    model = {
        'title': '咨询项目基线', 'nodes': [
            {'key': 'Service', 'kind': 'domain', 'name': '咨询服务领域'},
            {'key': 'Consultation', 'kind': 'module', 'name': '咨询受理', 'parent_key': 'Service'},
        ], 'processes': [{
            'key': 'HandleConsultation', 'name': '处理咨询', 'steps': [
                {'key': 'Begin', 'name': '开始受理', 'kind': 'start', 'owner': 'Consultation'},
                {'key': 'End', 'name': '完成受理', 'kind': 'end', 'owner': 'Consultation'},
            ], 'transitions': [{'source': 'Begin', 'target': 'End'}],
        }],
    }

    def propose(snapshot, version=0, expected_status=201):
        response = client.post(path, json={'actor': ACTOR, 'expected_version': version, 'model': snapshot})
        assert response.status_code == expected_status, response.text
        return response.json()

    def review(proposal, action, expected_status=200):
        response = client.post(f'/operations/proposals/{proposal["id"]}/{action}', json={
            'expected_version': proposal['version'], 'actor': ACTOR, 'reason': '核对本次项目架构及流程差异',
        })
        assert response.status_code == expected_status, response.text
        return response.json()

    # 未确认的来源候选虽已生成稳定 ID，也不能作为正式项目基线的依据。
    imported = client.post(root + '/course-import', json={'release': 'v1', 'actor': ACTOR})
    assert imported.status_code == 201, imported.text
    pending_requirement = next(row for row in imported.json()['proposal']['plan']['entity_mapping'] if row['key'] == 'REQ-ENROLL')
    pending_model = deepcopy(model)
    pending_model['processes'][0]['requirement_refs'] = [{'id': pending_requirement['entity_id'], 'version': pending_requirement['version']}]
    propose(pending_model, expected_status=404)

    candidate_a = propose(model)
    competing = deepcopy(model)
    competing['summary'] = '竞争提案'
    candidate_b = propose(competing)
    checked_a = review(candidate_a, 'validate')['proposal']
    checked_b = review(candidate_b, 'validate')['proposal']
    assert checked_a['status'] == checked_b['status'] == 'validated'
    assert _read(client, path)['baseline'] is None
    operations = candidate_a['plan']['operations']
    assert {item['data']['kind'] for item in operations} == {'Domain', 'ArchitectureComponent'}
    assert all(client.get(f'/entities/{item["id"]}').status_code == 404 for item in operations)

    review(checked_a, 'commit')
    for item in operations:
        record = _read(client, f'/entities/{item["id"]}')
        assert record['project_id'] == project['id'] and record['kind'] == item['data']['kind']
    diff = _read(client, f'/workbench/proposals/{candidate_b["id"]}/diff')
    assert diff['stale'] and diff['project_model_changes']
    review(checked_b, 'commit', 409)
    first = _read(client, path)['baseline']
    assert first['version'] == 1 and first['proposal_id'] == candidate_a['id']

    amended = deepcopy(first['model'])
    amended['processes'][0]['steps'][1]['name'] = '发送受理结果'
    update = propose(amended, version=1)
    assert update['plan']['operations'] == []
    approve(client, update)
    second = _read(client, path)['baseline']
    assert second['version'] == 2 and second['model']['nodes'] == first['model']['nodes']
    assert any(item['op'] == 'updated' and item['kind'] == '流程步骤' and '发送受理结果' in item['label']
               and '完成受理' in item['detail'] for item in second['changes'])
    assert _read(client, path, 1)['baseline'] == first
