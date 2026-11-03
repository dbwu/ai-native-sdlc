"""第三阶段的 Agent 运行时与协作 API。"""
from uuid import UUID

from fastapi import APIRouter, Query

from .runtime import AgentRuntime
from .runtime_models import AgentCreate, AgentStateChange, ClaimRequest, LeaseRequest, TaskControl, TaskCreate, TaskReport, TaskStatus


def runtime_router(url_provider):
    router = APIRouter(prefix="/runtime", tags=["Agent 协作（第三阶段）"])

    def service():
        return AgentRuntime(url_provider())

    @router.get("/contract", summary="查询 Agent 协作契约",
                description="查看六个标准能力及本地运行时边界；接口不自动调用模型、脚本或动态模块。")
    def contract():
        return {"version": 1, "methods": {
            "get_capabilities": "声明可以承担的任务能力", "get_context": "读取统一组装、固定知识版本的上下文包",
            "execute_task": "由外部执行方或本地适配器产生结构化结果", "propose_changes": "提出待审核的知识修订",
            "report_result": "按验收条件逐项报告结果", "request_review": "请求人工处理阻塞或不确定性"},
            "message": "执行结果是调用方声明。知识写入必须经过第二阶段提案，任务完成需显式确认；身份和组织授权仍待接入。"}

    @router.post("/agents", status_code=201, summary="注册项目 Agent",
                 description="注册职责、能力和知识读取／修订范围；职责标签不自动授予组织权限，注册信息不可原地改写。")
    def register(body: AgentCreate):
        return service().register_agent(body)

    @router.get("/agents", summary="查询项目 Agent",
                description="按创建顺序分页返回注册信息及最新启用状态。")
    def agents(project_id: UUID = Query(description="所属项目 ID"), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return service().list_agents(project_id, limit, offset)

    @router.get("/agents/{agent_id}", summary="读取 Agent 能力与范围",
                description="读取稳定注册身份、职责、能力和最新启用事件。")
    def agent(agent_id: UUID):
        return service().get_agent(agent_id)

    @router.post("/agents/{agent_id}/state", summary="启用或禁用 Agent",
                 description="追加状态事件，校验 expected_version；禁用后不可领取、续期或报告任务。")
    def agent_state(agent_id: UUID, body: AgentStateChange):
        return service().change_agent(agent_id, body)

    @router.post("/tasks", status_code=201, summary="创建协作任务",
                 description="指定目标、能力、验收条件、知识版本和已有前置任务；初始状态为 CREATED，不自动执行。")
    def create_task(body: TaskCreate):
        return service().create_task(body)

    @router.get("/tasks", summary="查询项目任务",
                description="分页查询任务定义和最新协作状态，不返回租约令牌或其摘要。")
    def tasks(project_id: UUID = Query(description="所属项目 ID"), status: TaskStatus | None = Query(None, description="任务状态"),
              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return service().list_tasks(project_id, status, limit, offset)

    @router.get("/tasks/{task_id}", summary="读取协作任务",
                description="读取目标、依赖、验收条件、上下文引用及最新结果。")
    def task(task_id: UUID):
        return service().get_task(task_id)

    @router.get("/tasks/{task_id}/history", summary="读取任务协作历史",
                description="按事件版本从新到旧读取不可变操作记录；不返回租约令牌摘要。")
    def history(task_id: UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return service().history(task_id, limit, offset)

    @router.get("/tasks/{task_id}/context", summary="读取任务的固定版本上下文",
                description="返回最近领取时持久保存的上下文包、内容摘要和构建时间，历史包不会被后续知识修改覆盖。")
    def context(task_id: UUID):
        return service().get_context(task_id)

    @router.post("/tasks/{task_id}/ready", summary="将任务放入待领取队列",
                 description="允许 CREATED、BLOCKED 或 NEEDS_HUMAN_REVIEW 入队；领取仍需满足依赖、能力和读取范围。")
    def ready(task_id: UUID, body: TaskControl):
        return service().control(task_id, body, "ready")

    @router.post("/tasks/{task_id}/cancel", summary="取消未终结的任务",
                 description="取消后旧租约不能继续回写结果；任务和全部审计记录保留。")
    def cancel(task_id: UUID, body: TaskControl):
        return service().control(task_id, body, "cancel")

    @router.post("/tasks/{task_id}/validate", summary="校验任务产物与关联提案",
                 description="核对逐项验收声明、上下文是否陈旧及知识提案是否通过试运行；不替代人工事实核验，不自动提交知识。")
    def validate(task_id: UUID, body: TaskControl):
        return service().control(task_id, body, "validate")

    @router.post("/tasks/{task_id}/complete", summary="显式确认任务完成",
                 description="仅允许 VALIDATING 完成；关联知识提案须已显式提交。检查上下文未出现任务自身提交以外的版本漂移。")
    def complete(task_id: UUID, body: TaskControl):
        return service().control(task_id, body, "complete")

    @router.post("/claim", summary="领取能力匹配的任务租约",
                 description="原子领取最早的可执行任务并组装上下文。无任务时 task 为 null；令牌仅本次返回。过期租约在下次领取时接管，达到次数上限则失败。")
    def claim(body: ClaimRequest):
        return service().claim(body)

    @router.post("/tasks/{task_id}/heartbeat", summary="续期任务租约",
                 description="需持有正确 Agent、令牌和当前任务事件版本。过期、被接管或禁用的执行方不能续期。")
    def heartbeat(task_id: UUID, body: LeaseRequest):
        return service().heartbeat(task_id, body)

    @router.post("/tasks/{task_id}/report", summary="报告产物、建议变更或请求复核",
                 description="成功产物转为 PROPOSED；知识修订自动转第二阶段待审提案并强制标记为推断。失败可在预算内重试；blocked／review 请求显式干预。")
    def report(task_id: UUID, body: TaskReport):
        return service().report(task_id, body)

    @router.get("/events", summary="按项目游标订阅协作事件",
                description="使用上次 next_cursor 作为 after 轮询。游标仅用于同一项目的任务事件，不含知识库所有变更，不发送网络回调。")
    def events(project_id: UUID = Query(description="所属项目 ID"), after: int = Query(0, ge=0, description="上一页事件游标"),
               limit: int = Query(50, ge=1, le=200, description="每页事件数")):
        return service().events(project_id, after, limit)

    return router
