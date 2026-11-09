"""将需求和已确认设计的不可变版本整理为中文交付文档。"""
from datetime import timezone
from html import escape
import json
import re
from urllib.parse import urlencode
from uuid import UUID

from psycopg.types.json import Jsonb

from .db import connect
from .design import DesignService
from .design_diagrams import design_diagrams
from .localization import LABELS
from .semantics import KnowledgeError
from .service import KnowledgeService as KS


DOCUMENT_TITLES = {'requirements_analysis': '需求分析文档', 'design': '设计文档'}
ARTIFACT_SECTIONS = {
    'ArchitectureComponent': '组件职责', 'Interface': '接口契约',
    'DataModel': '数据结构', 'TestCase': '测试计划（设计阶段）',
}


def _text(value):
    """保留文字，避免知识内容被解释为文档自带的 HTML 或 Markdown 结构。"""
    value = ' '.join(str(value).splitlines())
    return escape(re.sub(r'([\\`*_\[\]#|])', r'\\\1', value), quote=False)


def _bullets(lines, heading, values, empty):
    lines.extend(['', f'## {heading}', ''])
    if values:
        lines.extend(f'- {_text(value)}' for value in values)
    else:
        lines.append(empty)


def _json_block(lines, value):
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    fence = '`' * max(3, 1 + max((len(run) for run in re.findall(r'`+', raw)), default=0))
    lines.extend(['', fence + 'json', raw, fence, ''])


def _metadata(kind, requirement, flow):
    title = DOCUMENT_TITLES[kind]
    lines = [f'# {_text(requirement["name"])} · {title}', '',
             f'- 需求版本：v{requirement["version"]}', f'- 需求标识：`{requirement["id"]}`',
             f'- 项目标识：`{requirement["project_id"]}`',
             f'- 知识生命周期：{LABELS["entity_statuses"].get(requirement["status"], requirement["status"])}']
    if flow:
        lines.extend([f'- 已确认设计：{_text(flow["title"])} · 流程 v{flow["version"]}',
                      f'- 设计流程标识：`{flow["id"]}`',
                      f'- 确认时间：{flow["recorded_at"].astimezone(timezone.utc).isoformat()}'])
        if len(flow['definition']['requirements']) > 1:
            lines.append(f'- 本设计轮次覆盖 {len(flow["definition"]["requirements"])} 条需求，以下产物按本需求筛选。')
    return lines


def _requirements_analysis(requirement, flow, inputs, diagrams):
    lines = _metadata('requirements_analysis', requirement, flow)
    lines.extend(['', '## 需求说明', '', _text(requirement.get('description') or '未填写需求说明。')])
    _bullets(lines, '验收条件', requirement['attributes']['acceptance_criteria'], '该版本尚未记录验收条件。')
    provenance = requirement['provenance']
    _bullets(lines, '需求来源', [f'{provenance["source_uri"]} · {provenance["source_revision"]}',
                              f'变更依据：{provenance["change_reason"]}'], '')
    if flow:
        lines.extend(['', '## 本轮设计目标', '', _text(flow['definition']['goal'])])
        answers = [f'{item["question"]} 答复：{item["answer"]}' for item in flow['state']['answers']
                   if str(requirement['id']) in item['requirement_ids']]
        _bullets(lines, '本需求已保存的澄清答复', answers, '本轮没有关联本需求的人工澄清答复。')
        _bullets(lines, '本轮共同设计依据', [f'{record["name"]} · v{record["version"]}' for record in inputs],
                 '本轮未绑定额外设计依据。')
        _bullets(lines, '本轮后续事项', flow['state']['design'].get('deferred_items', []), '本轮没有记录后续事项。')
    use_cases = [diagram for diagram in diagrams if diagram['kind'] == 'use_case']
    lines.extend(['', '## 用例图', ''])
    if use_cases:
        _diagram_blocks(lines, use_cases)
    else:
        lines.append('此需求版本没有已确认的用例图。')
    return lines


def _diagram_blocks(lines, diagrams):
    for diagram in diagrams:
        sequence = diagram['kind'] == 'sequence'
        use_case = diagram['kind'] == 'use_case'
        title = f'业务时序图：{_text(diagram["title"])}' if sequence else _text(diagram['title'])
        lines.extend(['', f'### {title}', '', '```mermaid', diagram['mermaid'], '```', ''])
        names = {node['id']: node['label'] for node in diagram['nodes']}
        if use_case:
            lines.extend(['角色：' + '、'.join(_text(node['label']) for node in diagram['nodes'] if node['kind'] == 'Actor'),
                          '', '用例：' + '、'.join(_text(node['label']) for node in diagram['nodes'] if node['kind'] == 'UseCase'), ''])
        else:
            lines.extend([('参与者：' if sequence else '节点：') + '、'.join(_text(name) for name in names.values()), ''])
        for index, edge in enumerate(diagram['edges'], 1):
            marker = f'{index}.' if sequence else '-'
            connector = '—' if use_case else '→'
            lines.append(f'{marker} {_text(names[edge["source"]])} {connector} {_text(names[edge["target"]])}：{_text(edge["label"])}')


def _design_document(requirement, flow, artifacts, inputs, diagrams):
    lines = _metadata('design', requirement, flow)
    design = flow['state']['design']
    lines.extend(['', '## 本轮设计摘要', '', _text(design['summary'])])
    _bullets(lines, '本需求验收条件', requirement['attributes']['acceptance_criteria'], '该版本尚未记录验收条件。')
    lines.extend(['', '## 设计图示', ''])
    if not any(diagram['kind'] == 'sequence' for diagram in diagrams):
        lines.append('此设计没有记录本需求的业务时序图。')
    _diagram_blocks(lines, [diagram for diagram in diagrams if diagram['kind'] != 'use_case'])
    _bullets(lines, '本轮设计假设', design.get('assumptions', []), '本轮没有记录设计假设。')
    _bullets(lines, '本轮后续事项', design.get('deferred_items', []), '本轮没有记录后续事项。')
    lines.extend(['', '## 本轮共同设计依据'])
    if not inputs:
        lines.extend(['', '本轮未绑定额外设计依据。'])
    for record in inputs:
        label = LABELS['entity_kinds'][record['kind']]
        lines.extend(['', f'### {label}：{_text(record["name"])} · v{record["version"]}', '', _text(record['description'])])
        attributes = record['attributes']
        if record['kind'] in {'BusinessRule', 'Constraint'} and attributes.get('expression'):
            _json_block(lines, attributes['expression'])
        if record['kind'] == 'Decision' and attributes.get('rationale'):
            lines.extend(['', f'决策依据：{_text(attributes["rationale"])}'])
    component_names = {item['key']: item['name'] for item in design['components']}
    parents = {(kind, item['key']): component_names[item['component_key']]
               for kind, group in (('Interface', 'interfaces'), ('DataModel', 'data_models')) for item in design[group]}
    for kind, heading in ARTIFACT_SECTIONS.items():
        records = [artifact for artifact in artifacts if artifact['kind'] == kind]
        if not records:
            continue
        lines.extend(['', f'## {heading}'])
        for record in records:
            lines.extend(['', f'### {_text(record["name"])} · v{record["version"]}', '',
                          f'知识标识：`{record["id"]}`', '', _text(record['description'])])
            attributes = record['attributes']
            parent = parents.get((kind, attributes.get('extensions', {}).get('design_key')))
            if kind in {'Interface', 'DataModel'} and parent:
                lines.extend(['', f'所属组件：{_text(parent)}'])
            if kind == 'ArchitectureComponent' and attributes.get('responsibility'):
                lines.extend(['', _text(attributes['responsibility'])])
            elif kind == 'Interface':
                lines.extend(['', f'协议：{_text(attributes.get("protocol") or "未记录")}'])
                _json_block(lines, attributes.get('extensions', {}).get('contract', {}))
            elif kind == 'DataModel':
                _json_block(lines, attributes['schema_definition'])
            elif kind == 'TestCase':
                lines.extend(['', *[f'{i}. {_text(step)}' for i, step in enumerate(attributes['steps'], 1)],
                              '', f'预期结果：{_text(attributes.get("expected_result") or "未记录")}'])
    return lines


def _document(project_id, kind, requirement, flow, lines):
    stem = re.sub(r'[^\w-]+', '-', requirement['name']).strip('-_')[:70] or '需求'
    design_suffix = f'-{str(flow["id"])[:8]}' if flow else ''
    query = {'version': requirement['version']}
    if flow:
        query['design_id'] = flow['id']
    path = f'/workbench/projects/{project_id}/requirements/{requirement["id"]}/deliveries/{kind}'
    return {'title': DOCUMENT_TITLES[kind], 'filename': f'{stem}-v{requirement["version"]}-{DOCUMENT_TITLES[kind]}{design_suffix}.md',
            'markdown': '\n'.join(lines).rstrip() + '\n', 'download_url': f'{path}?{urlencode(query)}'}


def _design_info(flow):
    return {key: flow[key] for key in ('id', 'title', 'version')} | {'confirmed_at': flow['recorded_at']}


def _related_artifacts(conn, flow, requirement_id):
    artifacts = []
    for ref in flow['state']['artifact_versions']:
        if ref['table'] != 'entity':
            continue
        record = KS._get(conn, 'entity', UUID(ref['id']), ref['version'])
        if (record['kind'] in ARTIFACT_SECTIONS
                and str(requirement_id) in record['attributes'].get('extensions', {}).get('requirement_ids', [])):
            artifacts.append(record)
    return artifacts


class RequirementDeliveries:
    def __init__(self, database_url):
        self.database_url = database_url

    def read(self, project_id, requirement_id, version, design_id=None, *, select_default=True):
        with connect(self.database_url) as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            requirement = KS._get(conn, 'entity', requirement_id, version)
            if requirement['project_id'] != project_id or requirement['kind'] != 'Requirement':
                raise KnowledgeError('未找到当前项目的指定需求版本', 404)
            reference = {'id': str(requirement_id), 'version': version}
            designs = conn.execute("""SELECT id,title,version,recorded_at FROM current_design_flows
                WHERE project_id=%s AND status='COMPLETED' AND definition->'requirements' @> %s
                ORDER BY recorded_at DESC,id LIMIT 100""", (project_id, Jsonb([reference]))).fetchall()
            flow = None
            if design_id or (designs and select_default):
                flow = DesignService._get(conn, design_id or designs[0]['id'])
                if flow['project_id'] != project_id:
                    raise KnowledgeError('未找到当前项目的设计流程', 404)
                if flow['status'] != 'COMPLETED' or reference not in flow['definition']['requirements']:
                    raise KnowledgeError('该设计未确认完成，或没有绑定所选需求版本', 409)
            inputs, artifacts = [], []
            if flow:
                inputs = [KS._get(conn, 'entity', UUID(ref['id']), ref['version']) for ref in flow['definition']['input_knowledge']]
                artifacts = _related_artifacts(conn, flow, requirement_id)
        choices = [_design_info(row) for row in designs]
        selected = None
        design_document = None
        diagrams = []
        if flow:
            selected = _design_info(flow)
            if not any(choice['id'] == selected['id'] for choice in choices):
                choices.append(selected.copy())
            diagrams = design_diagrams(flow['state']['design'], requirement_id)
            selected.update(shared_requirement_count=len(flow['definition']['requirements']), diagrams=diagrams, artifacts=[
                {field: artifact[field] for field in ('id', 'kind', 'name', 'version', 'status', 'description')} for artifact in artifacts])
            design_document = _document(project_id, 'design', requirement, flow, _design_document(requirement, flow, artifacts, inputs, diagrams))
        analysis = _document(project_id, 'requirements_analysis', requirement, flow, _requirements_analysis(requirement, flow, inputs, diagrams))
        return {'requirement': requirement, 'designs': choices, 'design': selected,
                'documents': {'requirements_analysis': analysis, 'design': design_document}}
