"""第四阶段流程的真实数据库回归：人工门禁、晚绑定与过程审计。"""
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import psycopg
import pytest

from knowledge.db import connect
from knowledge.models import Kind
from knowledge.operations import KnowledgeOperations
from knowledge.operations_models import ProposalReview
from knowledge.reference_runner import build_reference
from knowledge.runtime import AgentRuntime
from knowledge.runtime_models import AgentCreate, ClaimRequest, TaskControl, TaskReport
from knowledge.semantics import KnowledgeError
from knowledge.workflow import WorkflowService
from knowledge.workflow_models import ReferenceExecutionRequest, STAGES, WorkflowControl, WorkflowCreate


pytestmark = pytest.mark.integration


def identifier(value):
    return UUID(str(value))


def action_input(workflow):
    return WorkflowControl(expected_version=workflow["version"], actor="合成流程审核者", reason="明确审查本参考流程的阶段状态")


def task_control(task):
    return TaskControl(expected_version=task["version"], actor="合成任务审核者", reason="审阅合成阶段任务的结果")


def commit(operations, proposal_id):
    proposal = operations.get_proposal(identifier(proposal_id))
    checked = operations.review(identifier(proposal_id), ProposalReview(expected_version=proposal["version"],
        actor="合成提案审核者", reason="事务试运行并核对候选知识"), "validate")["proposal"]
    assert checked["result"]["valid"] is True, checked["result"]
    return operations.review(identifier(proposal_id), ProposalReview(expected_version=checked["version"],
        actor="合成提案审核者", reason="显式批准该参考知识提案"), "commit")["proposal"]


@pytest.fixture
def workflows(db_url):
    return WorkflowService(db_url)


@pytest.fixture
def runtime(db_url):
    return AgentRuntime(db_url)


@pytest.fixture
def operations(db_url):
    return KnowledgeOperations(db_url)


@pytest.fixture
def create_workflow(workflows, project):
    def create(**changes):
        data = {"project_id": project["id"], "title": "订单取消参考流程", "goal": "实际验证合成流程闭环",
                "scenario": "order_cancellation_v1", "actor": "合成流程创建者", **changes}
        return workflows.create(WorkflowCreate(**data))
    return create


@pytest.fixture
def started(workflows, operations, create_workflow):
    workflow = create_workflow()
    commit(operations, workflow["state"]["bootstrap_proposal_id"])
    return workflows.control(identifier(workflow["id"]), action_input(workflow), "start")


@pytest.fixture
def register_agent(runtime, project):
    def create(capability="sdlc.requirements", role="Product"):
        return runtime.register_agent(AgentCreate(project_id=project["id"], name="流程执行者-" + uuid4().hex,
            role=role, capabilities=[capability], read_kinds=list(Kind),
            write_kinds=[kind for kind in Kind if kind != Kind.PROJECT], actor="合成注册者"))
    return create


def claim(runtime, workflow, agent):
    leased = runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=workflow["state"]["task_id"]))
    assert leased["task"] is not None
    return leased


def finish_readonly(runtime, leased):
    task = leased["task"]
    proposed = runtime.report(identifier(task["id"]), TaskReport(agent_id=task["runtime"]["agent_id"],
        lease_token=leased["lease_token"], expected_version=task["version"], outcome="succeeded",
        summary="合成执行方声称阶段已完成，但不增加真实知识产物", acceptance=[{"criterion_index": 0, "passed": True}],
        artifacts={"stage": "requirements", "scenario": "order_cancellation_v1", "synthetic": True}))
    checked = runtime.control(identifier(task["id"]), task_control(proposed), "validate")
    assert checked["status"] == "VALIDATING"
    return runtime.control(identifier(task["id"]), task_control(checked), "complete")


def finish_stage_task(workflows, runtime, operations, workflow, register_agent, *, execute=True, modify_report=None, execution_override=None):
    from knowledge.workflow_adapter import WorkflowReferenceAdapter

    workflow = workflows.get(identifier(workflow["id"]))
    stage = workflow["state"]["stage"]
    definition = STAGES[stage]
    agent = register_agent(capability=definition["capability"], role=definition["role"])
    leased = claim(runtime, workflow, agent)
    task = leased["task"]
    package = runtime.get_context(identifier(task["id"]))
    execution = execution_override
    if stage == 3 and execute:
        execution = workflows.execute_reference(identifier(workflow["id"]), ReferenceExecutionRequest(
            agent_id=agent["id"], lease_token=leased["lease_token"], expected_version=task["version"]))
    report = WorkflowReferenceAdapter().build_report(stage, task, package, workflow["state"]["bindings"], execution)
    if modify_report:
        report = modify_report(report)
    proposed = runtime.report(identifier(task["id"]), TaskReport(agent_id=agent["id"], lease_token=leased["lease_token"],
        expected_version=task["version"], **report))
    assert proposed["status"] == "PROPOSED", proposed
    validated = runtime.control(identifier(task["id"]), task_control(proposed), "validate")
    assert validated["status"] == "VALIDATING", validated["runtime"].get("review")
    commit(operations, validated["runtime"]["proposal_id"])
    completed = runtime.control(identifier(task["id"]), task_control(validated), "complete")
    assert completed["status"] == "COMPLETED"
    return workflows.get(identifier(workflow["id"])), completed, execution


def pass_stage(workflows, runtime, operations, workflow, register_agent):
    workflow, task, execution = finish_stage_task(workflows, runtime, operations, workflow, register_agent)
    gated = workflows.control(identifier(workflow["id"]), action_input(workflow), "prepare_gate")
    assert gated["status"] == "AWAITING_GATE", gated["state"].get("diagnostic")
    commit(operations, gated["state"]["gate_proposal_id"])
    advanced = workflows.control(identifier(workflow["id"]), action_input(gated), "advance")
    return advanced, task, execution


def test_creation_only_proposes_scaffold_and_start_requires_explicit_commit(
        workflows, create_workflow, operations, project, db_url):
    workflow = create_workflow()
    assert workflow["status"] == "DRAFT"
    proposal = operations.get_proposal(identifier(workflow["state"]["bootstrap_proposal_id"]))
    assert proposal["status"] == "pending"
    assert len(workflow["state"]["bindings"]) == 8
    with connect(db_url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM entity WHERE project_id=%s", (project["id"],)).fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM workflow_stage_task WHERE workflow_id=%s", (workflow["id"],)).fetchone()["n"] == 0
    with pytest.raises(KnowledgeError) as not_approved:
        workflows.control(identifier(workflow["id"]), action_input(workflow), "start")
    assert not_approved.value.status_code == 409
    assert workflows.get(identifier(workflow["id"]))["version"] == workflow["version"]
    commit(operations, proposal["id"])
    started = workflows.control(identifier(workflow["id"]), action_input(workflow), "start")
    assert started["status"] == "RUNNING"
    assert started["state"]["stage"] == 0
    assert started["state"]["task_id"]
    assert len(workflows.tasks(identifier(workflow["id"]))) == 1


def test_phase_gate_never_accepts_an_unfinished_task(workflows, started):
    with pytest.raises(KnowledgeError) as unfinished:
        workflows.control(identifier(started["id"]), action_input(started), "prepare_gate")
    assert unfinished.value.status_code == 409
    assert workflows.get(identifier(started["id"]))["version"] == started["version"]


def test_worker_success_without_required_outputs_does_not_advance_the_workflow(
        workflows, runtime, started, register_agent):
    finish_readonly(runtime, claim(runtime, started, register_agent()))
    blocked = workflows.control(identifier(started["id"]), action_input(started), "prepare_gate")
    assert blocked["status"] == "BLOCKED"
    assert blocked["state"]["stage"] == 0
    assert len(workflows.tasks(identifier(started["id"]))) == 1


def test_pause_only_freezes_progress_and_resume_restores_current_task(
        workflows, runtime, started, register_agent):
    paused = workflows.control(identifier(started["id"]), action_input(started), "pause")
    assert paused["status"] == "PAUSED"
    assert paused["state"]["task_id"] == started["state"]["task_id"]
    assert claim(runtime, paused, register_agent())["task"]["status"] == "RUNNING"
    with pytest.raises(KnowledgeError) as not_resumed:
        workflows.control(identifier(paused["id"]), action_input(paused), "advance")
    assert not_resumed.value.status_code == 409
    resumed = workflows.control(identifier(paused["id"]), action_input(paused), "resume")
    assert resumed["status"] == "RUNNING"
    assert resumed["state"]["task_id"] == started["state"]["task_id"]


def test_cancel_fences_current_worker_and_is_terminal(workflows, runtime, started, register_agent):
    leased = claim(runtime, started, register_agent())
    cancelled = workflows.control(identifier(started["id"]), action_input(started), "cancel")
    assert cancelled["status"] == "CANCELLED"
    current = runtime.get_task(identifier(started["state"]["task_id"]))
    assert current["status"] == "CANCELLED"
    with pytest.raises(KnowledgeError):
        finish_readonly(runtime, leased)
    with pytest.raises(KnowledgeError) as terminal:
        workflows.control(identifier(cancelled["id"]), action_input(cancelled), "start")
    assert terminal.value.status_code == 409


def test_retry_rebuilds_stage_task_and_preserves_history(workflows, runtime, started, register_agent):
    leased = claim(runtime, started, register_agent())
    task = leased["task"]
    runtime.report(identifier(task["id"]), TaskReport(agent_id=task["runtime"]["agent_id"], lease_token=leased["lease_token"],
        expected_version=task["version"], outcome="blocked", summary="缺少阶段输入，需要人工处理"))
    blocked = workflows.control(identifier(started["id"]), action_input(started), "prepare_gate")
    assert blocked["status"] == "BLOCKED"
    rebuilt = workflows.control(identifier(blocked["id"]), action_input(blocked), "retry")
    assert rebuilt["status"] == "RUNNING"
    assert rebuilt["state"]["task_id"] != started["state"]["task_id"]
    assert len(workflows.tasks(identifier(started["id"]))) == 2
    assert runtime.get_task(identifier(started["state"]["task_id"]))["status"] == "CANCELLED"


def test_concurrent_start_creates_one_stage_task(workflows, operations, create_workflow):
    workflow = create_workflow()
    commit(operations, workflow["state"]["bootstrap_proposal_id"])
    def start(_):
        try:
            workflows.control(identifier(workflow["id"]), action_input(workflow), "start")
            return 200
        except KnowledgeError as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(start, range(2))) == [200, 409]
    assert len(workflows.tasks(identifier(workflow["id"]))) == 1


def test_workflow_list_and_events_are_project_scoped(workflows, create_workflow, project, client, provenance):
    workflow = create_workflow()
    other = client.post("/entities", json={"kind": "Project", "name": "另一个项目", "provenance": provenance}).json()
    assert workflows.list(identifier(project["id"])) == [workflow]
    assert workflows.list(identifier(other["id"])) == []
    assert len(workflows.history(identifier(workflow["id"]))) == 1
    assert workflows.list(identifier(project["id"]), limit=1, offset=1) == []
    with pytest.raises(KnowledgeError) as unknown:
        workflows.get(uuid4())
    assert unknown.value.status_code == 404


def test_workflow_and_event_history_are_append_only(workflows, started, db_url):
    for table, column in [("workflow_run", "id"), ("workflow_event", "workflow_id"), ("workflow_stage_task", "workflow_id")]:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with connect(db_url) as conn:
                conn.execute(f"DELETE FROM {table} WHERE {column}=%s", (started["id"],))
    assert workflows.get(identifier(started["id"])) == started


def test_extra_input_cannot_reference_another_project(create_workflow, client, entity, provenance):
    other = client.post("/entities", json={"kind": "Project", "name": "外部项目", "provenance": provenance}).json()
    external = entity(project_id=other["id"])
    with pytest.raises(KnowledgeError) as cross_project:
        create_workflow(input_knowledge=[{"id": external["id"], "version": 1}])
    assert cross_project.value.status_code == 422


def test_exhausted_workflow_retry_budget_cannot_create_another_stage_task(
        workflows, runtime, create_workflow, operations, register_agent):
    workflow = create_workflow(max_stage_retries=0)
    commit(operations, workflow["state"]["bootstrap_proposal_id"])
    started = workflows.control(identifier(workflow["id"]), action_input(workflow), "start")
    finish_readonly(runtime, claim(runtime, started, register_agent()))
    blocked = workflows.control(identifier(workflow["id"]), action_input(started), "prepare_gate")
    assert blocked["status"] == "BLOCKED"
    with pytest.raises(KnowledgeError) as exhausted:
        workflows.control(identifier(workflow["id"]), action_input(blocked), "retry")
    assert exhausted.value.status_code == 409
    assert len(workflows.tasks(identifier(workflow["id"]))) == 1


def test_workflow_creation_rolls_back_bootstrap_and_workflow_together(
        create_workflow, project, db_url, monkeypatch):
    original_ingest = KnowledgeOperations.ingest
    def fail_after_source_written(self, request):
        original_ingest(self, request)
        raise KnowledgeError("模拟创建流程末尾失败")
    monkeypatch.setattr(KnowledgeOperations, "ingest", fail_after_source_written)
    with pytest.raises(KnowledgeError, match="模拟创建"):
        create_workflow()
    with connect(db_url) as conn:
        for table in ("workflow_run", "knowledge_source", "change_proposal"):
            assert conn.execute(f"SELECT count(*) AS n FROM {table} WHERE project_id=%s", (project["id"],)).fetchone()["n"] == 0


def test_four_stage_reference_workflow_has_real_execution_and_verified_traceability(
        workflows, runtime, operations, started, register_agent, service, db_url):
    current = started
    requirement_id = current["state"]["bindings"]["requirement"]
    original_task = runtime.get_task(identifier(current["state"]["task_id"]))
    original_version = next(ref["version"] for ref in original_task["definition"]["input_knowledge"] if ref["id"] == requirement_id)
    for stage in range(4):
        assert current["state"]["stage"] == stage
        before_requirement = service.get_entity(identifier(requirement_id))
        task = runtime.get_task(identifier(current["state"]["task_id"]))
        assert next(ref["version"] for ref in task["definition"]["input_knowledge"] if ref["id"] == requirement_id) == before_requirement["version"]
        if stage == 1:
            assert before_requirement["version"] > original_version
        current, _, execution = pass_stage(workflows, runtime, operations, current, register_agent)
    assert current["status"] == "COMPLETED"
    assert len(current["state"]["completed_stages"]) == 4
    assert len(workflows.tasks(identifier(current["id"]))) == 4
    assert execution["document"]["executed"] is True
    assert execution["document"]["passed"] is True
    assert execution["document"]["tests_run"] >= 4
    assert len(workflows.executions(identifier(current["id"]))) == 1
    relationships = service.list_relationships(identifier(current["project_id"]), entity_id=identifier(requirement_id))
    trace = {link["type"]: link for link in relationships}
    assert trace["IMPLEMENTED_BY"]["status"] == "verified"
    assert trace["VERIFIED_BY"]["status"] == "verified"
    assert trace["VERIFIED_BY"]["evidence"]
    code = service.get_entity(identifier(current["state"]["bindings"]["code"]))
    test = service.get_entity(identifier(current["state"]["bindings"]["test"]))
    assert trace["IMPLEMENTED_BY"]["target_version"] == code["version"]
    assert trace["VERIFIED_BY"]["target_version"] == test["version"]
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with connect(db_url) as conn:
            conn.execute("DELETE FROM workflow_execution WHERE id=%s", (execution["id"],))


def test_gate_requires_explicit_commit_and_concurrent_advance_creates_one_successor(
        workflows, runtime, operations, started, register_agent):
    workflow, _, _ = finish_stage_task(workflows, runtime, operations, started, register_agent)
    gated = workflows.control(identifier(workflow["id"]), action_input(workflow), "prepare_gate")
    assert gated["status"] == "AWAITING_GATE"
    with pytest.raises(KnowledgeError) as pending_gate:
        workflows.control(identifier(gated["id"]), action_input(gated), "advance")
    assert pending_gate.value.status_code == 409
    assert len(workflows.tasks(identifier(workflow["id"]))) == 1
    commit(operations, gated["state"]["gate_proposal_id"])
    def advance(_):
        try:
            workflows.control(identifier(gated["id"]), action_input(gated), "advance")
            return 200
        except KnowledgeError as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(advance, range(2))) == [200, 409]
    assert len(workflows.tasks(identifier(gated["id"]))) == 2


def test_committed_gate_does_not_hide_unrelated_knowledge_drift(
        workflows, runtime, operations, started, register_agent, client, provenance):
    workflow, _, _ = finish_stage_task(workflows, runtime, operations, started, register_agent)
    gated = workflows.control(identifier(workflow["id"]), action_input(workflow), "prepare_gate")
    commit(operations, gated["state"]["gate_proposal_id"])
    code_id = workflow["state"]["bindings"]["code"]
    code = client.get(f"/entities/{code_id}").json()
    data = {key: code[key] for key in ("name", "description", "status", "attributes", "evidence")}
    data.update(name="门禁后被额外修改的代码资产", expected_version=code["version"], provenance=provenance)
    assert client.put(f"/entities/{code_id}", json=data).status_code == 200
    with pytest.raises(KnowledgeError) as drift:
        workflows.control(identifier(gated["id"]), action_input(gated), "advance")
    assert drift.value.status_code == 409
    assert len(workflows.tasks(identifier(gated["id"]))) == 1


def test_reference_executor_is_unavailable_outside_verification_stage(
        workflows, runtime, started, register_agent):
    leased = claim(runtime, started, register_agent())
    with pytest.raises(KnowledgeError) as wrong_stage:
        workflows.execute_reference(identifier(started["id"]), ReferenceExecutionRequest(agent_id=leased["task"]["runtime"]["agent_id"],
            lease_token=leased["lease_token"], expected_version=leased["task"]["version"]))
    assert wrong_stage.value.status_code == 409
    assert workflows.executions(identifier(started["id"])) == []


def test_worker_cannot_forge_a_passing_test_record_with_text_and_uuid(
        workflows, runtime, operations, started, register_agent):
    current = started
    for _ in range(3):
        current, _, _ = pass_stage(workflows, runtime, operations, current, register_agent)
    build = build_reference()
    forged = {"id": uuid4(), "code_sha256": build["sha256"]["order_service.py"],
              "test_sha256": build["sha256"]["test_order_service.py"],
              "document": {"passed": True, "executed": True, "tests_run": 4}}
    current, _, _ = finish_stage_task(workflows, runtime, operations, current, register_agent,
        execute=False, execution_override=forged)
    blocked = workflows.control(identifier(current["id"]), action_input(current), "prepare_gate")
    assert blocked["status"] == "BLOCKED"
    assert blocked["state"]["stage"] == 3
    assert blocked["state"].get("diagnostic")
    assert len(blocked["state"]["completed_stages"]) == 3
    assert workflows.executions(identifier(current["id"])) == []


def test_changed_code_is_rejected_before_running_the_reference_executor(
        workflows, runtime, operations, started, register_agent, client, provenance, monkeypatch):
    current = started
    for _ in range(3):
        current, _, _ = pass_stage(workflows, runtime, operations, current, register_agent)
    leased = claim(runtime, current, register_agent(capability=STAGES[3]["capability"], role=STAGES[3]["role"]))
    code_id = current["state"]["bindings"]["code"]
    code = client.get(f"/entities/{code_id}").json()
    data = {key: code[key] for key in ("name", "description", "status", "attributes", "evidence")}
    data.update(expected_version=code["version"], provenance=provenance, name="领取后发生修改的代码")
    assert client.put(f"/entities/{code_id}", json=data).status_code == 200
    def must_not_run(_scenario):
        raise AssertionError("陈旧代码不应触发参考执行器")
    monkeypatch.setattr("knowledge.workflow.run_reference", must_not_run)
    with pytest.raises(KnowledgeError) as stale_code:
        workflows.execute_reference(identifier(current["id"]), ReferenceExecutionRequest(
            agent_id=leased["task"]["runtime"]["agent_id"], lease_token=leased["lease_token"],
            expected_version=leased["task"]["version"]))
    assert stale_code.value.status_code == 409
    assert workflows.executions(identifier(current["id"])) == []


def test_retry_after_committed_invalid_output_requires_explicit_trace_alignment(
        workflows, runtime, operations, started, register_agent, db_url):
    def remove_required_acceptance(report):
        report["changes"][0]["data"]["attributes"]["acceptance_criteria"] = []
        return report

    workflow, completed, _ = finish_stage_task(workflows, runtime, operations, started, register_agent,
        modify_report=remove_required_acceptance)
    blocked = workflows.control(identifier(workflow["id"]), action_input(workflow), "prepare_gate")
    assert blocked["status"] == "BLOCKED"
    assert runtime.get_task(identifier(completed["id"]))["status"] == "COMPLETED"
    alignment = workflows.control(identifier(workflow["id"]), action_input(blocked), "retry")
    assert alignment["status"] == "AWAITING_GATE"
    assert alignment["state"]["gate_mode"] == "retry_alignment"
    assert alignment["state"]["completed_stages"] == []
    assert len(workflows.tasks(identifier(workflow["id"]))) == 1
    with pytest.raises(KnowledgeError) as pending_alignment:
        workflows.control(identifier(workflow["id"]), action_input(alignment), "advance")
    assert pending_alignment.value.status_code == 409
    commit(operations, alignment["state"]["gate_proposal_id"])
    rebuilt = workflows.control(identifier(workflow["id"]), action_input(alignment), "advance")
    assert rebuilt["status"] == "RUNNING"
    assert rebuilt["state"]["stage"] == 0
    assert rebuilt["state"]["completed_stages"] == []
    assert rebuilt["state"]["task_id"] != completed["id"]
    assert len(workflows.tasks(identifier(workflow["id"]))) == 2
    leased = claim(runtime, rebuilt, register_agent())
    assert leased["task"]["status"] == "RUNNING"
    package = runtime.get_context(identifier(leased["task"]["id"]))
    with connect(db_url) as conn:
        assert runtime.context.freshness(conn, package["id"])["fresh"] is True


@pytest.mark.parametrize("gate_state", ["pending", "validated", "rejected", "committed_with_drift"])
def test_explicit_retry_recovers_waiting_gate_and_retires_uncommitted_gate(
        workflows, runtime, operations, started, register_agent, gate_state, client, provenance):
    workflow, completed, _ = finish_stage_task(workflows, runtime, operations, started, register_agent)
    gated = workflows.control(identifier(workflow["id"]), action_input(workflow), "prepare_gate")
    old_gate_id = gated["state"]["gate_proposal_id"]
    if gate_state in {"validated", "rejected"}:
        gate = operations.get_proposal(identifier(old_gate_id))
        operations.review(identifier(old_gate_id), ProposalReview(expected_version=gate["version"],
            actor="合成提案审核者", reason="测试流程主动重试已有门禁"), "validate" if gate_state == "validated" else "reject")
    elif gate_state == "committed_with_drift":
        commit(operations, old_gate_id)
        code_id = gated["state"]["bindings"]["code"]
        code = client.get(f"/entities/{code_id}").json()
        data = {key: code[key] for key in ("name", "description", "status", "attributes", "evidence")}
        data.update(name="人工门禁后出现的新代码版本", expected_version=code["version"], provenance=provenance)
        assert client.put(f"/entities/{code_id}", json=data).status_code == 200
        with pytest.raises(KnowledgeError) as stale_gate:
            workflows.control(identifier(gated["id"]), action_input(gated), "advance")
        assert stale_gate.value.status_code == 409
    alignment = workflows.control(identifier(gated["id"]), action_input(gated), "retry")
    assert alignment["status"] == "AWAITING_GATE"
    assert alignment["state"]["gate_mode"] == "retry_alignment"
    assert alignment["state"]["gate_proposal_id"] != old_gate_id
    old_gate = operations.get_proposal(identifier(old_gate_id))
    assert old_gate["status"] == ("committed" if gate_state == "committed_with_drift" else "rejected")
    commit(operations, alignment["state"]["gate_proposal_id"])
    rebuilt = workflows.control(identifier(gated["id"]), action_input(alignment), "advance")
    assert rebuilt["state"]["stage"] == 0
    assert rebuilt["state"]["completed_stages"] == []
    assert rebuilt["state"]["task_id"] != completed["id"]
    assert claim(runtime, rebuilt, register_agent())["task"]["status"] == "RUNNING"


def test_completed_verification_archives_the_execution_used_by_the_gate_not_the_latest_attempt(
        workflows, runtime, operations, started, register_agent):
    from knowledge.workflow_adapter import WorkflowReferenceAdapter

    current = started
    for _ in range(3):
        current, _, _ = pass_stage(workflows, runtime, operations, current, register_agent)
    agent = register_agent(capability=STAGES[3]["capability"], role=STAGES[3]["role"])
    leased = claim(runtime, current, agent)
    task = leased["task"]
    lease = ReferenceExecutionRequest(agent_id=agent["id"], lease_token=leased["lease_token"], expected_version=task["version"])
    first = workflows.execute_reference(identifier(current["id"]), lease)
    second = workflows.execute_reference(identifier(current["id"]), lease)
    assert first["id"] != second["id"]
    assert first["document"]["passed"] and second["document"]["passed"]
    assert workflows.get(identifier(current["id"]))["state"]["last_execution_id"] == str(second["id"])
    report = WorkflowReferenceAdapter().build_report(3, task, runtime.get_context(identifier(task["id"])), current["state"]["bindings"], first)
    proposed = runtime.report(identifier(task["id"]), TaskReport(agent_id=agent["id"], lease_token=leased["lease_token"],
        expected_version=task["version"], **report))
    checked = runtime.control(identifier(task["id"]), task_control(proposed), "validate")
    assert checked["status"] == "VALIDATING"
    commit(operations, checked["runtime"]["proposal_id"])
    runtime.control(identifier(task["id"]), task_control(checked), "complete")
    current = workflows.get(identifier(current["id"]))
    gated = workflows.control(identifier(current["id"]), action_input(current), "prepare_gate")
    assert gated["status"] == "AWAITING_GATE"
    commit(operations, gated["state"]["gate_proposal_id"])
    completed = workflows.control(identifier(current["id"]), action_input(gated), "advance")
    assert completed["status"] == "COMPLETED"
    assert completed["state"]["completed_stages"][-1]["execution_id"] == str(first["id"])
    assert completed["state"]["completed_stages"][-1]["execution_id"] != str(second["id"])
