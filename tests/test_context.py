"""上下文包的精确版本、读取范围、来源证据与失效检测回归。"""
import hashlib
import json
from uuid import UUID, uuid4

import psycopg
import pytest

from knowledge.context import ContextBuilder
from knowledge.db import connect
from knowledge.models import EvidenceRef, Kind
from knowledge.semantics import KnowledgeError
from knowledge.service import KnowledgeService

pytestmark = pytest.mark.integration


@pytest.fixture
def context_task(db_url, project):
    task_id = uuid4()
    with connect(db_url) as conn:
        conn.execute("""INSERT INTO runtime_task (id,project_id,title,required_capability,document)
            VALUES (%s,%s,%s,%s,'{}'::jsonb)""",
            (task_id, UUID(project["id"]), "上下文包独立回归任务", "fixture.context"))
    return task_id


def build(db_url, project, task_id, seeds, kinds=None, **limits):
    with connect(db_url) as conn:
        KnowledgeService._lock(conn, UUID(project["id"]))
        return ContextBuilder(db_url).build(conn, UUID(project["id"]), task_id,
            [EvidenceRef(id=item["id"], version=item["version"]) for item in seeds],
            list(Kind) if kinds is None else kinds, **limits)


def revise(client, record, provenance, **changes):
    data = {field: record[field] for field in ("name", "description", "status", "attributes", "evidence")}
    response = client.put(f"/entities/{record['id']}", json={**data, "provenance": provenance,
                         "expected_version": record["version"], **changes})
    assert response.status_code == 200, response.text
    return response.json()


def test_context_pins_versions_sources_and_evidence_with_a_verifiable_digest(
        db_url, project, context_task, entity, relationship):
    evidence = entity("Evidence", attributes={"category": "document", "uri": "urn:fixture:context", "revision": "v1"})
    requirement = entity(evidence=[{"id": evidence["id"], "version": 1}])
    rule = entity("BusinessRule")
    code = entity("CodeArtifact")
    included = relationship(requirement, rule, type="DERIVED_FROM")
    relationship(requirement, code, type="IMPLEMENTED_BY")
    package = build(db_url, project, context_task, [requirement], kinds=[Kind.REQUIREMENT, Kind.BUSINESS_RULE, Kind.EVIDENCE])
    document = package["document"]
    assert {item["id"] for item in document["entities"]} == {requirement["id"], rule["id"], evidence["id"]}
    assert [item["id"] for item in document["relationships"]] == [included["id"]]
    assert document["truncated"] is False
    stored_requirement = next(item for item in document["entities"] if item["id"] == requirement["id"])
    assert stored_requirement["version"] == 1
    assert stored_requirement["provenance"] == requirement["provenance"]
    assert stored_requirement["evidence"] == [{"id": evidence["id"], "version": 1}]
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == package["sha256"]
    assert ContextBuilder(db_url).get(package["id"])["document"] == document
    with connect(db_url) as conn:
        assert ContextBuilder(db_url).freshness(conn, package["id"]) == {"fresh": True, "stale": []}


def test_context_never_joins_historical_edges_to_newer_entity_content(
        db_url, project, context_task, entity, relationship, client, provenance):
    original, rule = entity(), entity("BusinessRule")
    relation = relationship(original, rule, type="DERIVED_FROM")
    current = revise(client, original, provenance, name="新的需求版本")
    old_package = build(db_url, project, context_task, [original])
    old_document = old_package["document"]
    assert {item["version"] for item in old_document["entities"] if item["id"] == original["id"]} == {1}
    assert old_document["relationships"][0]["id"] == relation["id"]
    assert old_document["relationships"][0]["source_version"] == 1
    with connect(db_url) as conn:
        assert ContextBuilder(db_url).freshness(conn, old_package["id"])["fresh"] is False
    current_package = build(db_url, project, context_task, [current])
    assert [item["id"] for item in current_package["document"]["entities"]] == [original["id"]]
    assert current_package["document"]["relationships"] == []
    assert current_package["document"]["truncated"] is False
    multiple = build(db_url, project, context_task, [original, current], max_depth=0)
    assert {item["version"] for item in multiple["document"]["entities"]} == {1, 2}


def test_context_read_scope_rejects_seeds_without_leaking_excluded_entity_bodies(
        db_url, project, context_task, entity, relationship):
    requirement, code = entity(), entity("CodeArtifact", description="只允许开发角色读取的内容")
    relationship(requirement, code, type="IMPLEMENTED_BY")
    with pytest.raises(KnowledgeError, match="读取范围"):
        build(db_url, project, context_task, [code], kinds=[Kind.REQUIREMENT])
    package = build(db_url, project, context_task, [requirement], kinds=[Kind.REQUIREMENT])
    assert [item["id"] for item in package["document"]["entities"]] == [requirement["id"]]
    assert package["document"]["relationships"] == []
    assert "只允许开发角色读取的内容" not in json.dumps(package["document"], ensure_ascii=False)


def test_unread_evidence_content_stays_hidden_but_its_drift_invalidates_context(
        db_url, project, context_task, entity, client, provenance):
    evidence = entity("Evidence", description="证据私有正文", attributes={"category": "document", "uri": "urn:fixture:private", "revision": "v1"})
    requirement = entity(evidence=[{"id": evidence["id"], "version": 1}])
    package = build(db_url, project, context_task, [requirement], kinds=[Kind.REQUIREMENT])
    assert [item["kind"] for item in package["document"]["entities"]] == ["Requirement"]
    assert "证据私有正文" not in json.dumps(package["document"], ensure_ascii=False)
    revise(client, evidence, provenance, name="更新后的证据")
    with connect(db_url) as conn:
        result = ContextBuilder(db_url).freshness(conn, package["id"])
    assert result["fresh"] is False
    assert result["stale"][0]["table"] == "entity"
    assert result["stale"][0]["id"] == evidence["id"]


def test_context_change_detection_accepts_only_explicit_committed_versions(
        db_url, project, context_task, entity, client, provenance):
    original = entity()
    package = build(db_url, project, context_task, [original])
    current = revise(client, original, provenance, name="任务显式提交的修改")
    builder = ContextBuilder(db_url)
    accepted = {("entity", original["id"]): current["version"]}
    with connect(db_url) as conn:
        assert builder.freshness(conn, package["id"])["fresh"] is False
        assert builder.freshness(conn, package["id"], accepted_versions=accepted)["fresh"] is True
    revise(client, current, provenance, name="提交之后发生的另一次并发修改")
    with connect(db_url) as conn:
        stale = builder.freshness(conn, package["id"], accepted_versions=accepted)
    assert stale["fresh"] is False
    assert stale["stale"][0]["expected_version"] == 2
    assert stale["stale"][0]["current_version"] == 3


def test_relationship_revisions_invalidate_context_without_rewriting_its_snapshot(
        db_url, project, context_task, entity, relationship, client, provenance):
    requirement, component = entity(), entity("ArchitectureComponent")
    relation = relationship(requirement, component)
    package = build(db_url, project, context_task, [requirement])
    response = client.put(f"/relationships/{relation['id']}", json={
        "expected_version": 1, "source_version": 1, "target_version": 1,
        "status": "retired", "rationale": "显式退役该关系", "provenance": provenance,
    })
    assert response.status_code == 200, response.text
    with connect(db_url) as conn:
        result = ContextBuilder(db_url).freshness(conn, package["id"])
    assert any(item["table"] == "relationship" and item["id"] == relation["id"] for item in result["stale"])
    assert ContextBuilder(db_url).get(package["id"])["document"]["relationships"][0]["status"] == "proposed"


def test_context_bounds_are_explicit_and_do_not_report_complete_cycles_as_truncated(
        db_url, project, context_task, entity, relationship):
    a, b, c = entity(), entity(), entity()
    relationship(a, b, type="DEPENDS_ON")
    relationship(b, c, type="DEPENDS_ON")
    relationship(c, a, type="DEPENDS_ON")
    limited = build(db_url, project, context_task, [a], max_entities=1)
    assert len(limited["document"]["entities"]) == 1
    assert limited["document"]["relationships"] == []
    assert limited["document"]["truncated"] is True
    full = build(db_url, project, context_task, [a], max_depth=2)
    assert len(full["document"]["entities"]) == 3
    assert len(full["document"]["relationships"]) == 3
    assert full["document"]["truncated"] is False
    with pytest.raises(KnowledgeError, match="max_depth"):
        build(db_url, project, context_task, [a], max_depth=4)
    with pytest.raises(KnowledgeError, match="不能为空"):
        build(db_url, project, context_task, [a], kinds=[])
    with pytest.raises(KnowledgeError, match="不能重复"):
        build(db_url, project, context_task, [a, a])


def test_context_rejects_cross_project_inputs_and_missing_tasks(
        db_url, project, context_task, entity, client, provenance):
    other = client.post("/entities", json={"kind": "Project", "name": "其他项目", "provenance": provenance}).json()
    foreign = entity(project_id=other["id"])
    with pytest.raises(KnowledgeError, match="其他项目"):
        build(db_url, project, context_task, [foreign])
    with pytest.raises(KnowledgeError, match="任务不存在"):
        build(db_url, project, uuid4(), [])
    with pytest.raises(KnowledgeError) as error:
        ContextBuilder(db_url).get(uuid4())
    assert error.value.status_code == 404


def test_context_byte_limit_and_append_only_storage(db_url, project, context_task, entity):
    huge = [entity(description="字" * 100000) for _ in range(4)]
    with pytest.raises(KnowledgeError, match="1 MiB"):
        build(db_url, project, context_task, huge, max_depth=0)
    package = build(db_url, project, context_task, [huge[0]], max_depth=0)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with connect(db_url) as conn:
            conn.execute("DELETE FROM context_package WHERE id=%s", (package["id"],))
