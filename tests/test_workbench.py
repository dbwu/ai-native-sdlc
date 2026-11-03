"""中文验证工作台的 HTTP 闭环：参考执行停在待审，人审与版本保护真实生效。"""
from uuid import uuid4

import pytest

from knowledge.db import connect


pytestmark = pytest.mark.integration


def snapshot(client, workflow_id):
    response = client.get(f"/workbench/workflows/{workflow_id}/snapshot")
    assert response.status_code == 200, response.text
    return response.json()


def action(client, current, name, expected=200, **overrides):
    body = {"expected_version": current["workflow"]["version"],
            "task_version": current["task"]["version"] if current["task"] else None,
            "proposal_version": current["proposal"]["version"] if current["proposal"] else None,
            "actor": "页面验证者", "reason": "在工作台明确审阅本次产物后执行该操作", **overrides}
    response = client.post(f"/workbench/workflows/{current['workflow']['id']}/actions/{name}", json=body)
    assert response.status_code == expected, response.text
    return response.json()


def approve_current(client, current):
    checked = action(client, current, "validate_proposal")["snapshot"]
    assert checked["proposal"]["result"]["valid"] is True
    committed = action(client, checked, "commit_proposal")["snapshot"]
    assert committed["proposal"]["status"] == "committed"
    return committed


@pytest.fixture
def created(client, project):
    response = client.post("/workflows", json={"project_id": project["id"], "title": "页面验证参考流程",
        "goal": "通过中文工作台逐阶段验证真实提案与参考测试", "scenario": "order_cancellation_v1", "actor": "页面验证者"})
    assert response.status_code == 201, response.text
    return snapshot(client, response.json()["id"])


@pytest.fixture
def started(client, created):
    committed = approve_current(client, created)
    current = action(client, committed, "start")["snapshot"]
    assert current["workflow"]["status"] == "RUNNING"
    assert current["task"]["status"] == "READY"
    return current


def complete_stage(client, current):
    proposed = action(client, current, "run_reference_stage")["snapshot"]
    assert proposed["task"]["status"] == "PROPOSED"
    assert proposed["proposal_kind"] == "task"
    assert proposed["proposal"]["status"] == "pending"
    checked = action(client, proposed, "validate_task")["snapshot"]
    assert checked["task"]["status"] == "VALIDATING"
    assert checked["proposal"]["status"] == "validated"
    committed = action(client, checked, "commit_proposal")["snapshot"]
    assert committed["proposal"]["status"] == "committed"
    completed = action(client, committed, "complete_task")["snapshot"]
    assert completed["task"]["status"] == "COMPLETED"
    gated = action(client, completed, "prepare_gate")["snapshot"]
    assert gated["workflow"]["status"] == "AWAITING_GATE"
    assert gated["proposal_kind"] == "gate"
    assert gated["proposal"]["status"] == "pending"
    approved = approve_current(client, gated)
    return action(client, approved, "advance")["snapshot"]


def test_projects_can_be_discovered_without_a_project_uuid(client, project, entity):
    requirement = entity()
    response = client.get("/workbench/projects", params={"limit": 200})
    assert response.status_code == 200, response.text
    projects = response.json()
    assert project["id"] in {item["id"] for item in projects}
    assert requirement["id"] not in {item["id"] for item in projects}
    assert all(item["kind"] == "Project" for item in projects)
    assert client.get("/workbench/projects", params={"limit": 201}).status_code == 422


def test_draft_snapshot_exposes_real_bootstrap_and_readable_actions(client, created, project):
    assert created["mode"] == "reference"
    assert created["task"] is None
    assert created["proposal_kind"] == "bootstrap"
    assert created["proposal"]["status"] == "pending"
    assert created["workflow"]["status"] == "DRAFT"
    assert isinstance(created["actions"], list)
    actions = {item["id"]: item for item in created["actions"]}
    assert actions["validate_proposal"]["enabled"] is True
    assert actions["start"]["enabled"] is False
    assert actions["start"]["reason"]
    assert all(item["label"] for item in created["actions"])
    assert client.get("/entities", params={"project_id": project["id"]}).json() == [project]


def test_reference_stage_never_approves_or_commits_its_own_outputs(client, started, db_url):
    requirement_id = started["workflow"]["state"]["bindings"]["requirement"]
    before = client.get(f"/entities/{requirement_id}").json()
    proposed = action(client, started, "run_reference_stage")["snapshot"]
    assert proposed["task"]["status"] == "PROPOSED"
    assert proposed["proposal"]["status"] == "pending"
    assert proposed["workflow"]["state"]["stage"] == 0
    assert proposed["workflow"]["status"] == "RUNNING"
    assert client.get(f"/entities/{requirement_id}").json() == before
    with connect(db_url) as conn:
        events = conn.execute("SELECT action FROM proposal_event WHERE proposal_id=%s", (proposed["proposal"]["id"],)).fetchall()
    assert [item["action"] for item in events] == ["created"]
    serialized = str(proposed)
    assert "lease_token_hash" not in serialized
    assert "lease_token" not in serialized


def test_repeated_reference_run_does_not_duplicate_a_proposal(client, started, db_url):
    proposed = action(client, started, "run_reference_stage")["snapshot"]
    response = client.post(f"/workbench/workflows/{proposed['workflow']['id']}/actions/run_reference_stage", json={
        "expected_version": proposed["workflow"]["version"], "task_version": proposed["task"]["version"],
        "proposal_version": proposed["proposal"]["version"], "actor": "页面验证者", "reason": "模拟重复点击"})
    assert response.status_code in {200, 409}, response.text
    current = snapshot(client, proposed["workflow"]["id"])
    assert current["task"]["id"] == proposed["task"]["id"]
    assert current["proposal"]["id"] == proposed["proposal"]["id"]
    assert current["proposal"]["version"] == 1
    with connect(db_url) as conn:
        count = conn.execute("SELECT count(*) AS n FROM change_proposal WHERE project_id=%s", (proposed["workflow"]["project_id"],)).fetchone()["n"]
    assert count == 2  # 只有初始化和当前阶段提案。


def test_four_stages_are_completed_only_through_explicit_workbench_actions(client, started):
    current = started
    previous_task_id = current["task"]["id"]
    for stage in range(4):
        assert current["workflow"]["state"]["stage"] == stage
        current = complete_stage(client, current)
        assert len(current["workflow"]["state"]["completed_stages"]) == stage + 1
        if stage < 3:
            assert current["task"]["id"] != previous_task_id
            assert current["task"]["status"] == "READY"
            previous_task_id = current["task"]["id"]
    assert current["workflow"]["status"] == "COMPLETED"
    assert len(current["executions"]) >= 1
    report = current["executions"][-1]["document"]
    assert report["executed"] is True
    assert report["passed"] is True
    assert report["tests_run"] == 4
    verified = {item["type"] for item in current["relationships"] if item["status"] == "verified"}
    assert {"IMPLEMENTED_BY", "VERIFIED_BY"} <= verified
    assert all(item["enabled"] is False for item in current["actions"] if item["id"] in {"run_reference_stage", "advance", "retry"})


def test_proposal_version_conflict_is_not_silently_retried(client, created):
    checked = action(client, created, "validate_proposal")["snapshot"]
    assert checked["proposal"]["version"] > created["proposal"]["version"]
    action(client, checked, "commit_proposal", expected=409, proposal_version=created["proposal"]["version"])
    current = snapshot(client, created["workflow"]["id"])
    assert current["proposal"]["status"] == "validated"
    assert current["workflow"]["status"] == "DRAFT"


def test_workflow_version_conflict_cannot_advance_a_different_state(client, started):
    paused = action(client, started, "pause")["snapshot"]
    action(client, paused, "resume", expected=409, expected_version=started["workflow"]["version"])
    assert snapshot(client, paused["workflow"]["id"])["workflow"]["status"] == "PAUSED"


def test_exhausted_retry_budget_disables_both_retry_actions_without_mutating_the_task(client, project):
    response = client.post("/workflows", json={"project_id": project["id"], "title": "无人工重试预算的页面流程",
        "goal": "验证预算用尽时页面提示与后端保护一致", "scenario": "order_cancellation_v1",
        "max_stage_retries": 0, "actor": "页面验证者"})
    assert response.status_code == 201, response.text
    created = snapshot(client, response.json()["id"])
    current = action(client, approve_current(client, created), "start")["snapshot"]
    proposed = action(client, current, "run_reference_stage")["snapshot"]
    buttons = {item["id"]: item for item in proposed["actions"]}
    assert buttons["reject_task_and_retry"]["enabled"] is False
    assert "重试次数已用尽" in buttons["reject_task_and_retry"]["reason"]
    action(client, proposed, "reject_task_and_retry", expected=409)
    unchanged = snapshot(client, proposed["workflow"]["id"])
    for key in ("workflow", "task", "proposal"):
        assert unchanged[key] == proposed[key]
    assert unchanged["proposal"]["status"] == "pending"

    cancelled = client.post(f"/runtime/tasks/{proposed['task']['id']}/cancel", json={
        "expected_version": proposed["task"]["version"], "actor": "页面验证者", "reason": "显式取消任务以验证阻塞后的重试保护"})
    assert cancelled.status_code == 200, cancelled.text
    blocked = action(client, snapshot(client, proposed["workflow"]["id"]), "prepare_gate")["snapshot"]
    assert blocked["workflow"]["status"] == "BLOCKED"
    buttons = {item["id"]: item for item in blocked["actions"]}
    assert buttons["retry"]["enabled"] is False
    assert "重试次数已用尽" in buttons["retry"]["reason"]
    action(client, blocked, "retry", expected=409)
    unchanged = snapshot(client, blocked["workflow"]["id"])
    for key in ("workflow", "task", "proposal"):
        assert unchanged[key] == blocked[key]


@pytest.mark.parametrize("name", ["validate_task", "reject_task_and_retry"])
def test_task_review_requires_the_explicit_proposal_version(client, started, name):
    proposed = action(client, started, "run_reference_stage")["snapshot"]
    action(client, proposed, name, expected=422, proposal_version=None)
    unchanged = snapshot(client, proposed["workflow"]["id"])
    for key in ("workflow", "task", "proposal"):
        assert unchanged[key] == proposed[key]
    assert unchanged["task"]["status"] == "PROPOSED"
    assert unchanged["proposal"]["status"] == "pending"


def test_reject_is_explicit_and_task_retry_preserves_old_history(client, started):
    proposed = action(client, started, "run_reference_stage")["snapshot"]
    old_task_id = proposed["task"]["id"]
    old_proposal_id = proposed["proposal"]["id"]
    rejected = action(client, proposed, "reject_proposal")["snapshot"]
    assert rejected["proposal"]["status"] == "rejected"
    assert rejected["workflow"]["state"]["stage"] == 0
    assert rejected["task"]["id"] == old_task_id
    retried = action(client, rejected, "reject_task_and_retry")["snapshot"]
    assert retried["workflow"]["status"] == "RUNNING"
    assert retried["workflow"]["state"]["stage"] == 0
    assert retried["task"]["status"] == "READY"
    assert retried["task"]["id"] != old_task_id
    assert client.get(f"/runtime/tasks/{old_task_id}").json()["status"] == "CANCELLED"
    assert client.get(f"/operations/proposals/{old_proposal_id}").json()["status"] == "rejected"


def test_failed_validation_shows_human_review_instead_of_allowing_commit(client, started, provenance):
    proposed = action(client, started, "run_reference_stage")["snapshot"]
    requirement_id = proposed["workflow"]["state"]["bindings"]["requirement"]
    record = client.get(f"/entities/{requirement_id}").json()
    data = {key: record[key] for key in ("name", "description", "status", "attributes", "evidence")}
    data.update(expected_version=record["version"], provenance=provenance, description="模拟另一窗口修改知识")
    assert client.put(f"/entities/{requirement_id}", json=data).status_code == 200
    reviewed = action(client, proposed, "validate_task")["snapshot"]
    assert reviewed["task"]["status"] == "NEEDS_HUMAN_REVIEW"
    assert reviewed["task"]["runtime"]["review"]["valid"] is False
    commits = [item for item in reviewed["actions"] if item["id"] == "commit_proposal"]
    assert not commits or commits[0]["enabled"] is False


def test_diff_uses_the_original_baseline_instead_of_the_latest_entity(client, project, entity, provenance):
    original = entity(name="原始需求")
    update = {key: original[key] for key in ("name", "description", "status", "attributes", "evidence")}
    update.update(expected_version=1, name="待审候选需求", provenance=provenance)
    proposal_response = client.post("/operations/proposals", json={"project_id": project["id"], "title": "原基线差异测试",
        "actor": "页面验证者", "changes": [{"op": "update_entity", "id": original["id"], "data": update}]})
    assert proposal_response.status_code == 201, proposal_response.text
    proposal = proposal_response.json()
    changed = client.put(f"/entities/{original['id']}", json={**update, "name": "其他窗口的最新版本"})
    assert changed.status_code == 200
    response = client.get(f"/workbench/proposals/{proposal['id']}/diff")
    assert response.status_code == 200, response.text
    diff = response.json()
    assert diff["stale"] is True
    change = next(item for item in diff["changes"] if item["id"] == original["id"])
    assert change["before"]["version"] == 1
    assert change["before"]["name"] == "原始需求"
    assert "recorded_at" in change["before"]
    assert change["after"]["name"] == "待审候选需求"
    assert "recorded_at" not in change["after"]
    assert client.get(f"/operations/proposals/{proposal['id']}").json()["status"] == "pending"
    assert client.get(f"/entities/{original['id']}").json()["version"] == 2


def test_bootstrap_diff_has_creation_candidates_without_made_up_before_versions(client, created):
    response = client.get(f"/workbench/proposals/{created['proposal']['id']}/diff")
    assert response.status_code == 200, response.text
    diff = response.json()
    assert diff["id"] == created["proposal"]["id"]
    assert diff["stale"] is False
    assert len(diff["changes"]) == 15
    assert all(change["before"] is None for change in diff["changes"])
    assert {change["op"] for change in diff["changes"]} == {"create_entity", "create_relationship"}


def test_unknown_identifiers_and_unapproved_start_are_clear_errors(client, created):
    assert client.get(f"/workbench/workflows/{uuid4()}/snapshot").status_code == 404
    assert client.get(f"/workbench/proposals/{uuid4()}/diff").status_code == 404
    action(client, created, "start", expected=409)
    action(client, created, "commit_proposal", expected=409)
    action(client, created, "run_reference_stage", expected=409)
    assert snapshot(client, created["workflow"]["id"])["proposal"]["status"] == "pending"
