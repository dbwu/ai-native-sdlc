from knowledge.design_diagrams import design_diagrams


def test_legacy_architecture_uses_explicit_component_ownership_and_relevant_nodes():
    design = {'components': [
        {'key': 'Owner', 'name': '所属组件', 'requirement_ids': ['other']},
        {'key': 'Unrelated', 'name': '另一需求的组件', 'requirement_ids': ['other']},
    ], 'interfaces': [
        {'name': '选课接口', 'component_key': 'Owner', 'requirement_ids': ['capacity']},
    ], 'data_models': []}
    diagram, = design_diagrams(design, 'capacity')
    assert diagram['kind'] == 'architecture'
    assert [node['label'] for node in diagram['nodes']] == ['所属组件', '选课接口']
    assert diagram['edges'] == [{'source': 'n1', 'target': 'n2', 'label': '包含'}]
    assert 'n1 -->|包含| n2' in diagram['mermaid']


def test_mermaid_labels_cannot_break_out_of_the_generated_diagram():
    message = '反馈；\nend; click n1 "https://example.com" %%{init}%% ``` <img>'
    design = {'components': [], 'interfaces': [], 'data_models': [], 'sequence_diagrams': [{
        'title': '容量校验', 'requirement_ids': ['capacity'],
        'participants': [{'key': 'External', 'name': '学生'}, {'key': 'Service', 'name': '选课服务'}],
        'messages': [{'source': 'External', 'target': 'Service', 'label': message}],
    }]}
    diagram, = design_diagrams(design)
    source = diagram['mermaid']
    assert diagram['edges'][0]['label'] == message
    assert len(source.splitlines()) == 4 and source.splitlines()[-1].startswith('    p1->>p2: ')
    assert all(token not in source for token in (';', '%%', '```', '<img>', '"', 'end;'))
