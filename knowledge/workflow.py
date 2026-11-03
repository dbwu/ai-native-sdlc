"""四阶段 SDLC 流程：晚绑定任务、显式知识门禁与可核对的执行证据。"""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .db import connect
from .models import EntityUpdate, EvidenceRef, RelationUpdate
from .operations import KnowledgeOperations
from .operations_models import IngestionRequest, ProposalCreate, ProposalReview
from .reference_runner import build_reference, run_reference
from .runtime import AgentRuntime, TERMINAL
from .runtime_models import TaskControl, TaskCreate
from .semantics import KnowledgeError
from .service import KnowledgeService
from .workflow_models import ReferenceExecutionRequest, STAGES, WorkflowControl, WorkflowCreate


def _digest(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class WorkflowService:
    def __init__(self, database_url: str, *, connection=None):
        self.database_url = database_url
        self._connection = connection

    @contextmanager
    def _transaction(self):
        if self._connection is None:
            with connect(self.database_url) as conn:
                yield conn
        else:
            with self._connection.transaction():
                yield self._connection

    @staticmethod
    def _get(conn, workflow_id):
        row = conn.execute("SELECT * FROM current_workflows WHERE id=%s", (workflow_id,)).fetchone()
        if row is None:
            raise KnowledgeError("未找到指定 SDLC 流程", 404)
        return row

    @staticmethod
    def _event(conn, workflow, status, action, actor, reason, state):
        conn.execute("""INSERT INTO workflow_event (workflow_id,version,status,action,actor,reason,document)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (workflow["id"], workflow.get("version", 0) + 1,
                status, action, actor, reason, Jsonb(state)))

    @staticmethod
    def _scaffold(request):
        entries = [("requirement", "Requirement", request.title), ("architecture", "ArchitectureComponent", "待设计组件"),
                   ("interface", "Interface", "待设计接口"), ("data", "DataModel", "待设计数据模型"),
                   ("code", "CodeArtifact", "待实现代码"), ("test", "TestCase", "待实现测试"),
                   ("code-evidence", "Evidence", "待确认代码证据"), ("test-evidence", "Evidence", "待执行测试证据")]
        entities = [{"key": key, "kind": kind, "name": name + "（流程草稿）", "description": request.goal if key == "requirement" else "尚未完成的流程产物，不代表已实现或验证。",
                     "attributes": {"category": "document", "uri": "urn:workflow:pending:" + key, "revision": "pending"} if kind == "Evidence" else {}}
                    for key, kind, name in entries]
        relations = [("DEPENDS_ON", "requirement", "architecture"), ("IMPLEMENTED_BY", "requirement", "code"),
                     ("VERIFIED_BY", "requirement", "test")]
        relations.extend(("CONTAINS", "architecture", target) for target in ("interface", "data", "code", "test"))
        return {"entities": entities, "relationships": [{"type": kind, "source_key": source, "target_key": target,
                    "rationale": "流程模板中的待确认追溯关系，需在阶段门禁中复核。"} for kind, source, target in relations]}

    def create(self, request: WorkflowCreate):
        with self._transaction() as conn:
            KnowledgeService._lock(conn, request.project_id)
            KnowledgeService._assert_project(conn, request.project_id)
            for ref in request.input_knowledge:
                if KnowledgeService._get(conn, "entity", ref.id, ref.version)["project_id"] != request.project_id:
                    raise KnowledgeError("流程输入知识必须属于同一项目")
            workflow_id = uuid4()
            conn.execute("INSERT INTO workflow_run (id,project_id,title,definition) VALUES (%s,%s,%s,%s)",
                         (workflow_id, request.project_id, request.title, Jsonb(request.model_dump(mode="json"))))
            proposal = KnowledgeOperations(self.database_url, connection=conn).ingest(IngestionRequest(
                project_id=request.project_id, source_uri="urn:workflow:" + str(workflow_id), source_revision="scaffold-v1",
                format="json", content=json.dumps(self._scaffold(request), ensure_ascii=False), actor=request.actor,
                title="SDLC 草稿初始化：" + request.title))["proposal"]
            state = {"stage": 0, "stage_attempt": 0, "task_id": None, "gate_proposal_id": None,
                     "bootstrap_proposal_id": str(proposal["id"]),
                     "bindings": {item["key"]: item["entity_id"] for item in proposal["plan"]["entity_mapping"]},
                     "relation_ids": [item["id"] for item in proposal["plan"]["operations"] if item["op"] == "create_relationship"],
                     "completed_stages": [], "knowledge_versions": [], "last_execution_id": None}
            self._event(conn, {"id": workflow_id}, "DRAFT", "created", request.actor, "仅生成流程草稿和待审初始化提案", state)
            return self._get(conn, workflow_id)

    @staticmethod
    def _versions(operations):
        return {( "entity" if op["op"].endswith("entity") else "relationship", op["id"]): op["version"] for op in operations}

    @staticmethod
    def _assert_versions(conn, baselines, accepted=None):
        expected = {(item["table"], item["id"]): item["version"] for item in baselines}
        expected.update(accepted or {})
        for (table, record_id), version in expected.items():
            if KnowledgeService._get(conn, table, UUID(record_id))["version"] != version:
                raise KnowledgeError("流程基线已变化，请复核后显式重试；不能沿用旧门禁", 409)
        return [{"table": table, "id": record_id, "version": version} for (table, record_id), version in expected.items()]

    @staticmethod
    def _records(conn, state):
        return {key: KnowledgeService._get(conn, "entity", UUID(record_id)) for key, record_id in state["bindings"].items()}

    def _new_task(self, conn, workflow, state, actor):
        records = self._records(conn, state)
        stage = STAGES[state["stage"]]
        dependencies = [UUID(state["completed_stages"][-1]["task_id"])] if state["completed_stages"] else []
        refs = [{"id": str(item["id"]), "version": item["version"]} for item in records.values()]
        for ref in workflow["definition"]["input_knowledge"]:
            if ref not in refs:
                refs.append(ref)
        runtime = AgentRuntime(self.database_url, connection=conn)
        task = runtime.create_task(TaskCreate(project_id=workflow["project_id"], title=stage["title"] + "：" + workflow["title"],
            goal=workflow["definition"]["goal"], required_capability=stage["capability"], acceptance_criteria=[stage["acceptance"]],
            input_knowledge=[EvidenceRef(**ref) for ref in refs], dependencies=dependencies, actor=actor))
        task = runtime.control(task["id"], TaskControl(expected_version=task["version"], actor=actor, reason="前序知识已显式确认，生成本阶段最新版本任务"), "ready")
        state.update(task_id=str(task["id"]), gate_proposal_id=None, stage_attempt=state["stage_attempt"] + 1, last_execution_id=None)
        state.pop("gate_mode", None)
        state.pop("gate_execution_id", None)
        conn.execute("INSERT INTO workflow_stage_task (workflow_id,project_id,stage,attempt,task_id) VALUES (%s,%s,%s,%s,%s)",
                     (workflow["id"], workflow["project_id"], state["stage"], state["stage_attempt"], task["id"]))

    def _quality(self, conn, workflow, state, task):
        stage = STAGES[state["stage"]]
        if not task["runtime"]["proposal_id"]:
            raise KnowledgeError("阶段缺少已提交的知识产物提案")
        proposal = KnowledgeOperations._proposal(conn, UUID(task["runtime"]["proposal_id"]))
        expected_ids = {state["bindings"][key] for key in stage["outputs"]}
        operations = proposal["result"].get("operations", [])
        if (proposal["status"] != "committed" or {op["id"] for op in operations} != expected_ids
                or any(op["op"] != "update_entity" for op in operations)):
            raise KnowledgeError("阶段产物必须恰好修订模板声明的输出，且已通过提案显式提交")
        baselines = self._assert_versions(conn, state["knowledge_versions"], self._versions(operations))
        for ref in workflow["definition"]["input_knowledge"]:
            if KnowledgeService._get(conn, "entity", UUID(ref["id"]))["version"] != ref["version"]:
                raise KnowledgeError("额外输入知识已陈旧，请创建使用明确新输入的流程", 409)
            baselines.append({"table": "entity", **ref})
        records = self._records(conn, state)
        artifacts = task["runtime"]["result"]["artifacts"]
        if artifacts.get("stage") != stage["key"] or artifacts.get("scenario") != workflow["definition"]["scenario"]:
            raise KnowledgeError("阶段报告缺少匹配的阶段与参考场景标识")
        build = build_reference(workflow["definition"]["scenario"])
        if state["stage"] == 0:
            if not records["requirement"]["attributes"]["acceptance_criteria"]:
                raise KnowledgeError("需求阶段必须形成明确的验收条件")
        elif state["stage"] == 1:
            if (not records["architecture"]["attributes"]["responsibility"] or not records["interface"]["attributes"]["protocol"]
                    or not records["data"]["attributes"]["schema_definition"]):
                raise KnowledgeError("架构阶段必须给出组件职责、接口协议和数据结构")
        elif state["stage"] == 2:
            code, evidence = records["code"], records["code-evidence"]
            attrs = code["attributes"]
            source = attrs["extensions"].get("source")
            expected_sha = build["sha256"]["order_service.py"]
            if (source != build["files"]["order_service.py"] or attrs["revision"] != expected_sha
                    or attrs["extensions"].get("sha256") != expected_sha or _digest(source) != expected_sha):
                raise KnowledgeError("代码内容与当前可信参考模板或摘要不一致")
            ea = evidence["attributes"]
            if (ea["category"] != "code" or ea["subject_id"] != str(code["id"]) or ea["subject_version"] != code["version"]
                    or ea["sha256"] != expected_sha):
                raise KnowledgeError("实现证据必须固定到当前代码版本和内容摘要")
        else:
            execution_id = artifacts.get("execution_id")
            try:
                execution_id = UUID(str(execution_id))
            except ValueError as exc:
                raise KnowledgeError("验证阶段必须引用服务端实际执行记录") from exc
            execution = conn.execute("SELECT * FROM workflow_execution WHERE id=%s AND workflow_id=%s AND task_id=%s",
                                     (execution_id, workflow["id"], task["id"])).fetchone()
            if not execution or not execution["document"]["passed"] or not execution["document"]["executed"]:
                raise KnowledgeError("没有属于本阶段且真实通过的可信执行记录")
            code, test, evidence = records["code"], records["test"], records["test-evidence"]
            source = code["attributes"]["extensions"].get("source", "")
            test_source = test["attributes"]["extensions"].get("test_source", "")
            ea = evidence["attributes"]
            if (code["id"] != execution["code_id"] or code["version"] != execution["code_version"]
                    or _digest(source) != execution["code_sha256"] or test_source != build["files"]["test_order_service.py"]
                    or _digest(test_source) != execution["test_sha256"]
                    or ea["category"] != "test_execution" or ea["outcome"] != "passed"
                    or ea["subject_id"] != str(test["id"]) or ea["subject_version"] != test["version"]
                    or ea["extensions"].get("execution_id") != str(execution_id)
                    or ea["revision"] != str(execution_id) or ea["uri"] != "urn:workflow:execution:" + str(execution_id)
                    or ea["extensions"].get("code_sha256") != execution["code_sha256"]
                    or ea["extensions"].get("test_sha256") != execution["test_sha256"]):
                raise KnowledgeError("测试内容、代码版本或测试证据与真实执行记录不一致")
            state["gate_execution_id"] = str(execution_id)
        return records, baselines

    def _gate(self, conn, workflow, state, task, request):
        records, baselines = self._quality(conn, workflow, state, task)
        stage = STAGES[state["stage"]]
        promoted = set(stage["outputs"])
        versions = {key: item["version"] + int(key in promoted) for key, item in records.items()}
        provenance = {"actor": request.actor, "source_uri": "urn:workflow:" + str(workflow["id"]) + ":gate",
                      "source_revision": str(task["id"]), "change_reason": request.reason, "nature": "human_confirmed"}
        changes = []
        for key in stage["outputs"]:
            record = records[key]
            data = {field: deepcopy(record[field]) for field in ("name", "description", "status", "attributes", "evidence")}
            data.update(expected_version=record["version"], status="active", provenance=provenance)
            if key == "code-evidence":
                data["attributes"]["subject_version"] = versions["code"]
            elif key == "test-evidence":
                data["attributes"]["subject_version"] = versions["test"]
            changes.append({"op": "update_entity", "id": str(record["id"]), "data": data})
        by_id = {str(record["id"]): key for key, record in records.items()}
        for relation_id in state["relation_ids"]:
            relation = KnowledgeService._get(conn, "relationship", UUID(relation_id))
            source_key, target_key = by_id[str(relation["source_id"])], by_id[str(relation["target_id"])]
            data = {field: deepcopy(relation[field]) for field in ("source_version", "target_version", "status", "rationale", "evidence")}
            data.update(expected_version=relation["version"], source_version=versions[source_key], target_version=versions[target_key], provenance=provenance)
            if state["stage"] == 3 and relation["type"] in {"IMPLEMENTED_BY", "VERIFIED_BY"}:
                evidence_key = "code-evidence" if relation["type"] == "IMPLEMENTED_BY" else "test-evidence"
                data.update(status="verified", evidence=[{"id": str(records[evidence_key]["id"]), "version": versions[evidence_key]}])
            if (data["source_version"] != relation["source_version"] or data["target_version"] != relation["target_version"]
                    or data["status"] != relation["status"]):
                changes.append({"op": "update_relationship", "id": relation_id, "data": data})
        proposal = KnowledgeOperations(self.database_url, connection=conn).create_proposal(ProposalCreate(
            project_id=workflow["project_id"], title="阶段门禁：" + stage["title"] + "／" + workflow["title"], actor=request.actor, changes=changes))
        state.update(gate_proposal_id=str(proposal["id"]), gate_baselines=baselines)
        return proposal

    def _retry_alignment(self, conn, workflow, state, request):
        """已提交的失败产物可能使边引用陈旧；先形成显式修复提案，绝不暗改图谱。"""
        records = self._records(conn, state)
        by_id = {str(record["id"]): record for record in records.values()}
        baselines = [{"table": "entity", "id": str(item["id"]), "version": item["version"]} for item in records.values()]
        changes = []
        for relation_id in state["relation_ids"]:
            relation = KnowledgeService._get(conn, "relationship", UUID(relation_id))
            baselines.append({"table": "relationship", "id": relation_id, "version": relation["version"]})
            source_version = by_id[str(relation["source_id"])]["version"]
            target_version = by_id[str(relation["target_id"])]["version"]
            if source_version == relation["source_version"] and target_version == relation["target_version"]:
                continue
            data = {key: deepcopy(relation[key]) for key in ("rationale",)}
            data.update(expected_version=relation["version"], source_version=source_version, target_version=target_version,
                        status="proposed", evidence=[], provenance={"actor": request.actor,
                            "source_uri": "urn:workflow:" + str(workflow["id"]) + ":retry", "source_revision": str(workflow["version"]),
                            "change_reason": request.reason, "nature": "human_confirmed"})
            changes.append({"op": "update_relationship", "id": relation_id, "data": data})
        state["knowledge_versions"] = baselines
        if not changes:
            return False
        proposal = KnowledgeOperations(self.database_url, connection=conn).create_proposal(ProposalCreate(
            project_id=workflow["project_id"], title="重试前追溯关系对齐：" + workflow["title"], actor=request.actor, changes=changes))
        state.update(gate_proposal_id=str(proposal["id"]), gate_baselines=baselines, gate_mode="retry_alignment")
        return True

    def control(self, workflow_id: UUID, request: WorkflowControl, action: str):
        with self._transaction() as conn:
            workflow = self._get(conn, workflow_id)
            KnowledgeService._lock(conn, workflow["project_id"])
            KnowledgeService._assert_project(conn, workflow["project_id"])
            workflow = self._get(conn, workflow_id)
            AgentRuntime._version(workflow, request.expected_version)
            if workflow["status"] in {"COMPLETED", "CANCELLED"}:
                raise KnowledgeError("流程已处于终态", 409)
            state = deepcopy(workflow["state"])
            runtime = AgentRuntime(self.database_url, connection=conn)
            status = workflow["status"]
            if action == "start":
                if status != "DRAFT":
                    raise KnowledgeError("只有草稿流程可以启动", 409)
                proposal = KnowledgeOperations._proposal(conn, UUID(state["bootstrap_proposal_id"]))
                if proposal["status"] != "committed":
                    raise KnowledgeError("请先明确审阅并提交初始化知识提案", 409)
                baselines = [{"table": table, "id": record_id, "version": version} for (table, record_id), version in self._versions(proposal["result"]["operations"]).items()]
                state["knowledge_versions"] = self._assert_versions(conn, baselines)
                self._new_task(conn, workflow, state, request.actor)
                status = "RUNNING"
            elif action == "pause":
                if status not in {"RUNNING", "AWAITING_GATE", "BLOCKED"}:
                    raise KnowledgeError("当前流程状态不能暂停", 409)
                state["resume_status"] = status
                status = "PAUSED"
            elif action == "resume":
                if status != "PAUSED":
                    raise KnowledgeError("只有暂停中的流程可以恢复", 409)
                status = state.pop("resume_status")
            elif action == "cancel":
                if state["task_id"]:
                    task = runtime._task(conn, UUID(state["task_id"]))
                    if task["status"] not in TERMINAL:
                        runtime.control(task["id"], TaskControl(expected_version=task["version"], actor=request.actor, reason=request.reason), "cancel")
                status = "CANCELLED"
            elif action == "prepare_gate":
                if status != "RUNNING":
                    raise KnowledgeError("只有运行中的阶段可以生成门禁提案", 409)
                task = runtime._task(conn, UUID(state["task_id"]))
                if task["status"] in {"FAILED", "CANCELLED", "BLOCKED", "NEEDS_HUMAN_REVIEW"}:
                    state["diagnostic"] = "阶段任务未能完成，需要显式复核或重试。"
                    status = "BLOCKED"
                elif task["status"] != "COMPLETED":
                    raise KnowledgeError("阶段任务尚未完成，不能生成门禁提案", 409)
                else:
                    try:
                        with conn.transaction():
                            self._gate(conn, workflow, state, task, request)
                        status = "AWAITING_GATE"
                    except KnowledgeError as exc:
                        state["diagnostic"] = exc.message
                        status = "BLOCKED"
            elif action == "advance":
                if status != "AWAITING_GATE":
                    raise KnowledgeError("流程需要先形成待确认的阶段门禁", 409)
                proposal = KnowledgeOperations._proposal(conn, UUID(state["gate_proposal_id"]))
                if proposal["status"] != "committed":
                    raise KnowledgeError("阶段门禁提案尚未显式提交，不能推进", 409)
                state["knowledge_versions"] = self._assert_versions(conn, state["gate_baselines"], self._versions(proposal["result"]["operations"]))
                if state.pop("gate_mode", None) == "retry_alignment":
                    self._new_task(conn, workflow, state, request.actor)
                    state.pop("diagnostic", None)
                    status = "RUNNING"
                else:
                    state["completed_stages"].append({"stage": state["stage"], "key": STAGES[state["stage"]]["key"],
                        "task_id": state["task_id"], "gate_proposal_id": state["gate_proposal_id"], "execution_id": state.get("gate_execution_id")})
                    if state["stage"] == len(STAGES) - 1:
                        status = "COMPLETED"
                    else:
                        state.update(stage=state["stage"] + 1, stage_attempt=0)
                        self._new_task(conn, workflow, state, request.actor)
                        status = "RUNNING"
            elif action == "retry":
                if status not in {"BLOCKED", "AWAITING_GATE"}:
                    raise KnowledgeError("只有阻塞或等待门禁的阶段可以显式重建任务", 409)
                if state["stage_attempt"] > workflow["definition"]["max_stage_retries"]:
                    raise KnowledgeError("该阶段已达到人工重试次数上限", 409)
                if state["gate_proposal_id"]:
                    operations = KnowledgeOperations(self.database_url, connection=conn)
                    gate = operations._proposal(conn, UUID(state["gate_proposal_id"]))
                    if gate["status"] in {"pending", "validated"}:
                        operations.review(gate["id"], ProposalReview(expected_version=gate["version"], actor=request.actor,
                                          reason="流程显式重试，终止旧门禁：" + request.reason), "reject")
                old = runtime._task(conn, UUID(state["task_id"]))
                if old["status"] not in TERMINAL:
                    runtime.control(old["id"], TaskControl(expected_version=old["version"], actor=request.actor, reason=request.reason), "cancel")
                if self._retry_alignment(conn, workflow, state, request):
                    status = "AWAITING_GATE"
                else:
                    self._new_task(conn, workflow, state, request.actor)
                    state.pop("diagnostic", None)
                    status = "RUNNING"
            else:
                raise KnowledgeError("不支持的流程控制操作")
            self._event(conn, workflow, status, action, request.actor, request.reason, state)
            return self._get(conn, workflow_id)

    def execute_reference(self, workflow_id: UUID, request: ReferenceExecutionRequest):
        with self._transaction() as conn:
            workflow = self._get(conn, workflow_id)
            KnowledgeService._lock(conn, workflow["project_id"])
            KnowledgeService._assert_project(conn, workflow["project_id"])
            workflow = self._get(conn, workflow_id)
            state = deepcopy(workflow["state"])
            if workflow["status"] != "RUNNING" or state["stage"] != 3:
                raise KnowledgeError("仅运行中的测试验证阶段可以调用可信参考执行器", 409)
            task = AgentRuntime._task(conn, UUID(state["task_id"]))
            runtime = AgentRuntime(self.database_url, connection=conn)
            runtime._lease(conn, task, request)
            package_id = UUID(task["runtime"]["context_id"])
            if not runtime.context.freshness(conn, package_id)["fresh"]:
                raise KnowledgeError("测试执行前上下文已陈旧", 409)
            code = KnowledgeService._get(conn, "entity", UUID(state["bindings"]["code"]))
            build = build_reference(workflow["definition"]["scenario"])
            if code["attributes"]["extensions"].get("source") != build["files"]["order_service.py"]:
                raise KnowledgeError("当前代码不是受信任的内置参考实现，拒绝执行")
            report = run_reference(workflow["definition"]["scenario"])
            runtime._lease(conn, task, request)
            execution_id = uuid4()
            conn.execute("""INSERT INTO workflow_execution
                (id,workflow_id,project_id,task_id,code_id,code_version,code_sha256,test_sha256,document)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (execution_id, workflow_id, workflow["project_id"], task["id"],
                    code["id"], code["version"], report["sha256"]["order_service.py"], report["sha256"]["test_order_service.py"], Jsonb(report)))
            state["last_execution_id"] = str(execution_id)
            self._event(conn, workflow, "RUNNING", "reference_executed", "agent:" + str(request.agent_id), "实际执行固定可信参考测试并保存内容摘要", state)
            return conn.execute("SELECT * FROM workflow_execution WHERE id=%s", (execution_id,)).fetchone()

    def get(self, workflow_id: UUID):
        with self._transaction() as conn:
            return self._get(conn, workflow_id)

    def list(self, project_id: UUID, status=None, limit=50, offset=0):
        KnowledgeService._page(limit, offset)
        with self._transaction() as conn:
            return conn.execute("""SELECT * FROM current_workflows WHERE project_id=%s AND (%s::text IS NULL OR status=%s)
                ORDER BY created_at,id LIMIT %s OFFSET %s""", (project_id, status, status, limit, offset)).fetchall()

    def history(self, workflow_id: UUID, limit=50, offset=0):
        KnowledgeService._page(limit, offset)
        with self._transaction() as conn:
            self._get(conn, workflow_id)
            return conn.execute("SELECT * FROM workflow_event WHERE workflow_id=%s ORDER BY version DESC LIMIT %s OFFSET %s", (workflow_id, limit, offset)).fetchall()

    def tasks(self, workflow_id: UUID):
        with self._transaction() as conn:
            self._get(conn, workflow_id)
            rows = conn.execute("""SELECT t.*,w.stage,w.attempt AS stage_attempt FROM workflow_stage_task w
                JOIN current_runtime_tasks t ON t.id=w.task_id WHERE w.workflow_id=%s ORDER BY w.stage,w.attempt""", (workflow_id,)).fetchall()
            return [AgentRuntime._public(row) for row in rows]

    def executions(self, workflow_id: UUID):
        with self._transaction() as conn:
            self._get(conn, workflow_id)
            return conn.execute("SELECT * FROM workflow_execution WHERE workflow_id=%s ORDER BY recorded_at,id", (workflow_id,)).fetchall()
