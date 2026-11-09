"""工程路径边界、不可变快照和隔离执行的实际结果契约。"""
from io import BytesIO
import json
import re
import subprocess
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError

from knowledge.db import connect
from knowledge.semantics import KnowledgeError
from knowledge.workspace import (ExecutionCancel, ExecutionCreate, REPORT_PREFIX, SnapshotCreate,
                                 WorkspaceCreate, WorkspaceService, docker_command, executor_status, executor_statuses,
                                 normalized_files, run_isolated, snapshot_digest)


@pytest.mark.parametrize('path', ['../outside.py', '/root.py', 'C:/file.py', 'a\\file.py',
                                 'a/../b.py', 'a//file.py', 'a/./file.py', 'CON.py', 'a/NUL', 'a./b.py'])
def test_workspace_rejects_unsafe_paths(path):
    with pytest.raises(KnowledgeError):
        normalized_files([{'path': path, 'content': 'pass'}])


@pytest.mark.parametrize('paths', [['a.py', 'A.py'], ['a', 'a/test.py']])
def test_workspace_rejects_ambiguous_file_tree(paths):
    with pytest.raises(KnowledgeError):
        normalized_files([{'path': path, 'content': 'pass'} for path in paths])


def test_workspace_preserves_source_whitespace_and_stable_sorted_digest():
    body = SnapshotCreate(expected_version=1, actor='测试者', reason='核对源码空白', files=[
        {'path': 'b.py', 'content': '\n  value = 1  \n'}, {'path': 'a.py', 'content': '\n'}])
    files = normalized_files(body.files)
    assert files[1]['content'] == '\n  value = 1  \n'
    assert snapshot_digest(files) == snapshot_digest(normalized_files(reversed(body.files)))
    assert snapshot_digest(files) != snapshot_digest(normalized_files([{'path': 'b.py', 'content': 'value = 1'}]))


@pytest.mark.parametrize('field,invalid', [('name', 'name\x00'), ('actor', '\ud800')])
def test_workspace_rejects_text_that_cannot_be_saved_as_postgres_json(field, invalid):
    with pytest.raises(ValidationError):
        WorkspaceCreate(**{'name': '工作区', 'actor': '测试者', field: invalid})


def test_executor_disabled_never_checks_docker_or_runs_code(monkeypatch):
    monkeypatch.delenv('KNOWLEDGE_EXECUTOR', raising=False)
    monkeypatch.setattr('knowledge.workspace.subprocess.run', lambda *a, **kw: pytest.fail('禁用状态不应启动子进程'))
    status = executor_status()
    assert status['backend'] == 'disabled' and not status['available']


def test_docker_profile_has_fixed_entrypoint_and_isolation_limits():
    command = docker_command({'docker': 'docker'}, 'C:/temporary/work', 'job-example', 'sha256:' + 'a' * 64)
    for required in ('--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                     '--user=65534:65534', '--pids-limit=64', '--cpus=1', '--memory=256m', '--memory-swap=256m',
                     '--pull=never', '--entrypoint=python'):
        assert required in command
    assert 'type=bind,source=C:/temporary/work,target=/workspace,readonly' in command
    assert '--privileged' not in command and '-v' not in command
    assert command[-4:-1] == ['-I', '-B', '-c']
    assert 'unittest' in command[-1]


def fake_runner(monkeypatch, stdout, code=0, adapt_nonce=True, stderr=b''):
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=('sha256:' + 'a' * 64).encode(), stderr=b'')
    class Process:
        def __init__(self, command, **kwargs):
            calls.append(command)
            prefix = re.search(r"(?:print\(|REPORT_PREFIX = )'([^']*)'", command[-1]).group(1)
            output = stdout.replace(REPORT_PREFIX.encode(), prefix.encode()) if adapt_nonce else stdout
            self.stdout, self.stderr = BytesIO(output), BytesIO(stderr)
            self.returncode = code
        def poll(self): return self.returncode
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9
    monkeypatch.setattr('knowledge.workspace.subprocess.run', fake_run)
    monkeypatch.setattr('knowledge.workspace.subprocess.Popen', Process)
    return calls


def report(count=2, **changes):
    document = {'tests_run': count, 'failures': 0, 'errors': 0, 'skipped': 0, 'successful': True} | changes
    return (REPORT_PREFIX + json.dumps(document) + '\n').encode()


@pytest.mark.parametrize('stdout,code,passed', [(b'Ran 9 tests in 0.01s\nOK\n', 0, False),
                                              (report(0), 0, False), (report(), 1, False),
                                              (report(2, failures=1), 0, False), (report(), 0, True)])
def test_executor_requires_actual_runner_report_and_successful_process(monkeypatch, stdout, code, passed):
    calls = fake_runner(monkeypatch, stdout, code)
    result = run_isolated([{'path': 'test_sample.py', 'content': 'import unittest\n'}],
                          {'docker': 'docker', 'image': 'approved:1', 'timeout_seconds': 5})
    assert result['passed'] is passed
    assert any(command[1:3] == ['rm', '--force'] for command in calls)
    assert result['image_id'] == 'sha256:' + 'a' * 64


def test_execution_captures_bounded_logs_but_preserves_final_runner_report(monkeypatch):
    fake_runner(monkeypatch, b'x' * 80000 + b'\n' + report())
    result = run_isolated([{'path': 'test_sample.py', 'content': 'pass'}],
                          {'docker': 'docker', 'image': 'approved:1', 'timeout_seconds': 5})
    assert result['passed'] and result['output_truncated']
    assert len(result['stdout'].encode()) <= 16384


def test_native_executor_profiles_use_distinct_images_and_a_single_docker_probe(monkeypatch):
    monkeypatch.setenv('KNOWLEDGE_EXECUTOR', 'docker')
    monkeypatch.delenv('KNOWLEDGE_EXECUTOR_IMAGE', raising=False)
    monkeypatch.delenv('KNOWLEDGE_EXECUTOR_JAVA_IMAGE', raising=False)
    monkeypatch.setenv('KNOWLEDGE_EXECUTOR_C_IMAGE', 'cc:local')
    monkeypatch.setenv('KNOWLEDGE_EXECUTOR_CPP_IMAGE', 'cpp:local')
    monkeypatch.setattr('knowledge.workspace.shutil.which', lambda _: 'docker')
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr('knowledge.workspace.subprocess.run', probe)
    result = executor_statuses()
    assert not result['available'] and result['profile'] == 'python_unittest'
    assert result['profiles']['c_native']['image'] == 'cc:local'
    assert result['profiles']['cpp_native']['image'] == 'cpp:local'
    assert result['profiles']['c_native']['available'] and not result['profiles']['java_native']['available']
    assert len(calls) == 1


@pytest.mark.parametrize('exit_code,compiled,tested,compile_code,test_code,count,expected', [
    (2, 'failed', 'not_run', 1, None, 0, ('failed', 'not_run')),
    (3, 'passed', 'failed', 0, 7, 1, ('passed', 'failed')),
    (4, 'passed', 'no_tests', 0, None, 0, ('passed', 'not_run')),
    (4, 'not_run', 'no_tests', None, None, 0, ('not_run', 'not_run')),
    (0, 'passed', 'passed', 0, 0, 2, ('passed', 'passed')),
    (0, 'passed', 'passed', 0, 0, 0, (None, None)),
])
def test_native_execution_accepts_only_matching_phase_report(
        monkeypatch, exit_code, compiled, tested, compile_code, test_code, count, expected):
    payload = report(count, profile='c_native', compile_status=compiled, test_status=tested,
                     compile_returncode=compile_code, test_returncode=test_code, successful=exit_code == 0)
    logs = '【编译】开始\n'.encode() + b'\x00' + b'x' * 20000 + '\n【测试】结束\n'.encode()
    calls = fake_runner(monkeypatch, payload, code=exit_code, stderr=logs)
    result = run_isolated([{'path': 'test_example.c', 'content': 'int main(void) { return 0; }'}],
                          {'docker': 'docker', 'image': 'approved:1', 'timeout_seconds': 5, 'profile': 'c_native'})
    assert (result['compile_status'], result['test_status']) == expected
    assert result['passed'] == (exit_code == 0 and count > 0)
    assert '【编译】开始' in result['stderr'] and '【测试】结束' in result['stderr'] and result['output_truncated']
    assert '\x00' not in result['stderr'] and r'\x00' in result['stderr']
    assert REPORT_PREFIX not in result['stdout']
    command = next(command for command in calls if command[1] == 'run')
    assert '--network=none' in command and '--read-only' in command and '--user=65534:65534' in command
    assert '--tmpfs=/build:rw,exec,nosuid,nodev,size=64m,mode=1777' in command


def test_preconstructed_report_and_early_successful_exit_cannot_fake_execution(monkeypatch):
    fake_runner(monkeypatch, report(99), adapt_nonce=False)
    result = run_isolated([{'path': 'test_fake.py', 'content': 'print("伪造通过"); raise SystemExit(0)'}],
                          {'docker': 'docker', 'image': 'approved:1', 'timeout_seconds': 5})
    assert not result['passed'] and result['tests_run'] == 0


def test_execution_timeout_and_cancellation_clean_container(monkeypatch):
    calls = fake_runner(monkeypatch, report())
    class HangingProcess:
        stdout, stderr = BytesIO(b''), BytesIO(b'')
        returncode = None
        def __init__(self, *args, **kwargs): pass
        def poll(self): return self.returncode
        def kill(self): self.returncode = -9
        def wait(self, timeout=None): return self.returncode
    monkeypatch.setattr('knowledge.workspace.subprocess.Popen', HangingProcess)
    config = {'docker': 'docker', 'image': 'approved:1', 'timeout_seconds': 0}
    timed_out = run_isolated([{'path': 'test_sample.py', 'content': 'pass'}], config)
    assert timed_out['timed_out'] and not timed_out['passed']
    cancelled = run_isolated([{'path': 'test_sample.py', 'content': 'pass'}], config | {'timeout_seconds': 5}, lambda: True)
    assert cancelled['cancelled'] and not cancelled['passed']
    assert sum(command[1:3] == ['rm', '--force'] for command in calls) >= 2


@pytest.mark.integration
def test_engineering_snapshots_are_immutable_and_stale_writes_rejected(db_url, project):
    service = WorkspaceService(db_url)
    workspace = service.create(UUID(project['id']), WorkspaceCreate(name='课程工程', actor='测试者'))
    body = SnapshotCreate(expected_version=workspace['version'], actor='测试者', reason='保存第一版',
                          files=[{'path': 'app.py', 'content': '\nprint("课程")\n'}])
    detail = service.snapshot(workspace['id'], body)
    first = detail['latest_snapshot']
    assert first['files'][0]['content'] == '\nprint("课程")\n'
    assert first['sha256'] == snapshot_digest(first['files'])
    with pytest.raises(KnowledgeError) as stale:
        service.snapshot(workspace['id'], body)
    assert stale.value.status_code == 409
    second = service.snapshot(workspace['id'], body.model_copy(update={
        'expected_version': detail['workspace']['version'], 'files': [{'path': 'app.py', 'content': 'print(2)'}]}))
    assert second['latest_snapshot']['version'] == 2
    with connect(db_url) as conn:
        assert conn.execute('SELECT files FROM engineering_snapshot WHERE id=%s', (first['id'],)).fetchone()['files'] == first['files']
    with pytest.raises(psycopg.Error):
        with connect(db_url) as conn:
            conn.execute("UPDATE engineering_snapshot SET reason='覆盖' WHERE id=%s", (first['id'],))


@pytest.mark.integration
def test_engineering_execution_disabled_and_snapshot_drift_cannot_pass(db_url, project, monkeypatch):
    service = WorkspaceService(db_url)
    workspace = service.create(UUID(project['id']), WorkspaceCreate(name='测试工程', actor='测试者'))
    detail = service.snapshot(workspace['id'], SnapshotCreate(expected_version=1, actor='测试者', reason='建立代码基线',
                             files=[{'path': 'test_app.py', 'content': 'import unittest\n'}]))
    request = ExecutionCreate(expected_version=detail['workspace']['version'], snapshot_id=detail['latest_snapshot']['id'], actor='测试者', reason='运行固定测试')
    monkeypatch.setenv('KNOWLEDGE_EXECUTOR', 'disabled')
    with pytest.raises(KnowledgeError) as disabled:
        service.execute(workspace['id'], request)
    assert disabled.value.status_code == 503
    monkeypatch.setattr('knowledge.workspace.executor_status', lambda profile: {'available': True})
    monkeypatch.setattr('knowledge.workspace.executor_config', lambda profile: {'backend': 'docker', 'image': 'fixed:1', 'timeout_seconds': 30, 'profile': profile})
    def execute_and_modify(files, config, cancelled):
        # 另一个连接可以保存新快照，证明执行期间没有持有项目事务锁。
        current = service.detail(workspace['id'])
        service.snapshot(workspace['id'], SnapshotCreate(expected_version=current['workspace']['version'], actor='并发修改者', reason='产生新工程基线',
                         files=[{'path': 'test_new.py', 'content': 'changed'}]))
        return {'passed': True, 'tests_run': 2, 'returncode': 0}
    monkeypatch.setattr('knowledge.workspace.run_isolated', execute_and_modify)
    execution = service.execute(workspace['id'], request)
    assert execution['status'] == 'STALE' and not execution['result']['passed']
    assert execution['snapshot_sha256'] == detail['latest_snapshot']['sha256']


@pytest.mark.integration
def test_engineering_binding_requires_completed_design_in_same_project(db_url, project, entity):
    flow_id = uuid4()
    with connect(db_url) as conn:
        conn.execute('INSERT INTO design_flow(id,project_id,title,definition) VALUES(%s,%s,%s,%s)',
                     (flow_id, UUID(project['id']), '未确认设计', '{}'))
        conn.execute('''INSERT INTO design_flow_event(flow_id,version,status,action,actor,reason,document)
            VALUES(%s,1,'DRAFT','created','测试者','测试边界','{}')''', (flow_id,))
    service = WorkspaceService(db_url)
    with pytest.raises(KnowledgeError) as pending:
        service.create(UUID(project['id']), WorkspaceCreate(name='不能提前创建', actor='测试者', design_flow_id=flow_id))
    assert pending.value.status_code == 409
    other = entity(kind='Project', project_id=None, name='另一个项目')
    with pytest.raises(KnowledgeError) as foreign:
        service.create(UUID(other['id']), WorkspaceCreate(name='跨项目绑定', actor='测试者', design_flow_id=flow_id))
    assert foreign.value.status_code == 404
    with connect(db_url) as conn:
        conn.execute('''INSERT INTO design_flow_event(flow_id,version,status,action,actor,reason,document)
            VALUES(%s,2,'COMPLETED','confirmed','测试者','明确确认','{}')''', (flow_id,))
    workspace = service.create(UUID(project['id']), WorkspaceCreate(name='已确认设计工程', actor='测试者', design_flow_id=flow_id))
    assert workspace['design_flow_id'] == flow_id
