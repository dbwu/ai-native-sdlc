"""对照已确认 HTTP 契约与固定快照中的 FastAPI 静态声明。"""
import ast
from collections import defaultdict

from .db import connect
from .semantics import KnowledgeError
from .workspace import WorkspaceService


HTTP_METHODS = frozenset('GET POST PUT PATCH DELETE HEAD OPTIONS TRACE CONNECT'.split())
ROUTE_METHODS = {method.lower() for method in HTTP_METHODS} | {'api_route'}
INDIRECT_ROUTES = {'include_router', 'add_api_route', 'add_route', 'route', 'mount', 'router', 'routes'}
HTTP_PROTOCOLS = {'http', 'https', 'rest', 'restful'}
CONTRACT_FIELDS = {'method', 'path', 'status_code', 'response_model'}


def _qualified_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        if prefix:
            return f'{prefix}.{node.attr}'
    return None


def _fastapi_apps(module):
    constructors = set()
    apps = []
    for statement in module.body:
        if isinstance(statement, ast.ImportFrom) and statement.module == 'fastapi' and not statement.level:
            constructors.update(alias.asname or alias.name for alias in statement.names if alias.name == 'FastAPI')
        elif isinstance(statement, ast.Import):
            constructors.update(f'{alias.asname or alias.name}.FastAPI' for alias in statement.names if alias.name == 'fastapi')
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)) and isinstance(statement.value, ast.Call):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and _qualified_name(statement.value.func) in constructors:
                apps.append(targets[0].id)
    return apps


def _route_key(call):
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    path = call.args[0] if call.args else keywords.get('path')
    if None in keywords or not isinstance(path, ast.Constant) or not _is_path(path.value):
        return None
    method = call.func.attr
    if method != 'api_route':
        return (method.upper(),), path.value, keywords
    methods = keywords.get('methods')
    if not isinstance(methods, (ast.List, ast.Tuple)) or not methods.elts:
        return None
    if not all(isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value.upper() in HTTP_METHODS
               for item in methods.elts):
        return None
    return tuple(dict.fromkeys(item.value.upper() for item in methods.elts)), path.value, keywords


def _is_path(value):
    return isinstance(value, str) and value.startswith('/') and value.strip() == value and not any(
        character in value for character in ('\n', '\r', '\t', '?', '#'))


def _is_status(value):
    return type(value) is int and 100 <= value <= 599


def _declared_fields(keywords):
    fields = {}
    status = keywords.get('status_code')
    if isinstance(status, ast.Constant) and _is_status(status.value):
        fields['status_code'] = status.value
    model = keywords.get('response_model')
    if isinstance(model, ast.Constant) and model.value is None:
        fields['response_model'] = None
    elif name := _qualified_name(model):
        fields['response_model'] = name
    return fields


def extract_fastapi(files):
    """只解析文本 AST。对无法覆盖的注册方式保留定位与诊断。"""
    diagnostics, entries = [], []
    for file in files:
        if not file['path'].endswith('.py'):
            continue
        try:
            module = ast.parse(file['content'])
        except (SyntaxError, ValueError, RecursionError) as exc:
            diagnostics.append({'file': file['path'], 'line': getattr(exc, 'lineno', None),
                                'message': 'Python 源文件无法解析，可能影响接口覆盖。'})
            continue
        entries.extend((file, module, name) for name in _fastapi_apps(module))

    if len(entries) != 1:
        diagnostics.append({'message': '未识别到直接赋值的 FastAPI 应用。' if not entries
                            else '识别到多个 FastAPI 应用，当前需要单一入口才能进行对照。'})
        return [], diagnostics

    file, module, app = entries[0]
    source_lines = file['content'].replace('\r\n', '\n').replace('\r', '\n').split('\n')
    routes, visited = [], set()
    for handler in module.body:
        if not isinstance(handler, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in handler.decorator_list:
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if not isinstance(call.func.value, ast.Name) or call.func.value.id != app or call.func.attr not in ROUTE_METHODS:
                continue
            visited.add(call.func)
            declaration = _route_key(call)
            if declaration is None:
                diagnostics.append({'file': file['path'], 'line': call.lineno,
                                    'message': '路由的方法或路径无法从字面声明确定。'})
                continue
            methods, path, keywords = declaration
            end = min(handler.lineno, call.lineno + 7)
            evidence = {'file': file['path'], 'file_sha256': file['sha256'], 'line': call.lineno, 'end_line': end,
                        'snippet': '\n'.join(line[:240] for line in source_lines[call.lineno - 1:end])}
            routes.extend({'method': method, 'path': path, 'handler': handler.name, 'evidence': evidence,
                           'fields': _declared_fields(keywords)} for method in methods)

    for node in ast.walk(module):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == app
                and node.attr in INDIRECT_ROUTES | ROUTE_METHODS and node not in visited):
            diagnostics.append({'file': file['path'], 'line': node.lineno,
                                'message': '检测到其他路由注册方式，需进一步核实实际接口。'})
    return routes, diagnostics


def compare_interfaces(interfaces, routes, diagnostics):
    expected, observed = defaultdict(list), defaultdict(list)
    rows = []
    for item in interfaces:
        if item['protocol'].strip().lower() not in HTTP_PROTOCOLS:
            continue
        contract = item['contract']
        method, path = contract.get('method'), contract.get('path')
        if not isinstance(method, str) or method.strip().upper() not in HTTP_METHODS or not _is_path(path):
            rows.append({'key': f"design:{item['key']}", 'method': None, 'path': None, 'designs': [item],
                         'implementation': [], 'status': 'uncertain',
                         'reason': '已确认设计缺少可对照的 HTTP method 或 path。', 'fields': []})
        else:
            expected[method.strip().upper(), path].append(item)
    has_opaque_design = bool(rows)
    for route in routes:
        observed[route['method'], route['path']].append(route)

    for method, path in sorted(expected.keys() | observed.keys()):
        designs, implementations = expected.get((method, path), []), observed.get((method, path), [])
        fields = []
        if len(designs) > 1 or len(implementations) > 1:
            status, reason = 'uncertain', '同一方法和路径存在多条声明，请人工确认对应关系。'
        elif not designs:
            status, reason = ('uncertain', '当前设计缺少完整的结构化 HTTP 接口清单。') if (
                has_opaque_design or not expected) else ('extra', '快照中存在声明，已确认设计未列出此方法和路径。')
        elif not implementations:
            status, reason = ('uncertain', '静态扫描存在未解析范围，需核实此接口。') if diagnostics else (
                'missing', '已确认设计包含此接口，在当前快照的支持范围内未发现对应声明。')
        else:
            contract = designs[0]['contract']
            actual = implementations[0]['fields']
            fields = [{'name': name, 'expected': value, 'actual': actual.get(name), 'known': name in actual}
                      for name, value in contract.items() if name in CONTRACT_FIELDS - {'method', 'path'}]
            if any(field['known'] and field['actual'] != field['expected'] for field in fields):
                status, reason = 'changed', '方法和路径相同，至少一项可读取的契约声明发生变化。'
            elif any(not field['known'] for field in fields) or contract.keys() - CONTRACT_FIELDS:
                status, reason = 'uncertain', '方法和路径一致，部分设计字段超出静态对照范围。'
            else:
                status, reason = 'matched', '方法、路径和本轮可对照的设计字段一致。'
        rows.append({'key': f'{method} {path}', 'method': method, 'path': path, 'designs': designs,
                     'implementation': implementations, 'status': status, 'reason': reason, 'fields': fields})
    return rows


class InterfaceComparisonService:
    def __init__(self, database_url):
        self.database_url = database_url

    def snapshots(self, workspace_id):
        with connect(self.database_url) as conn:
            WorkspaceService._workspace(conn, workspace_id)
            return conn.execute('''SELECT id,version,sha256,recorded_at FROM engineering_snapshot
                WHERE workspace_id=%s ORDER BY version DESC''', (workspace_id,)).fetchall()

    def read(self, workspace_id, snapshot_id):
        with connect(self.database_url) as conn:
            workspace = WorkspaceService._workspace(conn, workspace_id)
            snapshot = conn.execute('''SELECT id,version,sha256,recorded_at,files FROM engineering_snapshot
                WHERE workspace_id=%s AND id=%s''', (workspace_id, snapshot_id)).fetchone()
            if not snapshot:
                raise KnowledgeError('未找到该工作区的指定文件快照', 404)
            if workspace['language'] != 'python' or not workspace['design_flow_id']:
                raise KnowledgeError('请选择关联已确认设计的 Python 工程工作区', 409)
            design = conn.execute('''SELECT id,version,title,state->'design'->'interfaces' AS interfaces,
                state->>'design_proposal_id' AS proposal_id
                FROM current_design_flows WHERE project_id=%s AND id=%s AND status='COMPLETED' ''',
                                  (workspace['project_id'], workspace['design_flow_id'])).fetchone()
            if not design:
                raise KnowledgeError('未找到该工作区关联的已确认设计', 409)
            tests = conn.execute('''SELECT id,status,recorded_at,result->'tests_run' AS tests_run
                FROM current_engineering_executions WHERE workspace_id=%s AND snapshot_id=%s AND snapshot_sha256=%s
                ORDER BY created_at DESC,id LIMIT 20''', (workspace_id, snapshot_id, snapshot['sha256'])).fetchall()
        routes, diagnostics = extract_fastapi(snapshot.pop('files'))
        rows = compare_interfaces(design.pop('interfaces') or [], routes, diagnostics)
        return {'snapshot': snapshot, 'design': design, 'rows': rows, 'diagnostics': diagnostics, 'executions': tests}
