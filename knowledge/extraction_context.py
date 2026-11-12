"""M2 有界增量检索与固定审阅上下文；词面召回不负责判断业务相同性。"""
from copy import deepcopy
import hashlib
import json
import re
import unicodedata

from fastapi.encoders import jsonable_encoder

from .semantics import KnowledgeError
from .service import KnowledgeService as KS


def project_state_token(conn, project_id):
    """项目锁下读取审阅依赖的知识头版本，变化后要求重新抽取而非静默覆盖。"""
    entities = conn.execute('SELECT id,version FROM current_entities WHERE project_id=%s ORDER BY id', (project_id,)).fetchall()
    relationships = conn.execute('SELECT id,version FROM current_relationships WHERE project_id=%s ORDER BY id', (project_id,)).fetchall()
    baseline = conn.execute('SELECT coalesce(max(version),0) AS version FROM project_model_revision WHERE project_id=%s',
                            (project_id,)).fetchone()['version']
    raw = json.dumps(jsonable_encoder([entities, relationships, baseline]), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def _terms(value):
    value = unicodedata.normalize('NFKC', value).lower()
    words = set(re.findall(r'[a-z0-9_]{2,}', value))
    for run in re.findall(r'[\u3400-\u9fff]+', value):
        words.update(run[i:i+2] for i in range(max(0, len(run)-1)))
    return words


def select_context_records(records, source_text, bound_ids=(), limit=80):
    """强制保留同来源身份，其余按可解释的中文二元词／英文词重叠召回。"""
    bound = {str(value) for value in bound_ids}
    by_id = {str(row['id']): row for row in records}
    mandatory = sorted(bound & set(by_id))
    if len(mandatory) > 200:
        raise KnowledgeError('同一来源超过 200 个已绑定知识，请拆分来源后审阅；系统不会丢弃既有身份')
    target_limit = max(limit, len(mandatory))
    terms = _terms(source_text)
    ranked = []
    for identity, row in by_id.items():
        if identity in mandatory or row.get('status') == 'archived':
            continue
        name = row.get('name', '')
        overlap = len(terms & _terms(name + '\n' + row.get('description', '')))
        score = overlap + (50 if name and name in source_text else 0)
        ranked.append((-score, identity))
    ranked.sort()
    selected = mandatory + [identity for _, identity in ranked[:max(0, target_limit-len(mandatory))]]
    reasons = [{'id': identity, 'version': by_id[identity]['version'],
                'reason': '同来源已确认身份' if identity in bound else '名称／正文词面召回，仍需人工核对'} for identity in selected]
    return [by_id[identity] for identity in selected], reasons


def capture_extraction_context(conn, project_id, source, bindings):
    rows = conn.execute('''SELECT id,version,status,kind,document->>'name' AS name,
        document->>'description' AS description FROM current_entities
        WHERE project_id=%s AND kind IN ('Requirement','BusinessRule','Domain') AND status<>'archived'
        ORDER BY id LIMIT 2001''', (project_id,)).fetchall()
    total = conn.execute('''SELECT count(*) AS n FROM current_entities WHERE project_id=%s
        AND kind IN ('Requirement','BusinessRule','Domain') AND status<>'archived' ''', (project_id,)).fetchone()['n']
    pool = {str(row['id']): row for row in rows[:2000]}
    # 即使来源已绑定对象落在检索池上限以外，也必须补回，不用文字相似度替代身份。
    for binding in bindings:
        identity = str(binding['entity_id'])
        if identity not in pool:
            record = KS._get(conn, 'entity', binding['entity_id'])
            pool[identity] = {key: record[key] for key in ('id','version','kind','name','description','status')}
    selected, reasons = select_context_records(list(pool.values()), source['content'],
                                               [row['entity_id'] for row in bindings])
    existing = [KS._get(conn, 'entity', row['id']) for row in selected]
    ids = [item['id'] for item in existing]
    relation_rows = conn.execute('''SELECT r.id FROM current_relationships r
        WHERE r.project_id=%s AND r.source_id=ANY(%s::uuid[]) AND r.target_id=ANY(%s::uuid[])
        ORDER BY r.id LIMIT 401''', (project_id, ids, ids)).fetchall()
    if len(relation_rows) > 400:
        raise KnowledgeError('召回知识间的关系超过 400 条，请缩小原文范围；系统不会静默截断关系')
    relationships = [KS._get(conn, 'relationship', row['id']) for row in relation_rows]
    included_active = sum(row['status'] != 'archived' for row in existing)
    omitted = max(0, total-included_active)
    retrieval = {'strategy': 'source-binding-and-lexical-v1', 'total_entities': total,
                 'included_entities': len(existing), 'omitted_entities': omitted, 'pool_limit': 2000,
                 'complete': omitted == 0, 'selected': reasons,
                 'message': ('已纳入全部当前需求、规则和领域知识。' if not omitted else
                             '本轮使用有界词面召回，未覆盖全部知识；没有提示不代表全项目没有重复或冲突。')}
    return existing, relationships, retrieval


def model_context(context):
    """模型只接收有关的语义内容；审阅用完整快照留在服务器，不重复发送图谱与旧审计正文。"""
    result = {key: deepcopy(context[key]) for key in
              ('source','guidance','bindings','relationship_bindings','project_baseline_version')}
    result.update(source_scope=context.get('source_scope', 'full'), review_contract_version=context.get('review_contract_version', 1))
    result['retrieval'] = {key: value for key, value in context.get('retrieval', {}).items() if key != 'selected'}
    result['existing_entities'] = []
    for item in context['existing_entities']:
        record = {key: deepcopy(item[key]) for key in ('id','version','kind','name','description','status')}
        attrs = deepcopy(item['attributes'])
        ext = attrs.get('extensions', {})
        attrs['extensions'] = {key: value for key, value in ext.items()
                               if key not in {'extraction', 'source_span', 'acceptance_items', 'reconciliation'}}
        if 'acceptance_items' in ext:
            attrs['extensions']['acceptance_items'] = [{'key': ac['key'], 'text': ac['text']}
                                                       for ac in ext['acceptance_items']]
        record['attributes'] = attrs
        result['existing_entities'].append(record)
    result['existing_relationships'] = [{key: deepcopy(item[key]) for key in
        ('id','version','type','status','source_id','target_id','source_version','target_version','rationale')}
        for item in context['existing_relationships']]
    return result


def existing_quote_supported(record, quote):
    if not quote:
        return False
    values = [record.get('name', ''), record.get('description', '')]
    attrs = record.get('attributes', {})
    values.extend(attrs.get('acceptance_criteria', []))
    values.extend(attrs.get('glossary', {}).values())
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)
    values.extend(strings(attrs.get('expression', '')))
    values.extend(ac['text'] for ac in attrs.get('extensions', {}).get('acceptance_items', []))
    return any(isinstance(value, str) and quote in value for value in values)
