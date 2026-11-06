"""第四阶段补强：项目知识、通用产物提交及版本固定的选课预览。"""
from copy import deepcopy
from io import BytesIO
from importlib.resources import files
import json
import os
from pathlib import Path
import tempfile
from typing import Literal
from uuid import UUID, uuid4
import zipfile

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import Field

from .course_application import CourseApplication, EnrollmentError
from .course_executor import build_course
from .db import connect
from .generic_workflow import report_generic, stage_for
from .models import RelationCreate, StrictModel
from .operations import KnowledgeOperations
from .operations_models import IngestionRequest, Change
from .runtime_models import RuntimeInput
from .semantics import KnowledgeError
from .service import KnowledgeService as KS
from .workbench import WorkbenchAction, WorkbenchService
from .workflow import WorkflowService


class CourseImport(RuntimeInput):
    release: Literal['v1','v1.1','v1.2']
    actor: str = Field(min_length=1,max_length=100)


class ArtifactSubmit(WorkbenchAction):
    changes: list[Change] = Field(min_length=1,max_length=100)


class Enroll(StrictModel):
    student: Literal['S001','S002','S003']
    offering: str = Field(min_length=1,max_length=100)


COURSE_HTML = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>选课教学应用</title>
<style>body{font:16px system-ui;margin:30px auto;max-width:900px;padding:20px;background:#f3f6f8;color:#193247}section{background:white;border-radius:12px;padding:20px;margin:20px 0}button,select{padding:10px;margin:5px}td,th{text-align:left;padding:12px;border-bottom:1px solid #ddd}table{width:100%}#message{color:#2167a3}</style>
<h1>校园选课教学应用</h1><p>预置学生、单学期、固定周课表。本演示数据独立保存，不是学校生产数据。</p>
<label>当前学生<select id="student"><option>S001</option><option>S002</option><option>S003</option></select></label><p id="message" role="status"></p>
<section><h2>教学班目录</h2><table><thead><tr><th>课程</th><th>人数/容量</th><th>时间</th><th>操作</th></tr></thead><tbody id="courses"></tbody></table></section>
<section><h2>我的已选教学班</h2><ul id="chosen"></ul></section>
<script>
const api=location.pathname.replace(/\\/$/,'')+'/api';const student=document.querySelector('#student');
async function read(path,body){let r=await fetch(api+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});let data=await r.json();if(!r.ok)throw Error(data.detail||'请求失败');return data}
async function refresh(){try{let data=await read('?student='+student.value);document.querySelector('#courses').replaceChildren();for(let c of data.offerings){let row=document.createElement('tr');for(let text of [c.name,c.enrolled_count+'/'+c.capacity,c.slots.map(x=>'周'+x.weekday+' 第'+x.start_period+'～'+x.end_period+'节').join('；')]){let td=document.createElement('td');td.textContent=text;row.append(td)}let td=document.createElement('td'),button=document.createElement('button');button.textContent='选择 '+c.id;button.onclick=async()=>{button.disabled=true;try{let result=await read('',{student:student.value,offering:c.id});document.querySelector('#message').textContent=result.created?'选课成功':'已选过，返回原记录';await refresh()}catch(e){document.querySelector('#message').textContent=e.message}finally{button.disabled=false}};td.append(button);row.append(td);document.querySelector('#courses').append(row)}document.querySelector('#chosen').replaceChildren();for(let item of data.enrollments){let li=document.createElement('li');li.textContent=item.name+' · '+item.offering;document.querySelector('#chosen').append(li)}}catch(e){document.querySelector('#message').textContent=e.message}}
student.onchange=refresh;refresh();</script></html>'''


def platform_router(url_provider):
    router=APIRouter(prefix='/workbench',tags=['项目知识与通用流程'])

    @router.get('/projects/{project_id}/knowledge', summary='浏览项目全部知识与来源标识')
    def knowledge(project_id: UUID, limit:int=Query(200,ge=1,le=200), offset:int=Query(0,ge=0)):
        service=KS(url_provider())
        project=service.get_entity(project_id)
        if project['kind']!='Project': raise KnowledgeError('未找到项目',404)
        with connect(url_provider()) as conn:
            keys={str(r['entity_id']):r['external_key'] for r in conn.execute('SELECT entity_id,external_key FROM source_entity_binding WHERE project_id=%s',(project_id,))}
            total=conn.execute('SELECT count(*) AS n FROM current_entities WHERE project_id=%s',(project_id,)).fetchone()['n']
        return {'entities':service.list_entities(project_id,limit=limit,offset=offset),'keys':keys,'total':total,
                'relationships':service.list_relationships(project_id,limit=200),
                'proposals':KnowledgeOperations(url_provider()).list_proposals(project_id,limit=200)}

    @router.post('/projects/{project_id}/course-import',status_code=201,summary='提出选课教学知识版本，不自动提交')
    def import_course(project_id:UUID, body:CourseImport):
        content=files('knowledge').joinpath('templates',body.release+'-knowledge.json').read_text(encoding='utf-8')
        return KnowledgeOperations(url_provider()).ingest(IngestionRequest(project_id=project_id,source_uri='urn:tutorial:course-selection:spec',
            source_revision=body.release,format='json',content=content,actor=body.actor,title='选课知识 '+body.release))

    @router.post('/projects/{project_id}/relationships',status_code=201,summary='提出新关系，等待审核提交')
    def relationship(project_id:UUID, body:RelationCreate):
        with connect(url_provider()) as conn:
            KS._lock(conn,project_id); KS._assert_project(conn,project_id)
            baselines=[]
            for record_id,version in [(body.source_id,body.source_version),(body.target_id,body.target_version)]:
                record=KS._get(conn,'entity',record_id)
                if record['project_id']!=project_id or record['version']!=version: raise KnowledgeError('关系端点项目或版本不匹配',409)
                baselines.append({'table':'entity','id':str(record_id),'version':version})
            data=body.model_dump(mode='json');data['status']='proposed';data['evidence']=[];data['provenance']['nature']='inference'
            return KnowledgeOperations(url_provider(),connection=conn)._create(conn,project_id,'新增项目知识关系',body.provenance.actor,
                {'operations':[{'op':'create_relationship','id':str(uuid4()),'data':data}],'baselines':baselines,'entity_mapping':[],'removed_keys':[]})

    @router.get('/workflows/{workflow_id}/artifact-draft',summary='读取本阶段待提交产物表单')
    def draft(workflow_id:UUID):
        with connect(url_provider()) as conn:
            flow=WorkflowService._get(conn,workflow_id)
            if flow['definition']['scenario']!='generic_v1': raise KnowledgeError('仅通用流程支持人工产物表单')
            records=WorkflowService._records(conn,flow['state'])
            changes=[]
            for key in stage_for(flow,flow['state'])['outputs']:
                record=records[key]
                changes.append({'op':'update_entity','id':str(record['id']),'data':{
                    **{field:deepcopy(record[field]) for field in ('name','description','status','attributes','evidence','provenance')},'expected_version':record['version']}})
            return {'workflow_version':flow['version'],'changes':changes}

    @router.post('/workflows/{workflow_id}/artifacts',summary='提交人工产物并停在待审状态')
    def submit(workflow_id:UUID,body:ArtifactSubmit):
        with connect(url_provider()) as conn:
            flow=WorkflowService._get(conn,workflow_id);KS._lock(conn,flow['project_id']);KS._assert_project(conn,flow['project_id'])
            flow=WorkflowService._get(conn,workflow_id)
            snapshot=WorkbenchService(url_provider())._snapshot(conn,workflow_id)
            if flow['definition']['scenario']!='generic_v1' or flow['status']!='RUNNING' or not snapshot['task'] or snapshot['task']['status']!='READY':
                raise KnowledgeError('当前阶段不能提交产物',409)
            if flow['version']!=body.expected_version or snapshot['task']['version']!=body.task_version:
                raise KnowledgeError('流程或任务版本已变化，请重新读取产物表单',409)
            report_generic(WorkflowService(url_provider(),connection=conn),conn,flow,body.actor,body.reason,body.changes)
            return WorkbenchService(url_provider())._snapshot(conn,workflow_id)

    def preview(workflow_id):
        snapshot=WorkbenchService(url_provider()).snapshot(workflow_id)
        flow=snapshot['workflow']
        if flow['status']!='COMPLETED' or flow['definition'].get('executor')!='course_selection_v1':
            raise KnowledgeError('选课预览在该版本流程完成真实验证后开放',409)
        code=next(item['entity'] for item in snapshot['artifacts'] if item['key']=='code')
        build=build_course(flow['definition']['release'])
        if code['attributes']['extensions'].get('source')!=build['files']['course_app.py']:
            raise KnowledgeError('归档代码不匹配注册的预览实现',409)
        return flow,build

    def app(workflow_id):
        flow,_=preview(workflow_id)
        root=Path(os.environ.get('KNOWLEDGE_DEMO_DATA_DIR', str(Path(tempfile.gettempdir())/'engineering-knowledge-course')))
        root.mkdir(parents=True,exist_ok=True)
        return CourseApplication(root/(str(flow['id'])+'.sqlite'),flow['definition']['release'])

    @router.get('/workflows/{workflow_id}/app',response_class=HTMLResponse,summary='打开版本固定的选课教学应用')
    def page(workflow_id:UUID):
        preview(workflow_id)
        return COURSE_HTML

    @router.get('/workflows/{workflow_id}/app/api',summary='查询教学班和学生已选记录')
    def listing(workflow_id:UUID,student:Literal['S001','S002','S003']='S001'):
        application=app(workflow_id)
        return {'offerings':application.offerings(),'enrollments':application.enrollments(student)}

    @router.post('/workflows/{workflow_id}/app/api',summary='操作独立教学选课数据')
    def enroll(workflow_id:UUID,body:Enroll):
        try: return app(workflow_id).enroll(body.student,body.offering)
        except EnrollmentError as exc: raise KnowledgeError(str(exc),409) from exc

    @router.get('/workflows/{workflow_id}/bundle',summary='下载可独立运行的已验证选课代码与测试')
    def bundle(workflow_id:UUID):
        flow,build=preview(workflow_id)
        server='''from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from course_app import CourseApplication, EnrollmentError
app=FastAPI()
application=CourseApplication(Path(__file__).with_name("school.sqlite"))
HTML=__HTML__
class Choice(BaseModel):
    student:str
    offering:str
@app.get("/",response_class=HTMLResponse)
def page(): return HTML
@app.get("/api")
def listing(student:str="S001"): return {"offerings":application.offerings(),"enrollments":application.enrollments(student)}
@app.post("/api")
def enroll(body:Choice):
    try:return application.enroll(body.student,body.offering)
    except EnrollmentError as exc:raise HTTPException(409,str(exc))
'''.replace('__HTML__',repr(COURSE_HTML))
        stream=BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            for name,content in {**build['files'],'app.py':server,'README.txt':'选课教学应用 '+flow['definition']['release']+'\n安装 fastapi 与 uvicorn，执行 python -m uvicorn app:app --port 8010 后访问 http://127.0.0.1:8010/\n测试：python -m unittest discover -p test_course_app.py -v\n预置学生 S001/S002/S003。SQLite数据保存在school.sqlite。'}.items(): archive.writestr(name,content)
        return Response(stream.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="course-selection.zip"'})

    return router
