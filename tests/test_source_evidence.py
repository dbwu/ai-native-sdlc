"""用真实 PostgreSQL 验收固定来源版本和选课知识历史。"""
import hashlib
from uuid import uuid4

import pytest

from test_generic_workflow import approve, import_version


pytestmark = pytest.mark.integration
SNAPSHOTS = '/operations/source-snapshots'
PROPOSALS = '/operations/source-evidence/proposals'
ACTOR = '选课原文验收人员'


def checked(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def register(client, project_id, uri, external_revision, content):
    return checked(client.post(SNAPSHOTS, json={
        'project_id': project_id, 'source_uri': uri, 'source_revision': external_revision, 'content': content, 'actor': ACTOR,
    }), 201)


def proposal_input(project_id, table, record, snapshot, content, quote):
    start = content.index(quote)
    return {'project_id': project_id, 'target': {'table': table, 'id': record['id'], 'version': record['version']},
            'span': {'source_id': snapshot['source_id'], 'source_version': snapshot['version'], 'sha256': snapshot['sha256'],
                     'start': start, 'end': start + len(quote), 'quote': quote},
            'actor': ACTOR, 'reason': '将明确的选课约束关联到原文'}


def read_evidence(client, project_id, target):
    return checked(client.get(f'/operations/projects/{project_id}/source-evidence', params=target))['items']


def publish_baseline(client, project_id, version, release):
    path = f'/workbench/projects/{project_id}/project-model'
    candidate = checked(client.post(path + '/course-example', json={
        'expected_version': version, 'actor': ACTOR, 'release': release,
    }), 201)
    approve(client, candidate)
    return checked(client.get(path))['baseline']


def test_course_source_evidence_and_old_course_baselines_remain_pinned(client, project):
    project_id = project['id']
    import_version(client, project, 'v1')
    first = publish_baseline(client, project_id, 0, 'v1')
    initial_import = import_version(client, project, 'v1.1')
    second = publish_baseline(client, project_id, 1, 'v1.1')
    capacity = next(item for item in initial_import['result']['entity_mapping'] if item['key'] == 'REQ-CAPACITY')
    entity_url = f'/entities/{capacity["entity_id"]}'
    record = checked(client.get(entity_url))
    assert record['version'] == capacity['version']

    uri = 'urn:test:course-capacity-prd'
    quote = '  名额为零时拒绝新增。  '
    source_text = '# 选课 📚\r\n' + quote + '\r\n重复请求返回原记录。\r\n'
    snapshot = register(client, project_id, uri, '文档初稿', source_text)
    assert snapshot['version'] == 1 and snapshot['format'] == 'text' and not snapshot['replayed']
    assert snapshot['sha256'] == hashlib.sha256(source_text.encode()).hexdigest()
    assert register(client, project_id, uri, '文档初稿', source_text) == {**snapshot, 'replayed': True}
    assert client.post(SNAPSHOTS, json={'project_id': project_id, 'source_uri': uri, 'source_revision': '文档初稿',
                                       'content': '内容变更', 'actor': ACTOR}).status_code == 409
    body = proposal_input(project_id, 'entity', record, snapshot, source_text, quote)
    assert read_evidence(client, project_id, body['target']) == []
    assert client.post(PROPOSALS, json={**body, 'span': {**body['span'], 'sha256': '0' * 64}}).status_code == 409
    assert client.post(PROPOSALS, json={**body, 'span': {**body['span'], 'quote': quote.replace('拒绝', '允许')}}).status_code == 422

    candidate = checked(client.post(PROPOSALS, json=body), 201)
    proposal_path = f'/operations/proposals/{candidate["id"]}'
    approval = {'expected_version': candidate['version'], 'actor': ACTOR, 'reason': '核对容量规则的精确原文'}
    assert client.post(proposal_path + '/commit', json=approval).status_code == 409
    validated = checked(client.post(proposal_path + '/validate', json=approval))['proposal']
    assert validated['status'] == 'validated'
    assert checked(client.get(entity_url)) == record
    assert read_evidence(client, project_id, body['target']) == []

    # 来源可独立登记新版本。此提案明确引用不可变的旧快照。
    second_source = register(client, project_id, uri, '文档修订', '# 选课修订\n' + quote)
    assert second_source['source_id'] == snapshot['source_id'] and second_source['version'] == 2
    checked(client.post(proposal_path + '/commit', json={**approval, 'expected_version': validated['version']}))
    current = checked(client.get(entity_url))
    assert current['version'] == record['version'] + 1
    assert current['provenance'] == record['provenance'] and current['attributes'] == record['attributes']
    linked = {**body['target'], 'version': current['version']}
    items = read_evidence(client, project_id, linked)
    assert items[0]['span'] == body['span'] and items[0]['source'] == {
        'uri': uri, 'external_revision': '文档初稿', 'format': 'text',
    }
    source_url = f'/operations/sources/{snapshot["source_id"]}/versions/{items[0]["span"]["source_version"]}'
    assert checked(client.get(source_url))['content'] == source_text
    assert client.post(PROPOSALS, json=body).status_code == 409
    assert client.post(PROPOSALS, json={**body, 'target': linked}).status_code == 409

    import_version(client, project, 'v1.2')
    third = publish_baseline(client, project_id, 2, 'v1.2')
    assert third['version'] == 3
    path = f'/workbench/projects/{project_id}/project-model'
    for baseline in (first, second):
        assert checked(client.get(path, params={'version': baseline['version']}))['baseline'] == baseline
    assert checked(client.get(entity_url, params={'version': record['version']})) == record
    assert read_evidence(client, project_id, body['target']) == []
    assert read_evidence(client, project_id, linked) == items


def test_source_evidence_relationship_and_project_scope(client, project, entity, relationship, provenance):
    project_id = project['id']
    edge = relationship(entity(), entity())
    uri, content = 'urn:test:evidence-rel', '依据：组件须读取余位。'
    snapshot = register(client, project_id, uri, 'v1', content)
    body = proposal_input(project_id, 'relationship', edge, snapshot, content, '组件须读取余位。')
    approve(client, checked(client.post(PROPOSALS, json=body), 201))
    assert read_evidence(client, project_id, body['target']) == []
    linked = {**body['target'], 'version': edge['version'] + 1}
    evidence, = read_evidence(client, project_id, linked)
    assert evidence['span'] == body['span']

    other = checked(client.post('/entities', json={'kind': 'Project', 'name': '其他项目', 'provenance': provenance}), 201)
    foreign_snapshot = register(client, other['id'], uri, 'v1', content)
    wrong_source = {**body, 'target': linked, 'span': {**body['span'], 'source_id': foreign_snapshot['source_id']}}
    assert client.post(PROPOSALS, json=wrong_source).status_code == 404
    assert client.get(f'/operations/projects/{uuid4()}/source-evidence', params=linked).status_code == 404
