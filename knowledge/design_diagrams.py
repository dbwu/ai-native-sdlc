"""把受校验的设计结构投影为页面和交付文档共用的图示。"""
import re


def design_diagrams(design, requirement_id=None):
    if not design or design.get('questions'):
        return []

    def relevant(item):
        return requirement_id is None or str(requirement_id) in item['requirement_ids']

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
        diagrams.append({'kind': 'use_case', 'title': use_case['title'], 'nodes': nodes, 'edges': edges})

    for sequence in design.get('sequence_diagrams', []):
        if not relevant(sequence):
            continue
        actor_ids = {participant['key']: f'p{index}' for index, participant in enumerate(sequence['participants'], 1)}
        nodes = [{'id': actor_ids[participant['key']], 'label': participant['name'], 'kind': 'Participant'}
                 for participant in sequence['participants']]
        edges = [{'source': actor_ids[message['source']], 'target': actor_ids[message['target']], 'label': message['label']}
                 for message in sequence['messages']]
        diagrams.append({'kind': 'sequence', 'title': sequence['title'], 'nodes': nodes, 'edges': edges})

    children = [(kind, item) for kind, group in (('Interface', 'interfaces'), ('DataModel', 'data_models'))
                for item in design[group] if relevant(item)]
    owners = {item['component_key'] for _, item in children}
    nodes, edges, component_ids = [], [], {}
    for component in design['components']:
        if relevant(component) or component['key'] in owners:
            node_id = f'n{len(nodes) + 1}'
            component_ids[component['key']] = node_id
            nodes.append({'id': node_id, 'kind': 'ArchitectureComponent', 'label': component['name']})
    for kind, child in children:
        node_id = f'n{len(nodes) + 1}'
        nodes.append({'id': node_id, 'kind': kind, 'label': child['name']})
        edges.append({'source': component_ids[child['component_key']], 'target': node_id, 'label': '包含'})
    if nodes:
        diagrams.append({'kind': 'architecture', 'title': '架构图', 'nodes': nodes, 'edges': edges})
    for diagram in diagrams:
        diagram['mermaid'] = '\n'.join(mermaid_lines(diagram))
    return diagrams


# 只生成固定 Mermaid 语法。标签内替换控制字符和分隔符，模型无法注入语句、链接或 HTML。
_DELIMITERS = str.maketrans('`<>"\'[]{}()|;#\\&%:', '｀＜＞＂＇［］｛｝（）｜；＃＼＆％：')
_END_LETTERS = str.maketrans('eEnNdD', 'ｅＥｎＮｄＤ')
_NODE_LABELS = {'ArchitectureComponent': '组件', 'Interface': '接口', 'DataModel': '数据结构', 'Actor': '角色'}


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
    return ['flowchart LR', *nodes,
            *[f'    {edge["source"]} {arrow}|{_mermaid_label(edge["label"])}| {edge["target"]}' for edge in diagram['edges']]]
