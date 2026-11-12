"""固定抽取上下文的增量审阅。这里只提出检查项，不合并、归档或写入知识。"""
from copy import deepcopy
from difflib import SequenceMatcher
import hashlib
import json
import unicodedata
from uuid import UUID, uuid5

from fastapi.encoders import jsonable_encoder

from .models import ATTRIBUTE_MODELS, Kind
from .semantics import KnowledgeError


_AUDIT_EXTENSIONS = {'extraction', 'source_span', 'acceptance_items', 'reconciliation'}
MAX_FINDINGS = 500
_ACTIONS = {
    'keep_distinct': '保留为不同知识并说明依据',
    'confirm_binding': '确认复用该知识身份',
    'accept_change': '确认本轮修改',
    'keep_both': '保留两项并说明适用范围',
    'keep_existing': '保留已有知识或关系',
    'defer_retirement': '记录移除建议，另提生命周期审阅',
}


def _json(value):
    return jsonable_encoder(value)


def _normal_name(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _finding_id(kind, subject):
    content = json.dumps([kind, subject], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'review-' + hashlib.sha256(content.encode()).hexdigest()[:24]


def _ref(record):
    return {key: record[key] for key in ('id', 'version', 'kind', 'name') if key in record}


def _acceptance(record):
    attrs = record.get('attributes', {})
    items = attrs.get('extensions', {}).get('acceptance_items')
    if items is not None:
        return [{'key': item['key'], 'text': item['text']} for item in items]
    return [{'key': None, 'text': text} for text in attrs.get('acceptance_criteria', [])]


def _content(record, acceptance=None):
    attrs = deepcopy(record.get('attributes', {}))
    extensions = {key: value for key, value in attrs.get('extensions', {}).items() if key not in _AUDIT_EXTENSIONS}
    attrs['extensions'] = extensions
    attrs = ATTRIBUTE_MODELS[Kind(record['kind'])].model_validate(attrs).model_dump(mode='json')
    attrs.pop('acceptance_criteria', None)
    return {'name': record['name'], 'description': record.get('description', ''), 'attributes': attrs,
            'status': record.get('status', 'active'),
            'acceptance_items': _acceptance(record) if acceptance is None else acceptance}


def _effective_after(item, previous, identity):
    attrs = deepcopy(previous.get('attributes', {})) if previous else {}
    extensions = {**attrs.get('extensions', {}), **item.get('attributes', {}).get('extensions', {})}
    attrs.update({key: value for key, value in item.get('attributes', {}).items() if key != 'extensions'})
    attrs['extensions'] = extensions
    acceptance = (None if item.get('acceptance_items') is None else
                  [{'key': ac['key'], 'text': ac['text']} for ac in item['acceptance_items']])
    record = {'kind': item['kind'], 'name': item['name'], 'description': item.get('description', ''),
              'attributes': attrs, 'status': previous.get('status', 'active') if previous else 'active'}
    content = _content(record, acceptance)
    return {'id': identity, 'version': previous['version'] + 1 if previous else 1, 'kind': item['kind'], **content}


def _before(record):
    return {**_ref(record), **_content(record)}


def _same_relation_ends(kind, source, target):
    return (kind, *sorted((source, target))) if kind == 'CONFLICTS_WITH' else (kind, source, target)


def build_review(context, candidate):
    """对已校验的候选及固定上下文构建报告；输入均保持不变。

    模型提示的引用及逐字引文由调用方先核验。semantic_checked 只表示本任务
    已使用 M2 提示协议，不表示模型已完整发现所有语义冲突。
    """
    context, candidate = _json(context), _json(candidate)
    existing = {item['id']: item for item in context.get('existing_entities', [])}
    bindings = {item['key']: item for item in context.get('bindings', [])}
    relations = {item['id']: item for item in context.get('existing_relationships', [])}
    relation_bindings = {item['key']: item for item in context.get('relationship_bindings', [])}
    selected = {item['key']: item for item in candidate.get('entities', [])}
    source_id = context['source']['source_id']
    changes, findings, missing = [], {}, []
    resolved, after_records = {}, {}

    def add(kind, subject, title, detail, *, keys=(), record=None, before=None, after=None,
            evidence=(), actions=('accept_change',), origin='rule', **extra):
        finding_id = _finding_id(kind, subject)
        if finding_id in findings:
            previous = findings[finding_id]
            if detail not in previous['detail']:
                previous['detail'] += '\n' + detail
            for value in evidence:
                if value not in previous['evidence']:
                    previous['evidence'].append(deepcopy(value))
            previous.setdefault('origins', [previous['origin']])
            if origin not in previous['origins']:
                previous['origins'].append(origin)
            for key, value in extra.items():
                if key not in previous:
                    previous[key] = deepcopy(value)
            return
        if len(findings) >= MAX_FINDINGS:
            raise KnowledgeError('本轮增量审阅超过 500 个检查项，请减少本轮候选或按明确范围拆分原文后重新抽取；系统不会静默丢弃检查项')
        findings[finding_id] = {'id': finding_id, 'kind': kind, 'title': title, 'detail': detail,
            'required': True, 'origin': origin, 'candidate_keys': list(keys),
            'existing': _ref(record) if record else None, 'before': deepcopy(before), 'after': deepcopy(after),
            'evidence': deepcopy(list(evidence)),
            'existing_evidence': deepcopy(record.get('evidence', [])) if record else [],
            'allowed_actions': [{'value': action, 'label': _ACTIONS[action]} for action in actions], **extra}

    for key, item in selected.items():
        binding = bindings.get(key)
        reference = item.get('existing')
        if binding and reference is None:
            reference = {'id': binding['entity_id'], 'version': binding['version']}
        previous = existing.get(reference['id']) if reference else None
        if reference and (previous is None or previous['version'] != reference['version'] or previous['kind'] != item['kind']):
            raise KnowledgeError('增量审阅引用的已有知识必须属于本轮固定上下文且类型和版本一致', 409)
        if binding and (previous is None or previous['id'] != binding['entity_id']):
            raise KnowledgeError('增量审阅不能改变已经确认的来源知识身份', 409)
        identity = previous['id'] if previous else str(uuid5(UUID(source_id), 'entity:' + key))
        after = _effective_after(item, previous, identity)
        before = _before(previous) if previous else None
        field_labels = {'name': '名称', 'description': '说明', 'attributes': '知识属性',
                        'status': '生命周期', 'acceptance_items': '验收条件'}
        changed_fields = [label for field, label in field_labels.items() if before is None or before[field] != after[field]]
        operation = 'added' if previous is None else ('updated' if changed_fields else 'evidence_only')
        changes.append({'table': 'entity', 'key': key, 'operation': operation, 'before': before, 'after': after,
                        'changed_fields': changed_fields, 'entity_ref': _ref(previous) if previous else None})
        resolved[key], after_records[key] = (identity, after['version']), after
        if previous and not binding:
            add('cross_source_binding', [key, identity], '确认复用已有知识',
                '本来源尚未确认此知识身份；请核对是否确为同一业务对象。确认后复用已有 ID，不按名称自动合并。',
                keys=[key], record=previous, before=before, after=after, evidence=item.get('evidence', []),
                actions=('confirm_binding',))
        if previous and item.get('acceptance_items') is not None and before['acceptance_items'] != after['acceptance_items']:
            old_items, new_items = before['acceptance_items'], after['acceptance_items']
            new_keys = {ac['key'] for ac in new_items}
            new_texts = {ac['text'] for ac in new_items}
            removed = [ac for ac in old_items if (ac['key'] not in new_keys if ac['key'] is not None else ac['text'] not in new_texts)]
            changed = [ac for ac in old_items if any(new['key'] == ac['key'] and new['text'] != ac['text'] for new in new_items)]
            # 只有新增条件不称为删除或修改，新增已显示在内容差异中。
            if removed or changed:
                add('acceptance_changed', [key, identity], '验收条件发生修改或移除',
                    f'原验收条件中有 {len(changed)} 项文字修改、{len(removed)} 项不再出现。证据变化不计入此项；请核对本轮验收范围。',
                    keys=[key], record=previous, before=old_items, after=new_items,
                    evidence=[quote for ac in item['acceptance_items'] for quote in ac.get('evidence', [])],
                    removed_items=removed, updated_items=changed)

    matching_truncated = 0
    for key, item in selected.items():
        matches = []
        current_id = resolved[key][0]
        name = _normal_name(item['name'])
        for record in existing.values():
            if record['id'] == current_id or record['kind'] != item['kind'] or record.get('status') == 'archived':
                continue
            other = _normal_name(record['name'])
            ratio = SequenceMatcher(None, name, other, autojunk=False).ratio()
            if name == other or (min(len(name), len(other)) >= 4 and ratio >= .88):
                matches.append((name == other, ratio, record))
        matches.sort(key=lambda match: (-int(match[0]), -match[1], match[2]['id']))
        matching_truncated += max(0, len(matches) - 5)
        for exact, ratio, record in matches[:5]:
            add('possible_duplicate', ['existing', key, record['id']], '可能与已有知识重复',
                ('名称相同' if exact else '名称文字相近') + '仅是匹配线索，不代表业务身份相同。若为同一对象，请修改已有知识绑定并重新检查；否则说明保留独立身份的依据。',
                keys=[key], record=record, before=_before(record), after=after_records[key],
                evidence=item.get('evidence', []), actions=('keep_distinct',),
                match_basis='name_exact' if exact else 'name_similar', lexical_similarity=round(ratio, 4))
    clusters = {}
    for key, item in sorted(selected.items()):
        clusters.setdefault((item['kind'], _normal_name(item['name'])), []).append(key)
    for keys in clusters.values():
        if len(keys) > 1:
            add('possible_duplicate', ['candidates', keys], '本轮候选可能重复',
                '一组同类型候选使用相同名称。可取消重复候选并调整关系后重新检查；保留多项时需说明各自不同身份。',
                keys=keys, before=None, after=[after_records[key] for key in keys],
                evidence=[quote for key in keys for quote in selected[key].get('evidence', [])],
                actions=('keep_distinct',), match_basis='candidate_name_exact')

    for link in candidate.get('relationships', []):
        start = resolved.get(link['source_key'])
        end = resolved.get(link['target_key'])
        if start is None or end is None:
            raise KnowledgeError('增量审阅的关系两端必须同时选中')
        identity = _same_relation_ends(link['type'], start[0], end[0])
        bound = relation_bindings.get(link['key'])
        previous = relations.get(bound['relationship_id']) if bound else None
        if previous is None:
            previous = next((record for record in relations.values()
                if _same_relation_ends(record['type'], record['source_id'], record['target_id']) == identity), None)
        if link['type'] == 'CONFLICTS_WITH' and start[0] > end[0]:
            start, end = end, start
        relation_id = previous['id'] if previous else str(uuid5(UUID(source_id), 'relationship:' + link['key']))
        after = {'id': relation_id, 'version': previous['version'] + 1 if previous else 1,
                 'type': link['type'], 'source_id': start[0], 'source_version': start[1],
                 'target_id': end[0], 'target_version': end[1], 'rationale': link['rationale'], 'status': 'proposed'}
        fields = {'type': '关系类型', 'source_id': '起点身份', 'source_version': '起点版本',
                  'target_id': '终点身份', 'target_version': '终点版本', 'rationale': '关系依据', 'status': '关系状态'}
        before = {field: previous[field] for field in ['id', 'version', *fields]} if previous else None
        changed = [label for field, label in fields.items() if before is None or before[field] != after[field]]
        version_refresh = previous is not None and bool(changed) and set(changed) <= {'起点版本', '终点版本'}
        changes.append({'table': 'relationship', 'key': link['key'],
            'operation': 'added' if previous is None else ('updated' if changed else 'evidence_only'),
            'before': before, 'after': after, 'changed_fields': changed,
            'version_refresh_only': version_refresh,
            'change_note': '仅刷新固定端点版本，不表示关系含义改变。' if version_refresh else '',
            'entity_ref': None, 'relationship_ref': {'id': previous['id'], 'version': previous['version']} if previous else None})
        if link['type'] == 'CONFLICTS_WITH' and previous is None:
            add('proposed_conflict', [link['key'], start[0], end[0]], '本轮提出新的冲突关系',
                '提交将记录两项知识之间待确认的冲突关系。请对照双方内容及原文明确冲突范围；记录关系不表示冲突已经解决。',
                keys=[link['source_key'], link['target_key']], before=None, after=after,
                evidence=link.get('evidence', []), actions=('keep_both',))

    selected_ids = {value[0] for value in resolved.values()}
    full_scope = context.get('source_scope', 'full') == 'full'
    for key, binding in (bindings.items() if full_scope else []):
        record = existing.get(binding['entity_id'])
        if key in selected or record is None or record.get('status') == 'archived' or record['id'] in selected_ids:
            continue
        missing.append({'table': 'entity', 'key': key, **_ref(record)})
        add('missing_entity', [key, record['id']], '已有知识本轮未出现',
            '本来源此前确认的知识未被本轮选中。缺失不等于删除；可以保留，或记录移除建议后另建生命周期审阅提案。',
            record=record, before=_before(record), actions=('keep_existing', 'defer_retirement'))
    selected_link_keys = {link['key'] for link in candidate.get('relationships', [])}
    for key, binding in (relation_bindings.items() if full_scope else []):
        record = relations.get(binding['relationship_id'])
        if key in selected_link_keys or record is None or record.get('status') in {'retired', 'rejected'}:
            continue
        missing.append({'table': 'relationship', 'key': key, 'id': record['id'], 'version': record['version'],
                        'kind': record['type'], 'name': record.get('rationale', record['type'])})
        add('missing_relationship', [key, record['id']], '已有关系本轮未出现',
            '未选中原关系不会将其退役。可以保留，或记录移除建议后另建关系生命周期审阅提案。',
            before=record, actions=('keep_existing', 'defer_retirement'),
            relationship_ref={'id': record['id'], 'version': record['version']})

    for record in relations.values():
        touched = [key for key, value in resolved.items() if value[0] in {record['source_id'], record['target_id']}]
        if record['type'] != 'CONFLICTS_WITH' or record.get('status') in {'rejected', 'retired'} or not touched:
            continue
        other_id = record['target_id'] if resolved[touched[0]][0] == record['source_id'] else record['source_id']
        add('recorded_conflict', record['id'], '已有冲突关系涉及本轮知识',
            '固定上下文中已有显式冲突关系。请结合其保存的端点版本核对适用性；本轮不会自动删除或解除这条关系。',
            keys=touched, record=existing.get(other_id), before=record,
            after=[after_records[key] for key in touched], actions=('accept_change', 'keep_both'),
            relationship_ref={'id': record['id'], 'version': record['version']})

    for hint in candidate.get('review_hints', []):
        key = hint.get('candidate_key')
        if key is not None and key not in selected:
            continue
        reference = hint['existing']
        record = existing.get(reference['id'])
        if record is None or record['version'] != reference['version']:
            raise KnowledgeError('模型审阅提示必须引用固定上下文中的已有知识版本', 409)
        kind = hint['kind']
        common = {'keys': [key] if key else [], 'record': record, 'before': _before(record),
                  'after': after_records.get(key), 'evidence': hint.get('evidence', []),
                  'origin': 'model', 'existing_quote': hint.get('existing_quote')}
        if kind == 'possible_duplicate':
            if key and resolved[key][0] == record['id']:
                continue
            add(kind, ['existing', key, record['id']], '模型提示可能重复',
                hint['rationale'] + '\n模型匹配仅为建议；复用身份须修改绑定并重新检查。',
                actions=('keep_distinct',), match_basis='model_suggestion', **common)
        elif kind == 'possible_conflict':
            actions = ('accept_change',) if key and resolved[key][0] == record['id'] else ('keep_both',)
            add(kind, [key, record['id']], '模型提示可能存在语义冲突',
                hint['rationale'] + '\n请核对两侧内容与适用范围；模型提示尚未成为正式冲突关系。',
                actions=actions, **common)
        elif kind == 'retirement':
            add('retirement_suggestion', [key, record['id']], '模型提出移除建议',
                hint['rationale'] + '\n本轮只记录审阅结论，不归档实体；实际移除须另提生命周期审阅。',
                actions=('keep_existing', 'defer_retirement'), **common)
    values = sorted(findings.values(), key=lambda value: (value['kind'], value['id']))
    summary = {name: sum(item['operation'] == name for item in changes) for name in ('added', 'updated', 'evidence_only')}
    summary.update(findings=len(values), missing=len(missing),
                   semantic_checked=bool(context.get('review_contract_version', 0) >= 2),
                   matching_truncated=matching_truncated)
    return {'schema_version': 1, 'changes': changes, 'findings': values, 'missing': missing, 'summary': summary,
            'notes': ['相同或相近名称仅用于提示；模型语义检查不保证发现全部冲突。',
                      '同一内容的新原文依据仍形成补充证据修订；缺失项不会自动删除。',
                      *(['同名或近名匹配每个候选最多显示 5 项，其余匹配未完整显示。'] if matching_truncated else [])]}


def validate_decisions(report, decisions):
    """校验全部检查项均有明确处理和依据；返回稳定排序的可留档结论。"""
    report, decisions = _json(report), _json(decisions)
    expected = {item['id']: item for item in report.get('findings', [])}
    seen, normalized = set(), []
    for decision in decisions:
        finding_id = decision.get('finding_id')
        if finding_id not in expected:
            raise KnowledgeError('审阅结论包含当前报告不存在的检查项，请重新检查', 409)
        if finding_id in seen:
            raise KnowledgeError('同一检查项只能记录一个审阅结论')
        allowed = {action['value'] for action in expected[finding_id]['allowed_actions']}
        if decision.get('action') not in allowed:
            raise KnowledgeError('请为每个检查项选择允许的明确处理方式')
        reason = decision.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise KnowledgeError('请填写每个检查项的审阅依据')
        if len(reason) > 4000:
            raise KnowledgeError('检查项审阅依据不能超过 4000 字符')
        seen.add(finding_id)
        normalized.append({'finding_id': finding_id, 'action': decision['action'], 'reason': reason.strip()})
    if any(item.get('required', True) and item['id'] not in seen for item in expected.values()):
        raise KnowledgeError('存在未处理的增量审阅检查项，请逐项选择处理方式并填写依据', 409)
    return sorted(normalized, key=lambda item: item['finding_id'])
