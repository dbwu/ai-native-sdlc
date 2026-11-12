from copy import deepcopy
from uuid import uuid4

import pytest

from knowledge.project_comparison import build_comparison, comparison_markdown
from test_project_model import baseline  # noqa: F401


def revision(model, version):
    return {'version': version, 'proposal_id': uuid4(), 'document': {
        'model': model,
        'source_refs': {item['key']: {'requirement_refs': [], 'rule_refs': []}
                        for item in model.get('processes', [])},
    }}


def test_architecture_overlay_keeps_removed_nodes_edges_and_former_parent(baseline):
    after = deepcopy(baseline)
    after['nodes'] = [node for node in after['nodes'] if node['key'] != 'Catalog']
    after['nodes'][-1].update(parent_key='Course', name='选课记录')
    after['nodes'].append({'key': 'Capacity', 'name': '余位校验', 'kind': 'module', 'parent_key': 'Enroll'})
    after['dependencies'] = [{'source': 'Enroll', 'target': 'Capacity', 'label': '判断余位'}]
    after['processes'] = []
    report = build_comparison(revision(baseline, 1), revision(after, 2), 'architecture')
    overlay = report['overlay']
    assert {node.get('change') for node in overlay['nodes']} == {None, 'added', 'removed', 'updated'}
    assert '【删除】教学班目录' in overlay['mermaid'] and 'stroke-dasharray:5 3' in overlay['mermaid']
    assert '【删除】依赖：读取教学班' in overlay['mermaid']
    assert '【删除】包含' in overlay['mermaid'] and '【修改】包含' in overlay['mermaid']
    assert '教学班目录' not in report['after']['mermaid']
    assert overlay['mermaid'] in comparison_markdown(report)


def test_activity_overlay_removes_a_branch_and_step_without_rewriting_snapshots(baseline):
    after = deepcopy(baseline)
    process = after['processes'][0]
    process['steps'] = [step for step in process['steps'] if step['key'] != 'Save']
    process['steps'][1].update(name='校验完成', kind='task')
    process['transitions'] = [{'source': 'Start', 'target': 'Check'}, {'source': 'Check', 'target': 'Done'}]
    report = build_comparison(revision(baseline, 1), revision(after, 2), 'activity', 'Submit')
    assert '【删除】保存选课' in report['overlay']['mermaid']
    assert '【删除】有余位' in report['overlay']['mermaid']
    assert '【修改】校验完成' in report['overlay']['mermaid']
    assert '保存选课' in report['before']['mermaid']
    assert '保存选课' not in report['after']['mermaid']


def test_sequence_keeps_message_order_and_shows_deletions_as_notes(baseline):
    before_messages = baseline['processes'][0]['scenarios'][0]['messages']
    for message, key in zip(before_messages, ('Request', 'Query', 'Result'), strict=True):
        message['key'] = key
    after = deepcopy(baseline)
    after['processes'][0]['scenarios'][0]['messages'] = [
        before_messages[0], {**before_messages[1], 'key': 'Verify', 'label': '核对容量'},
        {**before_messages[2], 'label': '明确返回满员'},
    ]
    report = build_comparison(revision(baseline, 1), revision(after, 2), 'sequence', 'Submit', 'Full')
    source = report['overlay']['mermaid']
    assert not report['notice']
    assert '【新增】核对容量' in source and '【修改】明确返回满员' in source
    assert 'Note over' in source and '【删除】原第 2 条' in source
    arrows = [line for line in source.splitlines() if '->>' in line]
    assert [line.split(': ', 1)[1] for line in arrows] == ['提交请求', '【新增】核对容量', '【修改】明确返回满员']


def test_legacy_message_comparison_is_at_scene_level_and_removed_scene_can_be_selected(baseline):
    after = deepcopy(baseline)
    after['processes'][0]['scenarios'][0]['messages'][1]['label'] = '核对容量'
    report = build_comparison(revision(baseline, 1), revision(after, 2), 'sequence')
    assert report['notice'] and report['overlay']['title'].startswith('【修改】')
    assert '【修改】核对容量' not in report['overlay']['mermaid']
    after['processes'][0]['scenarios'] = []
    removed = build_comparison(revision(baseline, 1), revision(after, 2), 'sequence')
    assert removed['selection']['scenario_key'] == 'Full'
    assert removed['after'] is None
    assert '【删除】原第 1 条' in removed['overlay']['mermaid']


def test_adding_keys_to_legacy_messages_does_not_mark_unchanged_scene(baseline):
    after = deepcopy(baseline)
    for index, message in enumerate(after['processes'][0]['scenarios'][0]['messages']):
        message['key'] = f'Message{index}'
    report = build_comparison(revision(baseline, 1), revision(after, 2), 'sequence')
    assert report['notice']
    assert report['changes'] == []
    assert '【修改】' not in report['overlay']['title']
