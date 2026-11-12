"""M1 固定原文的模型抽取与人工选择，复用不可变知识提案。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from uuid import UUID, uuid4, uuid5

from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from . import model_gateway
from .db import connect
from .extraction_models import ExtractionCandidate
from .extraction_context import capture_extraction_context, existing_quote_supported, model_context, project_state_token
from .extraction_impact import capture_impact_snapshot, build_impact
from .extraction_review import build_review, validate_decisions
from .models import EntityCreate, EntityUpdate, Kind, RelationCreate, RelationType, RelationUpdate
from .operations import KnowledgeOperations
from .semantics import KnowledgeError, validate_types
from .service import KnowledgeService as KS
from .source_spans import SourceSpan, read_source_span


MAX_SOURCE_CHARS = 40000


def _json(value):
    return jsonable_encoder(value)


def _digest(value):
    return hashlib.sha256(json.dumps(_json(value), ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def acceptance_location_key(entity_key, condition_key):
    """键中允许冒号，使用结构编码避免不同父项与子项组合串用证据。"""
    return 'acceptance:' + json.dumps([entity_key, condition_key], ensure_ascii=False, separators=(',', ':'))


def locate_quotes(source, quotes):
    """模型逐字引用，服务端定位；重复引文必须显式指出位置。"""
    spans = []
    content = source['content']
    for item in quotes:
        start = item.start
        if start is None:
            start = content.find(item.quote)
            if start < 0:
                raise KnowledgeError('候选引文未出现在固定原文中，请逐字核对引文')
            if content.find(item.quote, start + 1) >= 0:
                raise KnowledgeError('候选引文出现多次，请补充起始位置或扩展为唯一引文')
        span = SourceSpan(source_id=source['source_id'], source_version=source['version'],
                          sha256=source['sha256'], start=start, end=start+len(item.quote), quote=item.quote)
        if content[span.start:span.end] != item.quote:
            raise KnowledgeError('候选引文与固定原文指定位置不一致')
        value = span.model_dump(mode='json')
        if value in spans:
            raise KnowledgeError('同一候选不能重复引用相同原文区间')
        spans.append(value)
    return spans


class ExtractionService:
    def __init__(self, database_url):
        self.database_url = database_url

    @staticmethod
    def _job(conn, job_id):
        row = conn.execute('SELECT * FROM current_extraction_jobs WHERE id=%s', (job_id,)).fetchone()
        if row is None:
            raise KnowledgeError('未找到知识抽取任务', 404)
        return row

    @staticmethod
    def _event(conn, job_id, version, status, result):
        conn.execute('INSERT INTO extraction_job_event(job_id,version,status,document) VALUES (%s,%s,%s,%s)',
                     (job_id, version, status, Jsonb(_json(result))))

    @staticmethod
    def _source(conn, project_id, source_id, version):
        row = conn.execute('''SELECT r.*,s.uri FROM source_revision r JOIN knowledge_source s ON s.id=r.source_id
            WHERE s.project_id=%s AND r.source_id=%s AND r.version=%s''', (project_id, source_id, version)).fetchone()
        if row is None:
            raise KnowledgeError('未找到本项目的固定原文版本', 404)
        return row

    @staticmethod
    def _fresh_source(conn, job):
        latest = conn.execute('SELECT max(version) AS v FROM source_revision WHERE source_id=%s',
                              (job['source_id'],)).fetchone()['v']
        if latest != job['source_version']:
            raise KnowledgeError('原文已有新版本，请选择最新原文重新抽取；历史任务和证据仍可回查', 409)
        KS._assert_project(conn, job['project_id'])

    @staticmethod
    def _bindings(conn, source_id):
        return conn.execute('''SELECT b.external_key AS key,b.entity_id,e.version FROM source_entity_binding b
            JOIN current_entities e ON e.id=b.entity_id WHERE b.source_id=%s ORDER BY b.external_key''',
                            (source_id,)).fetchall()

    def start(self, project_id, request):
        model_gateway.ensure_model_configured()
        with connect(self.database_url) as conn:
            KS._lock(conn, project_id)
            KS._assert_project(conn, project_id)
            source = self._source(conn, project_id, request.source_id, request.source_version)
            if source['format'] not in {'text', 'markdown'}:
                raise KnowledgeError('M1 请登记普通文本或 Markdown 原文后发起语义抽取')
            if len(source['content']) > MAX_SOURCE_CHARS:
                raise KnowledgeError('本轮抽取原文最多 40000 个 Unicode 字符，请拆分为有独立语义的文档；系统不会截断原文')
            self._fresh_source(conn, {'source_id': request.source_id, 'source_version': request.source_version,
                                      'project_id': project_id})
            running = conn.execute('''SELECT id FROM current_extraction_jobs WHERE project_id=%s AND source_id=%s
                AND status IN ('QUEUED','RUNNING') LIMIT 1''', (project_id, request.source_id)).fetchone()
            if running:
                raise KnowledgeError('此来源已有排队或运行中的抽取任务，请等待完成后再发起', 409)
            bindings = self._bindings(conn, request.source_id)
            existing, existing_relationships, retrieval = capture_extraction_context(conn, project_id, source, bindings)
            relationship_bindings = conn.execute('''SELECT external_key AS key,relationship_id
                FROM source_relationship_binding WHERE source_id=%s ORDER BY external_key''', (request.source_id,)).fetchall()
            project_version = conn.execute('''SELECT coalesce(max(version),0) AS v
                FROM project_model_revision WHERE project_id=%s''', (project_id,)).fetchone()['v']
            context = _json({'source': source, 'existing_entities': existing, 'bindings': bindings,
                             'existing_relationships': existing_relationships, 'relationship_bindings': relationship_bindings,
                             'project_baseline_version': project_version, 'guidance': request.guidance,
                             'source_scope': request.source_scope, 'review_contract_version': 2,
                             'retrieval': retrieval, 'impact_snapshot': capture_impact_snapshot(conn, project_id),
                             'project_state_token': project_state_token(conn, project_id)})
            config = model_gateway.model_config_snapshot()
            job_id = uuid4()
            payload = {'context': context, 'config': config['public'], 'config_fingerprint': config['fingerprint'],
                       'input_sha256': _digest(context)}
            conn.execute('''INSERT INTO extraction_job(id,project_id,source_id,source_version,actor,request)
                VALUES (%s,%s,%s,%s,%s,%s)''', (job_id, project_id, request.source_id, request.source_version,
                                               request.actor, Jsonb(payload)))
            self._event(conn, job_id, 1, 'QUEUED', {'diagnostic': '等待模型抽取，尚未形成正式知识'})
            return {key: value for key, value in self._job(conn, job_id).items() if key != 'request'}

    def list_jobs(self, project_id, limit=50, offset=0):
        with connect(self.database_url) as conn:
            KS._assert_project(conn, project_id)
            rows = conn.execute('''SELECT j.id,j.project_id,j.source_id,j.source_version,j.actor,j.created_at,j.status,j.version,
                j.updated_at,j.result->>'diagnostic' AS diagnostic,s.uri AS source_uri,r.external_revision
                FROM current_extraction_jobs j JOIN knowledge_source s ON s.id=j.source_id
                JOIN source_revision r ON r.source_id=j.source_id AND r.version=j.source_version
                WHERE j.project_id=%s ORDER BY j.created_at DESC,j.id LIMIT %s OFFSET %s''',
                (project_id, limit, offset)).fetchall()
            return {'items': rows, 'model_config': model_gateway.public_model_config()}

    def get_job(self, job_id):
        with connect(self.database_url) as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            job = self._job(conn, job_id)
            context = job['request']['context']
            proposals = conn.execute('''SELECT p.* FROM current_proposals p JOIN extraction_proposal x
                ON x.proposal_id=p.id WHERE x.job_id=%s ORDER BY p.created_at DESC,p.id''', (job_id,)).fetchall()
            public_job = {key: value for key, value in job.items() if key != 'request'}
            return {**public_job, 'context': context, 'guidance': context['guidance'],
                    'source': context['source'], 'existing_entities': context['existing_entities'],
                    'bindings': context['bindings'], 'proposals': proposals}

    @staticmethod
    def _check_candidate(job, candidate):
        context = job['request']['context']
        source = context['source']
        fixed = {item['id']: item for item in context['existing_entities']}
        bindings = {item['key']: item for item in context['bindings']}
        locations, resolved = {}, {}
        for item in candidate.entities:
            locations['entity:'+item.key] = locate_quotes(source, item.evidence)
            binding = bindings.get(item.key)
            if binding:
                if item.existing is None:
                    # 已经审阅确认的来源键不可因模型漏写而变成新增对象。
                    from .extraction_models import ExistingKnowledge
                    item.existing = ExistingKnowledge(id=binding['entity_id'], version=binding['version'])
                elif str(item.existing.id) != binding['entity_id']:
                    raise KnowledgeError('候选稳定键已绑定其他知识，不能改变已有对象身份')
            if item.existing:
                previous = fixed.get(str(item.existing.id))
                if previous is None or previous['version'] != item.existing.version or previous['kind'] != item.kind:
                    raise KnowledgeError('已有知识绑定必须引用本轮固定上下文中的同类型实体及版本')
            resolved[item.key] = item
            for condition in item.acceptance_items or []:
                locations[acceptance_location_key(item.key, condition.key)] = locate_quotes(source, condition.evidence)
        ids = [str(item.existing.id) for item in candidate.entities if item.existing]
        if len(ids) != len(set(ids)):
            raise KnowledgeError('多个候选不能同时修订同一个已有知识，请合并候选后审阅')
        seen = set()
        for link in candidate.relationships:
            if link.source_key not in resolved or link.target_key not in resolved:
                raise KnowledgeError('关系两端必须引用本次选中的知识候选')
            if link.source_key == link.target_key:
                raise KnowledgeError('抽取关系不能指向自身')
            source_kind, target_kind = Kind(resolved[link.source_key].kind), Kind(resolved[link.target_key].kind)
            validate_types(link.type, source_kind, target_kind)
            ends = (link.source_key, link.target_key)
            if link.type == RelationType.CONFLICTS_WITH:
                ends = tuple(sorted(ends))
            identity = (link.type, *ends)
            if identity in seen:
                raise KnowledgeError('同类型、同端点的候选关系不能重复')
            seen.add(identity)
            locations['relationship:'+link.key] = locate_quotes(source, link.evidence)
        for index, hint in enumerate(candidate.review_hints):
            old = fixed.get(str(hint.existing.id))
            if old is None or old['version'] != hint.existing.version:
                raise KnowledgeError('增量检查建议必须引用本轮上下文中的已有知识版本')
            if hint.candidate_key is None and hint.kind != 'retirement':
                raise KnowledgeError('重复或冲突建议须明确指出本轮候选键')
            if hint.candidate_key is not None and hint.candidate_key not in resolved:
                raise KnowledgeError('增量检查建议引用了不存在的候选')
            if not existing_quote_supported(old, hint.existing_quote):
                raise KnowledgeError('增量检查建议的已有知识引文不匹配，请查看模型原始答复')
            locations['review_hint:'+str(index)] = locate_quotes(source, hint.evidence)
        return locations

    @staticmethod
    def _review_candidate(job, submitted):
        candidate = submitted.model_copy(deep=True)
        original = job['result']['candidate']
        for group in ('entities', 'relationships'):
            originals = {item['key']: item for item in original[group]}
            for item in getattr(candidate, group):
                if item.key not in originals:
                    raise KnowledgeError('人工审阅不能新增或替换模型候选的稳定键，请保留键后编辑内容')
                if group == 'entities' and item.kind != originals[item.key]['kind']:
                    raise KnowledgeError('候选类型不能在审阅中改变，请重新抽取或另建知识')
        # 模型发现始终取原始任务，客户端不能删除提示绕过审阅；排除候选时仅排除其相关提示。
        original_candidate = ExtractionCandidate.model_validate(original)
        keys = {item.key for item in candidate.entities}
        candidate.review_hints = [hint for hint in original_candidate.review_hints
                                  if hint.candidate_key is None or hint.candidate_key in keys]
        return candidate

    def _review_report(self, conn, job, candidate):
        context = job['request']['context']
        if context.get('project_state_token') and project_state_token(conn, job['project_id']) != context['project_state_token']:
            raise KnowledgeError('知识或项目基线已变化，增量比较与影响范围需要基于最新上下文重新抽取', 409)
        report = build_review(context, candidate.model_dump(mode='json'))
        report['project_id'] = str(job['project_id'])
        roots = [item['entity_ref'] for item in report['changes']
                 if item.get('entity_ref') and item['operation'] == 'updated']
        for item in report.get('missing', []):
            if item.get('table') == 'entity':
                roots.append(item)
        for finding in report['findings']:
            if finding['kind'] == 'retirement_suggestion' and finding.get('existing'):
                roots.append(finding['existing'])
        if context.get('impact_snapshot'):
            report['impact'] = build_impact(context['impact_snapshot'], roots)
        else:
            report['impact'] = {'available': False, 'affected': [], 'model_affected': [], 'roots': [],
                'message': '历史任务没有保存影响图谱快照，无法回溯当时的影响；请新建抽取任务。'}
        report['retrieval'] = context.get('retrieval', {'complete': True, 'message': '历史任务使用当时完整上下文。'})
        report['source_scope'] = context.get('source_scope', 'full')
        report['review_digest'] = _digest({'job_id': str(job['id']), 'candidate': candidate.model_dump(mode='json'),
                                         'report': report, 'input_sha256': job['request']['input_sha256']})
        return report

    def review_candidate(self, job_id, request):
        with connect(self.database_url) as conn:
            # 与提案生成一致，使用项目锁固定审阅读取，接口只读，不写事件或知识。
            job = self._job(conn, job_id)
            KS._lock(conn, job['project_id'])
            job = self._job(conn, job_id)
            if job['status'] != 'SUCCEEDED':
                raise KnowledgeError('只有抽取成功的任务可以分析增量', 409)
            self._fresh_source(conn, job)
            candidate = self._review_candidate(job, request.candidate)
            self._check_candidate(job, candidate)
            return self._review_report(conn, job, candidate)

    def propose(self, job_id, request):
        with connect(self.database_url) as conn:
            job = self._job(conn, job_id)
            KS._lock(conn, job['project_id'])
            job = self._job(conn, job_id)
            if job['status'] != 'SUCCEEDED':
                raise KnowledgeError('只有抽取成功的任务可以形成知识提案', 409)
            self._fresh_source(conn, job)
            previous_proposals = conn.execute('''SELECT p.status FROM current_proposals p JOIN extraction_proposal x
                ON x.proposal_id=p.id WHERE x.job_id=%s''', (job_id,)).fetchall()
            if any(p['status'] != 'rejected' for p in previous_proposals):
                raise KnowledgeError('本任务已有待审或已提交提案，请完成审阅；修改已拒绝提案可重新提交，已提交任务可重新抽取', 409)
            candidate = self._review_candidate(job, request.candidate)
            locations = self._check_candidate(job, candidate)
            if not candidate.entities:
                raise KnowledgeError('当前只有移除建议，没有知识写入；请从项目知识另建生命周期修订提案')
            report = self._review_report(conn, job, candidate)
            if request.review_digest != report['review_digest']:
                raise KnowledgeError('请先分析当前候选的增量与影响，再逐项审阅；候选修改后必须重新分析', 409)
            decisions = validate_decisions(report, [item.model_dump(mode='json') for item in request.decisions])
            plan = self._plan(conn, job, candidate, locations, request.actor, request.reason)
            plan['extraction']['review'] = {**report, 'decisions': decisions, 'actor': request.actor, 'reason': request.reason}
            if guard := job['request']['context'].get('project_state_token'):
                plan['extraction']['project_state_token'] = guard
            proposal = KnowledgeOperations(self.database_url, connection=conn)._create(
                conn, job['project_id'], '文档抽取审阅：'+candidate.summary[:200], request.actor, plan)
            conn.execute('INSERT INTO extraction_proposal(job_id,proposal_id) VALUES (%s,%s)', (job_id, proposal['id']))
            return proposal

    def _plan(self, conn, job, candidate, locations, actor, reason):
        source = job['request']['context']['source']
        project_id, source_id = job['project_id'], job['source_id']
        provenance = {'actor': actor, 'source_uri': source['uri'], 'source_revision': source['external_revision'],
                      'change_reason': reason, 'nature': 'human_confirmed'}
        operations, baselines, mapping, relation_mapping, resolved = [], [], [], [], {}
        evidence_ids = {}

        def evidence(location_key):
            refs = []
            for span in locations[location_key]:
                read_source_span(conn, project_id, SourceSpan.model_validate(span))
                signature = (span['start'], span['end'])
                if signature not in evidence_ids:
                    record_id = uuid4()
                    evidence_ids[signature] = record_id
                    data = EntityCreate(project_id=project_id, kind=Kind.EVIDENCE, name='文档抽取原文证据',
                        description='固定原文中的逐字引文，语义适用性由提案审阅人确认', status='active', provenance=provenance,
                        attributes={'category': 'document', 'uri': source['uri'], 'revision': source['external_revision'],
                                    'sha256': source['sha256'], 'extensions': {'source_span': span}})
                    operations.append({'op': 'create_entity', 'id': str(record_id), 'data': data.model_dump(mode='json')})
                refs.append({'id': str(evidence_ids[signature]), 'version': 1})
            return refs

        for item in candidate.entities:
            current = None
            if item.existing:
                current = KS._get(conn, 'entity', item.existing.id)
                if (current['project_id'] != project_id or current['kind'] != item.kind
                        or current['version'] != item.existing.version or current['status'] == 'archived'):
                    raise KnowledgeError('已有知识已变化或不属于当前项目，请基于最新版本重新抽取', 409)
                record_id, version = current['id'], current['version']+1
                baselines.append({'table': 'entity', 'id': str(record_id), 'version': current['version']})
            else:
                record_id, version = uuid5(source_id, 'entity:'+item.key), 1
            # 键的绑定既约束旧键，也避免给同一对象偷偷分配第二个来源键。
            previous_binding = conn.execute('''SELECT external_key,entity_id FROM source_entity_binding
                WHERE source_id=%s AND (external_key=%s OR entity_id=%s)''', (source_id, item.key, record_id)).fetchall()
            if any(row['external_key'] != item.key or row['entity_id'] != record_id for row in previous_binding):
                raise KnowledgeError('来源知识键与已有身份冲突，请沿用已确认的稳定键', 409)
            attrs = deepcopy(current['attributes']) if current else {}
            ext = {**attrs.get('extensions', {}), **item.attributes.get('extensions', {})}
            attrs.update({key: value for key, value in item.attributes.items() if key != 'extensions'})
            ext['extraction'] = {'job_id': str(job['id']), 'source_id': str(source_id),
                'source_version': source['version'], 'source_sha256': source['sha256'], 'key': item.key,
                'previous_version': current['version'] if current else None}
            refs = evidence('entity:'+item.key)
            if item.acceptance_items is not None:
                attrs['acceptance_criteria'] = [condition.text for condition in item.acceptance_items]
                ext['acceptance_items'] = []
                for condition in item.acceptance_items:
                    key = acceptance_location_key(item.key, condition.key)
                    condition_refs = evidence(key)
                    refs.extend(ref for ref in condition_refs if ref not in refs)
                    ext['acceptance_items'].append({'key': condition.key, 'text': condition.text,
                        'evidence': condition_refs, 'source_spans': locations[key]})
            attrs['extensions'] = ext
            if current:
                refs = current['evidence'] + [ref for ref in refs if ref not in current['evidence']]
            content = {'name': item.name, 'description': item.description, 'status': current['status'] if current else 'active',
                       'attributes': attrs, 'evidence': refs, 'provenance': provenance}
            data = (EntityUpdate(**content, expected_version=current['version']) if current else
                    EntityCreate(**content, kind=item.kind, project_id=project_id))
            operations.append({'op': 'update_entity' if current else 'create_entity', 'id': str(record_id),
                               'data': data.model_dump(mode='json')})
            resolved[item.key] = (record_id, version)
            mapping.append({'key': item.key, 'entity_id': str(record_id), 'version': version,
                            'resolution': 'existing' if current else 'new'})

        for link in candidate.relationships:
            start_id, start_version = resolved[link.source_key]
            end_id, end_version = resolved[link.target_key]
            if link.type == RelationType.CONFLICTS_WITH and start_id > end_id:
                start_id, end_id, start_version, end_version = end_id, start_id, end_version, start_version
            binding = conn.execute('''SELECT relationship_id FROM source_relationship_binding
                WHERE source_id=%s AND external_key=%s''', (source_id, link.key)).fetchone()
            current = KS._get(conn, 'relationship', binding['relationship_id']) if binding else None
            if current and (current['type'], current['source_id'], current['target_id']) != (link.type, start_id, end_id):
                raise KnowledgeError('已有关系稳定键不能改变关系类型或端点，请保留旧关系并另提新关系', 409)
            if current is None:
                row = conn.execute('''SELECT id FROM current_relationships WHERE project_id=%s AND type=%s
                    AND source_id=%s AND target_id=%s''', (project_id, link.type, start_id, end_id)).fetchone()
                current = KS._get(conn, 'relationship', row['id']) if row else None
            record_id = current['id'] if current else uuid5(source_id, 'relationship:'+link.key)
            existing_keys = conn.execute('''SELECT external_key FROM source_relationship_binding
                WHERE source_id=%s AND relationship_id=%s''', (source_id, record_id)).fetchall()
            if any(row['external_key'] != link.key for row in existing_keys):
                raise KnowledgeError('关系已有来源键，请保留原关系稳定键', 409)
            if current:
                if current['status'] == 'retired':
                    raise KnowledgeError('已退役关系不能由抽取任务恢复', 409)
                fixed = {item['id']: item for item in job['request']['context']['existing_relationships']}
                previous = fixed.get(str(current['id']))
                if previous is None or previous['version'] != current['version']:
                    raise KnowledgeError('关系版本已变化，请重新抽取后审阅', 409)
                baselines.append({'table': 'relationship', 'id': str(record_id), 'version': current['version']})
            refs = evidence('relationship:'+link.key)
            if current:
                refs = current['evidence'] + [ref for ref in refs if ref not in current['evidence']]
            content = {'source_version': start_version, 'target_version': end_version, 'rationale': link.rationale,
                       'status': 'proposed', 'provenance': provenance, 'evidence': refs}
            data = (RelationUpdate(**content, expected_version=current['version']) if current else
                    RelationCreate(**content, type=link.type, source_id=start_id, target_id=end_id))
            operations.append({'op': 'update_relationship' if current else 'create_relationship', 'id': str(record_id),
                               'data': data.model_dump(mode='json')})
            relation_mapping.append({'key': link.key, 'relationship_id': str(record_id),
                                     'version': current['version']+1 if current else 1})
        if len(operations) > 300:
            raise KnowledgeError('本轮提案超过 300 项操作，请减少选中的知识或证据')
        return {'operations': operations, 'baselines': baselines, 'entity_mapping': mapping, 'removed_keys': [],
                'extraction': {'job_id': str(job['id']), 'source_id': str(source_id), 'source_version': source['version'],
                    'source_sha256': source['sha256'], 'project_baseline_version': job['request']['context']['project_baseline_version'],
                    'relationship_mapping': relation_mapping, 'candidate': candidate.model_dump(mode='json'),
                    'evidence_locations': locations},
                'message': '仅人工选择的候选形成提案；引文存在不代表语义已正确，未选中或缺失的知识不会自动删除。'}

    def _check_retry(self, job_id):
        with connect(self.database_url) as conn:
            job = self._job(conn, job_id)
            if job['status'] != 'RUNNING':
                raise KnowledgeError('抽取任务已结束，请人工重新发起', 409)
            self._fresh_source(conn, job)

    def run_job(self, job_id):
        with connect(self.database_url) as conn:
            job = self._job(conn, job_id)
            KS._lock(conn, job['project_id'])
            job = self._job(conn, job_id)
            if job['status'] != 'QUEUED':
                return
            try:
                self._fresh_source(conn, job)
            except KnowledgeError as exc:
                self._event(conn, job_id, job['version']+1, 'STALE', {'diagnostic': exc.message})
                return
            timeout = job['request']['config']['timeout_seconds']
            deadline = max(600, model_gateway.MAX_MODEL_ATTEMPTS*timeout+120)
            self._event(conn, job_id, job['version']+1, 'RUNNING', {'diagnostic': '模型正在抽取知识候选',
                'deadline_at': (datetime.now(timezone.utc)+timedelta(seconds=deadline)).isoformat()})
        exchange, trace = {}, None

        def audit(value):
            exchange['model_exchange'] = value
            # 发请求前就保存脱敏交互；后台中断后仍能定位当时发送的提示和上下文。
            with connect(self.database_url) as conn:
                KS._lock(conn, job['project_id'])
                current = self._job(conn, job_id)
                if current['status'] in {'RUNNING', 'FAILED', 'STALE'}:
                    self._event(conn, job_id, current['version']+1, current['status'],
                                {**current['result'], **exchange})

        try:
            result = model_gateway.complete_extraction(model_context(job['request']['context']), ExtractionCandidate.model_json_schema(),
                expected_fingerprint=job['request']['config_fingerprint'], before_retry=lambda: self._check_retry(job_id),
                on_exchange=audit)
            trace = {**result['trace'], 'context_sha256': job['request']['input_sha256']}
            candidate = ExtractionCandidate.model_validate(result['document'])
            locations = self._check_candidate(job, candidate)
            with connect(self.database_url) as conn:
                KS._lock(conn, job['project_id'])
                job = self._job(conn, job_id)
                if job['status'] != 'RUNNING':
                    return
                self._fresh_source(conn, job)
                self._event(conn, job_id, job['version']+1, 'SUCCEEDED', {'diagnostic': '候选已生成，请逐条审阅原文依据和已有知识绑定',
                    'candidate': candidate.model_dump(mode='json'), 'evidence_locations': locations, 'trace': trace, **exchange})
        except Exception as exc:
            if isinstance(exc, KnowledgeError):
                diagnostic = exc.message
            elif isinstance(exc, ValidationError):
                diagnostic = '模型抽取结果未通过结构校验，请检查原始答复后重试；未写入正式知识'
            else:
                diagnostic = '知识抽取任务处理失败，请检查后台日志和模型服务后手工重试'
                logging.getLogger(__name__).warning('知识抽取失败：%s', type(exc).__name__)
            with connect(self.database_url) as conn:
                KS._lock(conn, job['project_id'])
                job = self._job(conn, job_id)
                if job['status'] == 'RUNNING':
                    status = 'STALE' if isinstance(exc, KnowledgeError) and exc.status_code == 409 else 'FAILED'
                    self._event(conn, job_id, job['version']+1, status,
                                {**job['result'], 'diagnostic': diagnostic, 'trace': trace, **exchange})

    def recover_expired(self):
        with connect(self.database_url) as conn:
            rows = conn.execute('''SELECT id,project_id FROM current_extraction_jobs WHERE status='RUNNING'
                AND (result->>'deadline_at')::timestamptz < clock_timestamp() LIMIT 20''').fetchall()
        for row in rows:
            with connect(self.database_url) as conn:
                KS._lock(conn, row['project_id'])
                job = self._job(conn, row['id'])
                if job['status'] == 'RUNNING' and datetime.fromisoformat(job['result']['deadline_at']) < datetime.now(timezone.utc):
                    self._event(conn, job['id'], job['version']+1, 'FAILED',
                        {**job['result'], 'diagnostic': '后台抽取中断或超过恢复时限，请手工重新发起；本任务不会自动重发'})

    def run_next(self):
        self.recover_expired()
        with connect(self.database_url) as conn:
            row = conn.execute("SELECT id FROM current_extraction_jobs WHERE status='QUEUED' ORDER BY created_at,id LIMIT 1").fetchone()
        if row:
            self.run_job(row['id'])
            return True
        return False
