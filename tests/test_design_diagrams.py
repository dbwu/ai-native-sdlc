from knowledge.design_diagrams import design_diagrams
from knowledge.design_models import SequenceDiagram
import pytest
from pydantic import ValidationError


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


def test_architecture_explains_direct_responsibility_and_supporting_ownership():
    design = {'components': [
        {'key': 'Capacity', 'name': '容量检查服务', 'responsibility': '在事务内检查余位', 'requirement_ids': ['capacity']},
        {'key': 'Entry', 'name': '选课入口', 'responsibility': '接收选课请求', 'requirement_ids': ['basic']},
    ], 'interfaces': [
        {'name': '提交选课接口', 'component_key': 'Entry', 'requirement_ids': ['capacity']},
    ], 'data_models': [
        {'name': '教学班容量', 'component_key': 'Capacity', 'requirement_ids': ['capacity']},
    ]}
    requirements = [{'id': 'capacity', 'name': '教学班容量限制', 'version': 2},
                    {'id': 'basic', 'name': '基础选课', 'version': 1}]
    diagram, = design_diagrams(design, 'capacity', requirements=requirements)
    assert [node['label'] for node in diagram['nodes']] == [
        '教学班容量限制 · v2', '容量检查服务', '选课入口', '提交选课接口', '教学班容量']
    assert diagram['nodes'][1]['detail'] == '在事务内检查余位'
    assert [edge['label'] for edge in diagram['edges']] == ['由其负责', '关联产物归属', '包含', '包含']
    assert 'n1 -->|由其负责| n2' in diagram['mermaid']
    assert 'n1 -->|关联产物归属| n3' in diagram['mermaid']


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


def test_collaboration_keeps_direction_and_rationale_without_inferring_links_from_ownership():
    capacity = {'id': 'capacity', 'name': '教学班容量限制', 'version': 2}
    service = {'kind': 'component', 'key': 'Service'}
    offering = {'kind': 'data_model', 'key': 'Offering'}
    design = {
        'components': [{'key': 'Service', 'name': '选课服务', 'requirement_ids': ['capacity']},
                       {'key': 'Other', 'name': '课表服务', 'requirement_ids': ['timetable']}],
        'interfaces': [{'key': 'Enroll', 'name': '提交选课', 'component_key': 'Service', 'requirement_ids': ['capacity']}],
        'data_models': [{'key': 'Offering', 'name': '教学班', 'component_key': 'Service', 'requirement_ids': ['capacity']}],
        'collaboration_links': [
            {'source': service, 'target': offering, 'type': 'reads', 'rationale': '检查余位', 'requirement_ids': ['capacity']},
            {'source': service, 'target': offering, 'type': 'writes', 'rationale': '成功后扣减余位', 'requirement_ids': ['capacity']},
            {'source': {'kind': 'component', 'key': 'Other'}, 'target': service, 'type': 'depends_on',
             'rationale': '课表展示已选课程', 'requirement_ids': ['timetable']},
        ],
    }
    responsibility, collaboration = design_diagrams(design, capacity['id'], requirements=[capacity])
    assert responsibility['title'] == '职责图'
    assert [node['label'] for node in collaboration['nodes']] == ['选课服务', '教学班']
    assert collaboration['edges'] == [
        {'source': 'n1', 'target': 'n2', 'label': '读取', 'rationale': '检查余位', 'requirements': ['教学班容量限制 · v2']},
        {'source': 'n1', 'target': 'n2', 'label': '写入', 'rationale': '成功后扣减余位', 'requirements': ['教学班容量限制 · v2']},
    ]
    assert 'n1 -->|读取 / 写入| n2' in collaboration['mermaid']
    assert '包含' not in collaboration['mermaid']


def test_sequence_identity_is_explicit_and_requirement_versions_remain_fixed():
    candidate = {'key': 'CapacityRefusal', 'title': '容量限制', 'requirement_ids': ['capacity'],
                 'participants': [{'key': 'Student', 'name': '学生'}, {'key': 'Enrollment', 'name': '选课模块'}],
                 'messages': [{'key': 'Request', 'source': 'Student', 'target': 'Enrollment', 'label': '提交请求'},
                              {'key': 'Refusal', 'source': 'Enrollment', 'target': 'Student', 'label': '返回满员'}]}
    parsed = SequenceDiagram.model_validate(candidate)
    diagram, = design_diagrams({'components': [], 'interfaces': [], 'data_models': [],
                                'sequence_diagrams': [parsed.model_dump(mode='json')]},
                               requirements=[{'id': 'capacity', 'name': '容量限制', 'version': 2}])
    assert diagram['model_refs']['identity_complete']
    assert diagram['model_refs']['diagram_key'] == 'CapacityRefusal'
    assert diagram['model_refs']['requirement_refs'] == [{'id': 'capacity', 'version': 2}]
    assert [item['key'] for item in diagram['model_refs']['edges']] == ['Request', 'Refusal']
    assert diagram['model_refs']['nodes']['p2'] == {'kind': 'participant', 'key': 'Enrollment'}
    candidate['messages'][1]['key'] = 'Request'
    with pytest.raises(ValidationError, match='消息稳定标识不能重复'):
        SequenceDiagram.model_validate(candidate)
    candidate.pop('key')
    for message in candidate['messages']:
        message.pop('key')
    parsed = SequenceDiagram.model_validate(candidate)
    diagram, = design_diagrams({'components': [], 'interfaces': [], 'data_models': [],
                                'sequence_diagrams': [parsed.model_dump(mode='json')]})
    assert not diagram['model_refs']['identity_complete']
    assert all(item['key'] is None for item in diagram['model_refs']['edges'])


def test_design_reference_is_fixed_per_diagram_and_does_not_mutate_callers():
    design = {'components': [{'key': 'Enrollment', 'name': '选课模块', 'requirement_ids': ['capacity']}],
              'interfaces': [], 'data_models': [], 'sequence_diagrams': [{
                  'key': 'Submit', 'title': '提交选课', 'requirement_ids': ['capacity'],
                  'participants': [{'key': 'Student', 'name': '学生'}, {'key': 'Enrollment', 'name': '选课模块'}],
                  'messages': [{'key': 'Request', 'source': 'Student', 'target': 'Enrollment', 'label': '提交请求'}],
              }]}
    reference = {'flow_id': 'flow-a', 'flow_version': 4, 'proposal_id': 'proposal-a'}
    diagrams = design_diagrams(design, design_ref=reference)
    assert len(diagrams) == 2 and all(item['design_ref'] == reference for item in diagrams)
    reference['flow_version'] = 5
    assert all(item['design_ref']['flow_version'] == 4 for item in diagrams)
    diagrams[0]['design_ref']['flow_version'] = 6
    assert diagrams[1]['design_ref']['flow_version'] == 4
    assert all('design_ref' not in item for item in design_diagrams(design))
