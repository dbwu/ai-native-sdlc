"""工程快照与隔离执行接口；默认明确报告未配置。"""
from uuid import UUID

from fastapi import APIRouter

from .code_generation import CodeGenerationAction, CodeGenerationService
from .interface_comparison import InterfaceComparisonService
from .workspace import (ExecutionCancel, ExecutionCreate, SnapshotCreate, WorkspaceCreate,
                        WorkspaceService, executor_statuses)


def workspace_router(url_provider):
    router = APIRouter(prefix='/engineering', tags=['工程工作区与隔离执行'])

    def service():
        return WorkspaceService(url_provider())

    def code_service():
        return CodeGenerationService(url_provider())

    @router.get('/executor', summary='查询隔离执行器配置与可用状态')
    def executor():
        return executor_statuses()

    @router.get('/projects/{project_id}/workspaces', summary='查询项目工程工作区')
    def workspaces(project_id: UUID):
        return service().list(project_id)

    @router.post('/projects/{project_id}/workspaces', status_code=201, summary='建立工程工作区，不自动生成代码')
    def create(project_id: UUID, body: WorkspaceCreate):
        return service().create(project_id, body)

    @router.get('/workspaces/{workspace_id}', summary='读取工作区最新快照及隔离执行历史')
    def workspace(workspace_id: UUID):
        return service().detail(workspace_id)

    @router.post('/workspaces/{workspace_id}/snapshots', status_code=201, summary='保存完整不可变工程文件快照')
    def snapshot(workspace_id: UUID, body: SnapshotCreate):
        return service().snapshot(workspace_id, body)

    @router.get('/workspaces/{workspace_id}/snapshots', summary='列出工程文件快照版本与摘要')
    def snapshots(workspace_id: UUID):
        return InterfaceComparisonService(url_provider()).snapshots(workspace_id)

    @router.get('/workspaces/{workspace_id}/snapshots/{snapshot_id}/interface-comparison',
                summary='将固定快照中的 FastAPI 路由与已确认设计对照')
    def interface_comparison(workspace_id: UUID, snapshot_id: UUID):
        return InterfaceComparisonService(url_provider()).read(workspace_id, snapshot_id)

    @router.get('/workspaces/{workspace_id}/code-generations', summary='查看工作区代码生成记录')
    def code_generations(workspace_id: UUID):
        return code_service().generations(workspace_id)

    @router.post('/workspaces/{workspace_id}/code-generations', status_code=202, summary='从已确认设计排队生成代码候选')
    def generate_code(workspace_id: UUID, body: CodeGenerationAction):
        return code_service().enqueue(workspace_id, body)

    @router.get('/workspaces/{workspace_id}/code-generations/{generation_id}', summary='查看代码候选文件差异')
    def code_generation(workspace_id: UUID, generation_id: UUID):
        return code_service().generation(workspace_id, generation_id)

    @router.post('/workspaces/{workspace_id}/code-generations/{generation_id}/apply', status_code=201,
                 summary='明确确认代码候选并保存为不可变工程快照')
    def apply_code(workspace_id: UUID, generation_id: UUID, body: CodeGenerationAction):
        return code_service().apply(workspace_id, generation_id, body)

    @router.post('/workspaces/{workspace_id}/executions', status_code=201, summary='在隔离容器实际执行固定测试方式')
    def execute(workspace_id: UUID, body: ExecutionCreate):
        return service().execute(workspace_id, body)

    @router.post('/executions/{execution_id}/cancel', summary='请求取消运行并清理隔离容器')
    def cancel(execution_id: UUID, body: ExecutionCancel):
        return service().cancel(execution_id, body)

    return router
