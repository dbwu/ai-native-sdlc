"""第二阶段的真实数据库集成测试：提案隔离、原子提交及来源演进。"""
import hashlib
import json
from uuid import uuid4

import pytest
import psycopg

from knowledge.db import connect
from knowledge.semantics import KnowledgeError
from knowledge.service import KnowledgeService


pytestmark = pytest.mark.integration


def source_document(name="支持订单取消"):
    return {"entities": [
        {"key": "REQ-001", "kind": "Requirement", "name": name,
         "attributes": {"acceptance_criteria": ["未支付订单可以取消"]}},
        {"key": "BR-001", "kind": "BusinessRule", "name": "支付前允许取消"},
    ], "relationships": [{"type": "DERIVED_FROM", "source_key": "REQ-001", "target_key": "BR-001",
                           "rationale": "需求依据业务规则提出"}]}


def entity_data(record, provenance, **changes):
    return {"expected_version": record["version"], "name": record["name"], "description": record["description"],
            "status": record["status"], "attributes": record["attributes"], "evidence": record["evidence"],
            "provenance": provenance, **changes}


def relationship_data(record, provenance, **changes):
    return {"expected_version": record["version"], "source_version": record["source_version"],
            "target_version": record["target_version"], "status": record["status"],
            "rationale": record["rationale"], "evidence": record["evidence"], "provenance": provenance, **changes}


def review(client, proposal, action, expected=200, **changes):
    response = client.post(f"/operations/proposals/{proposal['id']}/{action}", json={
        "expected_version": proposal["version"], "actor": "本地审核者", "reason": "依据提案内容进行明确审查", **changes,
    })
    assert response.status_code == expected, response.text
    return response.json()


def validate_and_commit(client, proposal):
    validated = review(client, proposal, "validate")["proposal"]
    assert validated["result"]["valid"] is True
    return review(client, validated, "commit")["proposal"]


@pytest.fixture
def ingest(client, project):
    def create(document=None, revision="source-v1", uri="urn:fixture:phase2:source", content=None,
               format="json", expected=201, project_id=None):
        payload = {"project_id": project_id or project["id"], "source_uri": uri, "source_revision": revision,
                   "format": format, "content": content if content is not None else json.dumps(
                       source_document() if document is None else document, ensure_ascii=False),
                   "actor": "采集提交者", "title": "订单知识采集提案"}
        response = client.post("/operations/ingestions", json=payload)
        assert response.status_code == expected, response.text
        return response.json()
    return create


@pytest.fixture
def propose(client, project):
    def create(changes, expected=201, project_id=None):
        response = client.post("/operations/proposals", json={
            "project_id": project_id or project["id"], "title": "已有知识修订提案", "actor": "提案作者", "changes": changes,
        })
        assert response.status_code == expected, response.text
        return response.json()
    return create


def test_ingestion_and_validation_never_publish_knowledge(client, project, ingest, db_url):
    created = ingest()
    proposal = created["proposal"]
    assert created["replayed"] is False
    assert proposal["status"] == "pending"
    assert proposal["version"] == 1
    assert len(proposal["plan"]["operations"]) == 3
    for operation in proposal["plan"]["operations"]:
        data = operation["data"]
        assert data["provenance"]["nature"] == "inference"
        assert data["status"] == ("draft" if operation["op"] == "create_entity" else "proposed")
    assert client.get("/entities", params={"project_id": project["id"]}).json() == [project]
    validated = review(client, proposal, "validate")["proposal"]
    assert validated["status"] == "validated"
    assert validated["result"]["valid"] is True
    assert validated["result"]["preview"]["operation_count"] == 3
    for mapping in proposal["plan"]["entity_mapping"]:
        assert client.get(f"/entities/{mapping['entity_id']}").status_code == 404
    assert client.get("/relationships", params={"project_id": project["id"]}).json() == []
    with connect(db_url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM source_entity_binding WHERE source_id=%s",
                            (proposal["source_id"],)).fetchone()["n"] == 0


def test_commit_is_explicit_and_replays_without_new_revisions(client, project, ingest):
    proposal = ingest()["proposal"]
    review(client, proposal, "commit", expected=409)
    committed = validate_and_commit(client, proposal)
    assert committed["status"] == "committed"
    assert committed["version"] == 3
    assert committed["result"]["operation_count"] == 3
    assert len(client.get("/entities", params={"project_id": project["id"]}).json()) == 3
    assert len(client.get("/relationships", params={"project_id": project["id"]}).json()) == 1
    repeated = review(client, proposal, "commit")
    assert repeated["replayed"] is True
    assert repeated["proposal"] == committed
    history = client.get(f"/operations/proposals/{proposal['id']}/history").json()
    assert [event["action"] for event in history] == ["committed", "validated", "created"]
    assert [event["version"] for event in history] == [3, 2, 1]
    for item in committed["result"]["entity_mapping"]:
        assert client.get(f"/entities/{item['entity_id']}").json()["version"] == 1


def test_repeated_ingestion_reuses_original_proposal_and_source_snapshot(client, project, ingest):
    proposal = ingest()["proposal"]
    replay = ingest()
    assert replay["replayed"] is True
    assert replay["proposal"] == proposal
    committed = validate_and_commit(client, proposal)
    replay = ingest()
    assert replay["replayed"] is True
    assert replay["proposal"] == committed
    assert len(client.get("/operations/sources", params={"project_id": project["id"]}).json()) == 1
    assert len(client.get("/operations/proposals", params={"project_id": project["id"]}).json()) == 1


def test_source_snapshots_preserve_original_whitespace_and_digest(client, project, ingest):
    content = " \n\t" + json.dumps(source_document(), ensure_ascii=False, indent=2) + "\n  "
    proposal = ingest(content=content)["proposal"]
    source_id = proposal["source_id"]
    response = client.get(f"/operations/sources/{source_id}/versions/1")
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["content"] == content
    assert snapshot["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert snapshot["external_revision"] == "source-v1"
    assert snapshot["format"] == "json"
    index = client.get("/operations/sources", params={"project_id": project["id"]}).json()
    assert index[0]["sha256"] == snapshot["sha256"]
    assert index[0]["version"] == 1
    assert client.get(f"/operations/sources/{source_id}/versions/999").status_code == 404
    assert client.get(f"/operations/sources/{uuid4()}/versions/1").status_code == 404
    ingest(content=content + " ", expected=409)
    assert client.get(f"/operations/sources/{source_id}/versions/2").status_code == 404


def test_source_revision_cannot_be_reused_for_changed_content_or_format(ingest):
    ingest()
    ingest(source_document(name="变更后的需求"), expected=409)
    markdown = "# 来源\n\n```knowledge-json\n" + json.dumps(source_document()) + "\n```\n"
    ingest(content=markdown, format="markdown", expected=409)


def test_stable_source_keys_reuse_ids_and_unchanged_content_has_no_operations(client, project, ingest):
    first = validate_and_commit(client, ingest()["proposal"])
    first_mapping = {item["key"]: item for item in first["result"]["entity_mapping"]}
    changed = ingest(source_document(name="改名后的取消需求"), revision="source-v2")["proposal"]
    planned_mapping = {item["key"]: item for item in changed["plan"]["entity_mapping"]}
    assert {key: item["entity_id"] for key, item in planned_mapping.items()} == {
        key: item["entity_id"] for key, item in first_mapping.items()}
    assert planned_mapping["REQ-001"]["version"] == 2
    assert planned_mapping["BR-001"]["version"] == 1
    assert {operation["op"] for operation in changed["plan"]["operations"]} == {"update_entity", "update_relationship"}
    validate_and_commit(client, changed)
    requirement_id = first_mapping["REQ-001"]["entity_id"]
    current = client.get(f"/entities/{requirement_id}").json()
    assert current["name"] == "改名后的取消需求"
    assert current["version"] == 2
    unchanged = ingest(source_document(name="改名后的取消需求"), revision="source-v3")["proposal"]
    assert unchanged["plan"]["operations"] == []
    result = validate_and_commit(client, unchanged)
    assert result["result"]["operation_count"] == 0
    assert client.get(f"/entities/{requirement_id}").json() == current
    assert len(client.get(f"/entities/{requirement_id}/history").json()) == 2
    assert client.get("/operations/sources", params={"project_id": project["id"]}).json()[0]["version"] == 3


def test_new_source_snapshot_invalidates_older_pending_or_validated_proposals(client, project, ingest):
    first = ingest()["proposal"]
    second = ingest(source_document(name="第二版需求"), revision="source-v2")["proposal"]
    invalid = review(client, first, "validate")["proposal"]
    assert invalid["status"] == "pending"
    assert invalid["result"]["valid"] is False
    assert invalid["result"]["errors"][0]["status_code"] == 409
    validated = review(client, second, "validate")["proposal"]
    assert validated["status"] == "validated"
    ingest(source_document(name="第三版需求"), revision="source-v3")
    review(client, validated, "commit", expected=409)
    assert client.get(f"/operations/proposals/{second['id']}").json() == validated
    assert client.get("/entities", params={"project_id": project["id"]}).json() == [project]


def test_changed_knowledge_baseline_rejects_stale_import(client, ingest, provenance):
    committed = validate_and_commit(client, ingest()["proposal"])
    requirement_id = next(item["entity_id"] for item in committed["result"]["entity_mapping"] if item["key"] == "REQ-001")
    pending = ingest(source_document(name="来自来源的更新"), revision="source-v2")["proposal"]
    current = client.get(f"/entities/{requirement_id}").json()
    edited = client.put(f"/entities/{requirement_id}", json=entity_data(current, provenance, name="并发人工修改"))
    assert edited.status_code == 200
    invalid = review(client, pending, "validate")["proposal"]
    assert invalid["result"]["valid"] is False
    assert invalid["result"]["errors"][0]["status_code"] == 409
    assert client.get(f"/entities/{requirement_id}").json() == edited.json()


def test_proposal_rejection_is_terminal_and_expected_version_is_checked(client, ingest):
    proposal = ingest()["proposal"]
    review(client, proposal, "reject", expected=409, expected_version=999)
    rejected = review(client, proposal, "reject")["proposal"]
    assert rejected["status"] == "rejected"
    assert rejected["version"] == 2
    for action in ("validate", "commit", "reject"):
        review(client, rejected, action, expected=409)
    assert client.get(f"/operations/proposals/{proposal['id']}/history").json()[0]["action"] == "rejected"


def test_imports_and_manual_revisions_are_project_scoped(client, project, ingest, propose, entity, provenance):
    other_response = client.post("/entities", json={"kind": "Project", "name": "另一项目", "status": "active", "provenance": provenance})
    assert other_response.status_code == 201
    other = other_response.json()
    own = ingest()["proposal"]
    theirs = ingest(project_id=other["id"])["proposal"]
    assert own["source_id"] != theirs["source_id"]
    assert {item["entity_id"] for item in own["plan"]["entity_mapping"]}.isdisjoint(
        {item["entity_id"] for item in theirs["plan"]["entity_mapping"]})
    assert [proposal["id"] for proposal in client.get("/operations/proposals", params={"project_id": project["id"]}).json()] == [own["id"]]
    assert [source["id"] for source in client.get("/operations/sources", params={"project_id": project["id"]}).json()] == [own["source_id"]]
    other_entity = entity(project_id=other["id"])
    propose([{"op": "update_entity", "id": other_entity["id"], "data": entity_data(other_entity, provenance)}], expected=422)


def test_manual_proposal_can_activate_entity_and_attach_matching_verification_evidence(
        client, entity, relationship, propose, provenance):
    requirement = entity(status="draft")
    code = entity("CodeArtifact")
    link = relationship(requirement, code, type="IMPLEMENTED_BY")
    evidence = entity("Evidence", attributes={"category": "code", "uri": "urn:fixture:phase2:code",
        "revision": "commit-001", "subject_id": code["id"], "subject_version": code["version"]})
    proposal = propose([
        {"op": "update_entity", "id": requirement["id"], "data": entity_data(requirement, provenance, status="active")},
        {"op": "update_relationship", "id": link["id"], "data": relationship_data(link, provenance,
         status="verified", source_version=2, evidence=[{"id": evidence["id"], "version": 1}])},
    ])
    validated = review(client, proposal, "validate")["proposal"]
    assert validated["result"]["valid"] is True
    assert client.get(f"/entities/{requirement['id']}").json() == requirement
    assert client.get(f"/relationships/{link['id']}").json() == link
    committed = review(client, validated, "commit")["proposal"]
    assert committed["result"]["operation_count"] == 2
    assert client.get(f"/entities/{requirement['id']}").json()["status"] == "active"
    verified = client.get(f"/relationships/{link['id']}").json()
    assert verified["status"] == "verified"
    assert verified["source_version"] == 2
    assert verified["evidence"] == [{"id": evidence["id"], "version": 1}]


def test_commit_rolls_back_all_writes_and_event_if_later_operation_fails(
        client, entity, propose, provenance, monkeypatch):
    first, second = entity(name="第一个实体"), entity(name="第二个实体")
    proposal = propose([
        {"op": "update_entity", "id": first["id"], "data": entity_data(first, provenance, name="第一次修改")},
        {"op": "update_entity", "id": second["id"], "data": entity_data(second, provenance, name="第二次修改")},
    ])
    validated = review(client, proposal, "validate")["proposal"]
    assert validated["result"]["valid"] is True
    original_update = KnowledgeService.update_entity

    def fail_second(self, record_id, request):
        if str(record_id) == second["id"]:
            raise KnowledgeError("模拟第二项操作在提交时校验失败")
        return original_update(self, record_id, request)

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeService, "update_entity", fail_second)
        review(client, validated, "commit", expected=422)
    assert client.get(f"/entities/{first['id']}").json() == first
    assert client.get(f"/entities/{second['id']}").json() == second
    assert client.get(f"/operations/proposals/{proposal['id']}").json() == validated
    assert len(client.get(f"/operations/proposals/{proposal['id']}/history").json()) == 2
    committed = review(client, validated, "commit")["proposal"]
    assert committed["result"]["operation_count"] == 2
    assert client.get(f"/entities/{first['id']}").json()["version"] == 2
    assert client.get(f"/entities/{second['id']}").json()["version"] == 2


def test_terminal_entities_and_archived_projects_reject_operations(client, project, entity, propose, ingest, provenance):
    archived = entity(status="archived")
    proposal = propose([{"op": "update_entity", "id": archived["id"], "data": entity_data(archived, provenance, name="不能再修订")}])
    invalid = review(client, proposal, "validate")["proposal"]
    assert invalid["result"]["valid"] is False
    assert invalid["result"]["errors"][0]["status_code"] == 409
    response = client.put(f"/entities/{project['id']}", json=entity_data(project, provenance, status="archived"))
    assert response.status_code == 200
    ingest(expected=409)
    review(client, invalid, "validate", expected=409)
    assert client.get(f"/entities/{archived['id']}").json() == archived


def test_missing_source_keys_are_reported_without_deleting_knowledge(client, ingest):
    committed = validate_and_commit(client, ingest()["proposal"])
    rule_id = next(item["entity_id"] for item in committed["result"]["entity_mapping"] if item["key"] == "BR-001")
    original = client.get(f"/entities/{rule_id}").json()
    document = source_document()
    document["entities"] = document["entities"][:1]
    document["relationships"] = []
    missing = ingest(document, revision="source-v2")["proposal"]
    assert missing["plan"]["removed_keys"] == ["BR-001"]
    validate_and_commit(client, missing)
    assert client.get(f"/entities/{rule_id}").json() == original
    restored = ingest(revision="source-v3")["proposal"]
    binding = next(item for item in restored["plan"]["entity_mapping"] if item["key"] == "BR-001")
    assert binding["entity_id"] == rule_id
    assert binding["resolution"] == "existing"


def test_proposal_history_and_list_filters_are_bounded(client, project, ingest):
    pending = ingest()["proposal"]
    rejected = review(client, ingest(revision="source-v2")["proposal"], "reject")["proposal"]
    query = {"project_id": project["id"], "status": "rejected", "limit": 1}
    assert client.get("/operations/proposals", params=query).json() == [rejected]
    assert client.get("/operations/proposals", params={**query, "offset": 1}).json() == []
    assert len(client.get(f"/operations/proposals/{rejected['id']}/history", params={"limit": 1}).json()) == 1
    assert client.get(f"/operations/proposals/{pending['id']}").json() == pending
    assert client.get(f"/operations/proposals/{uuid4()}").status_code == 404
    assert client.get(f"/operations/proposals/{uuid4()}/history").status_code == 404
    assert client.get("/operations/proposals", params={"project_id": project["id"], "limit": 201}).status_code == 422
    assert client.get("/operations/sources", params={"project_id": project["id"], "offset": -1}).status_code == 422


def test_manual_proposals_reject_duplicate_targets_and_invalid_evidence_is_not_applied(
        client, entity, propose, provenance):
    requirement = entity()
    change = {"op": "update_entity", "id": requirement["id"], "data": entity_data(requirement, provenance)}
    propose([change, change], expected=422)
    invalid = propose([{**change, "data": entity_data(requirement, provenance,
        evidence=[{"id": str(uuid4()), "version": 1}])}])
    preview = review(client, invalid, "validate")["proposal"]
    assert preview["result"]["valid"] is False
    assert preview["result"]["errors"][0]["status_code"] == 404
    assert client.get(f"/entities/{requirement['id']}").json() == requirement


def test_source_update_preserves_lifecycle_but_never_reuses_old_evidence(client, ingest, entity, provenance):
    committed = validate_and_commit(client, ingest()["proposal"])
    requirement_id = next(item["entity_id"] for item in committed["result"]["entity_mapping"] if item["key"] == "REQ-001")
    original = client.get(f"/entities/{requirement_id}").json()
    evidence = entity("Evidence", attributes={"category": "document", "uri": "urn:fixture:review", "revision": "review-v1"})
    evidence_ref = [{"id": evidence["id"], "version": 1}]
    response = client.put(f"/entities/{requirement_id}", json=entity_data(original, provenance, status="active", evidence=evidence_ref))
    assert response.status_code == 200, response.text
    new_proposal = ingest(source_document(name="来源修改后的需求"), revision="source-v2")["proposal"]
    operation = next(item for item in new_proposal["plan"]["operations"] if item["id"] == requirement_id)
    assert operation["data"]["status"] == "active"
    assert operation["data"]["provenance"]["nature"] == "inference"
    assert operation["data"]["evidence"] == []
    validate_and_commit(client, new_proposal)
    latest = client.get(f"/entities/{requirement_id}").json()
    assert latest["version"] == 3
    assert latest["status"] == "active"
    assert latest["provenance"]["nature"] == "inference"
    assert latest["evidence"] == []
    assert client.get(f"/entities/{requirement_id}", params={"version": 2}).json()["evidence"] == evidence_ref


def test_phase2_sources_proposals_events_and_bindings_are_append_only(client, ingest, db_url):
    committed = validate_and_commit(client, ingest()["proposal"])
    for table, column, record_id in [
        ("knowledge_source", "id", committed["source_id"]),
        ("source_revision", "source_id", committed["source_id"]),
        ("change_proposal", "id", committed["id"]),
        ("proposal_event", "proposal_id", committed["id"]),
        ("source_entity_binding", "source_id", committed["source_id"]),
    ]:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with connect(db_url) as conn:
                # 表名及列名为本测试内的常量，不使用调用方输入。
                conn.execute(f"DELETE FROM {table} WHERE {column}=%s", (record_id,))
    assert client.get(f"/operations/proposals/{committed['id']}").json() == committed
