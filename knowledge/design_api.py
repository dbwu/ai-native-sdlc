"""需求设计页面接口；模型任务入队后立即返回，所有审批仍需明确操作。"""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response

from .design import DesignService
from .design_models import AnswerSubmission, DesignAction, DesignFlowCreate
from .model_gateway import public_model_config


def design_router(url_provider):
    router = APIRouter(prefix='/design', tags=['需求与模型设计'])

    def service():
        return DesignService(url_provider())

    @router.get('/config', summary='查看脱敏模型配置与可用状态')
    def config():
        return public_model_config()

    @router.get('/projects/{project_id}/flows', summary='查询项目需求设计流程')
    def flows(project_id: UUID, limit: int = Query(100, ge=1, le=100)):
        return service().list(project_id, limit)

    @router.post('/projects/{project_id}/flows', status_code=201, summary='仅绑定需求启动设计草稿')
    def create(project_id: UUID, body: DesignFlowCreate):
        return service().create(project_id, body)

    @router.get('/flows/{flow_id}', summary='读取设计、澄清问题、提案与模型调用历史')
    def detail(flow_id: UUID):
        return service().snapshot(flow_id)

    @router.get('/flows/{flow_id}/generations/{job_id}/model-exchange', summary='查看本轮发给模型的输入和模型回复')
    def model_exchange(flow_id: UUID, job_id: UUID, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return service().model_exchange(flow_id, job_id)

    @router.post('/flows/{flow_id}/actions/{action}', summary='确认需求、生成设计及明确审阅确认')
    def action(flow_id: UUID, action: Literal['start', 'generate', 'generate_confirmed', 'validate', 'commit', 'complete', 'reject', 'cancel'],
               body: DesignAction, response: Response):
        result = service().action(flow_id, action, body)
        if action in {'generate', 'generate_confirmed'}:
            response.status_code = 202
        return result

    @router.post('/flows/{flow_id}/answers', summary='逐项提交需求澄清答复')
    def answers(flow_id: UUID, body: AnswerSubmission):
        return service().answers(flow_id, body)

    return router
