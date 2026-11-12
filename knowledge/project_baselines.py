"""项目级架构和业务流程的候选、固定版本、差异及 Markdown 交付。"""
from copy import deepcopy
from datetime import timezone
from typing import Literal
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb
from pydantic import Field

from .db import connect
from .models import EntityCreate, Kind, RelationType, StrictModel, Text
from .operations import KnowledgeOperations
from .project_architecture_views import logical_structure_diagram, project_collaboration_diagram
from .project_model import ProjectModel, architecture_diagram, edge_identity as _edge_identity, process_diagrams
from .requirement_deliveries import _text
from .semantics import KnowledgeError, validate_types
from .service import KnowledgeService as KS


class BaselineRequest(StrictModel):
    expected_version: int = Field(ge=0, strict=True, title='预期项目基线版本（首版填 0）')
    actor: Text = Field(title='提案署名')


class ProjectModelRequest(BaselineRequest):
    model: ProjectModel


class CourseModelRequest(BaselineRequest):
    release: Literal['v1', 'v1.1', 'v1.2']


def _version(conn, project_id):
    return conn.execute('SELECT coalesce(max(version), 0) AS version FROM project_model_revision WHERE project_id=%s',
                        (project_id,)).fetchone()['version']


def _entity_refs(conn, project_id, refs, kind):
    items = []
    for ref in refs:
        record = KS._get(conn, 'entity', ref.id, ref.version)
        if record['project_id'] != project_id or record['kind'] != kind or record['status'] == 'archived':
            raise KnowledgeError('流程依据必须引用本项目相应类型、未归档的知识版本')
        items.append({'id': str(ref.id), 'version': ref.version, 'name': record['name']})
    return items


def _without_empty_keys(value):
    """兼容旧快照缺少可选 key 的写法，不把补出的 null 视作业务修改。"""
    if isinstance(value, dict):
        return {key: _without_empty_keys(item) for key, item in value.items() if key != 'key' or item is not None}
    if isinstance(value, list):
        return [_without_empty_keys(item) for item in value]
    return value


def _diff(before, after, *, before_version=None, after_version=None):
    changes = []

    def compare(kind, old, new, describe, *, subject_kind, process_key=None, scenario_key=None,
                old_process=None, new_process=None, reordered=()):
        for key in dict.fromkeys([*new, *old]):
            previous, current = old.get(key), new.get(key)
            left, right = _without_empty_keys(previous), _without_empty_keys(current)
            if subject_kind == 'message':
                left = {k: v for k, v in left.items() if k != 'position'} if left else left
                right = {k: v for k, v in right.items() if k != 'position'} if right else right
            if left == right and key not in reordered:
                continue
            old_label, old_detail = describe(previous, before) if previous is not None else ('', '')
            new_label, new_detail = describe(current, after) if current is not None else ('', '')
            op = 'added' if previous is None else 'removed' if current is None else 'updated'
            if op == 'updated':
                left = f'{old_label} · {old_detail}' if old_detail else old_label
                right = f'{new_label} · {new_detail}' if new_detail else new_label
                detail = f'{left} → {right}'
            else:
                detail = new_detail or old_detail
            subject = {'kind': subject_kind, 'key': key, 'identity': 'stable_key'}
            if isinstance(key, tuple):
                subject.update(key=key[1] if key[0] == 'key' else None,
                               identity='stable_key' if key[0] == 'key' else 'endpoint_pair')
                if key[0] == 'endpoints':
                    subject.update(source_key=key[1], target_key=key[2])
            if process_key is not None:
                subject['process_key'] = process_key
            if scenario_key is not None:
                subject['scenario_key'] = scenario_key
            flags = []
            if op == 'updated' and isinstance(previous, dict) and isinstance(current, dict):
                if previous.get('name') != current.get('name'):
                    flags.append('renamed')
                if (previous.get('parent_key') != current.get('parent_key')
                        or previous.get('owner') != current.get('owner')):
                    flags.append('moved')
                if previous.get('entity_version') != current.get('entity_version'):
                    flags.append('revision_changed')
                if subject_kind in {'requirement_refs', 'rule_refs'} and previous.get('version') != current.get('version'):
                    flags.append('revision_changed')
                if any(previous.get(field) != current.get(field) for field in ('source', 'target')):
                    flags.append('endpoints_changed')
                ignored = {'name', 'parent_key', 'owner', 'entity_version', 'position', 'key', 'source', 'target'}
                if subject_kind in {'requirement_refs', 'rule_refs'}:
                    ignored.add('version')
                if ({k: v for k, v in previous.items() if k not in ignored}
                        != {k: v for k, v in current.items() if k not in ignored}):
                    flags.append('content_changed')
            if key in reordered:
                flags.append('reordered')
            changes.append({'op': op, 'kind': kind, 'label': new_label or old_label, 'detail': detail,
                            'subject': subject, 'before': deepcopy(previous), 'after': deepcopy(current),
                            'change_flags': flags,
                            'comparison': {'before_version': before_version, 'after_version': after_version},
                            'requirement_refs': {'before': deepcopy((old_process or {}).get('requirement_refs', [])),
                                                 'after': deepcopy((new_process or {}).get('requirement_refs', []))},
                            'rule_refs': {'before': deepcopy((old_process or {}).get('rule_refs', [])),
                                          'after': deepcopy((new_process or {}).get('rule_refs', []))}})

    compare('基线说明', {'about': (before['title'], before['summary'])} if before else {},
            {'about': (after['title'], after['summary'])}, lambda item, _: item, subject_kind='baseline')

    def node_description(node, model):
        names = {item['key']: item['name'] for item in model['nodes']}
        return node['name'], ' · '.join(filter(None, (names.get(node['parent_key']), node['description'],
                                                     f'知识 v{node["entity_version"]}')))

    for node_kind, label in (('domain', '领域'), ('module', '模块')):
        old_nodes = {node['key']: node for node in before.get('nodes', []) if node['kind'] == node_kind}
        new_nodes = {node['key']: node for node in after['nodes'] if node['kind'] == node_kind}
        compare(label, old_nodes, new_nodes, node_description, subject_kind=node_kind)

    def dependency(link, model):
        names = {node['key']: node['name'] for node in model['nodes']}
        return f'{names[link["source"]]} → {names[link["target"]]}', link['label']

    def dependencies(model):
        return {_edge_identity(link): link for link in model.get('dependencies', [])}

    compare('模块依赖', dependencies(before), dependencies(after), dependency, subject_kind='dependency')

    def ports(model):
        return {item['key']: item for item in model.get('architecture_ports', [])}

    def collaboration(link, model):
        names = {item['key']: item['name'] for item in [*model['nodes'], *ports(model).values()]}
        return f'{names[link["source"]]} → {names[link["target"]]}', link['label']

    compare('接口与数据对象', ports(before), ports(after),
            lambda item, _: (item['name'], f'知识 v{item["knowledge"]["version"]}'), subject_kind='architecture_port')
    compare('项目协作', {item['key']: item for item in before.get('collaboration_links', [])},
            {item['key']: item for item in after.get('collaboration_links', [])},
            collaboration, subject_kind='project_collaboration')
    old_processes = {row['key']: row for row in before.get('processes', [])}
    new_processes = {row['key']: row for row in after['processes']}
    compare('业务流程', {key: {'name': item['name'], 'description': item['description']} for key, item in old_processes.items()},
            {key: {'name': item['name'], 'description': item['description']} for key, item in new_processes.items()},
            lambda item, _: (item['name'], item['description']),
            subject_kind='process')
    empty_process = {field: [] for field in ('steps', 'transitions', 'scenarios', 'participants', 'requirement_refs', 'rule_refs')}
    for key in dict.fromkeys([*new_processes, *old_processes]):
        old, new = old_processes.get(key, empty_process), new_processes.get(key, empty_process)
        name = (new_processes.get(key) or old_processes[key])['name']
        context = {'process_key': key, 'old_process': old, 'new_process': new}
        compare('流程步骤', {item['key']: item for item in old['steps']}, {item['key']: item for item in new['steps']},
                lambda item, model: (f'{name} · {item["name"]}', _step_owner(item['owner'], model, key)),
                subject_kind='step', **context)
        compare('流程分支', _transitions(old), _transitions(new),
                lambda item, model: _transition_description(item, model, key, name), subject_kind='transition', **context)
        compare('时序场景', {item['key']: item for item in old['scenarios']}, {item['key']: item for item in new['scenarios']},
                lambda item, model: _scenario_description(item, model, key, name), subject_kind='scenario', **context)
        old_scenarios = {item['key']: item for item in old['scenarios']}
        new_scenarios = {item['key']: item for item in new['scenarios']}
        for scenario_key in dict.fromkeys([*new_scenarios, *old_scenarios]):
            old_messages = old_scenarios.get(scenario_key, {}).get('messages', [])
            new_messages = new_scenarios.get(scenario_key, {}).get('messages', [])
            # 缺少稳定消息标识的历史只进行场景级比较，不按位置或文本猜测对应关系。
            if any(not item.get('key') for item in [*old_messages, *new_messages]):
                continue
            common = {item['key'] for item in old_messages} & {item['key'] for item in new_messages}
            old_order = [item['key'] for item in old_messages if item['key'] in common]
            new_order = [item['key'] for item in new_messages if item['key'] in common]
            reordered = {item for index, item in enumerate(old_order) if new_order[index] != item}
            compare('时序消息', {item['key']: {**item, 'position': index} for index, item in enumerate(old_messages)},
                    {item['key']: {**item, 'position': index} for index, item in enumerate(new_messages)},
                    lambda item, model: (f'{name} · {item["label"]}',
                        f'{_step_owner(item["source"], model, key)} → {_step_owner(item["target"], model, key)}'),
                    subject_kind='message', scenario_key=scenario_key, reordered=reordered, **context)
        compare('流程参与者', {item['key']: item for item in old['participants']},
                {item['key']: item for item in new['participants']}, lambda item, _: (f'{name} · {item["name"]}', ''),
                subject_kind='participant', **context)
        for field, label in (('requirement_refs', '需求依据'), ('rule_refs', '规则依据')):
            compare(label, {item['id']: item for item in old[field]}, {item['id']: item for item in new[field]},
                    lambda item, model: (name, f'{item["name"]} · v{item["version"]}'), subject_kind=field, **context)
    return changes


def _step_owner(key, model, process_key):
    process = next(item for item in model['processes'] if item['key'] == process_key)
    return next(item['name'] for item in [*model['nodes'], *process['participants']] if item['key'] == key)


def _transitions(process):
    return {_edge_identity(item): item for item in process['transitions']}


def _transition_description(item, model, process_key, name):
    process = next(row for row in model['processes'] if row['key'] == process_key)
    steps = {step['key']: step['name'] for step in process['steps']}
    return f'{name} · {steps[item["source"]]} → {steps[item["target"]]}', item['condition'] or ''


def _scenario_description(item, model, process_key, name):
    messages = (f'{_step_owner(message["source"], model, process_key)} → '
                f'{_step_owner(message["target"], model, process_key)}（{message["label"]}）' for message in item['messages'])
    return f'{name} · {item["name"]}', ' / '.join(messages)


class ProjectBaselines:
    def __init__(self, database_url):
        self.database_url = database_url

    def propose(self, project_id, request):
        with connect(self.database_url) as conn:
            KS._lock(conn, project_id)
            KS._assert_project(conn, project_id)
            if _version(conn, project_id) != request.expected_version:
                raise KnowledgeError('项目基线已更新，请刷新并在最新版上重新提交', 409)
            if isinstance(request, CourseModelRequest):
                from .project_course import course_model
                model = course_model(conn, project_id, request.release)
            else:
                model = request.model
            return self._propose(conn, project_id, request, model)

    def _propose(self, conn, project_id, request, model, *, model_origin=None, model_exchange=None):
        previous_rows = conn.execute('SELECT document FROM project_model_revision WHERE project_id=%s ORDER BY version DESC',
                                     (project_id,)).fetchall()
        known = {node['key']: UUID(node['entity_id']) for row in reversed(previous_rows)
                 for node in row['document']['model']['nodes']}
        keys_by_id = {record_id: key for key, record_id in known.items()}
        version = request.expected_version + 1
        provenance = {'actor': request.actor, 'source_uri': f'urn:project-model:{project_id}', 'source_revision': str(version),
                      'change_reason': '项目架构与业务流程提案', 'nature': 'inference'}
        operations, baselines, nodes, resolved_ids = [], [], [], set()
        node_kinds = {}
        for node in model.nodes:
            known_id = known.get(node.key)
            if known_id and node.entity_id and node.entity_id != known_id:
                raise KnowledgeError(f'架构标识 {node.key} 已绑定另一条知识，请沿用原标识', 409)
            record_id = known_id or node.entity_id or uuid5(project_id, 'project-model:' + node.key)
            if record_id in resolved_ids or keys_by_id.get(record_id, node.key) != node.key:
                raise KnowledgeError(f'架构节点 {node.key} 与其他节点引用了同一条知识')
            resolved_ids.add(record_id)
            kind = Kind.DOMAIN if node.kind == 'domain' else Kind.COMPONENT
            node_kinds[node.key] = kind
            if known_id or node.entity_id:
                record = KS._get(conn, 'entity', record_id, node.entity_version)
                if record['project_id'] != project_id or record['kind'] != kind or record['status'] == 'archived':
                    raise KnowledgeError(f'架构节点 {node.name} 必须引用本项目相应类型、未归档的知识版本')
                entity_version = record['version']
                if node.entity_version is None:
                    baselines.append({'table': 'entity', 'id': str(record_id), 'version': entity_version})
            else:
                attributes = {'responsibility': node.description} if kind == Kind.COMPONENT and node.description else {}
                entity = EntityCreate(kind=kind, project_id=project_id, name=node.name, description=node.description,
                                      status='active', provenance=provenance, attributes=attributes)
                operations.append({'op': 'create_entity', 'id': str(record_id), 'data': entity.model_dump(mode='json')})
                entity_version = 1
            nodes.append(node.model_copy(update={'entity_id': record_id, 'entity_version': entity_version}))
        for node in model.nodes:
            if node.parent_key is not None:
                validate_types(RelationType.CONTAINS, node_kinds[node.parent_key], node_kinds[node.key])
        for link in model.dependencies:
            validate_types(RelationType.DEPENDS_ON, node_kinds[link.source], node_kinds[link.target])
        for port in model.architecture_ports:
            kind = Kind.INTERFACE if port.kind == 'interface' else Kind.DATA_MODEL
            _entity_refs(conn, project_id, [port.knowledge], kind)

        source_refs = {}
        for process in model.processes:
            source_refs[process.key] = {
                'requirement_refs': _entity_refs(conn, project_id, process.requirement_refs, Kind.REQUIREMENT),
                'rule_refs': _entity_refs(conn, project_id, process.rule_refs, Kind.BUSINESS_RULE),
            }
        snapshot = model.model_copy(update={'nodes': nodes}).model_dump(mode='json')
        changes = _diff(_with_source_names(previous_rows[0]['document']) if previous_rows else {},
                        _with_source_names({'model': snapshot, 'source_refs': source_refs}),
                        before_version=request.expected_version or None, after_version=version)
        if previous_rows and not changes:
            raise KnowledgeError('项目基线没有变化，请修改内容后再提出', 409)
        plan = {'operations': operations, 'baselines': baselines, 'entity_mapping': [], 'removed_keys': [],
                'project_model': {'version': version, 'before_version': request.expected_version or None,
                                  'model': snapshot, 'changes': changes, 'source_refs': source_refs}}
        if model_origin:
            plan['project_model']['model_origin'] = model_origin
        if model_exchange:
            plan['project_model_exchange'] = model_exchange
        return KnowledgeOperations(self.database_url, connection=conn)._create(
            conn, project_id, f'项目架构与业务流程 v{version} · {model.title}', request.actor, plan)

    def read(self, project_id, version=None):
        with connect(self.database_url) as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            if not conn.execute("SELECT 1 FROM entity WHERE id=%s AND kind='Project'", (project_id,)).fetchone():
                raise KnowledgeError('未找到当前项目', 404)
            versions = conn.execute("""SELECT version, proposal_id, recorded_at AS confirmed_at,
                document->'model'->>'title' AS title FROM project_model_revision
                WHERE project_id=%s ORDER BY version DESC""", (project_id,)).fetchall()
            if version is None and not versions:
                return {'versions': [], 'baseline': None}
            version = version or versions[0]['version']
            row = conn.execute('SELECT * FROM project_model_revision WHERE project_id=%s AND version=%s',
                               (project_id, version)).fetchone()
            if row is None:
                raise KnowledgeError('未找到当前项目的指定已确认基线', 404)
        document = row['document']
        model = ProjectModel.model_validate(document['model'])
        baseline = {'version': version, 'proposal_id': row['proposal_id'], 'confirmed_at': row['recorded_at'],
                    'title': model.title, 'summary': model.summary, 'model': document['model'],
                    'node_versions': {node.key: node.entity_version for node in model.nodes},
                    'architecture': architecture_diagram(model), 'logical_structure': logical_structure_diagram(model),
                    'collaboration': project_collaboration_diagram(model), 'changes': document['changes'],
                    'processes': [{'key': process.key, 'name': process.name, 'description': process.description,
                                   **document['source_refs'][process.key], 'diagrams': process_diagrams(model, process)}
                                  for process in model.processes],
                    'download_url': f'/workbench/projects/{project_id}/project-model/markdown?version={version}'}
        reference = {'project_id': str(project_id), 'version': version, 'proposal_id': str(row['proposal_id'])}
        baseline['architecture']['baseline_ref'] = reference
        baseline['logical_structure']['baseline_ref'] = reference
        if baseline['collaboration']:
            baseline['collaboration']['baseline_ref'] = reference
        for process in baseline['processes']:
            for diagram in process['diagrams']:
                diagram['baseline_ref'] = reference
        if 'before_version' in document:
            baseline['comparison'] = {'before_version': document['before_version'], 'after_version': version}
        if document.get('model_origin'):
            baseline['model_origin'] = document['model_origin']
        return {'versions': versions, 'baseline': baseline}


def _with_source_names(document):
    return {**document['model'], 'processes': [{**item, **document['source_refs'][item['key']]}
                                               for item in document['model']['processes']]}


def baseline_markdown(baseline):
    when = baseline['confirmed_at'].astimezone(timezone.utc).isoformat()
    lines = [f'# {_text(baseline["title"])}', '', f'- 项目基线 v{baseline["version"]}',
             f'- 确认时间 {when}', f'- 审阅提案 `{baseline["proposal_id"]}`', '', _text(baseline['summary']), '', '## 架构总览']
    if baseline.get('comparison'):
        previous = baseline['comparison']['before_version']
        lines.insert(5, f'- 比较范围 {"首次创建" if previous is None else f"v{previous}"} → v{baseline["version"]}')
    if origin := baseline.get('model_origin'):
        lines[5:5] = [f'- 模型分析 {_text(origin["model"])} · 设计流程 `{origin["design_id"]}` · v{origin["design_version"]}',
                      f'- 模型输出摘要 `{origin["output_sha256"]}`']

    def diagram_block(diagram):
        return ['', '```mermaid', diagram['mermaid'], '```', '']

    lines.extend(diagram_block(baseline['architecture']))
    if baseline.get('logical_structure'):
        lines.extend(['## 逻辑结构总览', *diagram_block(baseline['logical_structure'])])
    if baseline.get('collaboration'):
        lines.extend(['## 接口与协作', *diagram_block(baseline['collaboration'])])
        for link in baseline['model']['collaboration_links']:
            lines.append(f'- {_text(link["label"])} · {_text(link["rationale"])}')
        lines.append('')
    for process in baseline['processes']:
        lines.extend(['', f'## {_text(process["name"])}', '', _text(process['description'])])
        for field, label in (('requirement_refs', '需求'), ('rule_refs', '业务规则')):
            for ref in process[field]:
                lines.append(f'- {label} {_text(ref["name"])} · v{ref["version"]} · `{ref["id"]}`')
        for diagram in process['diagrams']:
            lines.extend(['', f'### {_text(diagram["title"])}', *diagram_block(diagram)])
    lines.extend(['', '## 本轮变更', ''])
    labels = {'added': '新增', 'updated': '修改', 'removed': '移除'}
    for item in baseline['changes']:
        detail = f' · {_text(item["detail"])}' if item['detail'] else ''
        lines.append(f'- {labels[item["op"]]}{_text(item["kind"])} · {_text(item["label"])}{detail}')
    return '\n'.join(lines).rstrip() + '\n'
