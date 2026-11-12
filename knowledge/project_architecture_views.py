"""从已确认的项目模型投影逻辑结构和显式协作关系。"""
from collections import defaultdict

from .design_diagrams import COLLABORATION_LABELS, _mermaid_label
from .project_model import _marked_styles, marked_label


def logical_structure_diagram(model):
    """用领域边框表达归属。总览折叠子模块，归并到直接所属模块。"""
    nodes = {node.key: node for node in model.nodes}
    by_parent = defaultdict(list)
    for node in model.nodes:
        by_parent[node.parent_key].append(node)
    displayed = {item.key for domain in by_parent[None] for item in by_parent[domain.key]}
    representatives = {}
    for node in model.nodes:
        parent = node
        while parent.key not in displayed and parent.parent_key:
            parent = nodes[parent.parent_key]
        representatives[node.key] = parent.key
    ids = {key: f'n{index}' for index, key in enumerate(nodes, 1)}
    visible, edges, lines = [], [], ['flowchart LR']
    for domain in by_parent[None]:
        children = by_parent[domain.key]
        label = _mermaid_label(f'领域：{domain.name}')
        if children:
            lines.append(f'    subgraph group{ids[domain.key]}["{label}"]')
            for node in children:
                lines.append(f'        {ids[node.key]}["模块：{_mermaid_label(node.name)}"]')
                visible.append({'id': ids[node.key], 'label': node.name, 'kind': 'ArchitectureComponent',
                                'detail': node.description})
            lines.append('    end')
        else:
            lines.append(f'    {ids[domain.key]}["{label}"]')
            visible.append({'id': ids[domain.key], 'label': domain.name, 'kind': 'Domain'})
    grouped = defaultdict(list)
    for dependency in model.dependencies:
        source, target = (representatives[key] for key in (dependency.source, dependency.target))
        if source != target:
            grouped[source, target].append(dependency.label)
    for (source, target), labels in grouped.items():
        label = labels[0] if len(labels) == 1 else f'{len(labels)} 项依赖'
        edges.append({'source': ids[source], 'target': ids[target], 'label': label})
        lines.append(f'    {ids[source]} -.->|"{_mermaid_label(label)}"| {ids[target]}')
    return {'kind': 'logical_structure', 'title': '领域与一级模块', 'nodes': visible,
            'edges': edges, 'mermaid': '\n'.join(lines)}


def _endpoints(model):
    if model is None:
        return {}
    return {**{node.key: (node.name, 'ArchitectureComponent', '模块') for node in model.nodes},
            **{port.key: (port.name, 'Interface', '接口') if port.kind == 'interface'
               else (port.name, 'DataModel', '数据') for port in model.architecture_ports}}


def project_collaboration_diagram(model, *, before=None, changes=None):
    links = {link.key: link for link in model.collaboration_links}
    if changes is not None and before:
        for link in before.collaboration_links:
            links.setdefault(link.key, link)
    if not links:
        return None

    current, old = _endpoints(model), _endpoints(before)
    endpoints = {**old, **current}
    edge_marks = {row['subject']['key']: row['op'] for row in changes or []
                  if row['subject']['kind'] == 'project_collaboration'}
    node_marks = {row['subject']['key']: row['op'] for row in changes or []
                  if row['subject']['kind'] in {'architecture_port', 'module'}}
    visible = dict.fromkeys(key for link in links.values() for key in (link.source, link.target))
    ids = {key: f'p{index}' for index, key in enumerate(visible, 1)}
    nodes, edges, lines = [], [], ['flowchart LR']
    for key in visible:
        name, kind, category = endpoints[key]
        change = node_marks.get(key)
        label = marked_label(name, change)
        nodes.append({'id': ids[key], 'label': label, 'kind': kind, 'change': change})
        lines.append(f'    {ids[key]}["{category}：{_mermaid_label(label)}"]')
        style = {'ArchitectureComponent': 'module', 'Interface': 'port', 'DataModel': 'data'}[kind]
        lines.append(f'    class {ids[key]} {style}')
    for link in links.values():
        change = edge_marks.get(link.key)
        label = marked_label(f'{COLLABORATION_LABELS[link.type]}：{link.label}', change)
        source, target = ids[link.source], ids[link.target]
        edges.append({'source': source, 'target': target, 'label': label, 'change': change,
                      'rationale': link.rationale})
        arrow = '-.->' if link.type == 'depends_on' else '-->'
        lines.append(f'    {source} {arrow}|"{_mermaid_label(label)}"| {target}')
    lines.extend(['    classDef module fill:#eef7ef,stroke:#5d9074,color:#193247',
                  '    classDef port fill:#fff7e8,stroke:#ad7e39,color:#193247',
                  '    classDef data fill:#f3effc,stroke:#8b79a9,color:#193247'])
    _marked_styles(lines, nodes, edges)
    return {'kind': 'project_collaboration', 'title': '项目接口与协作', 'nodes': nodes,
            'edges': edges, 'mermaid': '\n'.join(lines)}
