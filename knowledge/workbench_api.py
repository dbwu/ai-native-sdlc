"""中文页面的项目发现、聚合视图及显式单步操作。"""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query

from .workbench import WorkbenchAction, WorkbenchService


def workbench_router(url_provider):
    router = APIRouter(prefix="/workbench", tags=["中文验证工作台"])

    def service():
        return WorkbenchService(url_provider())

    @router.get("/projects", summary="查询可选择的项目", description="无需预先知道 UUID，按创建时间从新到旧分页返回项目。")
    def projects(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return service().projects(limit, offset)

    @router.get("/workflows/{workflow_id}/snapshot", summary="读取验证页面所需的一致快照", description="聚合流程、当前任务、待审提案、产物和证据，以及当前可用动作和禁用原因。")
    def snapshot(workflow_id: UUID):
        return service().snapshot(workflow_id)

    @router.get("/proposals/{proposal_id}/diff", summary="对照原始基线审阅提案差异", description="使用提案 expected_version 读取旧快照，禁止用最新实体混充提案原基线。")
    def diff(proposal_id: UUID):
        return service().proposal_diff(proposal_id)

    @router.post("/workflows/{workflow_id}/actions/{action}", summary="执行工作台显式单步操作", description="参考运行只停在待审产物，绝不自动审批或推进；校验、提交和阶段放行仍是独立动作。操作携带流程、任务和提案版本。")
    def action(workflow_id: UUID, action: Literal["run_reference_stage", "validate_proposal", "commit_proposal", "reject_proposal", "start",
               "validate_task", "complete_task", "prepare_gate", "advance", "pause", "resume", "retry", "cancel", "reject_task_and_retry"], body: WorkbenchAction):
        return service().action(workflow_id, action, body)

    return router
