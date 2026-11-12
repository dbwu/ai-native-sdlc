"""固定快照接口对照与选课 HTTP 入口的边界。"""
import importlib.util
from pathlib import Path
import runpy
import sys

from fastapi.testclient import TestClient
import pytest

from knowledge.course_executor import build_course
from knowledge.interface_comparison import compare_interfaces, extract_fastapi
from knowledge.workspace import normalized_files


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples' / 'course-selection' / 'app.py'


def scan(content):
    return extract_fastapi(normalized_files([{'path': 'app.py', 'content': content}]))


def contract(path='/enroll', **fields):
    return {'key': 'Enroll', 'protocol': 'HTTP', 'name': '提交选课',
            'contract': {'method': 'POST', 'path': path, **fields}}


def test_static_declarations_preserve_literal_fields_without_importing_snapshot():
    routes, issues = scan("""import fastapi as fa
raise RuntimeError('此文件不能被导入')
app = fa.FastAPI()
@app.api_route(path='/enroll', methods=['GET', 'POST'], status_code=201, response_model=Result)
async def submit():
    pass
""")
    assert not issues
    assert [route['method'] for route in routes] == ['GET', 'POST']
    assert routes[1]['evidence']['line'] == 4
    assert 'async def submit' in routes[1]['evidence']['snippet']
    rows = compare_interfaces([contract(status_code=200, response_model='Result')], routes, issues)
    assert [(row['method'], row['status']) for row in rows] == [('GET', 'extra'), ('POST', 'changed')]
    assert {field['name']: field['actual'] for field in rows[1]['fields']} == {
        'status_code': 201, 'response_model': 'Result',
    }


def test_unknown_route_or_missing_structured_contract_does_not_imply_missing_interface():
    routes, issues = scan("""from fastapi import FastAPI
app = FastAPI()
app.include_router(other_router)
@app.post(PREFIX + '/enroll')
def submit():
    pass
""")
    assert not routes
    assert {item['line'] for item in issues} == {3, 4}
    assert compare_interfaces([contract()], routes, issues)[0]['status'] == 'uncertain'
    routes, issues = scan(EXAMPLE.read_text(encoding='utf-8'))
    legacy = {**contract(), 'contract': {'description': '按选课规则返回结果'}}
    assert {row['status'] for row in compare_interfaces([legacy], routes, issues)} == {'uncertain'}
    assert compare_interfaces([contract(response_body={'id': 'integer'})], routes, issues)[0]['status'] == 'uncertain'


@pytest.mark.parametrize('release', ['v1', 'v1.1', 'v1.2'])
def test_course_adapter_matches_openapi_and_keeps_business_rules(release, tmp_path, monkeypatch):
    source = tmp_path / 'course_app.py'
    source.write_text(build_course(release)['files']['course_app.py'], encoding='utf-8')
    spec = importlib.util.spec_from_file_location('course_app', source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'course_app', module)
    spec.loader.exec_module(module)
    adapter = tmp_path / 'app.py'
    adapter.write_text(EXAMPLE.read_text(encoding='utf-8'), encoding='utf-8')
    app = runpy.run_path(str(adapter))['app']
    with TestClient(app) as client:
        schema = client.get('/openapi.json').json()
        assert {path: set(methods) for path, methods in schema['paths'].items()} == {'/enroll': {'post'}}
        body = {'student': 'S001', 'offering': 'O101'}
        first = client.post('/enroll', json=body)
        again = client.post('/enroll', json=body)
        assert first.status_code == again.status_code == 200
        assert first.json() == {'id': again.json()['id'], 'created': True}
        assert again.json()['created'] is False
        capacity = client.post('/enroll', json={**body, 'student': 'S002'})
        assert capacity.status_code == (200 if release == 'v1' else 409)
        if release == 'v1.2':
            overlap = client.post('/enroll', json={**body, 'offering': 'O102'})
            assert overlap.status_code == 409 and '时间冲突' in overlap.json()['detail']
            assert client.post('/enroll', json={**body, 'offering': 'O103'}).status_code == 200
