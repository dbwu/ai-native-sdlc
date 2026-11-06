"""通用绑定、人工产物与注册适配器；复用原工作流事件、任务和提案。"""
from copy import deepcopy
import json
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .course_executor import build_course, digest, run_course
from .models import EntityCreate, EntityUpdate, RelationCreate
from .operations import KnowledgeOperations
from .operations_models import ProposalCreate
from .runtime import AgentRuntime
from .runtime_models import AgentCreate, ClaimRequest, TaskReport
from .models import Kind
from .semantics import KnowledgeError
from .service import KnowledgeService as KS
from .workflow_models import STAGES, WorkflowCreate
from .ingestion import parse_source
from importlib.resources import files

SLOTS = {'requirement':'Requirement', 'architecture':'ArchitectureComponent', 'interface':'Interface',
         'data':'DataModel', 'code':'CodeArtifact', 'test':'TestCase'}


def stage_for(workflow, state):
    stage = deepcopy(STAGES[state['stage']])
    if workflow['definition']['scenario'] == 'generic_v1':
        bases = stage['outputs']
        stage['outputs'] = [key for key in state['bindings'] if key.split(':')[0] in bases]
    return stage


def create_generic(service, request):
    with service._transaction() as conn:
        KS._lock(conn, request.project_id)
        KS._assert_project(conn, request.project_id)
        if set(request.bindings) != set(SLOTS) or any(not values for values in request.bindings.values()):
            raise KnowledgeError('通用流程需绑定需求、组件、接口、模型、代码和测试；每类至少一项')
        bindings, baselines, used = {}, [], set()
        for slot, refs in request.bindings.items():
            for index, ref in enumerate(refs):
                record = KS._get(conn, 'entity', ref.id)
                if record['project_id'] != request.project_id or record['kind'] != SLOTS[slot] or record['status'] == 'archived':
                    raise KnowledgeError('绑定知识必须属于本项目、匹配类型且未归档')
                if record['version'] != ref.version:
                    raise KnowledgeError('绑定知识已有新版本，请刷新后选择', 409)
                if ref.id in used:
                    raise KnowledgeError('流程不能重复绑定同一实体')
                used.add(ref.id)
                bindings[slot if index == 0 else f'{slot}:{index}'] = str(ref.id)
                baselines.append({'table':'entity','id':str(ref.id),'version':ref.version})
        for ref in request.input_knowledge:
            record = KS._get(conn, 'entity', ref.id)
            if record['project_id'] != request.project_id or record['version'] != ref.version or record['status'] == 'archived':
                raise KnowledgeError('额外知识需为本项目当前有效版本',409)
            if ref.id in used:
                raise KnowledgeError('额外知识与产物绑定不能重复')
            used.add(ref.id)
            bindings['input:'+str(ref.id)] = str(ref.id)
            baselines.append({'table':'entity','id':str(ref.id),'version':ref.version})
        if len(used) > 45:
            raise KnowledgeError('本版通用流程最多绑定45项知识')
        if request.executor == 'course_selection_v1':
            validate_course_bindings(conn, request)
        # 将当前关系连通分量装入流程。门禁会显式对齐所有受影响的边，避免外围旧版本污染下一任务上下文。
        while True:
            ids=list(bindings.values())
            related=conn.execute('SELECT source_id,target_id FROM current_relationships WHERE project_id=%s AND status IN (\'proposed\',\'verified\') AND (source_id=ANY(%s::uuid[]) OR target_id=ANY(%s::uuid[]))', (request.project_id,ids,ids)).fetchall()
            added=False
            for row in related:
                for record_id in (row['source_id'],row['target_id']):
                    if record_id in used: continue
                    record=KS._get(conn,'entity',record_id)
                    if record['status']=='archived': raise KnowledgeError('关联图包含归档知识，请先处理关系',409)
                    used.add(record_id);bindings['input:'+str(record_id)]=str(record_id)
                    baselines.append({'table':'entity','id':str(record_id),'version':record['version']})
                    added=True
            if len(used)>45: raise KnowledgeError('关联知识超过本版45项上限，请拆分项目或流程输入')
            if not added: break
        flow_id = uuid4()
        conn.execute('INSERT INTO workflow_run(id,project_id,title,definition) VALUES (%s,%s,%s,%s)',
            (flow_id, request.project_id, request.title, Jsonb(request.model_dump(mode='json'))))
        provenance = {'actor':request.actor,'source_uri':'urn:workflow:'+str(flow_id),'source_revision':'bindings-v1',
                      'change_reason':'绑定已存在知识，仅创建执行证据占位及缺失追溯关系','nature':'inference'}
        operations, relation_ids = [], []
        for slot in ('code-evidence','test-evidence'):
            record_id = uuid4()
            bindings[slot] = str(record_id)
            body = EntityCreate(project_id=request.project_id, kind='Evidence', name='待提交'+slot,
                description='尚未形成执行证据', provenance=provenance,
                attributes={'category':'document','uri':f'urn:workflow:{flow_id}:{slot}','revision':'pending'})
            operations.append({'op':'create_entity','id':str(record_id),'data':body.model_dump(mode='json')})
        ids = list(bindings.values())
        for row in conn.execute('SELECT * FROM current_relationships WHERE project_id=%s AND source_id=ANY(%s::uuid[]) AND target_id=ANY(%s::uuid[]) AND status<>\'retired\'', (request.project_id,ids,ids)):
            relation_ids.append(str(row['id']))
            baselines.append({'table':'relationship','id':str(row['id']),'version':row['version']})
            source=KS._get(conn,'entity',row['source_id']);target=KS._get(conn,'entity',row['target_id'])
            if source['version']!=row['source_version'] or target['version']!=row['target_version']:
                full=KS._get(conn,'relationship',row['id'])
                operations.append({'op':'update_relationship','id':str(row['id']),'data':{
                    'expected_version':row['version'],'source_version':source['version'],'target_version':target['version'],
                    'status':'proposed','evidence':[],'rationale':full['rationale'],'provenance':provenance}})
        for req in request.bindings['requirement']:
            for kind, slot in [('IMPLEMENTED_BY','code'),('VERIFIED_BY','test')]:
                for target in request.bindings[slot]:
                    existing = conn.execute('SELECT id FROM current_relationships WHERE source_id=%s AND target_id=%s AND type=%s', (req.id,target.id,kind)).fetchone()
                    if existing:
                        if str(existing['id']) not in relation_ids:
                            raise KnowledgeError('已有追溯关系已退役，需先人工处理其生命周期',409)
                        continue
                    record_id = uuid4()
                    relation_ids.append(str(record_id))
                    body = RelationCreate(type=kind,source_id=req.id,target_id=target.id,source_version=req.version,
                        target_version=target.version,provenance=provenance,rationale='流程绑定的需求产物追溯，待实际验证')
                    operations.append({'op':'create_relationship','id':str(record_id),'data':body.model_dump(mode='json')})
        proposal = KnowledgeOperations(service.database_url,connection=conn)._create(conn,request.project_id,
            '确认通用流程绑定：'+request.title,request.actor,{'operations':operations,'baselines':baselines,'entity_mapping':[],'removed_keys':[]})
        state = {'stage':0,'stage_attempt':0,'task_id':None,'gate_proposal_id':None,'bootstrap_proposal_id':str(proposal['id']),
                 'bindings':bindings,'relation_ids':relation_ids,'completed_stages':[],'knowledge_versions':[], 'last_execution_id':None}
        service._event(conn,{'id':flow_id},'DRAFT','created',request.actor,'已有知识已固定版本，待审阅绑定提案',state)
        return service._get(conn,flow_id)


def validate_course_bindings(conn, request):
    if any(len(request.bindings[slot]) != 1 for slot in ('architecture','interface','data','code','test')):
        raise KnowledgeError('选课适配器每类技术产物需恰好绑定一项；人工流程可绑定多项')
    expected_keys = {'REQ-ENROLL'} | ({'REQ-CAPACITY'} if request.release != 'v1' else set()) | ({'REQ-TIME-CONFLICT'} if request.release == 'v1.2' else set())
    rows = conn.execute('''SELECT b.external_key,b.entity_id FROM source_entity_binding b JOIN knowledge_source s ON s.id=b.source_id
        WHERE s.project_id=%s AND s.uri=%s''', (request.project_id,'urn:tutorial:course-selection:spec')).fetchall()
    mapping = {r['external_key']:str(r['entity_id']) for r in rows}
    if {str(ref.id) for ref in request.bindings['requirement']} != {mapping.get(key) for key in expected_keys}:
        raise KnowledgeError('选课教学适配器需绑定所选版本的累计需求，请先导入对应版本并选择全部相关需求')
    for slot,key in [('architecture','COMP-ENROLL'),('interface','IF-ENROLL'),('data','DM-ENROLL'),('code','CODE-ENROLL'),('test','TEST-ENROLL')]:
        if str(request.bindings[slot][0].id) != mapping.get(key):
            raise KnowledgeError('选课适配器需绑定教程中的同一组技术产物，不能套用到任意业务')
    rule_keys={'BR-UNIQUE'} | ({'BR-CAPACITY'} if request.release!='v1' else set()) | ({'BR-TIME-CONFLICT'} if request.release=='v1.2' else set())
    if not {mapping.get(key) for key in rule_keys} <= {str(ref.id) for ref in request.input_knowledge}:
        raise KnowledgeError('请同时选择本版本对应的业务规则作为固定输入')
    canonical=parse_source('json',files('knowledge').joinpath('templates',request.release+'-knowledge.json').read_text(encoding='utf-8'))
    for entity in canonical['entities']:
        if entity['key'] in expected_keys | rule_keys:
            actual=KS._get(conn,'entity',UUID(mapping[entity['key']]))
            if any(actual[field]!=entity[field] for field in ('name','description','attributes')):
                raise KnowledgeError('业务需求已超出所选教学适配器范围，请使用人工流程或扩展执行器')


def report_generic(service, conn, workflow, actor, reason, supplied=None):
    state = workflow['state']
    stage = stage_for(workflow,state)
    runtime = AgentRuntime(service.database_url,connection=conn)
    name = '通用流程提交-'+stage['role']
    existing = conn.execute('SELECT * FROM current_runtime_agents WHERE project_id=%s AND name=%s',(workflow['project_id'],name)).fetchone()
    agent = existing or runtime.register_agent(AgentCreate(project_id=workflow['project_id'],name=name,role=stage['role'],
        capabilities=[stage['capability']],read_kinds=list(Kind),write_kinds=list(Kind)[1:],actor=actor))
    if agent['status'] != 'active':
        raise KnowledgeError('产物提交 Agent 已禁用',409)
    lease = runtime.claim(ClaimRequest(agent_id=agent['id'],task_id=state['task_id'],lease_seconds=120))
    task = lease['task']
    if task is None:
        diagnostic=runtime._task(conn,UUID(state['task_id']))['runtime'].get('review',{})
        raise KnowledgeError(f"任务无法领取（{workflow['definition']['release']}／{state['stage']}）："+json.dumps(diagnostic,ensure_ascii=False),409)
    runtime.get_context(task['id'])
    records = service._records(conn,state)
    execution = None
    if supplied is None and workflow['definition']['executor'] != 'course_selection_v1':
        raise KnowledgeError('当前为人工流程，请提交本阶段实际产物；未配置自动执行器')
    build = build_course(workflow['definition']['release']) if workflow['definition']['executor']=='course_selection_v1' else None
    if state['stage'] == 3 and build:
        execution = execute_course(service,conn,workflow,task,records)
        if not execution['document']['passed']:
            runtime.report(task['id'],TaskReport(agent_id=agent['id'],lease_token=lease['lease_token'],expected_version=task['version'],
                outcome='failed',summary='实际测试失败，执行日志已保存',acceptance=[{'criterion_index':0,'passed':False}],artifacts={'execution_id':str(execution['id'])}))
            return False
    changes=[]
    expected = {str(records[key]['id']) for key in stage['outputs']}
    if supplied is not None:
        if {str(change.id) for change in supplied} != expected or len(supplied)!=len(expected) or any(change.op!='update_entity' for change in supplied):
            raise KnowledgeError('提交内容必须恰好包含本阶段全部绑定产物的完整实体修订')
        changes=[change.model_dump(mode='json') for change in supplied]
    else:
        for key in stage['outputs']:
            record = records[key]
            data={field:deepcopy(record[field]) for field in ('name','description','status','attributes','evidence','provenance')}
            data['expected_version']=record['version']
            if key=='code':
                data.update(name='选课实现 '+workflow['definition']['release'],description='已生成的单学期选课教学实现，等待审阅。')
                data['attributes'].update(path='course_app.py',language='python',revision=build['sha256']['course_app.py'],
                    extensions={'source':build['files']['course_app.py'],'sha256':build['sha256']['course_app.py'],'release':workflow['definition']['release']})
            elif key=='code-evidence':
                data.update(name='选课代码内容证据',description='固定到本阶段代码版本及内容摘要。',attributes={'category':'code',
                    'uri':'urn:course:code:'+build['sha256']['course_app.py'],'revision':build['sha256']['course_app.py'],
                    'subject_id':str(records['code']['id']),'subject_version':records['code']['version']+1,'sha256':build['sha256']['course_app.py']})
            elif key=='test':
                data.update(name='选课回归测试 '+workflow['definition']['release'],description='独立测试已实际运行，覆盖本版本与基础行为，等待确认。')
                data['attributes'].update(expected_result=f"{build['tests_expected']}项真实测试通过",extensions={'test_source':build['files']['test_course_app.py']})
                data['attributes']['steps']=['实际执行目录、选课、幂等、学生隔离及无效教学班测试'] + (['执行容量为零、满额及最后名额并发测试'] if workflow['definition']['release']!='v1' else []) + (['执行闭区间冲突、相邻时段、多时段和同学生并发测试'] if workflow['definition']['release']=='v1.2' else [])
            elif key=='test-evidence':
                data.update(name='选课真实测试证据',description='本阶段实际执行记录及对应代码测试摘要。',attributes={'category':'test_execution',
                    'uri':'urn:workflow:execution:'+str(execution['id']),'revision':str(execution['id']),'outcome':'passed',
                    'subject_id':str(records['test']['id']),'subject_version':records['test']['version']+1,
                    'extensions':{'execution_id':str(execution['id']),'code_sha256':execution['code_sha256'],'test_sha256':execution['test_sha256']}})
            changes.append({'op':'update_entity','id':str(record['id']),'data':data})
    artifacts={'stage':stage['key'],'scenario':'generic_v1'}
    if execution: artifacts['execution_id']=str(execution['id'])
    runtime.report(task['id'],TaskReport(agent_id=agent['id'],lease_token=lease['lease_token'],expected_version=task['version'],
        outcome='succeeded',summary=reason,acceptance=[{'criterion_index':0,'passed':True}],artifacts=artifacts,changes=changes))
    return True


def execute_course(service,conn,workflow,task,records):
    code=records['code']
    result=run_course(workflow['definition']['release'],code['attributes']['extensions'].get('source',''))
    execution_id=uuid4()
    conn.execute('''INSERT INTO workflow_execution(id,workflow_id,project_id,task_id,code_id,code_version,code_sha256,test_sha256,document)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(execution_id,workflow['id'],workflow['project_id'],task['id'],code['id'],code['version'],
        result['sha256']['course_app.py'],result['sha256']['test_course_app.py'],Jsonb(result)))
    return conn.execute('SELECT * FROM workflow_execution WHERE id=%s',(execution_id,)).fetchone()


def quality_generic(service,conn,workflow,state,records):
    if workflow['definition']['executor']=='course_selection_v1':
        # 人工阶段产物也可能修改需求；每次门禁重新确认固定执行器仍覆盖当前需求与规则。
        validate_course_bindings(conn,WorkflowCreate.model_validate(workflow['definition']))
    for key,record in records.items():
        attrs=record['attributes']
        if state['stage']==0 and key.split(':')[0]=='requirement' and not attrs.get('acceptance_criteria'):
            raise KnowledgeError('每条需求必须有验收条件')
        if state['stage']==1 and ((key.startswith('architecture') and not attrs.get('responsibility')) or
            (key.startswith('interface') and not attrs.get('protocol')) or (key.startswith('data') and not attrs.get('schema_definition'))):
            raise KnowledgeError('设计产物缺少职责、协议或数据结构')
    if state['stage'] in (2,3):
        if workflow['definition']['executor']!='course_selection_v1':
            raise KnowledgeError('人工通用流程尚未配置可信代码执行适配器；不能放行实现或验证门禁')
        build=build_course(workflow['definition']['release'])
        code=records['code']; attrs=code['attributes']; sha=build['sha256']['course_app.py']
        if attrs['extensions'].get('source')!=build['files']['course_app.py'] or attrs.get('revision')!=sha:
            raise KnowledgeError('代码内容或摘要与注册执行器不一致')
        if state['stage']==2:
            ea=records['code-evidence']['attributes']
            if ea.get('sha256')!=sha or ea.get('subject_id')!=str(code['id']) or ea.get('subject_version')!=code['version']:
                raise KnowledgeError('代码证据未固定到当前代码版本')
        else:
            task=AgentRuntime._task(conn,UUID(state['task_id']))
            execution_id=task['runtime']['result']['artifacts'].get('execution_id')
            execution=conn.execute('SELECT * FROM workflow_execution WHERE id=%s AND workflow_id=%s AND task_id=%s',
                (execution_id,workflow['id'],task['id'])).fetchone()
            test=records['test']; ea=records['test-evidence']['attributes']
            if (not execution or not execution['document']['passed'] or not execution['document']['executed']
                or execution['code_id']!=code['id'] or execution['code_version']!=code['version']
                or execution['code_sha256']!=digest(attrs['extensions']['source'])
                or execution['test_sha256']!=digest(test['attributes']['extensions'].get('test_source',''))
                or ea.get('subject_id')!=str(test['id']) or ea.get('subject_version')!=test['version']
                or ea.get('outcome')!='passed' or ea.get('extensions',{}).get('execution_id')!=str(execution['id'])
                or ea.get('extensions',{}).get('code_sha256')!=execution['code_sha256']
                or ea.get('extensions',{}).get('test_sha256')!=execution['test_sha256']):
                raise KnowledgeError('测试、代码版本或证据与真实执行记录不一致')
            state['gate_execution_id']=str(execution['id'])
