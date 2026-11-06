"""工程快照与隔离执行接口；默认明确报告未配置。"""
from uuid import UUID

from fastapi import APIRouter

from .workspace import (ExecutionCancel, ExecutionCreate, SnapshotCreate, WorkspaceCreate,
                        WorkspaceService, executor_status)


def workspace_router(url_provider):
    router = APIRouter(prefix='/engineering', tags=['工程工作区与隔离执行'])

    def service():
        return WorkspaceService(url_provider())

    @router.get('/executor', summary='查询隔离执行器配置与可用状态')
    def executor():
        return executor_status()

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

    @router.post('/workspaces/{workspace_id}/executions', status_code=201, summary='在隔离容器实际执行固定测试方式')
    def execute(workspace_id: UUID, body: ExecutionCreate):
        return service().execute(workspace_id, body)

    @router.post('/executions/{execution_id}/cancel', summary='请求取消运行并清理隔离容器')
    def cancel(execution_id: UUID, body: ExecutionCancel):
        return service().cancel(execution_id, body)

    return router
