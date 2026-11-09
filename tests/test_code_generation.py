"""候选差异、确认保存和已审阅快照的执行回归。"""
from uuid import UUID

import pytest

from knowledge import model_gateway
from knowledge.code_generation import CodeCandidate, CodeGenerationService, candidate_changes
from knowledge.semantics import KnowledgeError
from knowledge.workspace import normalized_files
from test_design import action, create, fake_model, generate, model_environment, requirement


SOURCE = 'def can_enroll(count):\n    return count < 2\n'
TEST = '''import unittest
from enrollment import can_enroll

class EnrollmentTests(unittest.TestCase):
    def test_limits(self):
        self.assertTrue(can_enroll(1))
        self.assertFalse(can_enroll(2))
'''


def test_candidate_review_preserves_unmentioned_files_and_reports_exact_diff():
    base = normalized_files([{'path': 'README.md', 'content': '保留说明'}, {'path': 'enrollment.py', 'content': '# 初版\n'}])
    candidate = CodeCandidate(summary='选课实现', files=[{'path': 'enrollment.py', 'content': SOURCE},
                                                       {'path': 'test_enrollment.py', 'content': TEST}])
    changes, files = candidate_changes(candidate, base)
    assert changes == [
        {'path': 'enrollment.py', 'operation': 'modify', 'before': '# 初版\n', 'after': SOURCE},
        {'path': 'test_enrollment.py', 'operation': 'add', 'before': None, 'after': TEST},
    ]
    assert files[0] == base[0] and base[1]['content'] == '# 初版\n'


@pytest.mark.parametrize('paths', [['../test_x.py'], ['test_x.py', 'Dockerfile'], ['nested/test_x.py'],
                                   ['Test_x.py'], ['test-invalid.py'], ['test_x.py', 'APP.py']])
def test_invalid_candidate_cannot_reach_review(paths):
    base = normalized_files([{'path': 'app.py', 'content': 'pass'}])
    with pytest.raises(KnowledgeError):
        candidate_changes(CodeCandidate(summary='候选', files=[{'path': path, 'content': 'pass'} for path in paths]), base)


@pytest.mark.parametrize('language,files,wrong_extension', [
    ('c', ['enrollment.c', 'include/enrollment.h'], 'enrollment.cpp'),
    ('cpp', ['enrollment.cc', 'logic.cpp', 'checks.cxx', 'include/enrollment.hpp', 'include/common.h',
             'include/detail.hh', 'include/ports.hxx'], 'enrollment.c'),
    ('java', ['src/main/java/course/Enrollment.java'], 'enrollment.py'),
])
def test_candidate_extensions_follow_workspace_language(language, files, wrong_extension):
    candidate = CodeCandidate(summary='候选', files=[{'path': path, 'content': '// 示例源文件'} for path in files])
    changes, merged = candidate_changes(candidate, [], language)
    assert [item['path'] for item in changes] == sorted(files)
    assert [item['path'] for item in merged] == sorted(files)
    wrong = CodeCandidate(summary='错误语言', files=[{'path': wrong_extension, 'content': '示例'}])
    with pytest.raises(KnowledgeError, match='扩展名'):
        candidate_changes(wrong, merged, language)


@pytest.fixture
def confirmed_design(client, db_url, project, requirement, fake_model):
    current = generate(client, db_url, action(client, create(client, project, requirement), 'start'))
    for step in ('validate', 'commit', 'complete'):
        current = action(client, current, step)
    return current


def create_workspace(client, project, design=None, language=None):
    response = client.post(f"/engineering/projects/{project['id']}/workspaces", json={
        'name': '选课工程', 'actor': '开发者', **({'design_flow_id': design['flow']['id']} if design else {}),
        **({'language': language} if language else {}),
    })
    assert response.status_code == 201, response.text
    row = response.json()
    return row, '/engineering/workspaces/' + row['id']


def operation(version=1):
    return {'expected_version': version, 'actor': '开发者', 'reason': '按已确认的数量规则实现并覆盖边界测试'}


@pytest.fixture
def model_code(monkeypatch):
    calls = []

    def complete(context, schema, **kwargs):
        calls.append(context)
        assert 'files' in schema['properties'] and kwargs['expected_fingerprint']
        return {'document': {'summary': '实现选课上限', 'files': [{'path': 'enrollment.py', 'content': SOURCE},
                 {'path': 'test_enrollment.py', 'content': TEST}]}, 'trace': {'prompt_version': 'code-test'}}

    monkeypatch.setattr(model_gateway, 'complete_code', complete)
    return calls


def enqueue(client, url, version=1):
    response = client.post(url + '/code-generations', json=operation(version))
    assert response.status_code == 202, response.text
    return response.json()['id']


@pytest.mark.integration
def test_confirmed_design_to_reviewed_snapshot_and_test_result(client, db_url, project, confirmed_design, model_code, monkeypatch):
    workspace, url = create_workspace(client, project, confirmed_design)
    assert workspace['language'] == 'python'
    generation_id = enqueue(client, url)
    candidate_url = url + '/code-generations/' + generation_id
    assert client.post(url + '/code-generations', json=operation()).status_code == 409
    assert client.post(candidate_url + '/apply', json=operation()).status_code == 409
    assert model_code == []

    assert CodeGenerationService(db_url).run_next()
    context, = model_code
    assert context['design'] == confirmed_design['design'] and context['workspace_files'] == []
    assert context['language'] == 'python' and context['profile'] == 'python_unittest'
    assert context['generation_guidance'] == operation()['reason']
    assert context['requirements'][0]['id'] == confirmed_design['requirements'][0]['id']
    review = client.get(candidate_url).json()
    assert review['status'] == 'REVIEW_PENDING' and review['changes'][0]['after'] == SOURCE
    assert client.get(url).json()['latest_snapshot'] is None
    history = client.get(url + '/code-generations').json()
    assert history[0]['id'] == generation_id and 'changes' not in history[0]

    response = client.post(candidate_url + '/apply', json=operation())
    assert response.status_code == 201, response.text
    saved = response.json()['latest_snapshot']
    assert client.get(candidate_url).json()['applied_snapshot_id'] == saved['id']
    assert client.post(candidate_url + '/apply', json=operation(2)).status_code == 409

    monkeypatch.setattr('knowledge.workspace.executor_status', lambda profile: {'available': True})
    monkeypatch.setattr('knowledge.workspace.executor_config', lambda profile: {
        'backend': 'docker', 'image': 'test-image', 'timeout_seconds': 30, 'profile': profile,
    })

    def execute(files, config, cancelled):
        assert not cancelled()
        assert files == saved['files'] and files[0]['content'] == SOURCE
        return {'passed': True, 'tests_run': 1, 'returncode': 0, 'stdout': '隔离执行协议替身'}

    monkeypatch.setattr('knowledge.workspace.run_isolated', execute)
    response = client.post(url + '/executions', json=operation(2) | {'snapshot_id': saved['id']})
    assert response.status_code == 201, response.text
    execution = response.json()
    assert execution['status'] == 'SUCCEEDED' and execution['result']['tests_run'] == 1
    assert execution['snapshot_sha256'] == saved['sha256']


@pytest.mark.integration
def test_stale_candidate_cannot_overwrite_new_snapshot_or_cross_workspace(client, db_url, project, confirmed_design, model_code):
    workspace, url = create_workspace(client, project, confirmed_design)
    _, other_url = create_workspace(client, project)
    assert client.post(other_url + '/code-generations', json=operation()).status_code == 409
    job = enqueue(client, url)
    service = CodeGenerationService(db_url)
    service.run_job(UUID(workspace['id']), UUID(job))
    assert client.get(other_url + '/code-generations/' + job).status_code == 404
    response = client.post(url + '/snapshots', json=operation() | {'files': [{'path': 'notes.txt', 'content': '手工保存'}]})
    assert response.status_code == 201, response.text
    assert client.post(url + '/code-generations/' + job + '/apply', json=operation(2)).status_code == 409
    assert client.get(url).json()['latest_snapshot']['files'][0]['path'] == 'notes.txt'


@pytest.mark.integration
@pytest.mark.parametrize('language,path,source,compile_status,test_status,expected_status', [
    ('c', 'enrollment.c', 'int can_enroll(int count) { return count < 2; }\n', 'failed', 'not_run', 'COMPILE_FAILED'),
    ('cpp', 'enrollment.cpp', 'bool can_enroll(int count) { return count < 2; }\n', 'passed', 'failed', 'TEST_FAILED'),
    ('java', 'course/Enrollment.java', 'package course;\npublic class Enrollment {}\n', 'passed', 'passed', 'SUCCEEDED'),
])
def test_native_candidate_is_reviewed_saved_and_executed_with_distinct_phase_statuses(
        client, db_url, project, confirmed_design, monkeypatch, language, path, source, compile_status, test_status, expected_status):
    workspace, url = create_workspace(client, project, confirmed_design, language)
    assert workspace['language'] == language

    def complete(context, schema, **kwargs):
        assert context['language'] == kwargs['language'] == language
        assert 'profile' not in context
        return {'document': {'summary': '实现选课', 'files': [{'path': path, 'content': source}]}, 'trace': {}}

    monkeypatch.setattr(model_gateway, 'complete_code', complete)
    job = enqueue(client, url)
    candidate_url = url + '/code-generations/' + job
    CodeGenerationService(db_url).run_job(UUID(workspace['id']), UUID(job))
    candidate = client.get(candidate_url).json()
    assert candidate['status'] == 'REVIEW_PENDING'
    assert candidate['changes'] == [{'path': path, 'operation': 'add', 'before': None, 'after': source}]
    response = client.post(candidate_url + '/apply', json=operation())
    assert response.status_code == 201, response.text
    saved = response.json()['latest_snapshot']
    assert saved['files'][0]['content'] == source
    profile = language + '_native'
    execution = operation(2) | {'snapshot_id': saved['id']}
    monkeypatch.setattr('knowledge.workspace.executor_status', lambda _: pytest.fail('不匹配的执行方式不应探测执行器'))
    response = client.post(url + '/executions', json=execution | {'profile': 'python_unittest'})
    assert response.status_code == 409 and '不匹配' in response.json()['detail']

    monkeypatch.setattr('knowledge.workspace.executor_status', lambda _: {'available': True})
    monkeypatch.setattr('knowledge.workspace.executor_config', lambda selected: {
        'backend': 'docker', 'image': 'native-test-image', 'timeout_seconds': 30, 'profile': selected,
    })

    def run(files, config, cancelled):
        assert config['profile'] == profile and files == saved['files']
        return {'passed': test_status == 'passed', 'compile_status': compile_status, 'test_status': test_status,
                'tests_run': int(test_status != 'not_run'), 'stderr': '【编译】记录\n【测试】记录'}

    monkeypatch.setattr('knowledge.workspace.run_isolated', run)
    response = client.post(url + '/executions', json=execution)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result['profile'] == profile and result['status'] == expected_status
    assert result['snapshot_sha256'] == saved['sha256'] and '【编译】记录' in result['result']['stderr']
