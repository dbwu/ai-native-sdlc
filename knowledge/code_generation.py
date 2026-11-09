"""固定已确认设计与工程基线，异步生成代码候选，人工审阅后原子保存。"""
from datetime import datetime, timedelta, timezone
import logging
from pathlib import PurePosixPath
import re
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import Field, ValidationError

from . import model_gateway
from .db import connect
from .design import DesignService, generation_recovery_seconds
from .models import Text, Version
from .runtime_models import RuntimeInput
from .semantics import KnowledgeError
from .service import KnowledgeService as KS
from .workspace import MAX_FILES, WorkspaceFile, WorkspaceLanguage, WorkspaceService, normalized_files


SOURCE_SUFFIXES = {
    'python': ('.py',),
    'c': ('.c', '.h'),
    'cpp': ('.cpp', '.cc', '.cxx', '.h', '.hpp', '.hh', '.hxx'),
    'java': ('.java',),
}


class CodeGenerationAction(RuntimeInput):
    expected_version: Version = Field(title='预期工作区版本')
    actor: Text = Field(title='操作署名')
    reason: Text = Field(title='生成或审阅依据')


class CodeCandidate(RuntimeInput):
    summary: Text = Field(title='候选代码摘要')
    files: list[WorkspaceFile] = Field(min_length=1, max_length=MAX_FILES, title='新增或修改的完整源文件')


def candidate_changes(candidate, base_files, language: WorkspaceLanguage = 'python'):
    original = {item['path']: item for item in base_files}
    merged = original.copy()
    changes = []
    suffixes = SOURCE_SUFFIXES[language]
    for file in normalized_files(candidate.files):
        path, content = file['path'], file['content']
        if PurePosixPath(path).suffix not in suffixes:
            raise KnowledgeError('当前工作区只允许生成以下扩展名的源文件：' + '、'.join(suffixes))
        previous = original.get(path)
        before = previous['content'] if previous else None
        if before == content:
            continue
        changes.append({'path': path, 'operation': 'add' if previous is None else 'modify',
                        'before': before, 'after': content})
        merged[path] = file
    if not changes:
        raise KnowledgeError('模型没有返回实际代码变更')
    files = normalized_files(merged.values())
    if language != 'python':
        return changes, files
    for file in files:
        parts = file['path'].split('/')
        if (re.fullmatch(r'test\w*\.py', parts[-1])
                and all('/'.join(parts[:i]) + '/__init__.py' in merged for i in range(1, len(parts)))):
            return changes, files
    raise KnowledgeError('候选工程需包含可被 unittest 发现的 test*.py 文件')


class CodeGenerationService(WorkspaceService):
    @staticmethod
    def _generation(conn, workspace_id, generation_id):
        job = conn.execute('SELECT * FROM current_engineering_code_generations WHERE workspace_id=%s AND id=%s',
                           (workspace_id, generation_id)).fetchone()
        if job is None:
            raise KnowledgeError('未找到当前工作区的代码生成记录', 404)
        return job

    @staticmethod
    def _view(job, *, detail=False):
        data = {key: job[key] for key in ('id', 'status', 'created_at', 'base_snapshot_id', 'applied_snapshot_id',
                                         'design_flow_id', 'design_version', 'actor', 'reason', 'updated_at')}
        data.update(summary=job['result'].get('summary'), diagnostic=job['result']['diagnostic'])
        if detail:
            data.update(changes=job['result'].get('changes', []), trace=job['result'].get('trace'))
        return data

    @staticmethod
    def _generation_event(conn, job, status, result, *, actor=None, reason=None, snapshot_id=None, recovery_seconds=None):
        deadline = None if recovery_seconds is None else datetime.now(timezone.utc) + timedelta(seconds=recovery_seconds)
        conn.execute('''INSERT INTO engineering_code_generation_event
            (generation_id,workspace_id,version,status,applied_snapshot_id,deadline_at,actor,reason,result)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (job['id'], job['workspace_id'], job.get('version', 0)+1, status, snapshot_id, deadline,
             actor or job['actor'], reason or result['diagnostic'], Jsonb(result)))

    def _lock_workspace(self, conn, workspace_id):
        workspace = self._workspace(conn, workspace_id)
        KS._lock(conn, workspace['project_id'])
        return self._workspace(conn, workspace_id)

    def _basis(self, conn, workspace, job=None):
        KS._assert_project(conn, workspace['project_id'])
        if workspace['design_flow_id'] is None:
            raise KnowledgeError('请先使用已确认设计创建关联的工程工作区', 409)
        flow = DesignService._get(conn, workspace['design_flow_id'])
        if (flow['project_id'] != workspace['project_id'] or flow['status'] != 'COMPLETED'
                or not flow['state'].get('design') or not flow['state'].get('artifact_versions')):
            raise KnowledgeError('当前工作区需要关联同项目的已确认设计', 409)
        DesignService._fresh(conn, flow)
        for ref in flow['state']['artifact_versions']:
            record = KS._get(conn, ref['table'], UUID(ref['id']))
            if record['version'] != ref['version'] or record['status'] == 'archived':
                raise KnowledgeError('已确认的设计产物已变化，请基于最新设计建立关联工作区', 409)
        snapshot = self._latest_snapshot(conn, workspace['id'])
        if job and (flow['id'] != job['design_flow_id'] or flow['version'] != job['design_version']
                    or (snapshot['id'] if snapshot else None) != job['base_snapshot_id']):
            raise KnowledgeError('已确认设计或已保存的工程快照已变化，请基于当前版本重新生成代码', 409)
        return flow, snapshot

    @staticmethod
    def _context(conn, flow, snapshot, reason, language):
        requirements = [KS._get(conn, 'entity', UUID(ref['id']), ref['version'])
                        for ref in flow['definition']['requirements']]
        return jsonable_encoder({
            'design': flow['state']['design'], 'requirements': requirements,
            'design_artifacts': flow['state']['artifact_versions'],
            'workspace_files': [{'path': item['path'], 'content': item['content']} for item in snapshot['files']] if snapshot else [],
            'generation_guidance': reason, 'language': language,
            **({'profile': 'python_unittest'} if language == 'python' else {}),
        })

    def generations(self, workspace_id):
        with connect(self.database_url) as conn:
            self._workspace(conn, workspace_id)
            rows = conn.execute('''SELECT id,status,created_at,base_snapshot_id,applied_snapshot_id,design_flow_id,
                design_version,actor,reason,updated_at,jsonb_build_object('summary',result->'summary',
                'diagnostic',result->'diagnostic') AS result FROM current_engineering_code_generations
                WHERE workspace_id=%s ORDER BY created_at DESC,id LIMIT 20''', (workspace_id,)).fetchall()
            return [self._view(row) for row in rows]

    def generation(self, workspace_id, generation_id):
        with connect(self.database_url) as conn:
            return self._view(self._generation(conn, workspace_id, generation_id), detail=True)

    @staticmethod
    def _check_version(workspace, request):
        if workspace['version'] != request.expected_version:
            raise KnowledgeError('工作区版本已变化，请刷新后重新审阅', 409)

    def enqueue(self, workspace_id, request):
        with connect(self.database_url) as conn:
            workspace = self._lock_workspace(conn, workspace_id)
            self._check_version(workspace, request)
            flow, snapshot = self._basis(conn, workspace)
            model_gateway.ensure_model_configured()
            if conn.execute("""SELECT 1 FROM current_engineering_code_generations
                WHERE workspace_id=%s AND status IN ('QUEUED','RUNNING') LIMIT 1""", (workspace_id,)).fetchone():
                raise KnowledgeError('此工作区已有代码生成任务正在处理', 409)
            job_id = uuid4()
            conn.execute('''INSERT INTO engineering_code_generation
                (id,workspace_id,base_snapshot_id,design_flow_id,design_version,actor,reason,request)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''', (job_id, workspace_id, snapshot['id'] if snapshot else None,
                flow['id'], flow['version'], request.actor, request.reason, Jsonb(model_gateway.model_config_snapshot())))
            self._generation_event(conn, {'id': job_id, 'workspace_id': workspace_id, 'actor': request.actor},
                                   'QUEUED', {'diagnostic': '等待后台模型生成代码'}, reason=request.reason)
            return self._view(self._generation(conn, workspace_id, job_id))

    def _check_retry(self, workspace_id, job_id):
        with connect(self.database_url) as conn:
            workspace = self._lock_workspace(conn, workspace_id)
            job = self._generation(conn, workspace_id, job_id)
            if job['status'] != 'RUNNING':
                raise KnowledgeError('代码生成任务已停止', 409)
            self._basis(conn, workspace, job)

    def run_job(self, workspace_id, job_id):
        with connect(self.database_url) as conn:
            workspace = self._lock_workspace(conn, workspace_id)
            job = self._generation(conn, workspace_id, job_id)
            if job['status'] != 'QUEUED':
                return
            try:
                flow, snapshot = self._basis(conn, workspace, job)
                language = workspace['language']
                context = self._context(conn, flow, snapshot, job['reason'], language)
            except KnowledgeError as exc:
                self._generation_event(conn, job, 'STALE', {'diagnostic': exc.message})
                return
            timeout = job['request']['public']['timeout_seconds']
            self._generation_event(conn, job, 'RUNNING', {'diagnostic': '模型正在生成代码候选'},
                                   recovery_seconds=generation_recovery_seconds(timeout))
        try:
            result = model_gateway.complete_code(context, CodeCandidate.model_json_schema(), language=language,
                expected_fingerprint=job['request']['fingerprint'], before_retry=lambda: self._check_retry(workspace_id, job_id))
            candidate = CodeCandidate.model_validate(result['document'])
            changes, _ = candidate_changes(candidate, snapshot['files'] if snapshot else [], language)
            with connect(self.database_url) as conn:
                workspace = self._lock_workspace(conn, workspace_id)
                job = self._generation(conn, workspace_id, job_id)
                if job['status'] != 'RUNNING':
                    return
                self._basis(conn, workspace, job)
                self._generation_event(conn, job, 'REVIEW_PENDING', {'diagnostic': '代码候选已生成，等待人工审阅',
                    'summary': candidate.summary, 'changes': changes, 'trace': result['trace']})
        except Exception as exc:
            if isinstance(exc, KnowledgeError):
                message = exc.message
            elif isinstance(exc, ValidationError):
                message = '模型代码候选不符合文件结构，请调整生成依据后重新生成'
            else:
                message = '代码生成任务处理失败，请检查服务配置或后台日志后重新生成'
                logging.getLogger(__name__).warning('代码任务失败：%s', type(exc).__name__)
            with connect(self.database_url) as conn:
                self._lock_workspace(conn, workspace_id)
                job = self._generation(conn, workspace_id, job_id)
                if job['status'] == 'RUNNING':
                    status = 'STALE' if isinstance(exc, KnowledgeError) and exc.status_code == 409 else 'FAILED'
                    self._generation_event(conn, job, status, {'diagnostic': message})

    def apply(self, workspace_id, job_id, request):
        with connect(self.database_url) as conn:
            workspace = self._lock_workspace(conn, workspace_id)
            self._check_version(workspace, request)
            job = self._generation(conn, workspace_id, job_id)
            if job['status'] != 'REVIEW_PENDING':
                raise KnowledgeError('只有待审阅的代码候选可以保存为工程快照', 409)
            _, snapshot = self._basis(conn, workspace, job)
            candidate = CodeCandidate(summary=job['result']['summary'], files=[
                {'path': change['path'], 'content': change['after']} for change in job['result']['changes']])
            language = workspace['language']
            _, files = candidate_changes(candidate, snapshot['files'] if snapshot else [], language)
            snapshot_id = self._save_snapshot(conn, workspace, files, request.actor, request.reason, action='code_generation_applied')
            result = job['result'] | {'diagnostic': '人工确认后已保存为工程快照，等待执行测试'}
            self._generation_event(conn, job, 'APPLIED', result, actor=request.actor, reason=request.reason, snapshot_id=snapshot_id)
            return self._detail(conn, workspace_id)

    def recover_expired(self):
        with connect(self.database_url) as conn:
            expired = conn.execute("""SELECT workspace_id,id FROM current_engineering_code_generations
                WHERE status='RUNNING' AND deadline_at <= clock_timestamp() LIMIT 20""").fetchall()
        for row in expired:
            with connect(self.database_url) as conn:
                self._lock_workspace(conn, row['workspace_id'])
                job = self._generation(conn, row['workspace_id'], row['id'])
                if job['status'] == 'RUNNING':
                    self._generation_event(conn, job, 'FAILED', {'diagnostic': '后台代码生成任务中断或超过恢复时限，请重新生成'})

    def run_next(self):
        self.recover_expired()
        with connect(self.database_url) as conn:
            row = conn.execute("""SELECT workspace_id,id FROM current_engineering_code_generations
                WHERE status='QUEUED' ORDER BY created_at,id LIMIT 1""").fetchone()
        if row is None:
            return False
        self.run_job(row['workspace_id'], row['id'])
        return True
