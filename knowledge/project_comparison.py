"""按固定项目基线比较图形，并以 Mermaid 投影变化。"""
from .db import connect
from .design_diagrams import _mermaid_label
from .project_baselines import _diff, _with_source_names
from .project_model import (
    CHANGE_LABELS, ProjectModel, _activity_diagram, _sequence_diagram,
    architecture_diagram, edge_identity, marked_label, process_owners,
)
from .requirement_deliveries import _text
from .semantics import KnowledgeError


def _by_key(items):
    return {item.key: item for item in items}


def _with_removed(old, new, identity):
    present = {identity(item) for item in new}
    return [*new, *(item for item in old if identity(item) not in present)]


def _link_identity(item):
    return edge_identity(item.model_dump())


def _edge_marks(changes, kind):
    return {
        edge_identity(change['after'] or change['before']): change['op']
        for change in changes if change['subject']['kind'] == kind
    }


def _architecture_overlay(before, after, changes):
    old_nodes = before.nodes if before else []
    old_links = before.dependencies if before else []
    nodes = _with_removed(old_nodes, after.nodes, lambda item: item.key)
    links = _with_removed(old_links, after.dependencies, _link_identity)
    node_marks = {row['subject']['key']: row['op'] for row in changes
                  if row['subject']['kind'] in {'domain', 'module'}}
    edge_marks = {('dependency', *identity): change
                  for identity, change in _edge_marks(changes, 'dependency').items()}
    old_parents = {node.key: node.parent_key for node in old_nodes}
    new_parents = {node.key: node.parent_key for node in after.nodes}
    former_parents = []
    for node in nodes:
        old, new = old_parents.get(node.key), new_parents.get(node.key)
        if old == new:
            continue
        if old is not None:
            edge_marks['containment', old, node.key] = 'removed'
            if node.key in new_parents:
                former_parents.append((old, node.key))
        if new is not None:
            edge_marks['containment', new, node.key] = 'updated' if old is not None else 'added'
    return architecture_diagram(after.model_copy(update={'nodes': nodes, 'dependencies': links}),
                                node_marks=node_marks, edge_marks=edge_marks, former_parents=former_parents)


def _activity_overlay(old, new, old_owners, new_owners, changes):
    process = new or old
    steps = _with_removed(old.steps if old else [], new.steps if new else [], lambda step: step.key)
    transitions = _with_removed(old.transitions if old else [], new.transitions if new else [], _link_identity)
    owners = {**old_owners, **new_owners}
    owner_marks = {key: 'updated' for key in old_owners.keys() & new_owners.keys()
                   if old_owners[key] != new_owners[key]}
    step_marks = {row['subject']['key']: row['op'] for row in changes if row['subject']['kind'] == 'step'}
    return _activity_diagram(process.model_copy(update={'steps': steps, 'transitions': transitions}), owners,
                             step_marks=step_marks, edge_marks=_edge_marks(changes, 'transition'),
                             owner_marks=owner_marks)


_SEQUENCE_COLORS = {'added': '231, 246, 237', 'updated': '232, 241, 255', 'removed': '252, 235, 237'}


def _sequence_overlay(old, new, old_owners, new_owners, changes):
    old_messages, new_messages = old.messages if old else [], new.messages if new else []
    legacy = bool(old and new and any(not message.key for message in [*old_messages, *new_messages]))
    scenario_op = next((row['op'] for row in changes if row['subject']['kind'] == 'scenario'), None)
    marks = {row['subject']['key']: row['op'] for row in changes if row['subject']['kind'] == 'message'}

    def participants(messages):
        return dict.fromkeys(key for message in messages for key in (message.source, message.target))

    old_keys, new_keys = participants(old_messages), participants(new_messages)
    keys = new_keys if legacy else {**new_keys, **old_keys}
    ids = {key: f'p{index}' for index, key in enumerate(keys, 1)}
    owners = {**old_owners, **new_owners}
    nodes, edges = [], []
    lines = ['sequenceDiagram']
    for key in keys:
        change = None
        if key in old_keys and key in new_keys and old_owners[key] != new_owners[key]:
            change = 'updated'
        elif not legacy and key not in old_keys:
            change = 'added'
        elif not legacy and key not in new_keys:
            change = 'removed'
        label = marked_label(owners[key], change)
        nodes.append({'id': ids[key], 'label': label, 'kind': 'Participant'})
        lines.append(f'    participant {ids[key]} as {_mermaid_label(label)}')

    def message_line(message, change, position=None):
        source, target = ids[message.source], ids[message.target]
        label = message.label
        if change == 'removed':
            label = f'原第 {position} 条 · {old_owners[message.source]} → {old_owners[message.target]} · {label}'
        label = marked_label(label, change)
        edges.append({'source': source, 'target': target, 'label': label,
                      **({'change': change} if change else {})})
        label = _mermaid_label(label)
        if change:
            lines.append(f'    rect rgb({_SEQUENCE_COLORS[change]})')
        if change == 'removed':
            # 旧消息以注释保留原序号，避免将已删除消息画进现行执行顺序。
            recipients = ','.join(dict.fromkeys([source, target]))
            lines.append(f'        Note over {recipients}: {label}')
        else:
            lines.append(f'    {source}->>{target}: {label}')
        if change:
            lines.append('    end')

    for message in new_messages:
        message_line(message, None if legacy else 'added' if old is None else marks.get(message.key))
    if not legacy:
        new_ids = {message.key for message in new_messages}
        for position, message in enumerate(old_messages, 1):
            if new is None or message.key not in new_ids:
                message_line(message, 'removed', position)
    notice = ('历史场景存在无稳定标识的消息。请使用左右对照核对消息内容，变化标记仅到场景层级。'
              if legacy else '')
    title = marked_label((new or old).name, scenario_op)
    return {'kind': 'sequence', 'title': title, 'nodes': nodes, 'edges': edges,
            'mermaid': '\n'.join(lines)}, notice


def _baseline_ref(row):
    return {'version': row['version'], 'proposal_id': str(row['proposal_id']),
            'title': row['document']['model']['title']}


def _scoped_model(row, model, process_key, kind, scenario_key):
    if model is None:
        return {}
    document = _with_source_names({'model': model.model_dump(mode='json'),
                                   'source_refs': row['document']['source_refs']})
    document['processes'] = [item for item in document['processes'] if item['key'] == process_key]
    for item in document['processes']:
        item['scenarios'] = [scene for scene in item['scenarios']
                             if kind == 'sequence' and scene['key'] == scenario_key]
    return document


def _compare_scenes(left, right):
    """旧场景只比较可见消息，补上稳定标识本身不属于业务改动。"""
    groups = [scene['messages'] for model in (left, right)
              for process in model.get('processes', []) for scene in process['scenarios']]
    if len(groups) == 2 and any(not item.get('key') for group in groups for item in group):
        for group in groups:
            for item in group:
                item.pop('key', None)


def build_comparison(before_row, after_row, kind, process_key=None, scenario_key=None):
    before = ProjectModel.model_validate(before_row['document']['model']) if before_row else None
    after = ProjectModel.model_validate(after_row['document']['model'])
    old_processes = _by_key(before.processes) if before else {}
    new_processes = _by_key(after.processes)
    catalog = []
    for key, process in {**old_processes, **new_processes}.items():
        old_scenes = _by_key(old_processes[key].scenarios) if key in old_processes else {}
        new_scenes = _by_key(new_processes[key].scenarios) if key in new_processes else {}
        catalog.append({'key': key, 'name': process.name,
                        'scenarios': [{'key': scene.key, 'name': scene.name}
                                      for scene in {**old_scenes, **new_scenes}.values()]})

    if kind == 'architecture':
        process_key = scenario_key = None
    else:
        if process_key and process_key not in old_processes and process_key not in new_processes:
            raise KnowledgeError('两个基线均未记录所选业务流程', 404)
        if not process_key:
            default = next((item for item in catalog if kind != 'sequence' or item['scenarios']), None)
            process_key = default['key'] if default else None
        selected = next((item for item in catalog if item['key'] == process_key), None)
        scenarios = [item['key'] for item in selected['scenarios']] if selected else []
        if kind == 'sequence':
            if scenario_key and scenario_key not in scenarios:
                raise KnowledgeError('两个基线均未记录所选时序场景', 404)
            scenario_key = scenario_key or next(iter(scenarios), None)
        else:
            scenario_key = None

    comparison = {'before_version': before_row['version'] if before_row else 0,
                  'after_version': after_row['version']}
    left_model = _scoped_model(before_row, before, process_key, kind, scenario_key)
    right_model = _scoped_model(after_row, after, process_key, kind, scenario_key)
    if kind == 'sequence':
        _compare_scenes(left_model, right_model)
    all_changes = _diff(left_model, right_model,
                        before_version=comparison['before_version'] or None,
                        after_version=comparison['after_version'])
    notice = ''
    left = right = overlay = None
    if kind == 'architecture':
        changes = [row for row in all_changes if row['subject']['kind'] in {'domain', 'module', 'dependency'}]
        left = architecture_diagram(before) if before else None
        right = architecture_diagram(after)
        overlay = _architecture_overlay(before, after, changes)
    else:
        old, new = old_processes.get(process_key), new_processes.get(process_key)
        old_owners = process_owners(before, old) if old else {}
        new_owners = process_owners(after, new) if new else {}
        if kind == 'activity':
            changes = [row for row in all_changes if row['subject']['kind'] in
                       {'process', 'step', 'transition', 'participant', 'requirement_refs', 'rule_refs'}]
            if old or new:
                left = _activity_diagram(old, old_owners) if old else None
                right = _activity_diagram(new, new_owners) if new else None
                overlay = _activity_overlay(old, new, old_owners, new_owners, changes)
        else:
            changes = [row for row in all_changes if row['subject']['kind'] in {'scenario', 'message'}]
            old_scene = _by_key(old.scenarios).get(scenario_key) if old else None
            new_scene = _by_key(new.scenarios).get(scenario_key) if new else None
            if old_scene or new_scene:
                left = _sequence_diagram(old_scene, old_owners, process_key) if old_scene else None
                right = _sequence_diagram(new_scene, new_owners, process_key) if new_scene else None
                overlay, notice = _sequence_overlay(old_scene, new_scene, old_owners, new_owners, changes)

    return {**comparison, 'before_ref': _baseline_ref(before_row) if before_row else None,
            'after_ref': _baseline_ref(after_row), 'catalog': catalog,
            'selection': {'kind': kind, 'process_key': process_key, 'scenario_key': scenario_key},
            'before': left, 'after': right, 'overlay': overlay, 'notice': notice, 'changes': changes}


def read_comparison(database_url, project_id, before_version, after_version, kind, process_key, scenario_key):
    if before_version >= after_version:
        raise KnowledgeError('对照的旧版本必须早于新版本')
    with connect(database_url) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        rows = conn.execute('''SELECT version,proposal_id,document FROM project_model_revision
            WHERE project_id=%s AND version IN (%s,%s)''', (project_id, before_version, after_version)).fetchall()
    by_version = {row['version']: row for row in rows}
    if after_version not in by_version or (before_version and before_version not in by_version):
        raise KnowledgeError('未找到当前项目的指定已确认基线', 404)
    return build_comparison(by_version.get(before_version), by_version[after_version], kind, process_key, scenario_key)


def comparison_markdown(comparison):
    lines = ['# 项目图形版本对照', '']
    for name, side in (('旧版', 'before_ref'), ('新版', 'after_ref')):
        ref = comparison[side]
        if ref:
            lines.append(f'- {name} v{ref["version"]} · {_text(ref["title"])} · 提案 `{ref["proposal_id"]}`')
        else:
            lines.append('- 旧版为首次发布前')
    lines.extend(['', '图例为【新增】、【修改】和【删除】。删除项表示旧版内容。'])
    if comparison['notice']:
        lines.extend(['', comparison['notice']])
    for field, title in (('overlay', '变化叠加'), ('before', '旧版'), ('after', '新版')):
        diagram = comparison[field]
        lines.extend(['', f'## {title}', ''])
        if diagram:
            lines.extend([_text(diagram['title']), '', '```mermaid', diagram['mermaid'], '```'])
        else:
            lines.append('此版本没有所选图表。')
    lines.extend(['', '## 变更说明', ''])
    for item in comparison['changes']:
        lines.append(f'- {CHANGE_LABELS[item["op"]]}{_text(item["kind"])} · {_text(item["label"])}'
                     + (f' · {_text(item["detail"])}' if item['detail'] else ''))
    if not comparison['changes']:
        lines.append('所选图表没有记录结构变化。')
    return '\n'.join(lines) + '\n'
