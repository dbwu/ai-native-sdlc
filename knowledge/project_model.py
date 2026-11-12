"""已确认项目基线的结构约束，以及项目级 Mermaid 投影。"""
from collections import defaultdict
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .design_diagrams import _mermaid_label, mermaid_lines
from .models import EvidenceRef, StrictModel, Version
from .runtime_models import RuntimeInput


Key = Annotated[str, Field(min_length=1, max_length=80, pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')]
Label = Annotated[str, Field(min_length=1, max_length=160, pattern=r'^[^\x00-\x1f\x7f\u2028\u2029]+$')]
Description = Annotated[str, Field(max_length=4000)]


class ArchitectureNode(StrictModel):
    key: Key
    name: Label
    kind: Literal['domain', 'module']
    parent_key: Key | None = None
    description: Description = ''
    entity_id: UUID | None = None
    entity_version: Version | None = None

    @model_validator(mode='after')
    def version_requires_entity(self):
        if self.entity_version is not None and self.entity_id is None:
            raise ValueError('固定知识版本时必须指定知识实体标识')
        return self


class ArchitectureDependency(StrictModel):
    key: Key | None = Field(default=None, title='依赖稳定标识，历史记录可为空')
    source: Key
    target: Key
    label: Label


class ProcessParticipant(StrictModel):
    key: Key
    name: Label


class ProcessStep(StrictModel):
    key: Key
    name: Label
    kind: Literal['start', 'task', 'decision', 'end']
    owner: Key


class ProcessTransition(StrictModel):
    key: Key | None = Field(default=None, title='分支稳定标识，历史记录可为空')
    source: Key
    target: Key
    condition: Label | None = None


class SequenceExchange(StrictModel):
    key: Key | None = Field(default=None, title='消息稳定标识，重命名或调整顺序时沿用')
    source: Key
    target: Key
    label: Label


class ProcessScenario(StrictModel):
    key: Key
    name: Label
    messages: list[SequenceExchange] = Field(min_length=1, max_length=50)


class BusinessProcess(StrictModel):
    key: Key
    name: Label
    description: Description = ''
    steps: list[ProcessStep] = Field(min_length=2, max_length=80)
    transitions: list[ProcessTransition] = Field(min_length=1, max_length=150)
    participants: list[ProcessParticipant] = Field(default_factory=list, max_length=20)
    scenarios: list[ProcessScenario] = Field(default_factory=list, max_length=12)
    requirement_refs: list[EvidenceRef] = Field(default_factory=list, max_length=40)
    rule_refs: list[EvidenceRef] = Field(default_factory=list, max_length=40)


def _unique(values, message):
    values = list(values)
    if len(set(values)) != len(values):
        raise ValueError(message)


def _reachable(keys, graph):
    visited = set()
    pending = list(keys)
    while pending:
        key = pending.pop()
        if key not in visited:
            visited.add(key)
            pending.extend(graph[key])
    return visited


def _validate_process(process, architecture):
    title = f'流程「{process.name}」'
    _unique((step.key for step in process.steps), f'{title}的步骤标识不能重复')
    _unique((item.key for item in process.participants), f'{title}的参与者标识不能重复')
    _unique((item.key for item in process.scenarios), f'{title}的场景标识不能重复')
    _unique((ref.id for ref in process.requirement_refs), f'{title}只能为每条需求指定一个版本')
    _unique((ref.id for ref in process.rule_refs), f'{title}只能为每条业务规则指定一个版本')
    _unique(((edge.source, edge.target) for edge in process.transitions), f'{title}的同向步骤连线不能重复')
    _unique((edge.key for edge in process.transitions if edge.key is not None), f'{title}的分支稳定标识不能重复')
    steps = {step.key: step for step in process.steps}
    roles = {item.key for item in process.participants}
    if roles & architecture.keys():
        raise ValueError(f'{title}的角色标识不能与领域或模块标识相同')
    owners = roles | {key for key, node in architecture.items() if node.kind == 'module'}
    for step in process.steps:
        if step.owner not in owners:
            raise ValueError(f'{title}的步骤「{step.name}」必须引用已定义的模块或角色')
    for scenario in process.scenarios:
        _unique((message.key for message in scenario.messages if message.key is not None),
                f'{title}的时序场景「{scenario.name}」的消息稳定标识不能重复')
        for message in scenario.messages:
            if message.source not in owners or message.target not in owners:
                raise ValueError(f'{title}的时序场景「{scenario.name}」只能引用已定义的模块或角色')

    starts = [step.key for step in process.steps if step.kind == 'start']
    ends = [step.key for step in process.steps if step.kind == 'end']
    if len(starts) != 1 or not ends:
        raise ValueError(f'{title}必须恰有一个开始步骤及至少一个结束步骤')
    outgoing, reverse = defaultdict(list), defaultdict(list)
    for edge in process.transitions:
        if edge.source not in steps or edge.target not in steps or edge.source == edge.target:
            raise ValueError(f'{title}的连线必须连接不同的已定义步骤')
        outgoing[edge.source].append(edge)
        reverse[edge.target].append(edge.source)
    if reverse[starts[0]]:
        raise ValueError(f'{title}的开始步骤不能有入线')
    for step in process.steps:
        edges = outgoing[step.key]
        if step.kind == 'end' and edges:
            raise ValueError(f'{title}的结束步骤不能有出线')
        if step.kind in {'start', 'task'} and len(edges) != 1:
            raise ValueError(f'{title}的开始和普通步骤必须各有一条出线')
        if step.kind == 'decision':
            conditions = {edge.condition for edge in edges}
            if len(conditions) < 2 or len(conditions) != len(edges) or None in conditions:
                raise ValueError(f'{title}的判断步骤「{step.name}」至少需要两个条件不同的分支')
    forward = defaultdict(list, {key: [edge.target for edge in edges] for key, edges in outgoing.items()})
    if _reachable(starts, forward) != steps.keys() or _reachable(ends, reverse) != steps.keys():
        raise ValueError(f'{title}的所有步骤必须从开始可达且能够通往结束')


class ProjectModel(RuntimeInput):
    title: Label
    summary: Description = ''
    nodes: list[ArchitectureNode] = Field(min_length=1, max_length=150)
    dependencies: list[ArchitectureDependency] = Field(default_factory=list, max_length=200)
    processes: list[BusinessProcess] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def consistent_baseline(self):
        _unique((node.key for node in self.nodes), '架构节点标识不能重复')
        _unique((node.entity_id for node in self.nodes if node.entity_id), '同一知识实体不能绑定多个架构节点')
        _unique((process.key for process in self.processes), '项目业务流程标识不能重复')
        _unique(((edge.source, edge.target) for edge in self.dependencies), '同向架构依赖不能重复')
        _unique((edge.key for edge in self.dependencies if edge.key is not None), '架构依赖稳定标识不能重复')
        nodes = {node.key: node for node in self.nodes}
        for node in self.nodes:
            if node.kind == 'domain' and node.parent_key is not None:
                raise ValueError(f'领域「{node.name}」必须位于架构根层')
            if node.kind == 'module' and node.parent_key not in nodes:
                raise ValueError(f'模块「{node.name}」必须归属于一个已定义的领域或模块')
        for node in self.nodes:
            ancestors = {node.key}
            parent_key = node.parent_key
            while parent_key is not None:
                if parent_key in ancestors:
                    raise ValueError(f'架构节点「{node.name}」的归属形成循环')
                ancestors.add(parent_key)
                parent_key = nodes[parent_key].parent_key
        modules = {key for key, node in nodes.items() if node.kind == 'module'}
        for edge in self.dependencies:
            if edge.source not in modules or edge.target not in modules or edge.source == edge.target:
                raise ValueError('架构依赖必须连接不同的已定义模块')
        for process in self.processes:
            _validate_process(process, nodes)
        return self


def edge_identity(item):
    return ('key', item['key']) if item.get('key') else ('endpoints', item['source'], item['target'])


CHANGE_LABELS = {'added': '新增', 'updated': '修改', 'removed': '删除'}
_CHANGE_COLORS = {
    'added': ('#e7f6ed', '#26734c'),
    'updated': ('#e8f1ff', '#285ca1'),
    'removed': ('#fcebed', '#a43746'),
}


def marked_label(label, change):
    return f'【{CHANGE_LABELS[change]}】{label}' if change else label


def _marked_styles(lines, nodes, edges):
    for node in nodes:
        if change := node.get('change'):
            fill, stroke = _CHANGE_COLORS[change]
            dashed = ',stroke-dasharray:5 3' if change == 'removed' else ''
            lines.append(f'    style {node["id"]} fill:{fill},stroke:{stroke},stroke-width:2px{dashed}')
    for index, edge in enumerate(edges):
        if change := edge.get('change'):
            _, stroke = _CHANGE_COLORS[change]
            # 删除以虚线显示。现行依赖仍保留依赖线原有的虚线样式。
            dashed = ',stroke-dasharray:5 3' if change == 'removed' else ''
            lines.append(f'    linkStyle {index} stroke:{stroke},color:{stroke},stroke-width:2px{dashed}')


def architecture_diagram(model, *, node_marks=None, edge_marks=None, former_parents=()):
    """绘制项目确认的归属及有向模块依赖，图内标识由系统生成。"""
    ids = {node.key: f'a{index}' for index, node in enumerate(model.nodes, 1)}
    nodes, edges = [], []
    lines = ['flowchart LR']
    node_marks, edge_marks = node_marks or {}, edge_marks or {}
    refs = []

    def contains(parent, child):
        change = edge_marks.get(('containment', parent, child))
        label = marked_label('包含', change)
        edges.append({'source': ids[parent], 'target': ids[child], 'label': label,
                      **({'change': change} if change else {})})
        lines.append(f'    {ids[parent]} -->|"{_mermaid_label(label)}"| {ids[child]}')
        refs.append({'kind': 'containment', 'key': child, 'source_key': parent, 'target_key': child})

    for node in model.nodes:
        domain = node.kind == 'domain'
        node_id = ids[node.key]
        kind = 'Domain' if domain else 'ArchitectureComponent'
        change = node_marks.get(node.key)
        label = marked_label(node.name, change)
        nodes.append({'id': node_id, 'label': label, 'kind': kind, 'detail': node.description,
                      **({'change': change} if change else {})})
        lines.append(f'    {node_id}["{"领域" if domain else "模块"}：{_mermaid_label(label)}"]')
        lines.append(f'    class {node_id} {"domain" if domain else "module"}')
        if node.parent_key is not None:
            contains(node.parent_key, node.key)
    for parent, child in former_parents:
        contains(parent, child)
    for edge in model.dependencies:
        source, target = ids[edge.source], ids[edge.target]
        change = edge_marks.get(('dependency', *edge_identity(edge.model_dump())))
        label = marked_label(f'依赖：{edge.label}', change)
        edges.append({'source': source, 'target': target, 'label': label, **({'change': change} if change else {})})
        lines.append(f'    {source} -.->|"{_mermaid_label(label)}"| {target}')
        refs.append({'kind': 'dependency', 'key': edge.key, 'source_key': edge.source, 'target_key': edge.target,
                     'identity': 'stable_key' if edge.key else 'endpoint_pair'})
    lines.extend(['    classDef domain fill:#eaf3fa,stroke:#5685a8,color:#193247',
                  '    classDef module fill:#eef7ef,stroke:#5d9074,color:#193247'])
    _marked_styles(lines, nodes, edges)
    model_refs = {
        'identity_scope': 'project',
        'nodes': {ids[node.key]: {'kind': node.kind, 'key': node.key,
                                 'entity_id': str(node.entity_id) if node.entity_id else None,
                                 'entity_version': node.entity_version} for node in model.nodes},
        'edges': refs,
    }
    return {'kind': 'project_architecture', 'title': '项目架构图', 'nodes': nodes, 'edges': edges,
            'model_refs': model_refs,
            'mermaid': '\n'.join(lines)}


_STEP_SHAPES = {'start': '(["{}"])', 'task': '["{}"]', 'decision': '{{"{}"}}', 'end': '(["{}"])'}
_STEP_LABELS = {'start': '开始', 'task': '步骤', 'decision': '判断', 'end': '结束'}


def _activity_diagram(process, owners, *, step_marks=None, edge_marks=None, owner_marks=None):
    ids = {step.key: f's{index}' for index, step in enumerate(process.steps, 1)}
    lanes = defaultdict(list)
    nodes, edges = [], []
    step_marks, edge_marks, owner_marks = step_marks or {}, edge_marks or {}, owner_marks or {}
    for step in process.steps:
        lanes[step.owner].append(step)
        change = step_marks.get(step.key)
        nodes.append({'id': ids[step.key], 'label': marked_label(step.name, change),
                      'detail': f'{_STEP_LABELS[step.kind]} · {owners[step.owner]}',
                      **({'change': change} if change else {})})
    labels = {node['id']: node['label'] for node in nodes}
    lines = ['flowchart LR']
    for index, (owner, steps) in enumerate(lanes.items(), 1):
        lane = marked_label(owners[owner], owner_marks.get(owner))
        lines.extend([f'    subgraph lane{index}["{_mermaid_label(lane)}"]', '        direction TB'])
        for step in steps:
            shape = _STEP_SHAPES[step.kind].format(_mermaid_label(labels[ids[step.key]]))
            lines.append(f'        {ids[step.key]}{shape}')
        lines.append('    end')
    for edge in process.transitions:
        source, target = ids[edge.source], ids[edge.target]
        change = edge_marks.get(edge_identity(edge.model_dump()))
        label = marked_label(edge.condition or '下一步', change)
        edges.append({'source': source, 'target': target, 'label': label, **({'change': change} if change else {})})
        arrow = f'-->|"{_mermaid_label(label)}"|' if edge.condition or change else '-->'
        lines.append(f'    {source} {arrow} {target}')
    _marked_styles(lines, nodes, edges)
    return {'kind': 'activity', 'title': f'{process.name} · 泳道图', 'nodes': nodes, 'edges': edges,
            'model_refs': {'identity_scope': 'process', 'process_key': process.key,
                           'nodes': {ids[step.key]: {'kind': 'step', 'key': step.key, 'owner_key': step.owner}
                                     for step in process.steps},
                           'edges': [{'kind': 'transition', 'key': edge.key, 'source_key': edge.source,
                                      'target_key': edge.target, 'identity': 'stable_key' if edge.key else 'endpoint_pair'}
                                     for edge in process.transitions]},
            'mermaid': '\n'.join(lines)}


def _sequence_diagram(scenario, owners, process_key):
    keys = dict.fromkeys(key for exchange in scenario.messages for key in (exchange.source, exchange.target))
    ids = {key: f'p{index}' for index, key in enumerate(keys, 1)}
    diagram = {'kind': 'sequence', 'title': scenario.name,
               'nodes': [{'id': node_id, 'label': owners[key], 'kind': 'Participant'} for key, node_id in ids.items()],
               'edges': [{'source': ids[item.source], 'target': ids[item.target], 'label': item.label}
                         for item in scenario.messages],
               'model_refs': {'identity_scope': 'scenario', 'process_key': process_key, 'scenario_key': scenario.key,
                              'identity_complete': all(item.key is not None for item in scenario.messages),
                              'nodes': {node_id: {'kind': 'participant', 'key': key} for key, node_id in ids.items()},
                              'edges': [{'kind': 'message', 'key': item.key, 'source_key': item.source,
                                         'target_key': item.target, 'position': index,
                                         'identity': 'stable_key' if item.key else 'legacy_scenario'}
                                        for index, item in enumerate(scenario.messages)]}}
    diagram['mermaid'] = '\n'.join(mermaid_lines(diagram))
    return diagram


def process_owners(model, process):
    owners = {node.key: node.name for node in model.nodes if node.kind == 'module'}
    owners.update((participant.key, participant.name) for participant in process.participants)
    return owners


def process_diagrams(model, process):
    """泳道图使用步骤责任方，场景时序只使用明确记录的交互。"""
    owners = process_owners(model, process)
    diagrams = [_activity_diagram(process, owners),
                *[_sequence_diagram(scenario, owners, process.key) for scenario in process.scenarios]]
    for diagram in diagrams:
        diagram['model_refs'].update(
            requirement_refs=[ref.model_dump(mode='json') for ref in process.requirement_refs],
            rule_refs=[ref.model_dump(mode='json') for ref in process.rule_refs])
    return diagrams
