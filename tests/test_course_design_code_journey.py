"""三轮选课端到端验收，模型响应使用教学替身，业务测试实际执行。"""
from pathlib import Path
from uuid import UUID

import pytest

from knowledge import model_gateway
from knowledge.code_generation import CodeGenerationService
from knowledge.course_executor import build_course, run_course
from test_design import action, detail, generate, model_environment, returned
from test_generic_workflow import approve, import_version


ACTOR = '选课验收者'
CASES = (
    ('REQ-ENROLL', 'BR-UNIQUE', '基础选课与重复请求', '返回选课记录', 'test_idempotent'),
    ('REQ-CAPACITY', 'BR-CAPACITY', '检查余位与满员拒绝', '返回满员结果', 'test_last_seat_concurrent'),
    ('REQ-TIME-CONFLICT', 'BR-TIME-CONFLICT', '课表冲突与拒绝新增', '返回时间冲突', 'test_same_student_concurrent'),
)


def _design_fixture(context):
    ids = [item['id'] for item in context['requirements']]
    scenarios = CASES[:len(ids)]
    # 教学导入独立维护项目基线的 Enrollment，这个固定响应单独验证生成设计和工程交付。
    component_key = 'EnrollmentDesign'
    sequence = [{
        'title': title, 'requirement_ids': [rid],
        'participants': [{'key': 'Student', 'name': '学生'}, {'key': component_key, 'name': '选课办理设计'}],
        'messages': [{'source': 'Student', 'target': component_key, 'label': '提交选课'},
                     {'source': component_key, 'target': 'Student', 'label': outcome}],
    } for rid, (_, _, title, outcome, _) in zip(ids, scenarios)]
    return {
        'summary': context['title'],
        'assumptions': ['本候选使用教学验收固定响应，供核对需求、设计及审阅流程。'],
        'components': [{'key': component_key, 'name': '选课办理设计', 'requirement_ids': ids,
                        'responsibility': '、'.join(case[2] for case in scenarios)}],
        'interfaces': [{'key': 'Enroll', 'name': '提交选课接口', 'component_key': component_key, 'protocol': 'HTTP',
                        'contract': {'method': 'POST', 'path': '/enroll'}, 'requirement_ids': ids}],
        'data_models': [{'key': 'Record', 'name': '学生选课记录', 'component_key': component_key,
                         'schema_definition': {'unique': ['student', 'offering']}, 'requirement_ids': ids}],
        'test_plan': [{'key': f'Scenario{index}', 'name': title, 'requirement_ids': [rid],
                       'steps': ['准备该场景的教学班与学生记录', '提交选课'], 'expected_result': outcome}
                      for index, (rid, (_, _, title, outcome, _)) in enumerate(zip(ids, scenarios), 1)],
        'sequence_diagrams': sequence,
        'use_case_diagrams': [{
            'title': '学生提交选课', 'requirement_ids': ids,
            'actors': [{'key': 'Student', 'name': '学生'}], 'use_cases': [{'key': 'Enroll', 'name': '提交选课'}],
            'associations': [{'actor': 'Student', 'use_case': 'Enroll'}],
        }],
        'collaboration_links': [
            {'source': {'kind': 'interface', 'key': 'Enroll'}, 'target': {'kind': 'component', 'key': component_key},
             'type': 'calls', 'rationale': '选课入口将请求交给负责本轮规则的选课办理模块。', 'requirement_ids': ids},
            {'source': {'kind': 'component', 'key': component_key}, 'target': {'kind': 'data_model', 'key': 'Record'},
             'type': 'reads', 'rationale': '先查原记录以保留重复请求的幂等行为，并读取本轮规则所需的已选记录。', 'requirement_ids': ids},
            {'source': {'kind': 'component', 'key': component_key}, 'target': {'kind': 'data_model', 'key': 'Record'},
             'type': 'writes', 'rationale': '首次选课通过本轮检查后再写入唯一选课记录。', 'requirement_ids': ids},
        ],
    }


def _read(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _post(client, path, status, body):
    response = client.post(path, json=body)
    assert response.status_code == status, response.text
    return response.json()


def exercise_course_journey(client, db_url, project, monkeypatch):
    project_id = project['id']
    workbench = f'/workbench/projects/{project_id}'
    baseline_url = workbench + '/project-model'
    releases = ('v1', 'v1.1', 'v1.2')
    builds = {f'选课需求设计 {release}': build_course(release) for release in releases}
    adapter = Path(__file__).resolve().parents[1] / 'examples' / 'course-selection' / 'app.py'
    for build in builds.values():
        build['files']['app.py'] = adapter.read_text(encoding='utf-8')
    design_inputs, code_inputs, saved_versions, results = [], [], [], []

    def design_model(context, schema, **kwargs):
        design_inputs.append(context)
        return returned(_design_fixture(context))

    def code_model(context, schema, **kwargs):
        code_inputs.append(context)
        build = builds[context['design']['summary']]
        return {'document': {'summary': '选课教学实现 ' + build['release'], 'files': [
            {'path': path, 'content': content} for path, content in build['files'].items()]},
            'trace': {'prompt_version': 'course-code-test-fixture'}}

    monkeypatch.setattr(model_gateway, 'complete_design', design_model)
    monkeypatch.setattr(model_gateway, 'complete_code', code_model)
    for index, (release, test_count) in enumerate(zip(releases, (5, 8, 13))):
        imported = import_version(client, project, release)
        knowledge = {item['key']: {'id': item['entity_id'], 'version': item['version']}
                     for item in imported['result']['entity_mapping']}
        cases = CASES[:index + 1]
        title = f'选课需求设计 {release}'
        requirements = [knowledge[item[0]] for item in cases]
        flow = _post(client, f'/design/projects/{project_id}/flows', 201, {
            'title': title, 'goal': '实现本轮选课业务并保留既有规则', 'actor': ACTOR,
            'requirements': requirements, 'input_knowledge': [knowledge[item[1]] for item in cases],
        })
        assert flow['definition'].get('project_model_version') == (index or None)
        review = generate(client, db_url, action(client, detail(client, flow['id']), 'start'))
        assert review['flow']['status'] == 'REVIEW_PENDING'
        assert design_inputs[-1].get('project_baseline', {}).get('version') == (index or None)
        newest = requirements[-1]
        deliveries = workbench + f'/requirements/{newest["id"]}/deliveries'
        assert _read(client, deliveries, version=newest['version'])['design'] is None
        for step in ('validate', 'commit', 'complete'):
            review = action(client, review, step)
        assert review['flow']['status'] == 'COMPLETED'
        documents = _read(client, deliveries, version=newest['version'], design_id=flow['id'])
        assert documents['design']['id'] == flow['id']
        for key, marker in (('requirements_analysis', '## 验收条件'), ('design', cases[-1][2])):
            document = documents['documents'][key]
            download = client.get(document['download_url'])
            assert download.status_code == 200, download.text
            assert download.text == document['markdown'] and marker in download.text
        assert any(diagram['kind'] == 'sequence' and diagram['title'] == cases[-1][2]
                   for diagram in documents['design']['diagrams'])
        assert {diagram['kind'] for diagram in documents['design']['diagrams']} == {
            'sequence', 'architecture', 'use_case', 'collaboration',
        }

        baseline_candidate = _post(client, baseline_url + '/course-example', 201, {
            'actor': ACTOR, 'release': release, 'expected_version': index,
        })
        approve(client, baseline_candidate)
        baseline = _read(client, baseline_url)['baseline']
        assert baseline['version'] == index + 1
        marker = ('提交选课', '检查余位', '检查课表冲突')[index]
        assert any(item['op'] == 'added' and marker in item['label'] for item in baseline['changes'])

        previous = saved_versions[-1][1] if saved_versions else None
        workspace = _post(client, f'/engineering/projects/{project_id}/workspaces', 201, {
            'name': f'选课工程 {release}', 'actor': ACTOR, 'design_flow_id': flow['id'], 'language': 'python',
            **({'source_snapshot_id': previous['id']} if previous else {}),
        })
        assert workspace['version'] == (2 if previous else 1)
        work = f'/engineering/workspaces/{workspace["id"]}'
        base = _read(client, work)['latest_snapshot']
        if previous:
            assert base['id'] != previous['id'] and base['sha256'] == previous['sha256']
            assert base['files'] == previous['files']
        else:
            assert base is None
        operation = {'expected_version': workspace['version'], 'actor': ACTOR, 'reason': f'核对并保存选课实现 {release}'}
        job = _post(client, work + '/code-generations', 202, operation)
        CodeGenerationService(db_url).run_job(UUID(workspace['id']), UUID(job['id']))
        review_url = work + f'/code-generations/{job["id"]}'
        candidate = _read(client, review_url)
        assert candidate['status'] == 'REVIEW_PENDING'
        assert candidate['base_snapshot_id'] == (base['id'] if base else None)
        assert code_inputs[-1]['design'] == review['design']
        previous_files = ({file['path']: file['content'] for file in previous['files']} if previous else {})
        assert {file['path']: file['content'] for file in code_inputs[-1]['workspace_files']} == previous_files
        changes = {item['path']: item for item in candidate['changes']}
        expected_files = builds[title]['files']
        assert changes == {path: {'path': path, 'operation': 'modify' if previous else 'add',
                                  'before': previous_files.get(path), 'after': content}
                           for path, content in expected_files.items() if previous_files.get(path) != content}
        assert cases[-1][4] in changes['test_course_app.py']['after']
        assert cases[-1][4] not in (changes['test_course_app.py']['before'] or '')
        assert _read(client, work)['latest_snapshot'] == base
        saved = _post(client, review_url + '/apply', 201, operation)['latest_snapshot']
        assert _read(client, review_url)['applied_snapshot_id'] == saved['id']
        saved_files = {file['path']: file['content'] for file in saved['files']}
        assert saved_files == expected_files
        comparison = _read(client, work + f'/snapshots/{saved["id"]}/interface-comparison')
        assert comparison['snapshot']['sha256'] == saved['sha256']
        assert comparison['design']['id'] == flow['id']
        assert comparison['diagnostics'] == []
        assert [(row['method'], row['path'], row['status']) for row in comparison['rows']] == [('POST', '/enroll', 'matched')]
        evidence = comparison['rows'][0]['implementation'][0]['evidence']
        assert evidence['file'] == 'app.py'
        assert evidence['file_sha256'] == next(file['sha256'] for file in saved['files'] if file['path'] == 'app.py')
        assert saved_files['app.py'].splitlines()[evidence['line'] - 1] in evidence['snippet']
        report = run_course(release, saved_files['course_app.py'])
        assert report['executed'] and report['passed'] and report['tests_run'] == test_count, report['stderr']
        for old_work, old_saved in saved_versions:
            assert _read(client, old_work)['latest_snapshot'] == old_saved
        saved_versions.append((work, saved))
        results.append({'release': release, 'flow': review['flow'], 'requirements': review['requirements'],
                        'baseline': baseline, 'workspace': workspace, 'snapshot': saved,
                        'generation': _read(client, review_url), 'report': report})
    return results


@pytest.mark.integration
def test_fresh_course_journey_reviews_design_and_carries_saved_code_through_three_releases(
        client, db_url, project, monkeypatch, model_environment):
    first, _, latest = exercise_course_journey(client, db_url, project, monkeypatch)
    workspace, snapshot = latest['workspace'], latest['snapshot']
    work = f'/engineering/workspaces/{workspace["id"]}'
    assert client.get(work + f'/snapshots/{first["snapshot"]["id"]}/interface-comparison').status_code == 404
    changed_files = [{'path': file['path'], 'content': file['content'].replace("'/enroll'", "'/enrollments'")
                      if file['path'] == 'app.py' else file['content']} for file in snapshot['files']]
    current = _read(client, work)['workspace']
    drift = _post(client, work + '/snapshots', 201, {
        'expected_version': current['version'], 'actor': ACTOR, 'reason': '独立对照案例：修改接口路径', 'files': changed_files,
    })['latest_snapshot']
    assert _read(client, work + '/snapshots')[0]['id'] == drift['id']
    difference = _read(client, work + f'/snapshots/{drift["id"]}/interface-comparison')
    assert {row['path']: row['status'] for row in difference['rows']} == {'/enroll': 'missing', '/enrollments': 'extra'}
    original = _read(client, work + f'/snapshots/{snapshot["id"]}/interface-comparison')
    assert [row['status'] for row in original['rows']] == ['matched']
