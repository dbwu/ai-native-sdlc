"""把受校验的设计结构投影为页面和交付文档共用的图示。"""
import re


COLLABORATION_NODES = {
    'component': ('components', 'ArchitectureComponent'),
    'interface': ('interfaces', 'Interface'),
    'data_model': ('data_models', 'DataModel'),
}
COLLABORATION_LABELS = {'calls': '调用', 'reads': '读取', 'writes': '写入', 'depends_on': '依赖'}


def design_diagrams(design, requirement_id=None, *, requirements=(), design_ref=None):
    if not design or design.get('questions'):
        return []

    def relevant(item):
        return requirement_id is None or str(requirement_id) in item['requirement_ids']

    requirements = [item for item in requirements if requirement_id is None or str(item['id']) == str(requirement_id)]
    fixed_requirements = {str(item['id']): {'id': str(item['id']), 'version': item['version']} for item in requirements}

    def refs(item):
        return [fixed_requirements[ref] for ref in item['requirement_ids'] if ref in fixed_requirements]

    diagrams = []
    for use_case in design.get('use_case_diagrams', []):
        if not relevant(use_case):
            continue
        actor_ids = {actor['key']: f'a{index}' for index, actor in enumerate(use_case['actors'], 1)}
        case_ids = {item['key']: f'u{index}' for index, item in enumerate(use_case['use_cases'], 1)}
        nodes = [{'id': actor_ids[actor['key']], 'label': actor['name'], 'kind': 'Actor'} for actor in use_case['actors']]
        nodes.extend({'id': case_ids[item['key']], 'label': item['name'], 'kind': 'UseCase'} for item in use_case['use_cases'])
        edges = [{'source': actor_ids[item['actor']], 'target': case_ids[item['use_case']], 'label': '参与'}
                 for item in use_case['associations']]
        diagrams.append({'kind': 'use_case', 'title': use_case['title'], 'nodes': nodes, 'edges': edges,
                         'model_refs': {'identity_scope': 'design_snapshot', 'diagram_key': use_case.get('key'),
                                        'requirement_refs': refs(use_case),
                                        'nodes': {**{actor_ids[item['key']]: {'kind': 'actor', 'key': item['key']}
                                                     for item in use_case['actors']},
                                                  **{case_ids[item['key']]: {'kind': 'use_case', 'key': item['key']}
                                                     for item in use_case['use_cases']}},
                                        'edges': [{'kind': 'association', 'key': item.get('key'),
                                                   'actor_key': item['actor'], 'use_case_key': item['use_case'],
                                                   'identity': 'stable_key' if item.get('key') else 'endpoint_pair'}
                                                  for item in use_case['associations']]}})

    for sequence in design.get('sequence_diagrams', []):
        if not relevant(sequence):
            continue
        actor_ids = {participant['key']: f'p{index}' for index, participant in enumerate(sequence['participants'], 1)}
        nodes = [{'id': actor_ids[participant['key']], 'label': participant['name'], 'kind': 'Participant'}
                 for participant in sequence['participants']]
        edges = [{'source': actor_ids[message['source']], 'target': actor_ids[message['target']], 'label': message['label']}
                 for message in sequence['messages']]
        diagrams.append({'kind': 'sequence', 'title': sequence['title'], 'nodes': nodes, 'edges': edges,
                         'model_refs': {'identity_scope': 'design_snapshot', 'diagram_key': sequence.get('key'),
                                        'requirement_refs': refs(sequence),
                                        'identity_complete': bool(sequence.get('key')) and all(
                                            message.get('key') for message in sequence['messages']),
                                        'nodes': {actor_ids[item['key']]: {'kind': 'participant', 'key': item['key']}
                                                  for item in sequence['participants']},
                                        'edges': [{'kind': 'message', 'key': item.get('key'), 'position': index,
                                                   'source_key': item['source'], 'target_key': item['target'],
                                                   'identity': 'stable_key' if item.get('key') else 'legacy_scenario'}
                                                  for index, item in enumerate(sequence['messages'])]}})

    architecture = _architecture(design, relevant, requirements)
    if architecture:
        diagrams.append(architecture)
    collaboration = _collaboration(design, relevant, requirements)
    if collaboration:
        diagrams.append(collaboration)
    for diagram in diagrams:
        diagram['mermaid'] = '\n'.join(mermaid_lines(diagram))
        if design_ref is not None:
            diagram['design_ref'] = dict(design_ref)
    return diagrams


def _architecture(design, relevant, requirements):
    children = [(kind, item) for kind, group in (('Interface', 'interfaces'), ('DataModel', 'data_models'))
                for item in design[group] if relevant(item)]
    owned_requirements = {}
    for _, child in children:
        owned_requirements.setdefault(child['component_key'], set()).update(child['requirement_ids'])
    components = [item for item in design['components'] if relevant(item) or item['key'] in owned_requirements]
    if not components:
        return None

    nodes, edges, node_refs, edge_refs = [], [], {}, []

    def add_node(kind, label, *, model_ref, **attributes):
        node_id = f'n{len(nodes) + 1}'
        nodes.append({'id': node_id, 'kind': kind, 'label': label, **attributes})
        node_refs[node_id] = model_ref
        return node_id

    requirement_nodes = {str(item['id']): add_node('Requirement', f'{item["name"]} · v{item["version"]}',
                                                 model_ref={'kind': 'requirement', 'entity_id': str(item['id']),
                                                            'entity_version': item['version']})
                         for item in requirements}
    component_nodes = {}
    for component in components:
        key = component['key']
        node_id = add_node('ArchitectureComponent', component['name'],
                           model_ref={'kind': 'component', 'key': key},
                           detail=component.get('responsibility') or component.get('description') or '')
        component_nodes[key] = node_id
        direct = set(component['requirement_ids'])
        for requirement_id, source in requirement_nodes.items():
            if requirement_id in direct:
                label = '由其负责'
            elif requirement_id in owned_requirements.get(key, ()):
                label = '关联产物归属'
            else:
                continue
            edges.append({'source': source, 'target': node_id, 'label': label})
            edge_refs.append({'kind': 'responsibility', 'requirement_id': requirement_id, 'component_key': key})
    for kind, child in children:
        node_id = add_node(kind, child['name'], model_ref={'kind': kind, 'key': child.get('key')})
        edges.append({'source': component_nodes[child['component_key']], 'target': node_id, 'label': '包含'})
        edge_refs.append({'kind': 'containment', 'component_key': child['component_key'], 'child_kind': kind,
                          'child_key': child.get('key')})
    return {'kind': 'architecture', 'title': '职责图', 'nodes': nodes, 'edges': edges,
            'model_refs': {'identity_scope': 'design_snapshot', 'diagram_key': 'responsibility',
                           'nodes': node_refs, 'edges': edge_refs,
                           'requirement_refs': [{'id': str(item['id']), 'version': item['version']} for item in requirements]}}


def _collaboration(design, relevant, requirements):
    links = [link for link in design.get('collaboration_links', []) if relevant(link)]
    if not links:
        return None
    labels = {str(item['id']): f'{item["name"]} · v{item["version"]}' for item in requirements}
    used = {(endpoint['kind'], endpoint['key']) for link in links for endpoint in (link['source'], link['target'])}
    nodes, ids, node_refs = [], {}, {}
    for kind, (group, entity_kind) in COLLABORATION_NODES.items():
        for item in design[group]:
            ref = (kind, item['key'])
            if ref in used:
                node_id = f'n{len(nodes) + 1}'
                ids[ref] = node_id
                node_refs[node_id] = {'kind': kind, 'key': item['key']}
                nodes.append({'id': node_id, 'kind': entity_kind, 'label': item['name']})
    edges = []
    for link in links:
        source, target = link['source'], link['target']
        edges.append({'source': ids[(source['kind'], source['key'])], 'target': ids[(target['kind'], target['key'])],
                      'label': COLLABORATION_LABELS[link['type']], 'rationale': link['rationale'],
                      'requirements': [labels[ref] for ref in link['requirement_ids'] if ref in labels]})
    return {'kind': 'collaboration', 'title': '协作图', 'nodes': nodes, 'edges': edges,
            'model_refs': {'identity_scope': 'design_snapshot', 'diagram_key': 'collaboration',
                           'nodes': node_refs,
                           'edges': [{'kind': 'collaboration', 'key': link.get('key'), 'type': link['type'],
                                      'source': link['source'], 'target': link['target'],
                                      'identity': 'stable_key' if link.get('key') else 'typed_endpoints'} for link in links],
                           'requirement_refs': [{'id': str(item['id']), 'version': item['version']} for item in requirements]}}


# 只生成固定 Mermaid 语法。标签内替换控制字符和分隔符，模型无法注入语句、链接或 HTML。
_DELIMITERS = str.maketrans('`<>"\'[]{}()|;#\\&%:', '｀＜＞＂＇［］｛｝（）｜；＃＼＆％：')
_END_LETTERS = str.maketrans('eEnNdD', 'ｅＥｎＮｄＤ')
_NODE_LABELS = {'Requirement': '需求', 'ArchitectureComponent': '组件', 'Interface': '接口', 'DataModel': '数据结构', 'Actor': '角色'}
_ARCHITECTURE_STYLES = {
    'Requirement': ('req', '#eaf3fa', '#5685a8'),
    'ArchitectureComponent': ('component', '#eef7ef', '#5d9074'),
    'Interface': ('interface', '#fff7e8', '#ad7e39'),
    'DataModel': ('data', '#f3effc', '#8b79a9'),
}


def _mermaid_label(value):
    value = ' '.join(value.split()).translate(_DELIMITERS)
    return re.sub(r'\bend\b', lambda match: match[0].translate(_END_LETTERS), value, flags=re.I)


def mermaid_lines(diagram):
    if diagram['kind'] == 'sequence':
        return ['sequenceDiagram',
                *[f'    participant {node["id"]} as {_mermaid_label(node["label"])}' for node in diagram['nodes']],
                *[f'    {edge["source"]}->>{edge["target"]}: {_mermaid_label(edge["label"])}' for edge in diagram['edges']]]
    use_case = diagram['kind'] == 'use_case'
    arrow = '---' if use_case else '-->'
    nodes = []
    for node in diagram['nodes']:
        label = _mermaid_label(node['label'])
        if node['kind'] in _NODE_LABELS:
            label = f'{_NODE_LABELS[node["kind"]]}：{label}'
        shape = f'(["{label}"])' if node['kind'] == 'UseCase' else f'["{label}"]'
        nodes.append(f'    {node["id"]}{shape}')
    lines = ['flowchart LR', *nodes]
    if diagram['kind'] == 'collaboration':
        # 同方向的多种关系共用一根连线，逐条依据在图下保留。
        grouped = {}
        for edge in diagram['edges']:
            grouped.setdefault((edge['source'], edge['target']), []).append(edge['label'])
        for (source, target), labels in grouped.items():
            lines.append(f'    {source} {arrow}|{_mermaid_label(" / ".join(labels))}| {target}')
    else:
        lines.extend(f'    {edge["source"]} {arrow}|{_mermaid_label(edge["label"])}| {edge["target"]}' for edge in diagram['edges'])
    if diagram['kind'] in {'architecture', 'collaboration'}:
        for kind, (class_name, fill, stroke) in _ARCHITECTURE_STYLES.items():
            ids = [node['id'] for node in diagram['nodes'] if node['kind'] == kind]
            if ids:
                lines.append(f'    classDef {class_name} fill:{fill},stroke:{stroke},color:#193247')
                lines.append(f'    class {",".join(ids)} {class_name}')
    return lines
