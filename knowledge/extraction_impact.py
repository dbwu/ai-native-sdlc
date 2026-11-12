"""M2 对固定知识图谱的只读影响追溯，不把潜在影响当作已发生的变更。"""
from collections import defaultdict, deque
from copy import deepcopy
import hashlib
import json
from uuid import UUID

from fastapi.encoders import jsonable_encoder

from .analysis import FORWARD_TYPES, REVERSE_TYPES
from .semantics import KnowledgeError


ENTITY_FIELDS = ('id', 'version', 'kind', 'name', 'status')
MESSAGE = '仅基于固定快照中的显式关系和模型引用列出待复核对象，不代表这些对象已经修改或影响已经证实。'


def capture_impact_snapshot(conn, project_id, *, max_entities=2000, max_relationships=4000):
    """由调用方在已有项目事务中冻结轻量图谱；不装载源码、证据正文或历史全集。"""
    if not 1 <= max_entities <= 10000 or not 1 <= max_relationships <= 20000:
        raise KnowledgeError('影响快照实体上限须为 1 至 10000，关系上限须为 1 至 20000')
    project_id = UUID(str(project_id))
    entities = conn.execute('''SELECT id,version,kind,status,document->>'name' AS name
        FROM current_entities WHERE project_id=%s AND kind NOT IN ('Project','Evidence')
        ORDER BY id LIMIT %s''', (project_id, max_entities + 1)).fetchall()
    relationships = conn.execute('''SELECT id,version,type,status,source_id,target_id,source_version,target_version
        FROM current_relationships WHERE project_id=%s AND status IN ('proposed','verified')
        ORDER BY id LIMIT %s''', (project_id, max_relationships + 1)).fetchall()
    baseline = conn.execute('''SELECT version,proposal_id,document FROM project_model_revision
        WHERE project_id=%s ORDER BY version DESC LIMIT 1''', (project_id,)).fetchone()
    captured_at = conn.execute('SELECT clock_timestamp() AS captured_at').fetchone()['captured_at']
    model = baseline['document'].get('model', {}) if baseline else {}
    reasons = []
    if len(entities) > max_entities:
        reasons.append('实体快照达到数量上限，影响结果可能不完整。')
    if len(relationships) > max_relationships:
        reasons.append('关系快照达到数量上限，影响结果可能不完整。')
    value = jsonable_encoder({
        'project_id': project_id, 'captured_at': captured_at,
        'entities': entities[:max_entities], 'relationships': relationships[:max_relationships],
        'baseline': None if baseline is None else {
            'project_id': project_id, 'version': baseline['version'], 'proposal_id': baseline['proposal_id'],
            'nodes': [{key: node.get(key) for key in ('key', 'name', 'kind', 'entity_id', 'entity_version')}
                      for node in model.get('nodes', [])],
            'processes': [{
                'key': process['key'], 'name': process['name'],
                'requirement_refs': process.get('requirement_refs', []),
                'rule_refs': process.get('rule_refs', []),
                'module_keys': sorted({step['owner'] for step in process.get('steps', [])}),
                'scenarios': [{
                    'key': scenario['key'], 'name': scenario['name'],
                    'module_keys': sorted({message[end] for message in scenario.get('messages', [])
                                           for end in ('source', 'target')}),
                } for scenario in process.get('scenarios', [])],
            } for process in model.get('processes', [])],
        },
        'truncated': bool(reasons), 'truncation_reasons': reasons,
        'limits': {'max_entities': max_entities, 'max_relationships': max_relationships},
    })
    value['sha256'] = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                               separators=(',', ':')).encode()).hexdigest()
    return value


def _summary(entity):
    return {key: entity[key] for key in ENTITY_FIELDS}


def _model_references(baseline, visited, entities):
    """模型级依据只到实际有引用的粒度，不从流程依据猜测具体步骤或消息受影响。"""
    if not baseline:
        return []
    baseline_ref = {key: baseline[key] for key in ('project_id', 'version', 'proposal_id')}
    choices = defaultdict(list)

    def add(subject, name, entity_id, referenced_version, role, *, module_key=None):
        if not entity_id or str(entity_id) not in visited:
            return
        entity_id = str(entity_id)
        current = entities[entity_id]
        route = visited[entity_id]
        edge = {
            'edge_kind': 'model_reference', 'from': _summary(current),
            'reference_role': role, 'referenced_version': referenced_version,
            'current_version': current['version'],
            'stale_reference': referenced_version is not None and referenced_version != current['version'],
            'reference_version_missing': referenced_version is None,
            'baseline_ref': deepcopy(baseline_ref), 'subject': deepcopy(subject),
        }
        if module_key is not None:
            edge['module_key'] = module_key
        value = {
            'subject': subject, 'name': name, 'baseline_ref': deepcopy(baseline_ref),
            'root_id': route['root_id'], 'depth': route['depth'] + 1,
            'level': 'direct' if route['depth'] == 0 else 'indirect',
            'path': deepcopy(route['path']) + [edge], 'code': 'POTENTIAL_MODEL_IMPACT',
            'message': '此模型对象显式引用受影响知识或模块，需复核固定基线；未自动判定其步骤或消息已经变化。',
        }
        identity = (subject['kind'], subject.get('process_key', ''), subject['key'])
        choices[identity].append(value)

    nodes = {node['key']: node for node in baseline.get('nodes', [])}
    for node in nodes.values():
        add({'kind': 'architecture_node', 'key': node['key']}, node['name'],
            node.get('entity_id'), node.get('entity_version'), 'node_binding')
    for process in baseline.get('processes', []):
        subject = {'kind': 'process', 'key': process['key']}
        for field in ('requirement_refs', 'rule_refs'):
            for reference in process.get(field, []):
                add(subject, process['name'], reference['id'], reference['version'], field)
        module_keys = set(process.get('module_keys', []))
        for scenario in process.get('scenarios', []):
            module_keys.update(scenario.get('module_keys', []))
            for key in scenario.get('module_keys', []):
                node = nodes.get(key)
                if node:
                    add({'kind': 'scenario', 'key': scenario['key'], 'process_key': process['key']},
                        f'{process["name"]} · {scenario["name"]}', node.get('entity_id'), node.get('entity_version'),
                        'module_usage', module_key=key)
        for key in sorted(module_keys):
            node = nodes.get(key)
            if node:
                add(subject, process['name'], node.get('entity_id'), node.get('entity_version'),
                    'module_usage', module_key=key)
    result = []
    for identity in sorted(choices):
        # 一个对象可能有多条依据，保留确定的最短解释路径和其余固定引用，避免误认为只有一条依据。
        options = sorted(choices[identity], key=lambda item: (
            item['depth'], item['root_id'], item['path'][-1]['from']['id'],
            item['path'][-1]['reference_role'], item['path'][-1].get('module_key', '')))
        item = options[0]
        item['supporting_references'] = [deepcopy(option['path'][-1]) for option in options]
        result.append(item)
    return sorted(result, key=lambda item: (item['depth'], item['subject']['kind'],
                                            item['subject'].get('process_key', ''), item['subject']['key']))


def build_impact(snapshot, changed_refs, *, max_depth=4, max_nodes=200):
    """在已冻结快照中查找最短有向路径；节点配额包含起点、知识与模型对象。"""
    if not 0 <= max_depth <= 12 or not 1 <= max_nodes <= 1000:
        raise KnowledgeError('影响追溯深度须为 0 至 12，节点上限须为 1 至 1000')
    if snapshot is None:
        return {'available': False, 'roots': [], 'affected': [], 'model_affected': [],
                'unavailable_roots': [], 'max_depth': max_depth, 'max_nodes': max_nodes,
                'truncated': False, 'truncation_reasons': [],
                'message': '历史任务未冻结 M2 影响快照，不能把今天的关系回填为当时的影响事实。'}
    snapshot = jsonable_encoder(snapshot)
    entities = {str(item['id']): item for item in snapshot.get('entities', [])}
    unique_refs = {}
    for reference in jsonable_encoder(changed_refs):
        identity = str(reference['id'])
        if identity in unique_refs and unique_refs[identity]['version'] != reference['version']:
            raise KnowledgeError('同一变更起点不能同时引用多个版本')
        unique_refs[identity] = reference
    roots, unavailable = [], []
    for identity in sorted(unique_refs):
        reference, entity = unique_refs[identity], entities.get(identity)
        if entity is None:
            unavailable.append({**reference, 'reason': '变更起点未包含在固定影响快照中。'})
        elif entity['version'] != reference['version']:
            raise KnowledgeError('变更起点版本与固定影响快照不一致，请重新核对审阅上下文', 409)
        else:
            roots.append(_summary(entity))
    if len(roots) > max_nodes:
        raise KnowledgeError('影响节点上限不能小于变更起点数量')
    reasons = list(snapshot.get('truncation_reasons', []))

    def truncated(reason):
        if reason not in reasons:
            reasons.append(reason)

    if unavailable:
        truncated('部分变更起点未包含在固定快照中，影响结果不完整。')
    if snapshot.get('truncated') and not reasons:
        truncated('固定影响快照已被截断，影响结果可能不完整。')
    adjacency = defaultdict(list)
    for relationship in snapshot.get('relationships', []):
        if relationship['status'] not in {'proposed', 'verified'}:
            continue
        source, target = str(relationship['source_id']), str(relationship['target_id'])
        directions = []
        if relationship['type'] in FORWARD_TYPES:
            directions.append((source, target, 'forward'))
        if relationship['type'] in REVERSE_TYPES:
            directions.append((target, source, 'reverse'))
        for start, end, direction in directions:
            if start not in entities or end not in entities:
                # 证据实体刻意不参与工程影响传播；截断导致的缺失由快照元数据报告。
                continue
            edge = {
                'edge_kind': 'relationship', 'relationship_id': str(relationship['id']),
                'relationship_version': relationship['version'], 'type': relationship['type'],
                'status': relationship['status'], 'direction': direction,
                'from': _summary(entities[start]), 'to': _summary(entities[end]),
                'source_version': relationship['source_version'], 'target_version': relationship['target_version'],
                'current_source_version': entities[source]['version'],
                'current_target_version': entities[target]['version'],
                'stale_endpoints': (relationship['source_version'] != entities[source]['version']
                                    or relationship['target_version'] != entities[target]['version']),
                'inactive_endpoints': any(entities[identity]['status'] in {'deprecated', 'archived'}
                                          for identity in (source, target)),
            }
            adjacency[start].append(edge)
    for edges in adjacency.values():
        edges.sort(key=lambda edge: (edge['to']['id'], edge['relationship_id'], edge['direction']))
    visited = {item['id']: {'root_id': item['id'], 'depth': 0, 'path': []} for item in roots}
    pending = deque(item['id'] for item in roots)
    affected = []
    while pending:
        current_id = pending.popleft()
        route = visited[current_id]
        for edge in adjacency[current_id]:
            target_id = edge['to']['id']
            if target_id in visited:
                continue
            if route['depth'] >= max_depth:
                truncated('达到影响追溯深度上限，仍有未展开的关系或模型引用。')
                continue
            if len(visited) >= max_nodes:
                truncated('达到影响节点数量上限，仍有未列出的关系或模型引用。')
                continue
            next_route = {'root_id': route['root_id'], 'depth': route['depth'] + 1,
                          'path': route['path'] + [edge]}
            visited[target_id] = next_route
            pending.append(target_id)
            affected.append({**_summary(entities[target_id]), **deepcopy(next_route),
                             'level': 'direct' if next_route['depth'] == 1 else 'indirect',
                             'code': 'POTENTIAL_IMPACT', 'message': MESSAGE})
    model_affected = []
    for item in _model_references(snapshot.get('baseline'), visited, entities):
        if item['depth'] > max_depth:
            truncated('达到影响追溯深度上限，仍有未展开的关系或模型引用。')
        elif len(visited) + len(model_affected) >= max_nodes:
            truncated('达到影响节点数量上限，仍有未列出的关系或模型引用。')
        else:
            model_affected.append(item)
    return {'available': True, 'project_id': snapshot['project_id'],
            'snapshot_sha256': snapshot.get('sha256'), 'captured_at': snapshot.get('captured_at'),
            'roots': roots, 'affected': affected, 'model_affected': model_affected,
            'unavailable_roots': unavailable, 'max_depth': max_depth, 'max_nodes': max_nodes,
            'truncated': bool(reasons), 'truncation_reasons': reasons, 'message': MESSAGE}
