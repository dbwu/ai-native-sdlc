"""以固定的已确认设计和历史项目模型为依据生成待审基线。"""
from uuid import UUID

from pydantic import Field, ValidationError

from . import model_gateway
from .db import connect
from .design import DesignService
from .project_baselines import BaselineRequest, ProjectBaselines, _version
from .project_model import ProjectModel
from .semantics import KnowledgeError
from .service import KnowledgeService as KS


class ProjectModelGenerationRequest(BaselineRequest):
    design_id: UUID = Field(title='已确认设计流程')
    guidance: str = Field(default='', max_length=4000, title='本轮架构与业务流程建模说明')


def _fixed_ref(ref):
    return str(ref['id']), ref['version']


def _validate_model(model, context):
    previous = context.get('project_baseline', {}).get('model', {})
    old_nodes = {node['key']: (node['entity_id'], node['entity_version']) for node in previous.get('nodes', [])}
    artifacts = {(row['id'], row['version']): row['kind'] for row in context['confirmed_artifacts']}
    confirmed_versions = {record_id: version for record_id, version in artifacts}
    nodes = []
    for node in model.nodes:
        if node.key in old_nodes:
            record_id, old_version = old_nodes[node.key]
            version = confirmed_versions.get(record_id, old_version)
            if str(node.entity_id) != record_id or node.entity_version not in {old_version, version}:
                raise KnowledgeError(f'节点「{node.name}」须沿用前版知识标识及已确认版本')
            node = node.model_copy(update={'entity_version': version})
        elif node.entity_id:
            kind = 'Domain' if node.kind == 'domain' else 'ArchitectureComponent'
            if artifacts.get((str(node.entity_id), node.entity_version)) != kind:
                raise KnowledgeError(f'新节点「{node.name}」引用了未提供的固定设计产物')
        if node.kind == 'module' and node.key not in old_nodes and not node.description.strip():
            raise KnowledgeError(f'新模块「{node.name}」须说明职责及建模依据')
        nodes.append(node)
    old_ports = {port['key']: _fixed_ref(port['knowledge']) for port in previous.get('architecture_ports', [])}
    ports = []
    for port in model.architecture_ports:
        kind = 'Interface' if port.kind == 'interface' else 'DataModel'
        ref = (str(port.knowledge.id), port.knowledge.version)
        if port.key in old_ports:
            record_id, old_version = old_ports[port.key]
            version = confirmed_versions.get(record_id, old_version)
            if ref[0] != record_id or ref[1] not in {old_version, version}:
                raise KnowledgeError(f'端口「{port.name}」须沿用前版知识标识及已确认版本')
            port = port.model_copy(update={'knowledge': port.knowledge.model_copy(update={'version': version})})
        elif ref not in old_ports.values() and artifacts.get(ref) != kind:
            raise KnowledgeError(f'端口「{port.name}」须引用上下文中的固定接口或数据模型')
        ports.append(port)

    required = {_fixed_ref(item) for item in context['requirements']}
    allowed_requirements = required | {_fixed_ref(ref) for process in previous.get('processes', [])
                                      for ref in process['requirement_refs']}
    allowed_rules = {_fixed_ref(item) for item in context['knowledge'] if item['kind'] == 'BusinessRule'}
    allowed_rules.update(_fixed_ref(ref) for process in previous.get('processes', []) for ref in process['rule_refs'])
    used_requirements = {(str(ref.id), ref.version) for process in model.processes for ref in process.requirement_refs}
    used_rules = {(str(ref.id), ref.version) for process in model.processes for ref in process.rule_refs}
    if not required <= used_requirements <= allowed_requirements or not used_rules <= allowed_rules:
        raise KnowledgeError('流程须覆盖本轮全部固定需求，并且仅引用已提供的需求与规则版本')
    return model.model_copy(update={'nodes': nodes, 'architecture_ports': ports})


class ProjectModelGeneration:
    def __init__(self, database_url):
        self.database_url = database_url

    def propose(self, project_id, request):
        with connect(self.database_url) as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            KS._assert_project(conn, project_id)
            flow = DesignService._get(conn, request.design_id)
            if flow['project_id'] != project_id or flow['status'] != 'COMPLETED':
                raise KnowledgeError('请选择当前项目已确认的需求设计', 409)
            if (_version(conn, project_id) != request.expected_version
                    or (flow['definition'].get('project_model_version') or 0) != request.expected_version):
                raise KnowledgeError('该设计依据的项目基线已变化，请在最新基线上完成本轮设计后生成', 409)
            context = DesignService._context(conn, flow)
            state = flow['state']
            artifacts = []
            for ref in state.get('artifact_versions', []):
                if ref['table'] != 'entity':
                    continue
                item = KS._get(conn, 'entity', UUID(ref['id']), ref['version'])
                if item['kind'] in ('Domain', 'ArchitectureComponent', 'Interface', 'DataModel'):
                    artifacts.append({'id': ref['id'], 'version': ref['version'], 'kind': item['kind'], 'name': item['name'],
                                      'design_key': item['attributes'].get('extensions', {}).get('design_key')})
            context.update(confirmed_design=state['design'], confirmed_artifacts=artifacts, guidance=request.guidance)

        exchange = {}
        result = model_gateway.complete_project_model(context, ProjectModel.model_json_schema(), on_exchange=exchange.update)
        try:
            model = ProjectModel.model_validate(result['document'])
            model = _validate_model(model, context)
        except ValidationError as exc:
            reasons = '；'.join(item['msg'] for item in exc.errors(include_input=False)[:3])
            raise KnowledgeError(f'模型基线结构未通过校验：{reasons}', 422) from None
        origin = {key: result['trace'][key] for key in (
            'provider', 'model', 'prompt_version', 'request_id', 'input_sha256', 'output_sha256')}
        origin.update(design_id=str(flow['id']), design_version=flow['version'], design_proposal_id=state['design_proposal_id'])
        with connect(self.database_url) as conn:
            KS._lock(conn, project_id)
            KS._assert_project(conn, project_id)
            if _version(conn, project_id) != request.expected_version:
                raise KnowledgeError('模型生成期间项目基线已更新，请在最新版上重新分析', 409)
            return ProjectBaselines(self.database_url)._propose(
                conn, project_id, request, model, model_origin=origin, model_exchange=exchange)
