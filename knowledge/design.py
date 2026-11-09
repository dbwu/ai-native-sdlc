"""固定需求版本、异步生成设计、显式审阅；不生成代码或测试通过证据。"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from uuid import UUID, uuid4, uuid5

from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from .db import connect
from .design_models import AnswerSubmission, ConfirmedScopeDesign, DesignAction, DesignDocument, DesignFlowCreate
from . import model_gateway
from .models import EntityCreate, RelationCreate
from .operations import KnowledgeOperations
from .operations_models import ProposalReview
from .semantics import KnowledgeError
from .service import KnowledgeService as KS

EXTRA_KINDS = {'Domain', 'BusinessRule', 'Constraint', 'Decision', 'ArchitectureComponent', 'Interface', 'DataModel'}
# 早期任务可能没有保存时限，沿用原来的十分钟恢复规则。
GENERATION_EXPIRED = "COALESCE((result->>'deadline_at')::timestamptz, updated_at + interval '10 minutes') <= clock_timestamp()"


def canonical(value):
    return json.dumps(jsonable_encoder(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha256(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def generation_recovery_seconds(timeout):
    attempts = model_gateway.MAX_MODEL_ATTEMPTS
    return max(600, attempts * timeout + (attempts - 1) * model_gateway.RETRY_DELAY_SECONDS + 60)


class DesignService:
    def __init__(self, database_url):
        self.database_url = database_url

    @staticmethod
    def _get(conn, flow_id):
        flow = conn.execute('SELECT * FROM current_design_flows WHERE id=%s', (flow_id,)).fetchone()
        if flow is None:
            raise KnowledgeError('未找到需求设计流程', 404)
        return flow

    @staticmethod
    def _job(conn, job_id):
        row = conn.execute('SELECT * FROM current_design_generations WHERE id=%s', (job_id,)).fetchone()
        if row is None:
            raise KnowledgeError('未找到模型生成任务', 404)
        return row

    @staticmethod
    def _event(conn, flow, status, action, actor, reason, state):
        conn.execute('''INSERT INTO design_flow_event(flow_id,version,status,action,actor,reason,document)
            VALUES (%s,%s,%s,%s,%s,%s,%s)''',
            (flow['id'], flow.get('version', 0)+1, status, action, actor, reason, Jsonb(state)))

    @staticmethod
    def _job_event(conn, job, status, result):
        conn.execute('INSERT INTO design_generation_event(job_id,version,status,document) VALUES (%s,%s,%s,%s)',
                     (job['id'], job.get('version', 0)+1, status, Jsonb(result)))

    @staticmethod
    def _lock(conn, flow_id, expected=None, *, writable=True):
        flow = DesignService._get(conn, flow_id)
        KS._lock(conn, flow['project_id'])
        if writable:
            KS._assert_project(conn, flow['project_id'])
        flow = DesignService._get(conn, flow_id)
        if expected is not None and expected != flow['version']:
            raise KnowledgeError('设计流程版本已变化，请刷新后重新审阅；本次操作没有自动重试', 409)
        return flow

    @staticmethod
    def _fresh(conn, flow):
        for baseline in flow['state']['baselines']:
            record = KS._get(conn, 'entity', UUID(baseline['id']))
            if record['version'] != baseline['version'] or record['status'] == 'archived':
                raise KnowledgeError('需求或设计依据已有新版本，请基于最新知识新建设计流程', 409)

    @staticmethod
    def _proposal(conn, state):
        return KnowledgeOperations._proposal(conn, UUID(state['design_proposal_id'])) if state.get('design_proposal_id') else None

    def create(self, project_id, request: DesignFlowCreate):
        with connect(self.database_url) as conn:
            KS._lock(conn, project_id)
            KS._assert_project(conn, project_id)
            seen, baselines = set(), []
            for required, refs in ((True, request.requirements), (False, request.input_knowledge)):
                for ref in refs:
                    record = KS._get(conn, 'entity', ref.id)
                    if ref.id in seen:
                        raise KnowledgeError('需求和设计依据不能重复选择同一知识')
                    seen.add(ref.id)
                    if record['project_id'] != project_id:
                        raise KnowledgeError('需求与设计依据必须属于当前项目')
                    if record['version'] != ref.version or record['status'] == 'archived':
                        raise KnowledgeError('所选知识版本已变化或归档，请刷新后重新选择', 409)
                    if required and (record['kind'] != 'Requirement' or not record['attributes'].get('acceptance_criteria')):
                        raise KnowledgeError('至少选择一条具有验收条件的需求；无需预先创建设计、代码或测试')
                    if not required and record['kind'] not in EXTRA_KINDS:
                        raise KnowledgeError('额外设计依据需为领域、规则、约束、决策或已有设计')
                    baselines.append({'table': 'entity', 'id': str(ref.id), 'version': ref.version})
            flow_id = uuid4()
            definition = request.model_dump(mode='json')
            conn.execute('INSERT INTO design_flow(id,project_id,title,definition) VALUES (%s,%s,%s,%s)',
                         (flow_id, project_id, request.title, Jsonb(definition)))
            state = {'baselines': baselines, 'completed_stages': [], 'design_proposal_id': None,
                     'active_job_id': None, 'questions': [], 'answers': [], 'assumptions': [],
                     'design': None, 'generation_count': 0, 'last_error': None}
            self._event(conn, {'id': flow_id}, 'DRAFT', 'created', request.actor, '仅固定需求与设计依据，等待确认启动', state)
            return self._get(conn, flow_id)

    def list(self, project_id, limit=100):
        with connect(self.database_url) as conn:
            project = KS._get(conn, 'entity', project_id)
            if project['kind'] != 'Project':
                raise KnowledgeError('未找到项目', 404)
            return conn.execute('SELECT * FROM current_design_flows WHERE project_id=%s ORDER BY created_at DESC,id LIMIT %s',
                                (project_id, limit)).fetchall()

    def snapshot(self, flow_id):
        with connect(self.database_url) as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            flow = self._get(conn, flow_id)
            state, definition = flow['state'], flow['definition']
            def records(refs):
                return [KS._get(conn, 'entity', UUID(ref['id']), ref['version']) for ref in refs]
            jobs = self._generation_history(conn, flow_id, state['answers'])
            history = conn.execute('SELECT * FROM design_flow_event WHERE flow_id=%s ORDER BY version DESC LIMIT 100', (flow_id,)).fetchall()
            return {'flow': flow, 'requirements': records(definition['requirements']), 'context': records(definition['input_knowledge']),
                    'design': state['design'], 'questions': state['questions'], 'answers': state['answers'],
                    'assumptions': state['assumptions'], 'proposal': self._proposal(conn, state),
                    'jobs': jobs, 'history': history, 'model_config': model_gateway.public_model_config()}

    @staticmethod
    def _generation_history(conn, flow_id, answers):
        jobs = conn.execute('''SELECT id,flow_id,version,status,actor,reason,created_at,updated_at,finished_at,
            diagnostic,trace,result - 'model_exchange' AS result,
            COALESCE(jsonb_typeof(result->'model_exchange') = 'object', false) AS has_model_exchange,
            COALESCE(request->>'generation_mode','standard') AS generation_mode
            FROM current_design_generations WHERE flow_id=%s ORDER BY created_at DESC,id LIMIT 20''', (flow_id,)).fetchall()
        answers_by_job = defaultdict(list)
        for answer in answers:
            answers_by_job[answer['job_id']].append(answer)
        legacy_ids = [str(job['id']) for job in jobs if job['status'] == 'SUCCEEDED' and 'questions' not in job['result']]
        legacy_questions = {}
        if legacy_ids:
            # 旧任务的原始问题在当时的不可变流程版本中，包括当时尚未回答的问题。
            rows = conn.execute("""SELECT document->>'active_job_id' AS job_id, document->'questions' AS questions
                FROM design_flow_event WHERE flow_id=%s AND action='generated' AND status='NEEDS_CLARIFICATION'
                AND document->>'active_job_id' = ANY(%s::text[])""", (flow_id, legacy_ids)).fetchall()
            legacy_questions = {row['job_id']: row['questions'] for row in rows}
        for job in jobs:
            job_id = str(job['id'])
            job['questions'] = job['result'].get('questions', legacy_questions.get(job_id, []))
            job['answers'] = answers_by_job[job_id]
        return jobs

    def model_exchange(self, flow_id, job_id):
        with connect(self.database_url) as conn:
            row = conn.execute("""SELECT result->'model_exchange' AS exchange FROM current_design_generations
                WHERE flow_id=%s AND id=%s""", (flow_id, job_id)).fetchone()
            if row is None:
                raise KnowledgeError('未找到当前设计流程的模型生成记录', 404)
            if row['exchange'] is None:
                raise KnowledgeError('此生成任务没有可查看的模型请求与回复记录', 404)
            return row['exchange']

    def action(self, flow_id, action, request: DesignAction):
        with connect(self.database_url) as conn:
            flow = self._lock(conn, flow_id, request.expected_version)
            state, status = deepcopy(flow['state']), flow['status']
            if status in {'COMPLETED', 'CANCELLED'}:
                raise KnowledgeError('设计流程已结束，只能查看其历史', 409)
            proposal = self._proposal(conn, state)
            if action == 'cancel':
                if state['active_job_id']:
                    job = self._job(conn, UUID(state['active_job_id']))
                    if job['status'] in {'QUEUED', 'RUNNING'}:
                        self._job_event(conn, job, 'CANCELLED', {'diagnostic': '流程已取消，返回内容不会发布为设计提案'})
                if proposal and proposal['status'] in {'pending', 'validated'}:
                    KnowledgeOperations(self.database_url, connection=conn).review(proposal['id'],
                        ProposalReview(expected_version=proposal['version'], actor=request.actor, reason=request.reason), 'reject')
                state['active_job_id'] = None
                status = 'CANCELLED'
            elif action == 'start':
                if status != 'DRAFT':
                    raise KnowledgeError('只有草稿设计流程可以确认需求并启动', 409)
                self._fresh(conn, flow)
                state['completed_stages'] = [{'stage': 0, 'key': 'requirements', 'actor': request.actor}]
                status = 'READY'
            elif action in {'generate', 'generate_confirmed'}:
                confirmed_scope = action == 'generate_confirmed'
                allowed_statuses = {'READY', 'BLOCKED', 'NEEDS_CLARIFICATION'} if confirmed_scope else {'READY', 'BLOCKED'}
                if status not in allowed_statuses and not (status == 'REVIEW_PENDING' and proposal and proposal['status'] == 'rejected'):
                    raise KnowledgeError('请先确认需求、完成澄清或明确拒绝旧设计后，再生成设计', 409)
                if proposal and proposal['status'] != 'rejected':
                    raise KnowledgeError('已有设计提案尚未拒绝，不能覆盖或重复生成', 409)
                self._fresh(conn, flow)
                model_gateway.ensure_model_configured()
                context = self._context(conn, flow, confirmed_guidance=request.reason if confirmed_scope else None)
                job_id = uuid4()
                config = model_gateway.model_config_snapshot()
                job_request = {'context': context, 'config': config['public'], 'config_fingerprint': config['fingerprint'],
                               'input_sha256': sha256(context), 'generation_mode': 'confirmed_scope' if confirmed_scope else 'standard'}
                conn.execute('''INSERT INTO design_generation(id,flow_id,project_id,actor,reason,request)
                    VALUES (%s,%s,%s,%s,%s,%s)''', (job_id, flow_id, flow['project_id'], request.actor, request.reason, Jsonb(job_request)))
                self._job_event(conn, {'id': job_id}, 'QUEUED', {'diagnostic': '等待后台模型任务执行'})
                if status == 'NEEDS_CLARIFICATION':
                    state['question_job_id'] = state.get('question_job_id') or state['active_job_id']
                state.update(active_job_id=str(job_id), generation_count=state['generation_count']+1, last_error=None,
                             design_proposal_id=None)
                status = 'GENERATING'
            elif action in {'validate', 'commit', 'reject'}:
                if status not in {'REVIEW_PENDING', 'DESIGN_COMMITTED'} or not proposal:
                    raise KnowledgeError('当前没有可以审阅的设计提案', 409)
                if request.proposal_version != proposal['version']:
                    raise KnowledgeError('设计提案版本已变化，请重新读取差异后再确认', 409)
                if action != 'reject':
                    self._fresh(conn, flow)
                reviewed = KnowledgeOperations(self.database_url, connection=conn).review(proposal['id'],
                    ProposalReview(expected_version=proposal['version'], actor=request.actor, reason=request.reason), action)['proposal']
                if action == 'reject':
                    status = 'READY'
                elif action == 'commit':
                    status = 'DESIGN_COMMITTED'
                else:
                    status = 'REVIEW_PENDING'
                    state['last_error'] = None if reviewed['status'] == 'validated' else '设计提案校验未通过，请核对具体错误'
            elif action == 'complete':
                if status not in {'REVIEW_PENDING', 'DESIGN_COMMITTED'} or not proposal or proposal['status'] != 'committed':
                    raise KnowledgeError('设计提案必须先校验并明确提交，才能完成本里程碑', 409)
                if request.proposal_version != proposal['version']:
                    raise KnowledgeError('设计提案版本已变化，请刷新后确认完成', 409)
                self._fresh(conn, flow)
                versions = []
                for operation in proposal['result']['operations']:
                    table = 'entity' if operation['op'].endswith('entity') else 'relationship'
                    record = KS._get(conn, table, UUID(operation['id']))
                    if record['version'] != operation['version']:
                        raise KnowledgeError('设计产物在提交后已有新版本，请重新复核', 409)
                    versions.append({'table': table, 'id': operation['id'], 'version': record['version']})
                state['artifact_versions'] = versions
                state['completed_stages'].append({'stage': 1, 'key': 'design', 'proposal_id': str(proposal['id']), 'actor': request.actor})
                status = 'COMPLETED'
            else:
                raise KnowledgeError('不支持的设计流程动作')
            self._event(conn, flow, status, action, request.actor, request.reason, state)
        return self.snapshot(flow_id)

    def answers(self, flow_id, request: AnswerSubmission):
        with connect(self.database_url) as conn:
            flow = self._lock(conn, flow_id, request.expected_version)
            if flow['status'] != 'NEEDS_CLARIFICATION':
                raise KnowledgeError('当前流程没有等待答复的澄清问题', 409)
            self._fresh(conn, flow)
            state = deepcopy(flow['state'])
            supplied = {answer.key: answer.answer for answer in request.answers}
            if len(supplied) != len(request.answers) or set(supplied) != {question['key'] for question in state['questions']}:
                raise KnowledgeError('请逐项回答当前全部澄清问题，不能重复或加入未知问题')
            question_job_id = state.get('question_job_id') or state['active_job_id']
            state['answers'].extend({**question, 'answer': supplied[question['key']], 'job_id': question_job_id} for question in state['questions'])
            state.update(questions=[], question_job_id=None, last_error=None)
            self._event(conn, flow, 'READY', 'answers', request.actor, request.reason, state)
        return self.snapshot(flow_id)

    @staticmethod
    def _context(conn, flow, *, confirmed_guidance=None):
        definition = flow['definition']
        def records(refs):
            return [jsonable_encoder(KS._get(conn, 'entity', UUID(ref['id']), ref['version'])) for ref in refs]
        context = {'title': flow['title'], 'goal': definition['goal'], 'requirements': records(definition['requirements']),
                   'knowledge': records(definition['input_knowledge']), 'clarifications': flow['state']['answers'],
                   'scope': '仅设计：形成可审阅的组件、接口、数据模型和测试计划；不生成代码，不声称执行或测试通过。'}
        if confirmed_guidance is not None:
            context.update(generation_guidance=confirmed_guidance, pending_questions=flow['state']['questions'])
        if len(canonical(context).encode('utf-8')) > 512*1024:
            raise KnowledgeError('需求与设计依据超过512KiB，请缩小本轮范围')
        return context

    @staticmethod
    def _check_design(document, flow, *, confirmed_context=None):
        model = ConfirmedScopeDesign if confirmed_context is not None else DesignDocument
        design = model.model_validate(document)
        requirements = {ref['id'] for ref in flow['definition']['requirements']}
        items = [*design.questions, *design.components, *design.interfaces, *design.data_models, *design.test_plan]
        for item in items:
            if not set(item.requirement_ids) <= requirements:
                raise KnowledgeError('模型设计引用了未选入本流程的需求，未发布提案')
        if confirmed_context is not None and design.questions:
            DesignService._check_acceptance_questions(design.questions, confirmed_context)
        if not design.questions:
            for group in (design.components, design.test_plan):
                covered = {ref for item in group for ref in item.requirement_ids}
                if covered != requirements:
                    raise KnowledgeError('组件设计和测试计划必须分别覆盖本轮全部需求，未发布提案')
        if len(canonical(design.model_dump(mode='json')).encode('utf-8')) > 512*1024:
            raise KnowledgeError('模型设计输出超过512KiB，未发布提案')
        return design.model_dump(mode='json')

    @staticmethod
    def _check_acceptance_questions(questions, context):
        criteria = {row['id']: set(row['attributes']['acceptance_criteria']) for row in context['requirements']}
        answered = defaultdict(set)
        for answer in context['clarifications']:
            answered[''.join(answer['question'].casefold().split()).rstrip('？?')].update(answer['requirement_ids'])
        for question in questions:
            if not any(question.acceptance_criterion in criteria[ref] for ref in question.requirement_ids):
                raise KnowledgeError('模型追问未引用所选需求的原始验收条件，请重新按已确认范围生成')
            if len(set(question.options)) != len(question.options):
                raise KnowledgeError('模型追问未给出不同的规则选项，请重新按已确认范围生成')
            normalized = ''.join(question.question.casefold().split()).rstrip('？?')
            if set(question.requirement_ids) & answered[normalized]:
                raise KnowledgeError('模型重复询问已有答复的问题，请重新按已确认范围生成')

    def _proposal_plan(self, flow, job, design, trace):
        project_id, flow_id = flow['project_id'], flow['id']
        provenance = {'actor': 'model:'+trace.get('provider', 'configured')+'/'+trace.get('model', 'configured'),
                      'source_uri': 'urn:design:generation:'+str(job['id']), 'source_revision': trace['output_sha256'],
                      'change_reason': '模型依据固定需求版本生成设计候选，尚待人工审阅；不代表实现或测试通过', 'nature': 'inference'}
        evidence_id = uuid5(job['id'], 'analysis-evidence')
        evidence = EntityCreate(project_id=project_id, kind='Evidence', name='模型设计生成记录',
            description='设计生成来源与输入输出摘要，不是代码执行或测试证据。', provenance=provenance,
            attributes={'category': 'analysis', 'uri': provenance['source_uri'], 'revision': trace['output_sha256'], 'extensions': trace})
        operations = [{'op': 'create_entity', 'id': str(evidence_id), 'data': evidence.model_dump(mode='json')}]
        mapping, refs, components = [], [], {}
        for group, kind in [('components', 'ArchitectureComponent'), ('interfaces', 'Interface'), ('data_models', 'DataModel'), ('test_plan', 'TestCase')]:
            for item in design[group]:
                record_id = uuid5(flow_id, kind+':'+item['key'])
                extensions = {'design_key': item['key'], 'requirement_ids': item['requirement_ids'],
                              'generation_id': str(job['id']), 'implementation_status': 'planned'}
                if group == 'components':
                    attrs = {'responsibility': item['responsibility'], 'extensions': extensions}
                    components[item['key']] = record_id
                elif group == 'interfaces':
                    attrs = {'protocol': item['protocol'], 'extensions': {**extensions, 'contract': item['contract']}}
                elif group == 'data_models':
                    attrs = {'schema_definition': item['schema_definition'], 'extensions': extensions}
                else:
                    attrs = {'steps': item['steps'], 'expected_result': item['expected_result'], 'extensions': extensions}
                body = EntityCreate(project_id=project_id, kind=kind, name=item['name'], description=item['description'],
                    attributes=attrs, provenance=provenance, evidence=[{'id': evidence_id, 'version': 1}])
                operations.append({'op': 'create_entity', 'id': str(record_id), 'data': body.model_dump(mode='json')})
                mapping.append({'key': kind+':'+item['key'], 'entity_id': str(record_id), 'version': 1, 'resolution': 'new'})
                refs.append((record_id, item, kind))
        requirements = {ref['id']: ref['version'] for ref in flow['definition']['requirements']}
        def edge(kind, source, target, source_version, target_version, rationale):
            body = RelationCreate(type=kind, source_id=source, target_id=target, source_version=source_version,
                                  target_version=target_version, rationale=rationale, provenance=provenance)
            key = kind+':'+str(source)+':'+str(target)
            operations.append({'op': 'create_relationship', 'id': str(uuid5(flow_id, key)), 'data': body.model_dump(mode='json')})
        for record_id, item, kind in refs:
            for req in item['requirement_ids']:
                edge('DERIVED_FROM', record_id, UUID(req), 1, requirements[req], '根据本轮固定版本需求形成设计候选，待审阅。')
                if kind == 'TestCase':
                    edge('VERIFIED_BY', UUID(req), record_id, requirements[req], 1, '计划使用本测试验收需求，尚未实现或执行。')
            if kind in {'Interface', 'DataModel'}:
                edge('CONTAINS', components[item['component_key']], record_id, 1, 1, '组件包含本次设计的接口或数据结构。')
        if len(operations) > 200:
            raise KnowledgeError('本轮设计关系超过200项操作，请拆分需求范围')
        return {'operations': operations, 'baselines': flow['state']['baselines'], 'entity_mapping': mapping, 'removed_keys': []}

    def _check_model_retry(self, flow_id, job_id):
        with connect(self.database_url) as conn:
            flow = self._lock(conn, flow_id, writable=False)
            if (flow['status'] != 'GENERATING' or flow['state']['active_job_id'] != str(job_id)
                    or self._job(conn, job_id)['status'] != 'RUNNING'):
                raise KnowledgeError('设计生成任务已停止，未再次调用模型', 409)
            KS._assert_project(conn, flow['project_id'])
            self._fresh(conn, flow)

    def _record_late_exchange(self, conn, job, exchange):
        if exchange and job['status'] in {'CANCELLED', 'FAILED', 'STALE'} and 'model_exchange' not in job['result']:
            self._job_event(conn, job, job['status'], job['result'] | exchange)

    def run_job(self, job_id):
        """先短事务领取，完全退出数据库事务后调用模型，再检查基线并原子保存结果。"""
        with connect(self.database_url) as conn:
            job = self._job(conn, job_id)
            flow = self._lock(conn, job['flow_id'], writable=False)
            job = self._job(conn, job_id)
            if job['status'] != 'QUEUED' or flow['status'] != 'GENERATING' or flow['state']['active_job_id'] != str(job_id):
                return
            try:
                KS._assert_project(conn, flow['project_id'])
                self._fresh(conn, flow)
            except KnowledgeError as exc:
                self._job_event(conn, job, 'STALE', {'diagnostic': exc.message})
                state = deepcopy(flow['state']); state['last_error'] = exc.message
                self._event(conn, flow, 'BLOCKED', 'generation_stale', 'model-worker', exc.message, state)
                return
            timeout = job['request']['config']['timeout_seconds']
            # 预留请求超时、重试等待和结果入库时间，避免其他 worker 提前回收。
            self._job_event(conn, job, 'RUNNING', {'diagnostic': '模型正在生成设计',
                'deadline_at': (datetime.now(timezone.utc)+timedelta(seconds=generation_recovery_seconds(timeout))).isoformat()})
        trace = None
        exchange = {}

        def record_exchange(value):
            exchange['model_exchange'] = value

        confirmed_scope = job['request'].get('generation_mode') == 'confirmed_scope'
        try:
            config = model_gateway.public_model_config()
            if config != job['request']['config']:
                raise KnowledgeError('模型服务配置在任务入队后发生变化，请核对配置后显式重新生成', 409)
            context = job['request']['context']
            schema = ConfirmedScopeDesign if confirmed_scope else DesignDocument
            result = model_gateway.complete_design(context, schema.model_json_schema(), confirmed_scope=confirmed_scope,
                expected_fingerprint=job['request']['config_fingerprint'],
                before_retry=lambda: self._check_model_retry(job['flow_id'], job_id), on_exchange=record_exchange)
            trace = {**result['trace'], 'context_sha256': job['request']['input_sha256']}
            design = self._check_design(result['document'], flow, confirmed_context=context if confirmed_scope else None)
            trace['design_sha256'] = sha256(design)
            with connect(self.database_url) as conn:
                flow = self._lock(conn, job['flow_id'], writable=False)
                job = self._job(conn, job_id)
                if job['status'] != 'RUNNING' or flow['status'] != 'GENERATING' or flow['state']['active_job_id'] != str(job_id):
                    if job['status'] == 'CANCELLED':
                        self._job_event(conn, job, 'CANCELLED', {'diagnostic': '取消后模型返回，设计内容未发布', 'trace': trace, **exchange})
                    else:
                        self._record_late_exchange(conn, job, exchange)
                    return
                KS._assert_project(conn, flow['project_id'])
                self._fresh(conn, flow)
                state = deepcopy(flow['state'])
                state.update(design=design, questions=design['questions'], assumptions=design['assumptions'], last_error=None,
                             question_job_id=str(job_id) if design['questions'] else None)
                if design['questions']:
                    status = 'NEEDS_CLARIFICATION'
                else:
                    plan = self._proposal_plan(flow, job, design, trace)
                    proposal = KnowledgeOperations(self.database_url, connection=conn)._create(conn, flow['project_id'],
                        '模型设计：'+flow['title'], job['actor'], plan)
                    state['design_proposal_id'] = str(proposal['id'])
                    status = 'REVIEW_PENDING'
                diagnostic = '等待澄清答复' if design['questions'] else '设计候选已生成，等待审阅'
                if trace.get('attempt_count', 1) > 1:
                    diagnostic = '模型自动重试成功。' + diagnostic
                self._job_event(conn, job, 'SUCCEEDED', {'diagnostic': diagnostic, 'trace': trace, 'questions': design['questions'], **exchange})
                self._event(conn, flow, status, 'generated', 'model-worker', '模型返回已校验；没有自动批准或提交知识', state)
        except Exception as exc:
            if isinstance(exc, KnowledgeError):
                message = exc.message
            elif isinstance(exc, ValidationError):
                message = ('模型返回未通过结构校验：请重新按已确认范围生成可审阅设计，或给出影响验收的具体未决规则。'
                           if confirmed_scope else '模型设计输出未通过结构校验，请重新生成；没有发布知识或执行代码')
            else:
                message = '模型设计任务处理失败，请检查服务配置或后台日志后显式重试'
                logging.getLogger(__name__).warning('设计任务失败：%s', type(exc).__name__)
            with connect(self.database_url) as conn:
                job = self._job(conn, job_id)
                flow = self._lock(conn, job['flow_id'], writable=False)
                if job['status'] == 'RUNNING' and flow['state']['active_job_id'] == str(job_id) and flow['status'] == 'GENERATING':
                    stale = isinstance(exc, KnowledgeError) and exc.status_code == 409
                    self._job_event(conn, job, 'STALE' if stale else 'FAILED', {'diagnostic': message, **({'trace': trace} if trace else {}), **exchange})
                    state = deepcopy(flow['state'])
                    state['last_error'] = message
                    status = 'NEEDS_CLARIFICATION' if not stale and state['questions'] and state.get('question_job_id') else 'BLOCKED'
                    self._event(conn, flow, status, 'generation_failed', 'model-worker', message, state)
                else:
                    self._record_late_exchange(conn, job, exchange)

    def recover_expired(self):
        """进程中断后的超时任务只标失败，不自动重发可能产生费用的模型调用。"""
        with connect(self.database_url) as conn:
            rows = conn.execute(f"SELECT id,flow_id FROM current_design_generations WHERE status='RUNNING' AND {GENERATION_EXPIRED} LIMIT 20").fetchall()
        for row in rows:
            with connect(self.database_url) as conn:
                flow = self._lock(conn, row['flow_id'], writable=False)
                job = conn.execute(f"SELECT * FROM current_design_generations WHERE id=%s AND status='RUNNING' AND {GENERATION_EXPIRED}",
                                   (row['id'],)).fetchone()
                if job is None:
                    continue
                message = '后台任务中断或超过恢复时限，需要人工确认后重新生成；没有自动重发模型请求'
                self._job_event(conn, job, 'FAILED', {'diagnostic': message})
                if flow['status'] == 'GENERATING' and flow['state']['active_job_id'] == str(job['id']):
                    state = deepcopy(flow['state']); state['last_error'] = message
                    status = 'NEEDS_CLARIFICATION' if state['questions'] and state.get('question_job_id') else 'BLOCKED'
                    self._event(conn, flow, status, 'generation_expired', 'model-worker', message, state)

    def run_next(self):
        self.recover_expired()
        with connect(self.database_url) as conn:
            row = conn.execute("SELECT id FROM current_design_generations WHERE status='QUEUED' ORDER BY created_at,id LIMIT 1").fetchone()
        if row:
            self.run_job(row['id'])
            return True
        return False
