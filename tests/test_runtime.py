"""第三阶段任务运行时：租约、状态机、提案通道和并发审计。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

import psycopg
import pytest

from knowledge.db import connect
from knowledge.models import Kind
from knowledge.operations import KnowledgeOperations
from knowledge.operations_models import ProposalReview
from knowledge.runtime import AgentRuntime
from knowledge.runtime_models import (
    AgentCreate, AgentStateChange, ClaimRequest, LeaseRequest, TaskControl,
    TaskCreate, TaskReport,
)
from knowledge.semantics import KnowledgeError


pytestmark = pytest.mark.integration


def identifier(value):
    return UUID(str(value))


def control_input(task, **changes):
    return TaskControl(expected_version=task["version"], actor="本地任务审核者", reason="依据测试任务状态进行显式操作", **changes)


def entity_data(record, provenance, **changes):
    return {"expected_version": record["version"], "name": record["name"], "description": record["description"],
            "status": record["status"], "attributes": record["attributes"], "evidence": record["evidence"],
            "provenance": provenance, **changes}


def lease_input(claim, **changes):
    task = claim["task"]
    return LeaseRequest(agent_id=task["runtime"]["agent_id"], lease_token=claim["lease_token"],
                        expected_version=task["version"], **changes)


def report_input(claim, **changes):
    body = {"agent_id": claim["task"]["runtime"]["agent_id"], "lease_token": claim["lease_token"],
            "expected_version": claim["task"]["version"], "outcome": "succeeded", "summary": "完成合成任务产物，等待显式复核",
            "acceptance": [{"criterion_index": 0, "passed": True}]}
    body.update(changes)
    return TaskReport(**body)


@pytest.fixture
def runtime(db_url):
    return AgentRuntime(db_url)


@pytest.fixture
def make_agent(runtime, project):
    def create(name=None, **changes):
        data = {"project_id": project["id"], "name": name or "示例执行者-" + uuid4().hex,
                "role": "Developer", "capabilities": ["review.requirement"], "read_kinds": list(Kind),
                "write_kinds": ["Requirement"], "actor": "本地注册者", **changes}
        return runtime.register_agent(AgentCreate(**data))
    return create


@pytest.fixture
def make_task(runtime, project, entity):
    def create(seed=None, **changes):
        seed = entity() if seed is None else seed
        data = {"project_id": project["id"], "title": "合成需求复核任务", "goal": "读取固定知识版本并返回可审查产物",
                "required_capability": "review.requirement", "acceptance_criteria": ["提交明确的需求复核结论"],
                "input_knowledge": [{"id": seed["id"], "version": seed["version"]}], "actor": "本地任务创建者", **changes}
        return runtime.create_task(TaskCreate(**data))
    return create


def ready_claim(runtime, agent, task):
    ready = runtime.control(identifier(task["id"]), control_input(task), "ready")
    assert ready["status"] == "READY"
    claimed = runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=task["id"]))
    assert claimed["task"] is not None
    assert claimed["task"]["status"] == "RUNNING"
    return claimed


def finish_readonly(runtime, claim):
    proposed = runtime.report(identifier(claim["task"]["id"]), report_input(claim))
    assert proposed["status"] == "PROPOSED"
    checked = runtime.control(identifier(proposed["id"]), control_input(proposed), "validate")
    assert checked["status"] == "VALIDATING"
    completed = runtime.control(identifier(checked["id"]), control_input(checked), "complete")
    assert completed["status"] == "COMPLETED"
    return completed


def test_registry_state_and_capability_matching(runtime, make_agent, make_task):
    agent = make_agent(name="唯一执行者")
    assert agent["status"] == "active"
    assert agent["version"] == 1
    assert agent["capabilities"] == ["review.requirement"]
    with pytest.raises(KnowledgeError) as duplicate:
        make_agent(name="唯一执行者")
    assert duplicate.value.status_code == 409
    task = make_task()
    ready = runtime.control(identifier(task["id"]), control_input(task), "ready")
    other = make_agent(capabilities=["unrelated.capability"])
    assert runtime.claim(ClaimRequest(agent_id=other["id"], task_id=task["id"]))["task"] is None
    disabled = runtime.change_agent(identifier(agent["id"]), AgentStateChange(
        expected_version=1, actor="本地维护者", reason="暂停领取任务", status="disabled"))
    assert disabled["status"] == "disabled"
    with pytest.raises(KnowledgeError) as inactive:
        runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=task["id"]))
    assert inactive.value.status_code == 409
    assert runtime.get_task(identifier(ready["id"]))["status"] == "READY"


def test_read_scope_prevents_claiming_an_inaccessible_seed(runtime, make_agent, make_task):
    agent = make_agent(read_kinds=["CodeArtifact"], write_kinds=["CodeArtifact"])
    task = make_task()
    runtime.control(identifier(task["id"]), control_input(task), "ready")
    assert runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=task["id"]))["task"] is None
    assert runtime.get_task(identifier(task["id"]))["status"] == "READY"


def test_readonly_task_requires_explicit_validation_and_completion(runtime, make_agent, make_task):
    task = make_task()
    assert task["status"] == "CREATED"
    claim = ready_claim(runtime, make_agent(), task)
    assert claim["lease_token"]
    assert claim["task"]["runtime"]["attempt"] == 1
    assert claim["task"]["runtime"]["context_id"]
    assert "lease_token_hash" not in json.dumps(claim["task"], default=str)
    completed = finish_readonly(runtime, claim)
    history = runtime.history(identifier(task["id"]))
    assert len(history) == 6
    assert {item["status"] for item in history} == {"CREATED", "READY", "RUNNING", "PROPOSED", "VALIDATING", "COMPLETED"}
    with pytest.raises(KnowledgeError) as terminal:
        runtime.control(identifier(task["id"]), control_input(completed), "ready")
    assert terminal.value.status_code == 409


def test_dependencies_gate_dispatch_until_the_previous_task_completes(runtime, make_agent, make_task):
    agent = make_agent()
    parent = make_task()
    child = make_task(dependencies=[parent["id"]])
    runtime.control(identifier(child["id"]), control_input(child), "ready")
    assert runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=child["id"]))["task"] is None
    finish_readonly(runtime, ready_claim(runtime, agent, parent))
    claim = runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=child["id"]))
    assert str(claim["task"]["id"]) == str(child["id"])


def test_versions_and_lease_ownership_reject_old_or_unrelated_writers(runtime, make_agent, make_task):
    agent, other = make_agent(), make_agent()
    task = make_task()
    claim = ready_claim(runtime, agent, task)
    with pytest.raises(KnowledgeError) as stale:
        runtime.control(identifier(task["id"]), control_input(task), "cancel")
    assert stale.value.status_code == 409
    bad = report_input(claim).model_copy(update={"agent_id": identifier(other["id"])})
    with pytest.raises(KnowledgeError) as owner:
        runtime.report(identifier(task["id"]), bad)
    assert owner.value.status_code == 409
    bad = lease_input(claim).model_copy(update={"lease_token": uuid4()})
    with pytest.raises(KnowledgeError) as token:
        runtime.heartbeat(identifier(task["id"]), bad)
    assert token.value.status_code == 409
    assert runtime.get_task(identifier(task["id"]))["version"] == claim["task"]["version"]


def test_heartbeat_extends_the_lease_and_versions_the_event(runtime, make_agent, make_task, monkeypatch):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(AgentRuntime, "_now", staticmethod(lambda conn: now))
    claim = ready_claim(runtime, make_agent(), make_task())
    original = claim["task"]
    now += timedelta(seconds=10)
    extended = runtime.heartbeat(identifier(original["id"]), lease_input(claim))
    assert extended["status"] == "RUNNING"
    assert extended["version"] == original["version"] + 1
    assert str(extended["runtime"]["lease_expires_at"]) > str(original["runtime"]["lease_expires_at"])


def test_expired_lease_is_reclaimed_and_old_token_is_fenced(runtime, make_agent, make_task, monkeypatch):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(AgentRuntime, "_now", staticmethod(lambda conn: now))
    first_agent, replacement_agent = make_agent(), make_agent()
    task = make_task()
    runtime.control(identifier(task["id"]), control_input(task), "ready")
    old = runtime.claim(ClaimRequest(agent_id=first_agent["id"], task_id=task["id"], lease_seconds=5))
    now += timedelta(seconds=6)
    new = runtime.claim(ClaimRequest(agent_id=replacement_agent["id"], task_id=task["id"], lease_seconds=5))
    assert new["task"]["runtime"]["attempt"] == 2
    assert new["lease_token"] != old["lease_token"]
    old_report = report_input(old).model_copy(update={"expected_version": new["task"]["version"]})
    with pytest.raises(KnowledgeError) as fenced:
        runtime.report(identifier(task["id"]), old_report)
    assert fenced.value.status_code == 409


def test_expired_final_attempt_is_marked_failed(runtime, make_agent, make_task, monkeypatch):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(AgentRuntime, "_now", staticmethod(lambda conn: now))
    agent = make_agent()
    task = make_task(max_attempts=1)
    runtime.control(identifier(task["id"]), control_input(task), "ready")
    runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=task["id"], lease_seconds=5))
    now += timedelta(seconds=6)
    assert runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=task["id"]))["task"] is None
    assert runtime.get_task(identifier(task["id"]))["status"] == "FAILED"


def test_failed_execution_retries_only_within_attempt_budget(runtime, make_agent, make_task):
    agent = make_agent()
    task = make_task(max_attempts=2)
    first = ready_claim(runtime, agent, task)
    retry = runtime.report(identifier(task["id"]), report_input(first, outcome="failed", acceptance=[]))
    assert retry["status"] == "READY"
    second = runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=task["id"]))
    failed = runtime.report(identifier(task["id"]), report_input(second, outcome="failed", acceptance=[]))
    assert failed["status"] == "FAILED"
    assert failed["runtime"]["attempt"] == 2


def test_blocked_and_human_review_tasks_need_explicit_requeue(runtime, make_agent, make_task):
    agent = make_agent()
    task = make_task()
    claim = ready_claim(runtime, agent, task)
    blocked = runtime.report(identifier(task["id"]), report_input(claim, outcome="blocked", acceptance=[]))
    assert blocked["status"] == "BLOCKED"
    assert runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=task["id"]))["task"] is None
    runtime.control(identifier(task["id"]), control_input(blocked), "ready")
    claimed = runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=task["id"]))
    human = runtime.report(identifier(task["id"]), report_input(claimed, outcome="review", acceptance=[]))
    assert human["status"] == "NEEDS_HUMAN_REVIEW"
    assert runtime.control(identifier(task["id"]), control_input(human), "ready")["status"] == "READY"


def test_missing_acceptance_is_not_accepted_as_completion(runtime, make_agent, make_task):
    task = make_task()
    claim = ready_claim(runtime, make_agent(), task)
    proposed = runtime.report(identifier(task["id"]), report_input(claim, acceptance=[]))
    checked = runtime.control(identifier(task["id"]), control_input(proposed), "validate")
    assert checked["status"] == "NEEDS_HUMAN_REVIEW"
    with pytest.raises(KnowledgeError) as invalid:
        runtime.control(identifier(task["id"]), control_input(checked), "complete")
    assert invalid.value.status_code == 409


def test_cancel_revokes_the_active_lease(runtime, make_agent, make_task):
    task = make_task()
    claim = ready_claim(runtime, make_agent(), task)
    cancelled = runtime.control(identifier(task["id"]), control_input(claim["task"]), "cancel")
    assert cancelled["status"] == "CANCELLED"
    with pytest.raises(KnowledgeError) as revoked:
        runtime.report(identifier(task["id"]), report_input(claim).model_copy(update={"expected_version": cancelled["version"]}))
    assert revoked.value.status_code == 409


def test_knowledge_drift_routes_a_task_to_human_review(runtime, make_agent, make_task, entity, client, provenance):
    seed = entity()
    task = make_task(seed=seed)
    claim = ready_claim(runtime, make_agent(), task)
    response = client.put(f"/entities/{seed['id']}", json=entity_data(seed, provenance, name="上下文创建后的新修订"))
    assert response.status_code == 200
    review = runtime.report(identifier(task["id"]), report_input(claim))
    assert review["status"] == "NEEDS_HUMAN_REVIEW"
    assert review["runtime"]["review"]["fresh"] is False


def test_agent_changes_use_the_phase2_proposal_gate(runtime, make_agent, make_task, entity, provenance, db_url, client):
    seed = entity()
    claim = ready_claim(runtime, make_agent(), make_task(seed=seed))
    changes = [{"op": "update_entity", "id": seed["id"], "data": entity_data(seed, provenance, name="Agent 建议修订的需求")}]
    proposed = runtime.report(identifier(claim["task"]["id"]), report_input(claim, changes=changes))
    proposal_id = identifier(proposed["runtime"]["proposal_id"])
    operations = KnowledgeOperations(db_url)
    proposal = operations.get_proposal(proposal_id)
    assert proposal["status"] == "pending"
    assert proposal["plan"]["operations"][0]["data"]["provenance"]["nature"] == "inference"
    assert client.get(f"/entities/{seed['id']}").json() == seed
    checked = runtime.control(identifier(proposed["id"]), control_input(proposed), "validate")
    assert checked["status"] == "VALIDATING"
    with pytest.raises(KnowledgeError) as not_committed:
        runtime.control(identifier(checked["id"]), control_input(checked), "complete")
    assert not_committed.value.status_code == 409
    proposal = operations.get_proposal(proposal_id)
    assert proposal["status"] == "validated"
    operations.review(proposal_id, ProposalReview(expected_version=proposal["version"], actor="显式人工确认者",
                      reason="审查 Agent 提案后允许提交"), "commit")
    completed = runtime.control(identifier(checked["id"]), control_input(checked), "complete")
    assert completed["status"] == "COMPLETED"
    assert client.get(f"/entities/{seed['id']}").json()["version"] == 2


def test_agent_write_scope_and_context_bounds_are_enforced(runtime, make_agent, make_task, entity, provenance, db_url):
    seed, code, unrelated = entity(), entity("CodeArtifact"), entity()
    agent = make_agent()
    task = make_task(seed=seed, input_knowledge=[{"id": seed["id"], "version": 1}, {"id": code["id"], "version": 1}])
    claim = ready_claim(runtime, agent, task)
    for target in (code, unrelated):
        changes = [{"op": "update_entity", "id": target["id"], "data": entity_data(target, provenance, name="不应写入的修订")}]
        with pytest.raises(KnowledgeError) as forbidden:
            runtime.report(identifier(task["id"]), report_input(claim, changes=changes))
        assert forbidden.value.status_code == 422
    assert runtime.get_task(identifier(task["id"]))["status"] == "RUNNING"
    with connect(db_url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM change_proposal WHERE project_id=%s", (task["project_id"],)).fetchone()["n"] == 0


def test_simultaneous_workers_claim_at_most_one_lease(runtime, make_agent, make_task):
    agents = [make_agent(), make_agent()]
    task = make_task()
    runtime.control(identifier(task["id"]), control_input(task), "ready")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda agent: runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=task["id"])), agents))
    assert sum(result["task"] is not None for result in results) == 1
    assert runtime.get_task(identifier(task["id"]))["runtime"]["attempt"] == 1


def test_event_cursor_is_project_scoped_and_hides_lease_material(runtime, make_agent, make_task, project, client, provenance):
    task = make_task()
    claim = ready_claim(runtime, make_agent(), task)
    other = client.post("/entities", json={"kind": "Project", "name": "另一个任务项目", "provenance": provenance}).json()
    other_task = make_task(project_id=other["id"], input_knowledge=[])
    cursor, rows = 0, []
    while True:
        page = runtime.events(identifier(project["id"]), after=cursor, limit=2)
        rows.extend(page["events"])
        assert page["next_cursor"] >= cursor
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(rows) == 3
    assert [row["event_id"] for row in rows] == sorted({row["event_id"] for row in rows})
    assert all(str(row["task_id"]) == str(task["id"]) for row in rows)
    assert "lease_token_hash" not in json.dumps(rows, default=str)
    assert claim["lease_token"] not in json.dumps(rows, default=str)
    assert runtime.events(identifier(project["id"]), after=cursor)["events"] == []
    assert runtime.get_task(identifier(other_task["id"]))["project_id"] != identifier(project["id"])


def test_cross_project_seeds_and_dependencies_are_rejected(runtime, make_task, entity, project, client, provenance):
    seed = entity()
    dependency = make_task(seed=seed)
    other = client.post("/entities", json={"kind": "Project", "name": "隔离项目", "provenance": provenance}).json()
    with pytest.raises(KnowledgeError) as foreign_seed:
        make_task(seed=seed, project_id=other["id"])
    assert foreign_seed.value.status_code == 422
    with pytest.raises(KnowledgeError) as foreign_dependency:
        make_task(project_id=other["id"], input_knowledge=[], dependencies=[dependency["id"]])
    assert foreign_dependency.value.status_code == 422


def test_archived_projects_freeze_runtime_and_records_remain_append_only(
        runtime, make_agent, make_task, project, client, provenance, db_url):
    agent = make_agent()
    task = make_task()
    claim = ready_claim(runtime, agent, task)
    for table, column, value in [
        ("runtime_agent", "id", agent["id"]), ("runtime_agent_event", "agent_id", agent["id"]),
        ("runtime_task", "id", task["id"]), ("runtime_task_event", "task_id", task["id"]),
        ("context_package", "id", claim["task"]["runtime"]["context_id"]),
    ]:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with connect(db_url) as conn:
                conn.execute(f"DELETE FROM {table} WHERE {column}=%s", (value,))
    response = client.put(f"/entities/{project['id']}", json=entity_data(project, provenance, status="archived"))
    assert response.status_code == 200
    with pytest.raises(KnowledgeError) as frozen:
        runtime.report(identifier(task["id"]), report_input(claim))
    assert frozen.value.status_code == 409
    assert runtime.get_task(identifier(task["id"]))["status"] == "RUNNING"


def test_context_failure_does_not_starve_the_next_claimable_task(runtime, make_agent, make_task, monkeypatch, db_url):
    agent = make_agent()
    failing, healthy = make_task(), make_task()
    for task in (failing, healthy):
        runtime.control(identifier(task["id"]), control_input(task), "ready")
    original_build = runtime.context.build

    def fail_first(conn, project_id, task_id, *args, **kwargs):
        if str(task_id) == str(failing["id"]):
            raise KnowledgeError("模拟首个任务的上下文超过可用容量")
        return original_build(conn, project_id, task_id, *args, **kwargs)

    monkeypatch.setattr(runtime.context, "build", fail_first)
    claimed = runtime.claim(ClaimRequest(agent_id=agent["id"]))
    assert str(claimed["task"]["id"]) == str(healthy["id"])
    failed_context = runtime.get_task(identifier(failing["id"]))
    assert failed_context["status"] == "NEEDS_HUMAN_REVIEW"
    assert failed_context["runtime"]["attempt"] == 0
    assert failed_context["runtime"]["context_id"] is None
    with connect(db_url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM context_package WHERE task_id=%s", (failing["id"],)).fetchone()["n"] == 0


def test_lease_starts_after_context_build_finishes(runtime, make_agent, make_task, monkeypatch):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(AgentRuntime, "_now", staticmethod(lambda conn: now))
    original_build = runtime.context.build

    def slow_build(*args, **kwargs):
        nonlocal now
        result = original_build(*args, **kwargs)
        now += timedelta(seconds=20)
        return result

    monkeypatch.setattr(runtime.context, "build", slow_build)
    task, agent = make_task(), make_agent()
    runtime.control(identifier(task["id"]), control_input(task), "ready")
    claimed = runtime.claim(ClaimRequest(agent_id=agent["id"], task_id=task["id"], lease_seconds=5))
    expiry = datetime.fromisoformat(claimed["task"]["runtime"]["lease_expires_at"])
    assert expiry == now + timedelta(seconds=5)
    assert runtime.heartbeat(identifier(task["id"]), lease_input(claimed))["status"] == "RUNNING"


@pytest.mark.parametrize("location", ["acceptance", "change"])
def test_reports_cannot_introduce_evidence_outside_the_context(
        runtime, make_agent, make_task, entity, provenance, location, db_url):
    seed = entity()
    external_evidence = entity("Evidence", attributes={"category": "document", "uri": "urn:fixture:outside-context", "revision": "v1"})
    task = make_task(seed=seed)
    claimed = ready_claim(runtime, make_agent(), task)
    evidence_ref = [{"id": external_evidence["id"], "version": 1}]
    if location == "acceptance":
        request = report_input(claimed, acceptance=[{"criterion_index": 0, "passed": True, "evidence": evidence_ref}])
    else:
        request = report_input(claimed, changes=[{"op": "update_entity", "id": seed["id"],
            "data": entity_data(seed, provenance, name="带包外证据的修订", evidence=evidence_ref)}])
    with pytest.raises(KnowledgeError) as rejected:
        runtime.report(identifier(task["id"]), request)
    assert rejected.value.status_code == 422
    assert runtime.get_task(identifier(task["id"]))["version"] == claimed["task"]["version"]
    with connect(db_url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM change_proposal WHERE project_id=%s", (task["project_id"],)).fetchone()["n"] == 0


def test_relation_report_cannot_switch_to_an_unloaded_historical_endpoint(
        runtime, make_agent, make_task, entity, relationship, provenance, client):
    seed, code = entity(), entity("CodeArtifact")
    response = client.put(f"/entities/{code['id']}", json=entity_data(code, provenance, name="当前代码资产版本"))
    assert response.status_code == 200
    current_code = response.json()
    relation = relationship(seed, current_code, type="IMPLEMENTED_BY")
    task = make_task(seed=seed)
    claimed = ready_claim(runtime, make_agent(write_kinds=["Requirement", "CodeArtifact"]), task)
    data = {key: relation[key] for key in ("source_version", "target_version", "status", "rationale", "evidence", "provenance")}
    data.update(expected_version=relation["version"], target_version=1)
    request = report_input(claimed, changes=[{"op": "update_relationship", "id": relation["id"], "data": data}])
    with pytest.raises(KnowledgeError) as rejected:
        runtime.report(identifier(task["id"]), request)
    assert rejected.value.status_code == 422
    assert runtime.get_task(identifier(task["id"]))["status"] == "RUNNING"


@pytest.mark.parametrize("revise_entity", [False, True])
def test_relation_report_accepts_original_pin_or_its_own_entity_revision(
        runtime, make_agent, make_task, entity, relationship, provenance, revise_entity):
    seed, code = entity(), entity("CodeArtifact")
    relation = relationship(seed, code, type="IMPLEMENTED_BY")
    claimed = ready_claim(runtime, make_agent(write_kinds=["Requirement", "CodeArtifact"]), make_task(seed=seed))
    changes = []
    data = {key: relation[key] for key in ("source_version", "target_version", "status", "rationale", "evidence", "provenance")}
    data.update(expected_version=relation["version"], rationale="基于包内版本或同批修订重新说明实现关系")
    if revise_entity:
        changes.append({"op": "update_entity", "id": code["id"], "data": entity_data(code, provenance, name="同批产生的代码资产修订")})
        data["target_version"] = code["version"] + 1
    changes.append({"op": "update_relationship", "id": relation["id"], "data": data})
    proposed = runtime.report(identifier(claimed["task"]["id"]), report_input(claimed, changes=changes))
    assert proposed["status"] == "PROPOSED"
    validated = runtime.control(identifier(proposed["id"]), control_input(proposed), "validate")
    assert validated["status"] == "VALIDATING"
