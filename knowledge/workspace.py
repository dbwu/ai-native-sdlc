"""不可变工程快照与固定隔离执行接口；默认不运行任何上传代码。"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Annotated, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import Field, StringConstraints, model_validator

from .db import connect
from .models import Text, Version
from .runtime_models import RuntimeInput
from .semantics import KnowledgeError
from .service import KnowledgeService as KS

MAX_FILES = 100
MAX_FILE_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024
LOG_LIMIT = 16384
REPORT_PREFIX = '__KNOWLEDGE_UNITTEST_RESULT_V1__'


class WorkspaceCreate(RuntimeInput):
    name: Annotated[str, Field(min_length=1, max_length=200)] = Field(title='工作区名称')
    actor: Text = Field(title='操作署名')
    design_flow_id: UUID | None = Field(default=None, title='已确认设计流程')


class WorkspaceFile(RuntimeInput):
    path: Annotated[str, Field(min_length=1, max_length=240)] = Field(title='工程相对路径')
    content: Annotated[str, StringConstraints(strip_whitespace=False), Field(max_length=MAX_FILE_BYTES)] = Field(title='文件文本，保留空白')


class SnapshotCreate(RuntimeInput):
    expected_version: Version = Field(title='预期工作区版本')
    actor: Text = Field(title='操作署名')
    reason: Text = Field(title='保存依据')
    files: list[WorkspaceFile] = Field(min_length=1, max_length=MAX_FILES, title='完整工程文件快照')

    @model_validator(mode='after')
    def valid_tree(self):
        normalized_files(self.files)
        return self


class ExecutionCreate(RuntimeInput):
    expected_version: Version = Field(title='预期工作区版本')
    snapshot_id: UUID = Field(title='待执行快照')
    profile: Literal['python_unittest'] = Field(default='python_unittest', title='服务端固定执行方式')
    actor: Text = Field(title='操作署名')
    reason: Text = Field(title='执行依据')


class ExecutionCancel(RuntimeInput):
    actor: Text = Field(title='操作署名')
    reason: Text = Field(title='取消依据')


def digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def normalized_files(files):
    """只接受文本与规范相对路径，不解释链接或读取宿主文件。"""
    entries, seen = [], set()
    total = 0
    for item in files:
        path, content = (item.path, item.content) if isinstance(item, WorkspaceFile) else (item['path'], item['content'])
        parts = path.split('/')
        if (path.startswith('/') or '\\' in path or ':' in path or any(ord(c) < 32 for c in path)
                or PurePosixPath(path).as_posix() != path or any(p in ('', '.', '..') for p in parts)
                or any(p.endswith(('.', ' ')) for p in parts)
                or any(re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p, re.I) for p in parts)):
            raise KnowledgeError('工程文件必须使用安全的相对路径，不能含绝对路径、上级目录、磁盘符或系统保留名称')
        try:
            size = len(content.encode('utf-8'))
        except UnicodeEncodeError as exc:
            raise KnowledgeError('工程文本必须是有效 UTF-8 字符') from exc
        if '\x00' in content or size > MAX_FILE_BYTES:
            raise KnowledgeError('工程文件必须是无 NUL 的文本，单文件最多256 KiB')
        folded = path.casefold()
        if folded in seen:
            raise KnowledgeError('同一快照不能包含重复或仅大小写不同的路径')
        seen.add(folded)
        total += size
        entries.append({'path': path, 'content': content, 'sha256': digest(content)})
    if not entries or len(entries) > MAX_FILES or total > MAX_SNAPSHOT_BYTES:
        raise KnowledgeError('快照需含1至100个文件，总文本最多1 MiB')
    if any('/'.join(path.split('/')[:i]) in seen for path in seen for i in range(1, len(path.split('/')))):
        raise KnowledgeError('工程文件路径不能同时作为另一个文件的目录')
    return sorted(entries, key=lambda row: row['path'])


def snapshot_digest(files):
    return digest(json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


def executor_config():
    backend = os.environ.get('KNOWLEDGE_EXECUTOR', 'disabled')
    image = os.environ.get('KNOWLEDGE_EXECUTOR_IMAGE', '')
    if backend not in ('disabled', 'docker'):
        raise KnowledgeError('KNOWLEDGE_EXECUTOR 仅支持 disabled 或 docker', 503)
    if image and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}', image):
        raise KnowledgeError('隔离执行镜像名称无效', 503)
    try:
        timeout = int(os.environ.get('KNOWLEDGE_EXECUTOR_TIMEOUT_SECONDS', '30'))
        if not 1 <= timeout <= 120:
            raise ValueError()
    except ValueError as exc:
        raise KnowledgeError('隔离执行超时必须为1至120秒', 503) from exc
    return {'backend': backend, 'image': image, 'timeout_seconds': timeout,
            'docker': shutil.which('docker'), 'profile': 'python_unittest'}


def executor_status():
    config = executor_config()
    configured = config['backend'] == 'docker' and bool(config['image'])
    available = False
    message = '隔离执行器尚未配置；不会在平台宿主直接执行工程代码。'
    if configured and not config['docker']:
        message = '已配置 Docker 镜像，但本机未找到 Docker 命令；执行接口暂不可用。'
    elif configured:
        try:
            check = subprocess.run([config['docker'], 'info', '--format', '{{.ServerVersion}}'],
                                   capture_output=True, timeout=3, shell=False,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            available = check.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
        message = '隔离执行器可用；仅运行固定 Python unittest 方式，镜像必须已在本机。' if available else 'Docker 服务未就绪，执行接口暂不可用；不会降级到宿主执行。'
    return {k: config[k] for k in ('backend', 'image', 'profile', 'timeout_seconds')} | {
        'configured': configured, 'available': available, 'message': message}


# 固定的外层测试入口从 unittest 的结果对象读取数量，不根据工程打印的“通过”文字判断。
RUNNER = '''import json,sys,unittest
sys.path.append('/workspace')
suite=unittest.defaultTestLoader.discover('/workspace',pattern='test*.py',top_level_dir='/workspace')
result=unittest.TextTestRunner(verbosity=2).run(suite)
report={'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'successful':result.wasSuccessful()}
print('__KNOWLEDGE_UNITTEST_RESULT_V1__'+json.dumps(report),flush=True)
sys.exit(0 if report['successful'] and report['tests_run']>0 else 1)
'''


def docker_command(config, directory, name, image_id, nonce=''):
    runner = RUNNER.replace(REPORT_PREFIX, REPORT_PREFIX + nonce + '_') if nonce else RUNNER
    return [config['docker'], 'run', '--rm', '--pull=never', '--name', name,
            '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--user=65534:65534', '--pids-limit=64', '--cpus=1', '--memory=256m', '--memory-swap=256m',
            '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m',
            '--mount', f'type=bind,source={directory},target=/workspace,readonly',
            '--workdir=/workspace', '--entrypoint=python', image_id, '-I', '-B', '-c', runner]


def _remove_container(config, name):
    try:
        cleanup = subprocess.run([config['docker'], 'rm', '--force', name], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, shell=False,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return cleanup.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class _BoundedCapture:
    def __init__(self):
        self.head, self.tail, self.size = bytearray(), bytearray(), 0

    def drain(self, stream):
        while chunk := stream.read(4096):
            self.size += len(chunk)
            self.head.extend(chunk[:max(0, LOG_LIMIT - len(self.head))])
            self.tail.extend(chunk)
            if len(self.tail) > LOG_LIMIT * 2:
                del self.tail[:-LOG_LIMIT * 2]


def run_isolated(files, config, cancelled=lambda: False):
    """无知识库事务地运行固定容器；只读工程、无网络、限资源且始终清理容器。"""
    started = time.monotonic()
    result = {'passed': False, 'tests_run': 0, 'returncode': None, 'stdout': '', 'stderr': '',
              'error': '', 'output_truncated': False, 'cancelled': False, 'timed_out': False,
              'duration_ms': 0, 'image_id': None}
    name = 'knowledge-engineering-' + uuid4().hex
    nonce = uuid4().hex
    report_prefix = REPORT_PREFIX + nonce + '_'
    process = None
    readers = []
    try:
        image = subprocess.run([config['docker'], 'image', 'inspect', '--format', '{{.Id}}', config['image']],
                               capture_output=True, timeout=5, shell=False,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        image_id = image.stdout.decode('utf-8', errors='replace').strip()
        if image.returncode != 0 or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
            result['error'] = '执行镜像未在本机准备就绪；系统不会自动拉取镜像。'
            return result
        result['image_id'] = image_id
        with tempfile.TemporaryDirectory(prefix='knowledge-engineering-') as directory:
            root = Path(directory).resolve()
            # Linux 临时目录默认0700；容器以非root用户读只读副本，需要显式设置副本读取权限。
            root.chmod(0o755)
            for item in normalized_files(files):
                target = root.joinpath(*item['path'].split('/'))
                if not target.resolve().is_relative_to(root):
                    raise KnowledgeError('工程路径超出隔离工作区')
                target.parent.mkdir(parents=True, exist_ok=True)
                parent = target.parent
                while parent != root:
                    parent.chmod(0o755)
                    parent = parent.parent
                target.write_text(item['content'], encoding='utf-8', newline='')
                target.chmod(0o644)
            command = docker_command(config, directory, name, image_id, nonce)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, shell=False,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            out, err = _BoundedCapture(), _BoundedCapture()
            readers = [threading.Thread(target=out.drain, args=(process.stdout,), daemon=True),
                       threading.Thread(target=err.drain, args=(process.stderr,), daemon=True)]
            for reader in readers:
                reader.start()
            while process.poll() is None:
                if cancelled():
                    result.update(cancelled=True, error='执行已取消；未生成通过结果。')
                    break
                if time.monotonic() - started >= config['timeout_seconds']:
                    result.update(timed_out=True, error='隔离执行超过时间限制，已终止。')
                    break
                threading.Event().wait(0.1)
            if process.poll() is None:
                _remove_container(config, name)
                process.kill()
            process.wait(timeout=3)
            # 在删除临时绑定目录前清理容器；finally 还会再次尝试清理。
            _remove_container(config, name)
            for reader in readers:
                reader.join(timeout=2)
            result.update(returncode=process.returncode, stdout=out.head.decode('utf-8', errors='replace'),
                          stderr=err.head.decode('utf-8', errors='replace'),
                          output_truncated=out.size > LOG_LIMIT or err.size > LOG_LIMIT)
            reports = [line.removeprefix(report_prefix) for line in out.tail.decode('utf-8', errors='replace').splitlines()
                       if line.startswith(report_prefix)]
            if reports:
                try:
                    report = json.loads(reports[-1])
                    count = report['tests_run']
                    valid = (type(count) is int and 0 < count < 100000 and report['successful'] is True
                             and type(report['failures']) is int and report['failures'] == 0
                             and type(report['errors']) is int and report['errors'] == 0
                             and type(report['skipped']) is int and 0 <= report['skipped'] < count)
                    result['tests_run'] = count if type(count) is int and count >= 0 else 0
                    result['passed'] = bool(valid and process.returncode == 0 and not result['cancelled'] and not result['timed_out'])
                except (ValueError, KeyError, TypeError):
                    pass
            if not result['passed'] and not result['error']:
                result['error'] = '测试未通过、未发现实际测试或执行报告不完整，请查看日志。'
    except (OSError, subprocess.SubprocessError, KnowledgeError) as exc:
        result['error'] = '隔离执行未能完成：' + type(exc).__name__
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        _remove_container(config, name)
        result['duration_ms'] = round((time.monotonic() - started) * 1000)
    return result


class WorkspaceService:
    def __init__(self, database_url):
        self.database_url = database_url

    @staticmethod
    def _workspace(conn, workspace_id):
        row = conn.execute('SELECT * FROM current_engineering_workspaces WHERE id=%s', (workspace_id,)).fetchone()
        if row is None:
            raise KnowledgeError('未找到工程工作区', 404)
        return row

    @staticmethod
    def _execution(conn, execution_id):
        row = conn.execute('SELECT * FROM current_engineering_executions WHERE id=%s', (execution_id,)).fetchone()
        if row is None:
            raise KnowledgeError('未找到隔离执行记录', 404)
        return row

    @staticmethod
    def _event(conn, workspace, action, actor, reason):
        conn.execute('INSERT INTO engineering_workspace_event(workspace_id,version,action,actor,reason) VALUES(%s,%s,%s,%s,%s)',
                     (workspace['id'], workspace.get('version', 0) + 1, action, actor, reason))

    def create(self, project_id, request):
        with connect(self.database_url) as conn:
            KS._lock(conn, project_id)
            KS._assert_project(conn, project_id)
            if request.design_flow_id:
                flow = conn.execute('SELECT project_id,status FROM current_design_flows WHERE id=%s', (request.design_flow_id,)).fetchone()
                if flow is None or flow['project_id'] != project_id:
                    raise KnowledgeError('设计流程不属于当前项目', 404)
                if flow['status'] != 'COMPLETED':
                    raise KnowledgeError('请先明确确认设计流程，再建立关联工程工作区', 409)
            workspace_id = uuid4()
            conn.execute('INSERT INTO engineering_workspace(id,project_id,design_flow_id,name,actor) VALUES(%s,%s,%s,%s,%s)',
                         (workspace_id, project_id, request.design_flow_id, request.name, request.actor))
            self._event(conn, {'id': workspace_id}, 'created', request.actor, '明确建立工程工作区；尚未生成或执行代码')
            return self._workspace(conn, workspace_id)

    def list(self, project_id):
        with connect(self.database_url) as conn:
            project = KS._get(conn, 'entity', project_id)
            if project['kind'] != 'Project':
                raise KnowledgeError('未找到项目', 404)
            return conn.execute('SELECT * FROM current_engineering_workspaces WHERE project_id=%s ORDER BY created_at DESC,id LIMIT 200', (project_id,)).fetchall()

    def _detail(self, conn, workspace_id):
        workspace = self._workspace(conn, workspace_id)
        snapshot = conn.execute('SELECT * FROM engineering_snapshot WHERE workspace_id=%s ORDER BY version DESC LIMIT 1', (workspace_id,)).fetchone()
        executions = conn.execute('SELECT * FROM current_engineering_executions WHERE workspace_id=%s ORDER BY created_at DESC,id LIMIT 100', (workspace_id,)).fetchall()
        return {'workspace': workspace, 'latest_snapshot': snapshot, 'executions': executions}

    def detail(self, workspace_id):
        with connect(self.database_url) as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            return self._detail(conn, workspace_id)

    def snapshot(self, workspace_id, request):
        files = normalized_files(request.files)
        with connect(self.database_url) as conn:
            workspace = self._workspace(conn, workspace_id)
            KS._lock(conn, workspace['project_id'])
            KS._assert_project(conn, workspace['project_id'])
            workspace = self._workspace(conn, workspace_id)
            if workspace['version'] != request.expected_version:
                raise KnowledgeError('工作区版本已变化，请刷新后重新审阅快照', 409)
            latest = conn.execute('SELECT coalesce(max(version),0)+1 AS v FROM engineering_snapshot WHERE workspace_id=%s', (workspace_id,)).fetchone()['v']
            self._event(conn, workspace, 'snapshot_saved', request.actor, request.reason)
            conn.execute('''INSERT INTO engineering_snapshot(id,workspace_id,version,workspace_version,sha256,files,actor,reason)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
                         (uuid4(), workspace_id, latest, workspace['version'] + 1, snapshot_digest(files), Jsonb(files), request.actor, request.reason))
            return self._detail(conn, workspace_id)

    def execute(self, workspace_id, request):
        status = executor_status()
        if not status['available']:
            raise KnowledgeError(status['message'], 503)
        config = executor_config()
        with connect(self.database_url) as conn:
            workspace = self._workspace(conn, workspace_id)
            KS._lock(conn, workspace['project_id'])
            KS._assert_project(conn, workspace['project_id'])
            workspace = self._workspace(conn, workspace_id)
            if workspace['version'] != request.expected_version:
                raise KnowledgeError('工作区版本已变化，请刷新后重新执行', 409)
            snapshot = conn.execute('SELECT * FROM engineering_snapshot WHERE workspace_id=%s ORDER BY version DESC LIMIT 1', (workspace_id,)).fetchone()
            if snapshot is None or snapshot['id'] != request.snapshot_id:
                raise KnowledgeError('仅能执行当前工作区的最新明确快照', 409)
            execution_id = uuid4()
            version = workspace['version'] + 1
            conn.execute('''INSERT INTO engineering_execution(id,workspace_id,snapshot_id,snapshot_sha256,workspace_version,profile,actor,reason,environment)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                         (execution_id, workspace_id, snapshot['id'], snapshot['sha256'], version, request.profile, request.actor, request.reason,
                          Jsonb({key: config[key] for key in ('backend', 'image', 'timeout_seconds', 'profile')})))
            self._event(conn, workspace, 'execution_started', request.actor, request.reason)
            conn.execute('''INSERT INTO engineering_execution_event(execution_id,version,status,actor,reason,result)
                VALUES(%s,1,'RUNNING',%s,%s,%s)''',
                         (execution_id, request.actor, request.reason, Jsonb({'passed': False, 'tests_run': 0})))
        # 容器执行时不持有连接、项目锁或数据库事务。
        def cancelled():
            with connect(self.database_url) as conn:
                return self._execution(conn, execution_id)['status'] == 'CANCELLED'
        result = run_isolated(snapshot['files'], config, cancelled)
        with connect(self.database_url) as conn:
            KS._lock(conn, workspace['project_id'])
            current = self._execution(conn, execution_id)
            if current['status'] == 'CANCELLED':
                return current
            current_workspace = self._workspace(conn, workspace_id)
            if current_workspace['version'] != version:
                result.update(passed=False, error='执行期间工程快照或版本已变化，结果仅保留供追溯，不能证明当前代码通过。')
                final_status = 'STALE'
            elif result.get('cancelled'):
                final_status = 'CANCELLED'
            elif result.get('timed_out'):
                final_status = 'TIMED_OUT'
            else:
                final_status = 'SUCCEEDED' if result['passed'] else 'FAILED'
            conn.execute('''INSERT INTO engineering_execution_event(execution_id,version,status,actor,reason,result)
                VALUES(%s,%s,%s,%s,%s,%s)''',
                         (execution_id, current['version'] + 1, final_status, request.actor, '隔离执行完成，保存实际结果及快照摘要', Jsonb(result)))
            return self._execution(conn, execution_id)

    def cancel(self, execution_id, request):
        with connect(self.database_url) as conn:
            execution = self._execution(conn, execution_id)
            workspace = self._workspace(conn, execution['workspace_id'])
            KS._lock(conn, workspace['project_id'])
            KS._assert_project(conn, workspace['project_id'])
            execution = self._execution(conn, execution_id)
            if execution['status'] != 'RUNNING':
                raise KnowledgeError('只能取消运行中的隔离执行', 409)
            conn.execute('''INSERT INTO engineering_execution_event(execution_id,version,status,actor,reason,result)
                VALUES(%s,%s,'CANCELLED',%s,%s,%s)''',
                         (execution_id, execution['version'] + 1, request.actor, request.reason,
                          Jsonb({'passed': False, 'tests_run': 0, 'error': '已请求取消，执行器将终止并清理隔离容器。'})))
            return self._execution(conn, execution_id)
