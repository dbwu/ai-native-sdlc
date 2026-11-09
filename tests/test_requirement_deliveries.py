"""使用选课教学知识验收固定版本的需求和设计文档。"""
from uuid import UUID, uuid4

import pytest

from knowledge import model_gateway
from knowledge.design import DesignService
from test_design import action, create, detail, generate, model_environment, returned
from test_generic_workflow import approve, import_version


pytestmark = pytest.mark.integration


def delivery_path(project, requirement):
    return f'/workbench/projects/{project["id"]}/requirements/{requirement["id"]}/deliveries'


def preview(client, url, version, **params):
    response = client.get(url, params={'version': version, **params})
    assert response.status_code == 200, response.text
    return response.json()


def revise(client, record, description):
    body = {key: record[key] for key in ('name', 'status', 'attributes', 'evidence', 'provenance')}
    body.update(expected_version=record['version'], description=description)
    response = client.post('/operations/proposals', json={
        'project_id': record['project_id'], 'title': '验收版本修订', 'actor': '选课验收人员',
        'changes': [{'op': 'update_entity', 'id': record['id'], 'data': body}],
    })
    assert response.status_code == 201, response.text
    approve(client, response.json())


def test_course_capacity_confirmed_deliveries_pin_versions_and_scope(client, db_url, project, monkeypatch):
    import_version(client, project, 'v1')
    import_version(client, project, 'v1.1')
    knowledge = client.get(f'/workbench/projects/{project["id"]}/knowledge').json()
    by_key = {knowledge['keys'].get(item['id']): item for item in knowledge['entities']}
    capacity, capacity_rule = by_key['REQ-CAPACITY'], by_key['BR-CAPACITY']
    other = next(item for item in knowledge['entities'] if item['kind'] == 'Requirement' and item['id'] != capacity['id'])
    url = delivery_path(project, capacity)
    diagrams_url = f'/workbench/projects/{project["id"]}/design-diagrams'

    def project_diagrams():
        response = client.get(diagrams_url)
        assert response.status_code == 200, response.text
        return response.json()['entries']

    baseline = preview(client, url, capacity['version'])
    assert baseline['design'] is None and baseline['documents']['design'] is None
    assert project_diagrams() == []
    standalone = baseline['documents']['requirements_analysis']
    assert '教学班容量限制' in standalone['markdown']
    assert client.get(url + '/design', params={'version': capacity['version']}).status_code == 404

    response = client.post(f'/design/projects/{project["id"]}/flows', json={
        'title': '选课容量限制验收', 'goal': '确认容量及基础选课规则', 'actor': '选课验收人员',
        'requirements': [{key: item[key] for key in ('id', 'version')} for item in (capacity, other)],
        'input_knowledge': [{key: capacity_rule[key] for key in ('id', 'version')}],
    })
    assert response.status_code == 201, response.text
    flow_id = response.json()['id']
    current = action(client, detail(client, flow_id), 'start')

    def complete(context, schema, **kwargs):
        if not context['clarifications']:
            return returned({'summary': '请确认选课口径', 'questions': [
                {'key': 'Duplicate', 'question': '满员时如何处理重复请求？', 'requirement_ids': [capacity['id']]},
                {'key': 'Other', 'question': '其他需求的问题不属于容量文档', 'requirement_ids': [other['id']]},
            ]})
        refs = [capacity['id']]
        return returned({'summary': '容量校验与幂等请求', 'components': [
            {'key': 'Capacity', 'name': '容量检查服务', 'description': '在事务内执行容量检查',
             'responsibility': '防止并发超额', 'requirement_ids': refs},
            {'key': 'Other', 'name': '不属于容量需求的组件', 'responsibility': '处理其他事项', 'requirement_ids': [other['id']]},
        ], 'interfaces': [
            {'key': 'Enroll', 'name': '提交选课', 'protocol': 'HTTP', 'component_key': 'Capacity',
             'contract': {'path': '/enroll'}, 'requirement_ids': refs},
            {'key': 'OtherInterface', 'name': '其他需求专用接口', 'protocol': 'HTTP', 'component_key': 'Other',
             'contract': {'path': '/other'}, 'requirement_ids': [other['id']]},
        ],
            'data_models': [{'key': 'Offering', 'name': '教学班', 'component_key': 'Capacity',
                             'schema_definition': {'fields': ['capacity']}, 'requirement_ids': refs}],
            'collaboration_links': [
                {'source': {'kind': 'interface', 'key': 'Enroll'}, 'target': {'kind': 'component', 'key': 'Capacity'},
                 'type': 'calls', 'rationale': '提交选课后先由容量检查服务处理满员规则', 'requirement_ids': refs},
                {'source': {'kind': 'component', 'key': 'Capacity'}, 'target': {'kind': 'data_model', 'key': 'Offering'},
                 'type': 'reads', 'rationale': '读取教学班容量和已选人数以判断是否满员', 'requirement_ids': refs},
                {'source': {'kind': 'component', 'key': 'Capacity'}, 'target': {'kind': 'data_model', 'key': 'Offering'},
                 'type': 'writes', 'rationale': '仅在新增选课成功时更新已选人数', 'requirement_ids': refs},
                {'source': {'kind': 'component', 'key': 'Other'}, 'target': {'kind': 'interface', 'key': 'OtherInterface'},
                 'type': 'depends_on', 'rationale': '这是另一需求专用的依赖依据', 'requirement_ids': [other['id']]},
            ],
            'test_plan': [
                {'key': 'CapacityTest', 'name': '满员拒绝新增', 'steps': ['提交新选课'], 'expected_result': '人数不变', 'requirement_ids': refs},
                {'key': 'OtherTest', 'name': '其他需求测试', 'steps': ['处理其他事项'], 'expected_result': '完成', 'requirement_ids': [other['id']]},
            ], 'assumptions': ['事务内维护人数'], 'deferred_items': ['候补另行设计'],
            'sequence_diagrams': [
                {'title': '满员时拒绝新增', 'requirement_ids': refs,
                 'participants': [{'key': 'Student', 'name': '学生'}, {'key': 'Service', 'name': '选课服务'}],
                 'messages': [{'source': 'Student', 'target': 'Service', 'label': '提交选课'},
                              {'source': 'Service', 'target': 'Student', 'label': '返回满员错误'}]},
                {'title': '另一需求专用时序', 'requirement_ids': [other['id']],
                 'participants': [{'key': 'Student', 'name': '学生'}, {'key': 'Service', 'name': '选课服务'}],
                 'messages': [{'source': 'Student', 'target': 'Service', 'label': '查看课表'}]},
            ], 'use_case_diagrams': [
                {'title': '容量限制用例', 'requirement_ids': refs, 'actors': [{'key': 'Student', 'name': '学生'}],
                 'use_cases': [{'key': 'Enroll', 'name': '选修教学班'}],
                 'associations': [{'actor': 'Student', 'use_case': 'Enroll'}]},
            ]})

    monkeypatch.setattr(model_gateway, 'complete_design', complete)
    for expected_state in ('NEEDS_CLARIFICATION', 'REVIEW_PENDING'):
        current = action(client, current, 'generate', 202)
        DesignService(db_url).run_job(UUID(current['flow']['state']['active_job_id']))
        current = detail(client, flow_id)
        assert current['flow']['status'] == expected_state
        if expected_state == 'NEEDS_CLARIFICATION':
            response = client.post(f'/design/flows/{flow_id}/answers', json={
                'expected_version': current['flow']['version'], 'actor': '选课验收人员', 'reason': '确认容量规则',
                'answers': [{'key': 'Duplicate', 'answer': '即使满员也返回原选课记录。'},
                            {'key': 'Other', 'answer': '另一需求的独有答复'}],
            })
            assert response.status_code == 200, response.text
            current = response.json()

    assert preview(client, url, capacity['version'])['design'] is None
    assert project_diagrams() == []
    candidate = {diagram['kind']: diagram for diagram in current['diagrams']}
    assert set(candidate) == {'use_case', 'sequence', 'architecture', 'collaboration'}
    assert {edge['label'] for edge in candidate['collaboration']['edges']} == {'调用', '读取', '写入', '依赖'}
    assert client.get(url, params={'version': capacity['version'], 'design_id': flow_id}).status_code == 409
    current = action(client, action(client, current, 'validate'), 'commit')
    assert preview(client, url, capacity['version'])['design'] is None
    assert project_diagrams() == []
    current = action(client, current, 'complete')

    delivery = preview(client, url, capacity['version'], design_id=flow_id)
    assert delivery['design']['id'] == flow_id and delivery['design']['version'] == current['flow']['version']
    assert delivery['design']['shared_requirement_count'] == 2
    assert {item['name'] for item in delivery['design']['artifacts']} == {'容量检查服务', '提交选课', '教学班', '满员拒绝新增'}
    analysis, design = (delivery['documents'][kind] for kind in ('requirements_analysis', 'design'))
    assert '即使满员也返回原选课记录' in analysis['markdown'] and '另一需求的独有答复' not in analysis['markdown']
    assert '防止并发超额' in design['markdown'] and '不属于容量需求的组件' not in design['markdown']
    assert '候补另行设计' in design['markdown'] and '测试计划（设计阶段）' in design['markdown']
    diagrams = {item['kind']: item for item in delivery['design']['diagrams']}
    assert set(diagrams) == {'use_case', 'sequence', 'architecture', 'collaboration'}
    architecture = diagrams['architecture']
    assert architecture['title'] == '职责图'
    assert architecture['nodes'][0]['label'] == f'{capacity["name"]} · v{capacity["version"]}'
    assert any(node.get('detail') == '防止并发超额' for node in architecture['nodes'])
    collaboration = diagrams['collaboration']
    assert collaboration['title'] == '协作图'
    assert {node['label'] for node in collaboration['nodes']} == {'容量检查服务', '提交选课', '教学班'}
    assert {edge['label'] for edge in collaboration['edges']} == {'调用', '读取', '写入'}
    assert all(edge['requirements'] == [f'{capacity["name"]} · v{capacity["version"]}'] for edge in collaboration['edges'])
    assert '|调用|' in collaboration['mermaid'] and '|读取 / 写入|' in collaboration['mermaid']
    assert '### 职责图' in design['markdown'] and '### 协作图' in design['markdown']
    assert '协作关系与设计依据' in design['markdown']
    assert all(edge['rationale'] in design['markdown'] for edge in collaboration['edges'])
    assert '这是另一需求专用的依赖依据' not in design['markdown']
    assert '其他需求专用接口' not in design['markdown']
    assert '交互顺序请看业务时序图' in design['markdown']
    assert '另一需求专用时序' not in design['markdown'] and '另一需求专用时序' not in analysis['markdown']
    assert f'```mermaid\n{diagrams["use_case"]["mermaid"]}\n```' in analysis['markdown']
    for kind in ('sequence', 'architecture', 'collaboration'):
        assert f'```mermaid\n{diagrams[kind]["mermaid"]}\n```' in design['markdown']
    for doc in (analysis, design, standalone):
        response = client.get(doc['download_url'])
        assert response.status_code == 200, response.text
        assert response.content == doc['markdown'].encode('utf-8')
        assert response.headers['Content-Type'].startswith('text/markdown')
        assert "filename*=UTF-8''" in response.headers['Content-Disposition']

    by_kind = {item['kind']: item for item in delivery['design']['artifacts']}
    component = client.get('/entities/' + by_kind['ArchitectureComponent']['id']).json()
    pinned_links = component['attributes']['extensions']['collaboration_links']
    target = by_kind['DataModel']
    assert pinned_links == [
        {'type': relation_type, 'target_id': target['id'], 'target_kind': target['kind'],
         'target_name': target['name'], 'target_version': target['version'], 'rationale': rationale,
         'requirements': [{'id': capacity['id'], 'version': capacity['version']}]}
        for relation_type, rationale in (('reads', '读取教学班容量和已选人数以判断是否满员'),
                                         ('writes', '仅在新增选课成功时更新已选人数'))
    ]

    confirmed_diagrams = project_diagrams()
    entries = {entry['requirement']['entity_id']: entry for entry in confirmed_diagrams}
    assert entries[capacity['id']] == {
        'requirement': {'entity_id': capacity['id'], 'name': capacity['name'], 'version': capacity['version']},
        'design_id': flow_id, 'confirmed_at': delivery['design']['confirmed_at'],
        'diagrams': delivery['design']['diagrams'],
    }
    assert {entry['design_id'] for entry in confirmed_diagrams} == {flow_id}
    other_collaboration = next(diagram for diagram in entries[other['id']]['diagrams'] if diagram['kind'] == 'collaboration')
    assert [edge['label'] for edge in other_collaboration['edges']] == ['依赖']

    revise(client, component, '后续组件修订不属于旧设计文档')
    revise(client, capacity, '后续容量需求说明')
    newer = preview(client, url, capacity['version'] + 1)
    assert '后续容量需求说明' in newer['documents']['requirements_analysis']['markdown'] and newer['design'] is None
    assert project_diagrams() == confirmed_diagrams
    assert preview(client, url, capacity['version'], design_id=flow_id) == delivery
    assert client.get(url, params={'version': capacity['version'] + 1, 'design_id': flow_id}).status_code == 409
    assert client.get(url.replace(project['id'], str(uuid4())), params={'version': capacity['version']}).status_code == 404

    def complete_replacement(context, schema, **kwargs):
        refs = [capacity['id']]
        return returned({
            'summary': '使用修订后的容量规则',
            'components': [{'key': 'Capacity', 'name': '修订版容量检查服务', 'responsibility': '防止并发超额',
                            'requirement_ids': refs}],
            'test_plan': [{'key': 'CapacityTest', 'name': '满员拒绝新增', 'steps': ['提交新选课'],
                           'expected_result': '人数不变', 'requirement_ids': refs}],
        })

    monkeypatch.setattr(model_gateway, 'complete_design', complete_replacement)
    latest = confirmed_diagrams
    for _ in range(2):
        replacement = create(client, project, newer['requirement'])
        replacement = generate(client, db_url, action(client, replacement, 'start'))
        assert replacement['flow']['status'] == 'REVIEW_PENDING'
        assert project_diagrams() == latest
        replacement = action(client, action(client, action(client, replacement, 'validate'), 'commit'), 'complete')
        latest = project_diagrams()
        assert len(latest) == 2 and latest[1] == entries[other['id']]
        assert latest[0]['design_id'] == replacement['flow']['id']
        assert latest[0]['requirement']['version'] == capacity['version'] + 1
        assert {diagram['kind'] for diagram in latest[0]['diagrams']} == {'architecture'}
