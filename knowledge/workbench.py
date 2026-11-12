"""面向页面的查询与单步操作；所有知识确认仍复用已有服务并要求显式动作。"""
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from pydantic import Field

from .db import connect
from .models import Kind, Text, Version
from .operations import KnowledgeOperations
from .operations_models import ProposalReview
from .runtime import AgentRuntime, TERMINAL
from .runtime_models import AgentCreate, ClaimRequest, RuntimeInput, TaskControl, TaskReport
from .service import KnowledgeService
from .semantics import KnowledgeError
from .workflow import WorkflowService
from .workflow_adapter import WorkflowReferenceAdapter
from .workflow_models import ReferenceExecutionRequest, STAGES, WorkflowControl
from .generic_workflow import report_generic


class WorkbenchAction(RuntimeInput):
    expected_version: Version = Field(title="预期流程版本")
    task_version: Version | None = Field(default=None, title="预期当前任务版本")
    proposal_version: Version | None = Field(default=None, title="预期当前提案版本")
    actor: Text = Field(title="操作署名", description="本地记录，不代表登录认证。")
    reason: Text = Field(title="操作理由")


WRITE_SCOPES = [["Requirement"], ["ArchitectureComponent", "Interface", "DataModel"], ["CodeArtifact", "Evidence"], ["TestCase", "Evidence"]]


class WorkbenchService:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def projects(self, limit=50, offset=0):
        KnowledgeService._page(limit, offset)
        with connect(self.database_url) as conn:
            rows = conn.execute("SELECT id FROM current_entities WHERE kind='Project' ORDER BY created_at DESC,id LIMIT %s OFFSET %s", (limit, offset)).fetchall()
            return [KnowledgeService._get(conn, "entity", row["id"]) for row in rows]

    @staticmethod
    def _selected_proposal(conn, workflow, task):
        state = workflow["state"]
        if workflow["status"] == "DRAFT":
            proposal_id, kind = state["bootstrap_proposal_id"], "bootstrap"
        elif state["gate_proposal_id"]:
            proposal_id, kind = state["gate_proposal_id"], "gate"
        elif task and task["runtime"]["proposal_id"]:
            proposal_id, kind = task["runtime"]["proposal_id"], "task"
        else:
            return None, None
        return KnowledgeOperations._proposal(conn, UUID(proposal_id)), kind

    @staticmethod
    def _actions(conn, workflow, task, proposal, kind):
        ws, ts, ps = workflow["status"], task["status"] if task else None, proposal["status"] if proposal else None
        normal = ws == "RUNNING"
        can_retry = workflow["state"]["stage_attempt"] <= workflow["definition"]["max_stage_retries"]
        retry_reason = "本阶段人工重试次数已用尽。" if not can_retry else None
        expired = bool(task and ts == "RUNNING" and datetime.fromisoformat(task["runtime"]["lease_expires_at"]) <= AgentRuntime._now(conn))
        rules = [
            ("run_reference_stage", "运行本阶段参考 Agent", normal and (ts == "READY" or expired), "需要当前阶段任务就绪，或其旧租约已过期。"),
            ("validate_proposal", "校验当前提案", kind in {"bootstrap", "gate"} and ps == "pending", "当前初始化或门禁提案需处于待校验状态。"),
            ("validate_task", "校验阶段产物", normal and ts == "PROPOSED", "运行参考 Agent 并产生待审产物后可校验。"),
            ("commit_proposal", "确认提交当前提案", ps == "validated" and (kind != "task" or ts == "VALIDATING"), "请先完成当前提案或任务的校验，确认差异后提交。"),
            ("start", "启动需求阶段", ws == "DRAFT" and ps == "committed", "请先校验并显式提交初始化提案。"),
            ("complete_task", "确认阶段任务完成", normal and ts == "VALIDATING" and ps == "committed", "当前任务需校验通过，且产物提案已显式提交。"),
            ("prepare_gate", "准备阶段放行提案", normal and ts in TERMINAL | {"BLOCKED", "NEEDS_HUMAN_REVIEW"}, "阶段任务完成后才能准备放行；失败任务可先记录诊断。"),
            ("advance", "确认放行并推进", ws == "AWAITING_GATE" and ps == "committed", "请先审阅并提交阶段放行提案。"),
            ("reject_proposal", "拒绝当前提案", ps in {"pending", "validated"}, "仅待审或已校验的提案可拒绝，历史提案不可改写。"),
            ("reject_task_and_retry", "退回本阶段并重试", can_retry and normal and task is not None and ts not in {"COMPLETED"}, retry_reason or "当前未完成的阶段可退回，旧提案与任务记录会保留。"),
            ("retry", "重试本阶段", can_retry and ws in {"BLOCKED", "AWAITING_GATE"}, retry_reason or "阻塞或等待放行的阶段可以显式重试。"),
            ("pause", "暂停流程推进", ws in {"RUNNING", "AWAITING_GATE", "BLOCKED"}, "仅运行、阻塞或等待放行的流程可暂停。"),
            ("resume", "恢复流程推进", ws == "PAUSED", "当前流程未暂停。"),
            ("cancel", "取消当前流程", ws not in {"COMPLETED", "CANCELLED"}, "已完成或取消的流程不能再次取消。"),
        ]
        if workflow['definition']['scenario'] == 'generic_v1' and workflow['definition']['executor'] == 'manual':
            rules = [(a,l,False if a=='run_reference_stage' else e,'当前为人工流程，请提交本阶段产物。' if a=='run_reference_stage' else r) for a,l,e,r in rules]
        project = KnowledgeService._get(conn, "entity", workflow["project_id"])
        result = []
        for action, label, enabled, reason in rules:
            if ws == "PAUSED" and action not in {"resume", "cancel"}:
                enabled, reason = False, "请先恢复流程推进。"
            if ws in {"COMPLETED", "CANCELLED"}:
                enabled, reason = False, "流程已处于终态，可查看历史和证据。"
            if project["status"] == "archived":
                enabled, reason = False, "项目已归档，只能读取历史。"
            result.append({"id": action, "label": label, "enabled": bool(enabled), "reason": "" if enabled else reason})
        return result

    def _snapshot(self, conn, workflow_id):
        workflow = WorkflowService._get(conn, workflow_id)
        state = workflow["state"]
        task = AgentRuntime._public(AgentRuntime._task(conn, UUID(state["task_id"]))) if state["task_id"] else None
        proposal, kind = self._selected_proposal(conn, workflow, task)
        artifacts, relationships = [], []
        for key, record_id in state["bindings"].items():
            try:
                pinned = next((r['version'] for r in state['knowledge_versions'] if r['table']=='entity' and str(r['id'])==str(record_id)), None) if workflow['status']=='COMPLETED' else None
                record = KnowledgeService._get(conn, "entity", UUID(record_id), pinned)
            except KnowledgeError as exc:
                if exc.status_code != 404:
                    raise
                record = None
            artifacts.append({"key": key, "entity": record})
        for record_id in state["relation_ids"]:
            try:
                pinned = next((r['version'] for r in state['knowledge_versions'] if r['table']=='relationship' and str(r['id'])==str(record_id)), None) if workflow['status']=='COMPLETED' else None
                relationships.append(KnowledgeService._get(conn, "relationship", UUID(record_id), pinned))
            except KnowledgeError as exc:
                if exc.status_code != 404:
                    raise
        executions = conn.execute("SELECT * FROM workflow_execution WHERE workflow_id=%s ORDER BY recorded_at DESC,id LIMIT 100", (workflow_id,)).fetchall()
        history = conn.execute("SELECT * FROM workflow_event WHERE workflow_id=%s ORDER BY version DESC LIMIT 200", (workflow_id,)).fetchall()
        return {"workflow": workflow, "task": task, "proposal": proposal, "proposal_kind": kind,
                "artifacts": artifacts, "relationships": relationships, "executions": executions, "history": history,
                "actions": self._actions(conn, workflow, task, proposal, kind), "mode": "generic" if workflow['definition']['scenario']=='generic_v1' else 'reference'}

    def snapshot(self, workflow_id: UUID):
        with connect(self.database_url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return self._snapshot(conn, workflow_id)

    def proposal_diff(self, proposal_id: UUID):
        with connect(self.database_url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            proposal = KnowledgeOperations._proposal(conn, proposal_id)
            changes, stale = [], False
            for operation in proposal["plan"]["operations"]:
                table = "entity" if operation["op"].endswith("entity") else "relationship"
                data = deepcopy(operation["data"])
                expected = data.pop("expected_version", None)
                before = KnowledgeService._get(conn, table, UUID(operation["id"]), expected) if expected is not None else None
                after = {**(deepcopy(before) if before else {}), **data, "id": operation["id"], "version": 1 if expected is None else expected + 1}
                after.pop("recorded_at", None)
                changes.append({"op": operation["op"], "id": operation["id"], "kind": (before or data).get("kind", "Relationship"),
                                "name": (before or data).get("name", "知识关系"), "before": before, "after": after})
            if proposal["status"] in {"pending", "validated"}:
                for baseline in proposal["plan"]["baselines"]:
                    if KnowledgeService._get(conn, baseline["table"], UUID(baseline["id"]))["version"] != baseline["version"]:
                        stale = True
                if proposal["source_id"]:
                    latest = conn.execute("SELECT max(version) AS v FROM source_revision WHERE source_id=%s", (proposal["source_id"],)).fetchone()["v"]
                    stale |= latest != proposal["source_version"]
                if (model := proposal["plan"].get("project_model")):
                    latest = conn.execute("SELECT coalesce(max(version), 0) AS version FROM project_model_revision WHERE project_id=%s",
                                          (proposal["project_id"],)).fetchone()["version"]
                    stale |= latest + 1 != model["version"]
                if (extraction := proposal['plan'].get('extraction')):
                    if extraction.get('project_state_token'):
                        from .extraction_context import project_state_token
                        stale |= project_state_token(conn, proposal['project_id']) != extraction['project_state_token']
                    latest = conn.execute('SELECT max(version) AS v FROM source_revision WHERE source_id=%s',
                                          (UUID(extraction['source_id']),)).fetchone()['v']
                    stale |= latest != extraction['source_version']
            return {"id": proposal["id"], "project_id": proposal['project_id'], "version": proposal["version"], "status": proposal["status"], "changes": changes, "stale": stale,
                    "extraction_review": proposal['plan'].get('extraction', {}).get('review'),
                    "project_model_changes": proposal["plan"].get("project_model", {}).get("changes", [])}

    def _reference_agent(self, conn, workflow, request):
        stage = STAGES[workflow["state"]["stage"]]
        runtime = AgentRuntime(self.database_url, connection=conn)
        name = "工作台参考-" + stage["role"]
        existing = conn.execute("SELECT * FROM current_runtime_agents WHERE project_id=%s AND name=%s", (workflow["project_id"], name)).fetchone()
        if existing:
            if (existing["status"] != "active" or existing["role"] != stage["role"] or stage["capability"] not in existing["capabilities"]
                    or set(existing["read_kinds"]) != set(Kind) or set(existing["write_kinds"]) != set(WRITE_SCOPES[workflow["state"]["stage"]])):
                raise KnowledgeError("同名工作台参考 Agent 已被禁用或能力范围不匹配，请检查 Agent 注册信息", 409)
            return existing
        return runtime.register_agent(AgentCreate(project_id=workflow["project_id"], name=name, role=stage["role"],
            capabilities=[stage["capability"]], read_kinds=list(Kind), write_kinds=WRITE_SCOPES[workflow["state"]["stage"]], actor=request.actor))

    def _run_reference(self, conn, workflow, request):
        if workflow['definition']['scenario']=='generic_v1':
            success=report_generic(WorkflowService(self.database_url,connection=conn),conn,workflow,request.actor,request.reason)
            return '本阶段产物已形成，等待审阅和明确提交。' if success else '实际测试未通过，日志已保存，未生成通过证据。'
        agent = self._reference_agent(conn, workflow, request)
        runtime = AgentRuntime(self.database_url, connection=conn)
        lease = runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=workflow["state"]["task_id"], lease_seconds=120))
        task = lease["task"]
        if task is None:
            return "任务未进入运行状态，请查看上下文、依赖或重试次数诊断。"
        package = runtime.get_context(task["id"])
        fields = {"agent_id": agent["id"], "lease_token": lease["lease_token"], "expected_version": task["version"]}
        execution = None
        if workflow["state"]["stage"] == 3:
            execution = WorkflowService(self.database_url, connection=conn).execute_reference(workflow["id"], ReferenceExecutionRequest(**fields))
            if not execution["document"]["passed"]:
                runtime.report(task["id"], TaskReport(**fields, outcome="failed", summary="可信参考测试未通过，未生成通过提案。",
                    acceptance=[{"criterion_index": 0, "passed": False}], artifacts={"execution_id": str(execution["id"])}))
                return "参考测试未通过，请查看真实执行日志；没有自动审批或提交知识。"
        report = WorkflowReferenceAdapter().build_report(workflow["state"]["stage"], task, package, workflow["state"]["bindings"], execution)
        runtime.report(task["id"], TaskReport(**fields, **report))
        return "本阶段参考产物已生成，停在待审状态。请先审阅差异，再校验和确认提交。"

    def action(self, workflow_id: UUID, action: str, request: WorkbenchAction):
        with connect(self.database_url) as conn:
            workflow = WorkflowService._get(conn, workflow_id)
            KnowledgeService._lock(conn, workflow["project_id"])
            KnowledgeService._assert_project(conn, workflow["project_id"])
            before = self._snapshot(conn, workflow_id)
            workflow, task, proposal = before["workflow"], before["task"], before["proposal"]
            if request.expected_version != workflow["version"]:
                raise KnowledgeError("流程版本已变化，请刷新页面后重新审阅", 409)
            if request.task_version is not None and (task is None or request.task_version != task["version"]):
                raise KnowledgeError("当前阶段任务版本已变化，请刷新页面", 409)
            if request.proposal_version is not None and (proposal is None or request.proposal_version != proposal["version"]):
                raise KnowledgeError("提案版本已变化，请刷新差异后重新确认", 409)
            rules = {item["id"]: item for item in before["actions"]}
            if action not in rules:
                raise KnowledgeError("不支持的工作台动作")
            if not rules[action]["enabled"]:
                raise KnowledgeError(rules[action]["reason"], 409)
            runtime = AgentRuntime(self.database_url, connection=conn)
            operations = KnowledgeOperations(self.database_url, connection=conn)
            workflows = WorkflowService(self.database_url, connection=conn)
            task_actions = {"run_reference_stage", "validate_task", "complete_task", "reject_task_and_retry"}
            if action in task_actions and request.task_version is None:
                raise KnowledgeError("页面动作必须携带当前任务版本")
            if (action.endswith("_proposal") or (proposal and action in {"validate_task", "reject_task_and_retry"})) and request.proposal_version is None:
                raise KnowledgeError("审阅操作必须携带当前提案版本")
            message = "操作已完成。"
            if action == "run_reference_stage":
                message = self._run_reference(conn, workflow, request)
            elif action in {"validate_proposal", "commit_proposal", "reject_proposal"}:
                operation = action.split("_", 1)[0]
                operations.review(proposal["id"], ProposalReview(expected_version=proposal["version"], actor=request.actor, reason=request.reason), operation)
            elif action in {"validate_task", "complete_task"}:
                runtime.control(task["id"], TaskControl(expected_version=task["version"], actor=request.actor, reason=request.reason), action.split("_", 1)[0])
            elif action == "reject_task_and_retry":
                if task["runtime"]["proposal_id"]:
                    old = operations._proposal(conn, UUID(task["runtime"]["proposal_id"]))
                    if old["status"] in {"pending", "validated"}:
                        operations.review(old["id"], ProposalReview(expected_version=old["version"], actor=request.actor, reason=request.reason), "reject")
                if task["status"] not in TERMINAL:
                    runtime.control(task["id"], TaskControl(expected_version=task["version"], actor=request.actor, reason=request.reason), "cancel")
                blocked = workflows.control(workflow_id, WorkflowControl(expected_version=workflow["version"], actor=request.actor, reason=request.reason), "prepare_gate")
                workflows.control(workflow_id, WorkflowControl(expected_version=blocked["version"], actor=request.actor, reason=request.reason), "retry")
                message = "已保留旧提案和任务，显式重试当前阶段；若出现关系对齐提案，请另行审阅确认。"
            else:
                workflows.control(workflow_id, WorkflowControl(expected_version=workflow["version"], actor=request.actor, reason=request.reason), action)
            snapshot = self._snapshot(conn, workflow_id)
            warning = snapshot["workflow"]["status"] == "BLOCKED" or bool(snapshot["task"] and snapshot["task"]["status"] in {"NEEDS_HUMAN_REVIEW", "FAILED", "BLOCKED"})
            if snapshot["proposal"] and snapshot["proposal"]["result"].get("valid") is False:
                warning = True
                message = "校验未通过，请查看当前提案的错误与基线信息。"
            return {"snapshot": snapshot, "replayed": False, "message": message,
                    "notice": {"level": "warning" if warning or "未通过" in message else "success", "message": message}}
