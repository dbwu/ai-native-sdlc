from copy import deepcopy

import pytest
from pydantic import ValidationError

from knowledge.project_model import ProjectModel, architecture_diagram, process_diagrams
from knowledge.project_baselines import _diff


@pytest.fixture
def baseline():
    return {
        'title': '选课基线',
        'nodes': [
            {'key': 'Course', 'name': '课程领域', 'kind': 'domain'},
            {'key': 'Catalog', 'name': '教学班目录', 'kind': 'module', 'parent_key': 'Course'},
            {'key': 'Selection', 'name': '选课领域', 'kind': 'domain'},
            {'key': 'Enroll', 'name': '选课办理', 'kind': 'module', 'parent_key': 'Selection'},
            {'key': 'Records', 'name': '已选记录', 'kind': 'module', 'parent_key': 'Enroll'},
        ],
        'dependencies': [{'source': 'Enroll', 'target': 'Catalog', 'label': '读取教学班'}],
        'processes': [{
            'key': 'Submit', 'name': '提交选课',
            'participants': [{'key': 'Student', 'name': '学生'}],
            'steps': [
                {'key': 'Start', 'name': '提交请求', 'kind': 'start', 'owner': 'Student'},
                {'key': 'Check', 'name': '还有名额？', 'kind': 'decision', 'owner': 'Enroll'},
                {'key': 'Save', 'name': '保存选课', 'kind': 'task', 'owner': 'Records'},
                {'key': 'Done', 'name': '结束', 'kind': 'end', 'owner': 'Student'},
            ],
            'transitions': [
                {'source': 'Start', 'target': 'Check'},
                {'source': 'Check', 'target': 'Save', 'condition': '有余位'},
                {'source': 'Check', 'target': 'Done', 'condition': '满员'},
                {'source': 'Save', 'target': 'Done'},
            ],
            'scenarios': [{
                'key': 'Full', 'name': '满员拒绝', 'messages': [
                    {'source': 'Student', 'target': 'Enroll', 'label': '提交请求'},
                    {'source': 'Enroll', 'target': 'Catalog', 'label': '读取余位'},
                    {'source': 'Enroll', 'target': 'Student', 'label': '返回满员'},
                ],
            }],
        }],
    }


def test_baseline_diagrams_keep_hierarchy_branch_conditions_and_explicit_messages(baseline):
    model = ProjectModel.model_validate(baseline)
    architecture = architecture_diagram(model)
    assert architecture['kind'] == 'project_architecture'
    assert architecture['edges'][-2:] == [
        {'source': 'a4', 'target': 'a5', 'label': '包含'},
        {'source': 'a4', 'target': 'a2', 'label': '依赖：读取教学班'},
    ]
    assert 'a4 -.->|"依赖：读取教学班"| a2' in architecture['mermaid']
    activity, sequence = process_diagrams(model, model.processes[0])
    assert activity['kind'] == 'activity'
    assert 'subgraph lane3["已选记录"]' in activity['mermaid']
    assert 's2 -->|"满员"| s4' in activity['mermaid']
    assert activity['nodes'][1]['detail'] == '判断 · 选课办理'
    assert sequence['kind'] == 'sequence'
    assert [node['label'] for node in sequence['nodes']] == ['学生', '选课办理', '教学班目录']
    assert [edge['label'] for edge in sequence['edges']] == ['提交请求', '读取余位', '返回满员']


def test_rejects_module_hierarchy_cycle(baseline):
    baseline['nodes'][3]['parent_key'] = 'Records'
    with pytest.raises(ValidationError, match='归属形成循环'):
        ProjectModel.model_validate(baseline)


def test_rejects_cross_references_without_known_modules_and_steps(baseline):
    baseline['dependencies'][0]['target'] = 'Course'
    with pytest.raises(ValidationError, match='架构依赖必须连接'):
        ProjectModel.model_validate(baseline)
    baseline['dependencies'].clear()
    baseline['processes'][0]['steps'][2]['owner'] = 'Missing'
    with pytest.raises(ValidationError, match='已定义的模块或角色'):
        ProjectModel.model_validate(baseline)
    baseline['processes'][0]['steps'][2]['owner'] = 'Enroll'
    baseline['processes'][0]['transitions'][0]['target'] = 'Missing'
    with pytest.raises(ValidationError, match='连线必须连接不同的已定义步骤'):
        ProjectModel.model_validate(baseline)


def test_accepts_retry_with_exit_and_rejects_unreachable_steps(baseline):
    process = baseline['processes'][0]
    process['transitions'][3]['target'] = 'Check'
    ProjectModel.model_validate(baseline)
    process['steps'].append({'key': 'Lost', 'name': '无法到达', 'kind': 'end', 'owner': 'Student'})
    with pytest.raises(ValidationError, match='所有步骤必须从开始可达'):
        ProjectModel.model_validate(baseline)


def test_preview_labels_cannot_inject_mermaid_statements(baseline):
    label = 'end; click s1 "https://example.com" %%{init}%% ``` <img>'
    process = baseline['processes'][0]
    process['participants'][0]['name'] = label
    process['transitions'][1]['condition'] = label
    process['scenarios'][0]['messages'][0]['label'] = label
    baseline['nodes'][0]['name'] = label
    model = ProjectModel.model_validate(baseline)
    for diagram in [architecture_diagram(model), *process_diagrams(model, model.processes[0])]:
        assert all(token not in diagram['mermaid'] for token in (';', '%%', '```', '<img>', 'end;'))


def test_optional_stable_keys_validate_within_their_model_scope(baseline):
    baseline['dependencies'][0]['key'] = 'ReadsCatalog'
    baseline['dependencies'].append({'key': 'ReadsCatalog', 'source': 'Records', 'target': 'Catalog', 'label': '查询'})
    with pytest.raises(ValidationError, match='架构依赖稳定标识不能重复'):
        ProjectModel.model_validate(baseline)
    baseline['dependencies'].pop()
    process = baseline['processes'][0]
    process['transitions'][0]['key'] = process['transitions'][1]['key'] = 'Next'
    with pytest.raises(ValidationError, match='分支稳定标识不能重复'):
        ProjectModel.model_validate(baseline)
    process['transitions'][1]['key'] = 'Available'
    messages = process['scenarios'][0]['messages']
    messages[0]['key'] = messages[1]['key'] = 'Request'
    with pytest.raises(ValidationError, match='消息稳定标识不能重复'):
        ProjectModel.model_validate(baseline)
    messages[1]['key'] = 'Check'
    messages[2]['key'] = 'Result'
    # 消息标识以场景为作用域，相同语义名称可在另一个场景独立使用。
    other = deepcopy(process['scenarios'][0])
    other['key'] = 'AnotherScenario'
    process['scenarios'].append(other)
    ProjectModel.model_validate(baseline)


def test_model_references_survive_render_order_changes_without_rewriting_legacy_messages(baseline):
    before = deepcopy(baseline)
    model = ProjectModel.model_validate(baseline)
    initial = architecture_diagram(model)
    reversed_model = model.model_copy(update={'nodes': list(reversed(model.nodes))})
    changed_layout = architecture_diagram(reversed_model)
    assert initial['model_refs']['nodes']['a1']['key'] == 'Course'
    assert changed_layout['model_refs']['nodes']['a5']['key'] == 'Course'
    sequence = process_diagrams(model, model.processes[0])[1]
    assert not sequence['model_refs']['identity_complete']
    assert all(item['key'] is None and item['identity'] == 'legacy_scenario' for item in sequence['model_refs']['edges'])
    assert baseline == before


def test_structured_diff_distinguishes_rename_move_revision_and_deleted_element(baseline):
    old = ProjectModel.model_validate(baseline).model_dump(mode='json')
    new = deepcopy(old)
    node = new['nodes'][3]
    node.update(name='选课申请办理', parent_key='Course', entity_version=2)
    new['processes'][0]['steps'][2]['owner'] = 'Enroll'
    # Diff 接受已校验的固定快照，本单测只检查定位与 before/after，不再执行图结构校验。
    new['nodes'].pop()
    changes = _diff(old, new, before_version=3, after_version=4)
    rename = next(item for item in changes if item['subject'] == {'kind': 'module', 'key': 'Enroll', 'identity': 'stable_key'})
    assert rename['op'] == 'updated'
    assert set(rename['change_flags']) == {'renamed', 'moved', 'revision_changed'}
    assert rename['before']['name'] == '选课办理' and rename['after']['name'] == '选课申请办理'
    assert rename['comparison'] == {'before_version': 3, 'after_version': 4}
    deleted = next(item for item in changes if item['subject']['key'] == 'Records')
    assert deleted['op'] == 'removed' and deleted['after'] is None
    assert deleted['before']['parent_key'] == 'Enroll'


def test_keyed_message_diff_marks_insertions_and_real_reordering_not_shifted_indexes(baseline):
    old = ProjectModel.model_validate(baseline).model_dump(mode='json')
    messages = old['processes'][0]['scenarios'][0]['messages']
    for key, message in zip(('Request', 'Check', 'Result'), messages, strict=True):
        message['key'] = key
    new = deepcopy(old)
    later = new['processes'][0]['scenarios'][0]['messages']
    later.insert(1, {'key': 'Added', 'source': 'Enroll', 'target': 'Records', 'label': '检查已有记录'})
    changes = _diff(old, new, before_version=1, after_version=2)
    message_changes = [item for item in changes if item['subject']['kind'] == 'message']
    assert [(item['op'], item['subject']['key']) for item in message_changes] == [('added', 'Added')]
    assert message_changes[0]['subject']['process_key'] == 'Submit'
    assert message_changes[0]['subject']['scenario_key'] == 'Full'
    later[0], later[2] = later[2], later[0]
    changes = _diff(old, new)
    reordered = {item['subject']['key'] for item in changes if 'reordered' in item['change_flags']}
    assert reordered == {'Request', 'Check'}
    # 旧版本没有消息 key 时保持场景级对照，不能凭位置和文字猜配。
    for message in messages:
        message.pop('key')
    legacy_before = deepcopy(old)
    changes = _diff(old, new)
    assert any(item['subject']['kind'] == 'scenario' for item in changes)
    assert all(item['subject']['kind'] != 'message' for item in changes)
    assert old == legacy_before


def test_null_optional_identity_fields_do_not_create_false_diff(baseline):
    old = ProjectModel.model_validate(baseline).model_dump(mode='json')
    new = deepcopy(old)
    old['dependencies'][0].pop('key')
    for item in old['processes'][0]['transitions']:
        item.pop('key')
    for item in old['processes'][0]['scenarios'][0]['messages']:
        item.pop('key')
    assert _diff(old, new) == []


def test_edge_identity_keeps_endpoint_changes_and_revision_changes_separate(baseline):
    old = ProjectModel.model_validate(baseline).model_dump(mode='json')
    old['dependencies'][0]['key'] = 'Lookup'
    old['nodes'][3]['entity_version'] = 1
    new = deepcopy(old)
    new['dependencies'][0]['target'] = 'Records'
    new['nodes'][3]['entity_version'] = 2
    changes = _diff(old, new)
    edge = next(item for item in changes if item['subject']['kind'] == 'dependency')
    assert edge['subject']['key'] == 'Lookup' and edge['op'] == 'updated'
    assert edge['change_flags'] == ['endpoints_changed']
    node = next(item for item in changes if item['subject']['key'] == 'Enroll')
    assert node['change_flags'] == ['revision_changed']
