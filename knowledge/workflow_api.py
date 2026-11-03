"""第四阶段 SDLC 流程 API。"""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query

from .workflow import WorkflowService
from .workflow_models import ReferenceExecutionRequest, STAGES, WorkflowControl, WorkflowCreate


def workflow_router(url_provider):
    router = APIRouter(prefix="/workflows", tags=["SDLC 流程（第四阶段）"])

    def service():
        return WorkflowService(url_provider())

    @router.get("/templates", summary="查询可用的 SDLC 参考流程",
                description="返回四阶段模板及唯一内置可信参考执行场景。扩展其他业务或仓库需另行实现并授权适配器。")
    def templates():
        return {"template": "feature_delivery_v1", "stages": STAGES, "scenarios": ["order_cancellation_v1"],
                "message": "订单取消仅为调用方明确选择的合成参考，不是对现有项目业务规则的推定。"}

    @router.post("", status_code=201, summary="创建 SDLC 草稿流程",
                 description="创建不可变流程定义及待审初始化知识提案，不直接发布草稿实体。需先显式提交初始化提案，再启动流程。")
    def create(body: WorkflowCreate):
        return service().create(body)

    @router.get("", summary="查询项目 SDLC 流程",
                description="分页读取流程定义、阶段及最新事件；所有版本号与任务、知识提案版本独立。")
    def workflows(project_id: UUID = Query(description="所属项目 ID"),
                  status: Literal["DRAFT", "RUNNING", "AWAITING_GATE", "PAUSED", "BLOCKED", "COMPLETED", "CANCELLED"] | None = Query(None),
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return service().list(project_id, status, limit, offset)

    @router.get("/{workflow_id}", summary="读取 SDLC 流程状态",
                description="读取当前阶段、任务、初始化与门禁提案、知识身份及已完成阶段。")
    def workflow(workflow_id: UUID):
        return service().get(workflow_id)

    @router.get("/{workflow_id}/history", summary="读取流程不可变事件历史",
                description="按事件版本从新到旧分页查询创建、阶段门禁、暂停、重试和推进记录。")
    def history(workflow_id: UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return service().history(workflow_id, limit, offset)

    @router.get("/{workflow_id}/tasks", summary="读取各阶段任务及重试记录",
                description="显示阶段关联的所有任务，不公开租约令牌摘要。")
    def tasks(workflow_id: UUID):
        return service().tasks(workflow_id)

    @router.get("/{workflow_id}/executions", summary="读取真实参考执行记录",
                description="读取服务端产生的代码／测试摘要、固定命令、退出码、测试数量及有界日志。")
    def executions(workflow_id: UUID):
        return service().executions(workflow_id)

    @router.post("/{workflow_id}/reference-executions", status_code=201, summary="实际执行内置可信参考测试",
                 description="仅验证阶段当前租约持有方可调用；只执行包内 allowlist 模板，不接受任意代码、路径或命令。不会修改用户业务仓库。")
    def execute(workflow_id: UUID, body: ReferenceExecutionRequest):
        return service().execute_reference(workflow_id, body)

    @router.post("/{workflow_id}/actions/{action}", summary="显式控制 SDLC 流程",
                 description="start 启动；prepare_gate 创建阶段知识门禁；advance 在门禁提案显式提交后推进。pause／resume 只暂停／恢复流程推进，不中断已领取任务；retry 重建阻塞阶段；cancel 原子取消当前未终结任务。")
    def control(workflow_id: UUID, action: Literal["start", "prepare_gate", "advance", "pause", "resume", "retry", "cancel"], body: WorkflowControl):
        return service().control(workflow_id, body, action)

    return router
