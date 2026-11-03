"""持久化的本地 Agent 协作运行时；不执行任意脚本或自动提交工程知识。"""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import secrets
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .context import ContextBuilder
from .db import connect
from .models import EvidenceRef, Kind
from .operations import KnowledgeOperations
from .operations_models import ProposalCreate, ProposalReview
from .runtime_models import AgentCreate, AgentStateChange, ClaimRequest, LeaseRequest, TaskControl, TaskCreate, TaskReport
from .semantics import KnowledgeError
from .service import KnowledgeService


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


class AgentRuntime:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.context = ContextBuilder(database_url)

    @staticmethod
    def _now(conn):
        return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]

    @staticmethod
    def _agent(conn, agent_id):
        record = conn.execute("SELECT * FROM current_runtime_agents WHERE id=%s", (agent_id,)).fetchone()
        if record is None:
            raise KnowledgeError("未找到指定 Agent", 404)
        return record

    @staticmethod
    def _task(conn, task_id):
        record = conn.execute("SELECT * FROM current_runtime_tasks WHERE id=%s", (task_id,)).fetchone()
        if record is None:
            raise KnowledgeError("未找到指定任务", 404)
        return record

    @staticmethod
    def _public(record):
        result = deepcopy(record)
        if "runtime" in result:
            result["runtime"].pop("lease_token_hash", None)
        if "document" in result:
            result["document"].pop("lease_token_hash", None)
        return result

    @staticmethod
    def _version(record, expected):
        if record["version"] != expected:
            raise KnowledgeError("expected_version 已过期，请读取最新任务或 Agent 版本", 409)

    @staticmethod
    def _event(conn, task, status, action, actor, reason, runtime):
        conn.execute("""INSERT INTO runtime_task_event
            (task_id,version,status,action,actor,reason,document) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (task["id"], task.get("version", 0) + 1, status, action, actor, reason, Jsonb(runtime)))

    def register_agent(self, request: AgentCreate):
        with connect(self.database_url) as conn:
            KnowledgeService._lock(conn, request.project_id)
            KnowledgeService._assert_project(conn, request.project_id)
            if conn.execute("SELECT 1 FROM runtime_agent WHERE project_id=%s AND name=%s", (request.project_id, request.name)).fetchone():
                raise KnowledgeError("同一项目内 Agent 名称已存在", 409)
            agent_id = uuid4()
            data = request.model_dump(mode="json")
            conn.execute("""INSERT INTO runtime_agent (id,project_id,name,role,capabilities,read_kinds,write_kinds)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""", (agent_id, request.project_id, request.name, request.role.value,
                    Jsonb(data["capabilities"]), Jsonb(data["read_kinds"]), Jsonb(data["write_kinds"])))
            conn.execute("INSERT INTO runtime_agent_event (agent_id,version,status,actor,reason) VALUES (%s,1,'active',%s,%s)",
                         (agent_id, request.actor, "注册 Agent 的职责、能力及知识范围"))
            return self._agent(conn, agent_id)

    def change_agent(self, agent_id: UUID, request: AgentStateChange):
        with connect(self.database_url) as conn:
            agent = self._agent(conn, agent_id)
            KnowledgeService._lock(conn, agent["project_id"])
            KnowledgeService._assert_project(conn, agent["project_id"])
            agent = self._agent(conn, agent_id)
            self._version(agent, request.expected_version)
            conn.execute("INSERT INTO runtime_agent_event (agent_id,version,status,actor,reason) VALUES (%s,%s,%s,%s,%s)",
                         (agent_id, agent["version"] + 1, request.status, request.actor, request.reason))
            return self._agent(conn, agent_id)

    def list_agents(self, project_id: UUID, limit=50, offset=0):
        KnowledgeService._page(limit, offset)
        with connect(self.database_url) as conn:
            return conn.execute("SELECT * FROM current_runtime_agents WHERE project_id=%s ORDER BY created_at,id LIMIT %s OFFSET %s",
                                (project_id, limit, offset)).fetchall()

    def create_task(self, request: TaskCreate):
        with connect(self.database_url) as conn:
            KnowledgeService._lock(conn, request.project_id)
            KnowledgeService._assert_project(conn, request.project_id)
            for ref in request.input_knowledge:
                record = KnowledgeService._get(conn, "entity", ref.id, ref.version)
                if record["project_id"] != request.project_id:
                    raise KnowledgeError("任务输入知识必须属于同一项目")
            for dependency in request.dependencies:
                if self._task(conn, dependency)["project_id"] != request.project_id:
                    raise KnowledgeError("前置任务必须属于同一项目")
            task_id = uuid4()
            conn.execute("INSERT INTO runtime_task (id,project_id,title,required_capability,document) VALUES (%s,%s,%s,%s,%s)",
                         (task_id, request.project_id, request.title, request.required_capability, Jsonb(request.model_dump(mode="json"))))
            self._event(conn, {"id": task_id}, "CREATED", "created", request.actor, "创建具有明确目标和验收条件的任务",
                        {"attempt": 0, "agent_id": None, "lease_expires_at": None, "lease_token_hash": None,
                         "context_id": None, "proposal_id": None, "result": None})
            return self._public(self._task(conn, task_id))

    def _lease(self, conn, task, request):
        self._version(task, request.expected_version)
        agent = self._agent(conn, request.agent_id)
        if agent["project_id"] != task["project_id"] or agent["status"] != "active":
            raise KnowledgeError("Agent 不属于该项目或已被禁用", 409)
        runtime = task["runtime"]
        token_hash = hashlib.sha256(str(request.lease_token).encode()).hexdigest()
        if (task["status"] != "RUNNING" or runtime["agent_id"] != str(request.agent_id)
                or not secrets.compare_digest(runtime.get("lease_token_hash") or "", token_hash)
                or datetime.fromisoformat(runtime["lease_expires_at"]) <= self._now(conn)):
            raise KnowledgeError("任务租约无效、已过期或已由新的执行尝试接管", 409)
        return agent

    def claim(self, request: ClaimRequest):
        with connect(self.database_url) as conn:
            agent = self._agent(conn, request.agent_id)
            KnowledgeService._lock(conn, agent["project_id"])
            KnowledgeService._assert_project(conn, agent["project_id"])
            agent = self._agent(conn, request.agent_id)
            if agent["status"] != "active":
                raise KnowledgeError("Agent 已被禁用，不能领取任务", 409)
            now = self._now(conn)
            candidates = conn.execute("""SELECT t.* FROM current_runtime_tasks t WHERE t.project_id=%s
                AND t.required_capability=ANY(%s::text[])
                AND (%s::uuid IS NULL OR t.id=%s)
                AND (t.status='READY' OR (t.status='RUNNING' AND (t.runtime->>'lease_expires_at')::timestamptz<=%s))
                AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(t.definition->'dependencies') d
                    JOIN current_runtime_tasks p ON p.id=d.value::uuid WHERE p.status<>'COMPLETED')
                AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(t.definition->'input_knowledge') p
                    JOIN entity e ON e.id=(p->>'id')::uuid WHERE NOT(e.kind=ANY(%s::text[])))
                ORDER BY t.created_at,t.id LIMIT 100""",
                (agent["project_id"], agent["capabilities"], request.task_id, request.task_id, now, agent["read_kinds"])).fetchall()
            for task in candidates:
                state = deepcopy(task["runtime"])
                if state["attempt"] >= task["definition"]["max_attempts"]:
                    state.update(lease_token_hash=None, lease_expires_at=None)
                    self._event(conn, task, "FAILED", "attempts_exhausted", "runtime", "租约过期且已达到最大执行次数", state)
                    continue
                try:
                    with conn.transaction():
                        package = self.context.build(conn, task["project_id"], task["id"],
                            [EvidenceRef(**ref) for ref in task["definition"]["input_knowledge"]], [Kind(k) for k in agent["read_kinds"]])
                        freshness = self.context.freshness(conn, package["id"])
                except KnowledgeError as exc:
                    state.update(lease_token_hash=None, lease_expires_at=None, context_id=None,
                                 review={"valid": False, "message": exc.message})
                    self._event(conn, task, "NEEDS_HUMAN_REVIEW", "context_failed", "runtime", "上下文组装失败，请人工处理；其余任务可继续领取", state)
                    continue
                state.update(context_id=str(package["id"]), agent_id=str(agent["id"]), lease_token_hash=None, lease_expires_at=None)
                if not freshness["fresh"]:
                    state["review"] = freshness
                    self._event(conn, task, "NEEDS_HUMAN_REVIEW", "stale_context", "runtime", "输入上下文已陈旧，请复核后创建新任务或修订输入", state)
                    continue
                token = uuid4()
                state.update(attempt=state["attempt"] + 1, lease_token_hash=hashlib.sha256(str(token).encode()).hexdigest(),
                             lease_expires_at=(self._now(conn) + timedelta(seconds=request.lease_seconds)).isoformat(),
                             lease_seconds=request.lease_seconds, proposal_id=None, result=None)
                state.pop("review", None)
                self._event(conn, task, "RUNNING", "claimed", str(agent["id"]), "能力匹配并领取具有期限的执行租约", state)
                return {"task": self._public(self._task(conn, task["id"])), "lease_token": str(token)}
            return {"task": None, "lease_token": None}

    def heartbeat(self, task_id: UUID, request: LeaseRequest):
        with connect(self.database_url) as conn:
            task = self._task(conn, task_id)
            KnowledgeService._lock(conn, task["project_id"])
            KnowledgeService._assert_project(conn, task["project_id"])
            task = self._task(conn, task_id)
            self._lease(conn, task, request)
            state = deepcopy(task["runtime"])
            state["lease_expires_at"] = (self._now(conn) + timedelta(seconds=state["lease_seconds"])).isoformat()
            self._event(conn, task, "RUNNING", "heartbeat", str(request.agent_id), "续期执行租约", state)
            return self._public(self._task(conn, task_id))

    @staticmethod
    def _known_evidence(document):
        known = {(e["id"], e["version"]) for e in document["entities"] if e["kind"] == "Evidence"}
        for record in document["entities"] + document["relationships"]:
            known.update((ref["id"], ref["version"]) for ref in record["evidence"])
        return known

    def _check_changes(self, conn, task, agent, changes, document):
        entities = {(str(e["id"]), e["version"]): e for e in document["entities"]}
        relations = {(str(r["id"]), r["version"]): r for r in document["relationships"]}
        known_evidence = self._known_evidence(document)
        revised_entities = {str(change.id): change.data.expected_version + 1 for change in changes if change.op == "update_entity"}
        for change in changes:
            if any((str(ref.id), ref.version) not in known_evidence for ref in change.data.evidence):
                raise KnowledgeError("Agent 只能引用上下文中已记录的证据版本")
            if change.op == "update_entity":
                record = entities.get((str(change.id), change.data.expected_version))
                if record is None or record["kind"] not in agent["write_kinds"]:
                    raise KnowledgeError("Agent 只能修订上下文中明确允许写入的实体版本")
            else:
                record = relations.get((str(change.id), change.data.expected_version))
                if record is None:
                    raise KnowledgeError("Agent 只能修订上下文内的关系版本")
                for endpoint in ("source", "target"):
                    target = KnowledgeService._get(conn, "entity", UUID(str(record[endpoint + "_id"])), record[endpoint + "_version"])
                    if target["kind"] not in agent["write_kinds"]:
                        raise KnowledgeError("修订关系两端的实体类型都必须在 Agent 的可写范围内")
                    proposed_version = getattr(change.data, endpoint + "_version")
                    if proposed_version not in {record[endpoint + "_version"], revised_entities.get(str(record[endpoint + "_id"]))}:
                        raise KnowledgeError("关系只能引用上下文版本或本提案中明确产生的实体版本")
            change.data.provenance = change.data.provenance.model_copy(update={"nature": "inference", "actor": "agent:" + str(agent["id"])})

    def report(self, task_id: UUID, request: TaskReport):
        with connect(self.database_url) as conn:
            task = self._task(conn, task_id)
            KnowledgeService._lock(conn, task["project_id"])
            KnowledgeService._assert_project(conn, task["project_id"])
            task = self._task(conn, task_id)
            agent = self._lease(conn, task, request)
            state = deepcopy(task["runtime"])
            package = conn.execute("SELECT * FROM context_package WHERE id=%s", (UUID(state["context_id"]),)).fetchone()
            known_evidence = self._known_evidence(package["document"])
            freshness = self.context.freshness(conn, UUID(state["context_id"]))
            if not freshness["fresh"]:
                state.update(review=freshness, lease_token_hash=None, lease_expires_at=None)
                self._event(conn, task, "NEEDS_HUMAN_REVIEW", "stale_context", str(agent["id"]), "结果提交前上下文已变化，拒绝按旧知识提出变更", state)
                return self._public(self._task(conn, task_id))
            if any(item.criterion_index >= len(task["definition"]["acceptance_criteria"]) for item in request.acceptance):
                raise KnowledgeError("验收条件序号超出任务范围")
            for item in request.acceptance:
                if any((str(ref.id), ref.version) not in known_evidence for ref in item.evidence):
                    raise KnowledgeError("验收声明只能引用上下文中已记录的证据版本")
                KnowledgeService._check_evidence(conn, task["project_id"], item.evidence)
            try:
                artifact_size = len(json.dumps(request.artifacts, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            except (ValueError, UnicodeEncodeError) as exc:
                raise KnowledgeError("产物必须是有效且只含有限数值的 UTF-8 JSON") from exc
            if artifact_size > 262144:
                raise KnowledgeError("单次结构化产物不能超过 256 KiB")
            state["result"] = request.model_dump(mode="json", exclude={"agent_id", "lease_token", "expected_version", "changes"})
            state.update(lease_token_hash=None, lease_expires_at=None)
            if request.outcome == "succeeded":
                if request.changes:
                    changes = deepcopy(request.changes)
                    self._check_changes(conn, task, agent, changes, package["document"])
                    proposal = KnowledgeOperations(self.database_url, connection=conn).create_proposal(ProposalCreate(
                        project_id=task["project_id"], title="Agent 任务提案：" + task["title"][:270], actor="agent:" + str(agent["id"]), changes=changes))
                    state["proposal_id"] = str(proposal["id"])
                status = "PROPOSED"
            elif request.outcome == "failed":
                status = "READY" if state["attempt"] < task["definition"]["max_attempts"] else "FAILED"
            else:
                status = "BLOCKED" if request.outcome == "blocked" else "NEEDS_HUMAN_REVIEW"
            self._event(conn, task, status, "reported", str(agent["id"]), request.summary, state)
            return self._public(self._task(conn, task_id))

    def control(self, task_id: UUID, request: TaskControl, action: str):
        with connect(self.database_url) as conn:
            task = self._task(conn, task_id)
            KnowledgeService._lock(conn, task["project_id"])
            KnowledgeService._assert_project(conn, task["project_id"])
            task = self._task(conn, task_id)
            self._version(task, request.expected_version)
            if task["status"] in TERMINAL:
                raise KnowledgeError("任务已处于终态，不能继续修改", 409)
            state = deepcopy(task["runtime"])
            if action == "ready":
                if task["status"] not in {"CREATED", "BLOCKED", "NEEDS_HUMAN_REVIEW"}:
                    raise KnowledgeError("该任务状态不能重新入队", 409)
                status = "READY"
            elif action == "cancel":
                status = "CANCELLED"
            elif action == "validate":
                if task["status"] != "PROPOSED":
                    raise KnowledgeError("仅已报告产物的任务可以校验", 409)
                freshness = self.context.freshness(conn, UUID(state["context_id"]))
                acceptance = state["result"]["acceptance"]
                valid = (freshness["fresh"] and len(acceptance) == len(task["definition"]["acceptance_criteria"])
                         and all(item["passed"] for item in acceptance))
                review_result = {"valid": valid, "context": freshness, "message": "执行方验收声明需人工确认，不代表外部代码或测试已获独立验证。"}
                if valid and state["proposal_id"]:
                    operations = KnowledgeOperations(self.database_url, connection=conn)
                    proposal = operations._proposal(conn, UUID(state["proposal_id"]))
                    if proposal["status"] == "committed":
                        valid = False
                        review_result["message"] = "任务校验前提案已被其他入口提交，请复核任务上下文。"
                    elif proposal["status"] == "rejected":
                        valid = False
                        review_result["message"] = "知识提案已被拒绝。"
                    else:
                        response = operations.review(proposal["id"], ProposalReview(expected_version=proposal["version"],
                            actor=request.actor, reason=request.reason), "validate")
                        review_result["proposal_validation"] = response["proposal"]["result"]
                        valid = response["proposal"]["result"]["valid"]
                review_result["valid"] = valid
                state["review"] = review_result
                status = "VALIDATING" if valid else "NEEDS_HUMAN_REVIEW"
            elif action == "complete":
                if task["status"] != "VALIDATING":
                    raise KnowledgeError("任务需先通过校验，再显式确认完成", 409)
                accepted = {}
                if state["proposal_id"]:
                    proposal = KnowledgeOperations._proposal(conn, UUID(state["proposal_id"]))
                    if proposal["status"] != "committed":
                        raise KnowledgeError("知识提案尚未显式提交，不能将任务标为完成", 409)
                    for operation in proposal["result"]["operations"]:
                        table = "entity" if operation["op"].endswith("entity") else "relationship"
                        accepted[(table, operation["id"])] = operation["version"]
                freshness = self.context.freshness(conn, UUID(state["context_id"]), accepted_versions=accepted)
                if not freshness["fresh"]:
                    raise KnowledgeError("完成前知识再次发生变化，请复核任务及已提交提案", 409)
                status = "COMPLETED"
            else:
                raise KnowledgeError("不支持的任务控制操作")
            if status != "RUNNING":
                state.update(lease_token_hash=None, lease_expires_at=None)
            self._event(conn, task, status, action, request.actor, request.reason, state)
            return self._public(self._task(conn, task_id))

    def get_task(self, task_id: UUID):
        with connect(self.database_url) as conn:
            return self._public(self._task(conn, task_id))

    def get_agent(self, agent_id: UUID):
        with connect(self.database_url) as conn:
            return self._agent(conn, agent_id)

    def get_context(self, task_id: UUID):
        with connect(self.database_url) as conn:
            task = self._task(conn, task_id)
            if not task["runtime"]["context_id"]:
                raise KnowledgeError("任务尚未领取，没有上下文包", 404)
            package = conn.execute("SELECT * FROM context_package WHERE id=%s", (UUID(task["runtime"]["context_id"]),)).fetchone()
            return package

    def list_tasks(self, project_id: UUID, status=None, limit=50, offset=0):
        KnowledgeService._page(limit, offset)
        with connect(self.database_url) as conn:
            rows = conn.execute("""SELECT * FROM current_runtime_tasks WHERE project_id=%s
                AND (%s::text IS NULL OR status=%s) ORDER BY created_at,id LIMIT %s OFFSET %s""",
                (project_id, status, status, limit, offset)).fetchall()
            return [self._public(row) for row in rows]

    def history(self, task_id: UUID, limit=50, offset=0):
        KnowledgeService._page(limit, offset)
        with connect(self.database_url) as conn:
            self._task(conn, task_id)
            rows = conn.execute("SELECT * FROM runtime_task_event WHERE task_id=%s ORDER BY version DESC LIMIT %s OFFSET %s",
                                (task_id, limit, offset)).fetchall()
            return [self._public(row) for row in rows]

    def events(self, project_id: UUID, after=0, limit=50):
        KnowledgeService._page(limit, 0)
        if after < 0:
            raise KnowledgeError("事件游标不能为负数")
        with connect(self.database_url) as conn:
            # 与项目内写入共享锁，避免尚未提交的较小事件编号被游标越过。
            KnowledgeService._lock(conn, project_id)
            rows = conn.execute("""SELECT e.* FROM runtime_task_event e JOIN runtime_task t ON t.id=e.task_id
                WHERE t.project_id=%s AND e.event_id>%s ORDER BY e.event_id LIMIT %s""",
                (project_id, after, limit + 1)).fetchall()
            events = [self._public(row) for row in rows[:limit]]
            return {"events": events, "next_cursor": events[-1]["event_id"] if events else after, "has_more": len(rows) > limit}
