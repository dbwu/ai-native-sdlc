"""文档语义抽取与人工审阅入口。"""
from uuid import UUID

from fastapi import APIRouter, Query

from .extraction import ExtractionService
from .extraction_models import ExtractionProposal, ExtractionStart, ExtractionReviewRequest


def extraction_router(url_provider):
    router = APIRouter(prefix='/extractions', tags=['文档知识抽取（M1）'])

    def service():
        return ExtractionService(url_provider())

    @router.post('/projects/{project_id}/jobs', status_code=201, summary='基于固定原文发起模型抽取',
                 description='发送原文与已有知识到已配置模型服务，仅生成候选，等待人工核对。')
    def start(project_id: UUID, body: ExtractionStart):
        return service().start(project_id, body)

    @router.get('/projects/{project_id}/jobs', summary='查询文档抽取任务和模型配置状态')
    def list_jobs(project_id: UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return service().list_jobs(project_id, limit, offset)

    @router.get('/jobs/{job_id}', summary='读取抽取候选、固定原文和脱敏模型问答')
    def get_job(job_id: UUID):
        return service().get_job(job_id)

    @router.post('/jobs/{job_id}/review', summary='比较当前候选、重复冲突建议及固定影响路径')
    def review_candidate(job_id: UUID, body: ExtractionReviewRequest):
        return service().review_candidate(job_id, body)

    @router.post('/jobs/{job_id}/proposals', status_code=201, summary='将人工选择和修订后的候选形成提案',
                 description='原文存在不代表候选语义成立。人工逐项核对后形成不可变提案，再沿用校验、确认提交或拒绝流程。')
    def propose(job_id: UUID, body: ExtractionProposal):
        return service().propose(job_id, body)

    return router
