from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import psycopg
import pytest

from knowledge.db import connect, migrate
from knowledge.models import EntityUpdate, RelationCreate
from knowledge.semantics import KnowledgeError

pytestmark = pytest.mark.integration


def entity_update(record, provenance, **changes):
    return {"expected_version": record["version"], "name": record["name"],
            "description": record["description"], "status": record["status"],
            "attributes": record["attributes"], "evidence": record["evidence"],
            "provenance": provenance, **changes}


def relation_update(record, provenance, **changes):
    return {"expected_version": record["version"], "source_version": record["source_version"],
            "target_version": record["target_version"], "status": record["status"],
            "rationale": record["rationale"], "evidence": record["evidence"],
            "provenance": provenance, **changes}


def test_migration_reentrant_and_database_immutable(db_url, project):
    migrate(db_url)
    migrate(db_url)
    with connect(db_url) as conn:
        migrations = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
        assert [row["name"] for row in migrations] == ["001_initial.sql", "002_knowledge_operations.sql", "003_agent_runtime.sql"]
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with connect(db_url) as conn:
            conn.execute("UPDATE entity_revision SET status='draft' WHERE entity_id=%s", (project["id"],))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with connect(db_url) as conn:
            conn.execute("DELETE FROM entity WHERE id=%s", (project["id"],))


@pytest.mark.parametrize("kind", ["Domain", "Requirement", "BusinessRule", "ArchitectureComponent",
    "Interface", "DataModel", "CodeArtifact", "TestCase", "Decision", "Constraint", "Evidence"])
def test_each_entity_round_trips(kind, entity, client, provenance):
    attrs = {"extensions": {"team:label": "synthetic"}}
    if kind == "Evidence":
        attrs.update(category="document", uri="urn:fixture:doc", revision="v1")
    original = entity(kind, attributes=attrs)
    updated = client.put(f"/entities/{original['id']}", json=entity_update(original, provenance, name="Renamed"))
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == original["id"]
    assert updated.json()["version"] == 2
    assert updated.json()["metamodel_version"] == 1
    assert updated.json()["attributes"]["extensions"] == attrs["extensions"]
    old = client.get(f"/entities/{original['id']}", params={"version": 1}).json()
    assert old == original
    history = client.get(f"/entities/{original['id']}/history").json()
    assert [r["version"] for r in history] == [2, 1]


def test_entity_validation_and_statuses(client, entity, project, provenance):
    item = entity("Requirement", attributes={"acceptance_criteria": ["Can cancel"]}, status="draft")
    assert client.put(f"/entities/{item['id']}", json=entity_update(item, provenance, status="deprecated")).status_code == 409
    assert client.put(f"/entities/{item['id']}", json=entity_update(item, provenance, attributes={"acceptance_criteria": "wrong"})).status_code == 422
    assert client.post("/entities", json={"kind": "Requirement", "name": "x", "project_id": project["id"]}).status_code == 422
    assert client.post("/entities", json={"kind": "Requirement", "name": "x", "project_id": item["id"], "provenance": provenance}).status_code == 422
    active = client.put(f"/entities/{item['id']}", json=entity_update(item, provenance, status="active")).json()
    assert client.put(f"/entities/{item['id']}", json=entity_update(item, provenance)).status_code == 409
    deprecated = client.put(f"/entities/{item['id']}", json=entity_update(active, provenance, status="deprecated")).json()
    archived = client.put(f"/entities/{item['id']}", json=entity_update(deprecated, provenance, status="archived")).json()
    assert client.put(f"/entities/{item['id']}", json=entity_update(archived, provenance)).status_code == 409
    assert client.get(f"/entities/{uuid4()}").status_code == 404
    assert client.get(f"/entities/{item['id']}", params={"version": 999}).status_code == 404


def test_queries_and_project_isolation(client, entity, project, provenance, relationship):
    source = entity(name="取消订单 cancel")
    target = entity("ArchitectureComponent")
    link = relationship(source, target)
    other = client.post("/entities", json={"kind": "Project", "name": "Other", "provenance": provenance}).json()
    assert client.get("/entities", params={"project_id": project["id"], "kind": "Requirement", "q": "取消"}).json() == [source]
    assert client.get("/entities", params={"project_id": other["id"], "q": "取消"}).json() == []
    assert client.get("/entities", params={"project_id": project["id"], "q": "%' OR 1=1 --"}).json() == []
    assert client.get("/entities", params={"project_id": project["id"], "limit": 1, "offset": 1}).status_code == 200
    assert client.get("/entities", params={"project_id": project["id"], "limit": 201}).status_code == 422
    query = {"project_id": project["id"], "entity_id": target["id"], "direction": "inbound", "type": "DEPENDS_ON"}
    assert client.get("/relationships", params=query).json() == [link]
    assert client.get("/relationships", params={**query, "direction": "outbound"}).json() == []
    relationship(source, other, expected=422)
    relationship(source, source, expected=422)
    relationship(source, target, expected=409)
    relationship(source, target, type="IMPLEMENTED_BY", target_version=999, expected=404)


@pytest.mark.parametrize("type,source_kind,target_kind", [
    ("CONTAINS", "Domain", "Requirement"), ("DERIVED_FROM", "Requirement", "BusinessRule"),
    ("DEPENDS_ON", "Interface", "DataModel"), ("IMPLEMENTED_BY", "Requirement", "CodeArtifact"),
    ("VERIFIED_BY", "BusinessRule", "TestCase"), ("CONSTRAINED_BY", "Interface", "Constraint"),
    ("IMPACTS", "Decision", "ArchitectureComponent"), ("SUPERSEDES", "Decision", "Decision"),
    ("CONFLICTS_WITH", "BusinessRule", "Constraint"),
])
def test_all_relationship_types_round_trip(type, source_kind, target_kind, entity, relationship, client):
    source, target = entity(source_kind), entity(target_kind)
    link = relationship(source, target, type=type)
    assert client.get(f"/relationships/{link['id']}").json() == link


@pytest.mark.parametrize("type", ["CONTAINS", "SUPERSEDES"])
def test_cycles_including_reactivation(type, entity, relationship, client, provenance):
    a, b, c = entity("Domain"), entity("Domain"), entity("Domain")
    ab = relationship(a, b, type=type)
    relationship(b, c, type=type)
    relationship(c, a, type=type, expected=409)
    rejected = client.put(f"/relationships/{ab['id']}", json=relation_update(ab, provenance, status="rejected")).json()
    relationship(c, a, type=type)
    assert client.put(f"/relationships/{ab['id']}", json=relation_update(rejected, provenance, status="proposed")).status_code == 409
    assert len(client.get(f"/relationships/{ab['id']}/history").json()) == 2


def test_symmetric_conflicts_and_version_pairing(entity, relationship, client, provenance):
    a, b = entity(), entity("Constraint")
    b2 = client.put(f"/entities/{b['id']}", json=entity_update(b, provenance, name="New name")).json()
    link = relationship(a, b2, type="CONFLICTS_WITH")
    assert UUID(link["source_id"]) < UUID(link["target_id"])
    versions = {link["source_id"]: link["source_version"], link["target_id"]: link["target_version"]}
    assert versions == {a["id"]: 1, b["id"]: 2}
    relationship(b2, a, type="CONFLICTS_WITH", expected=409)


def test_verified_evidence_and_revision_pinning(entity, relationship, client, provenance):
    requirement, code, test = entity(), entity("CodeArtifact"), entity("TestCase")
    implementation = relationship(requirement, code, type="IMPLEMENTED_BY")
    relationship(requirement, test, type="VERIFIED_BY", status="verified", expected=422)
    code_evidence = entity("Evidence", attributes={"category": "code", "uri": "git:synthetic/repo/path", "revision": "abc123",
                                                   "subject_id": code["id"], "subject_version": 1})
    evidence_ref = [{"id": code_evidence["id"], "version": 1}]
    promoted = client.put(f"/relationships/{implementation['id']}", json=relation_update(implementation, provenance,
                         status="verified", evidence=evidence_ref))
    assert promoted.status_code == 200, promoted.text
    verified = promoted.json()
    assert verified["version"] == 2
    assert client.get(f"/relationships/{implementation['id']}", params={"version": 1}).json() == implementation
    assert client.put(f"/relationships/{verified['id']}", json=relation_update(implementation, provenance)).status_code == 409
    code2 = client.put(f"/entities/{code['id']}", json=entity_update(code, provenance, name="Changed code")).json()
    assert code2["version"] == 2
    assert client.get(f"/relationships/{verified['id']}").json()["target_version"] == 1
    assert client.put(f"/relationships/{verified['id']}", json=relation_update(verified, provenance, target_version=2)).status_code == 422
    test_evidence = entity("Evidence", attributes={"category": "test_execution", "uri": "urn:synthetic:run:1", "revision": "run1",
        "subject_id": test["id"], "subject_version": 1, "outcome": "passed"})
    relationship(requirement, test, type="VERIFIED_BY", status="verified", evidence=[{"id": test_evidence["id"], "version": 1}])
    # One evidence record cannot be substituted for another target or evidence category.
    other_code = entity("CodeArtifact")
    relationship(requirement, other_code, type="IMPLEMENTED_BY", status="verified", evidence=evidence_ref, expected=422)
    rule = entity("BusinessRule")
    relationship(rule, test, type="VERIFIED_BY", status="verified", evidence=evidence_ref, expected=422)
    retired = client.put(f"/relationships/{verified['id']}", json=relation_update(verified, provenance, status="retired")).json()
    assert client.put(f"/relationships/{retired['id']}", json=relation_update(retired, provenance)).status_code == 409


@pytest.mark.parametrize("evidence_status,nature,outcome", [
    ("draft", "fact", "passed"), ("active", "inference", "passed"), ("active", "fact", "failed"),
    ("active", "fact", "unknown"),
])
def test_test_verification_rejects_unqualified_evidence(evidence_status, nature, outcome, entity, relationship, provenance):
    requirement, test = entity(), entity("TestCase")
    evidence = entity("Evidence", status=evidence_status, provenance={**provenance, "nature": nature}, attributes={
        "category": "test_execution", "uri": "urn:synthetic:test", "revision": "1",
        "subject_id": test["id"], "subject_version": 1, "outcome": outcome})
    relationship(requirement, test, type="VERIFIED_BY", status="verified", expected=422,
                 evidence=[{"id": evidence["id"], "version": 1}])


def test_evidence_reference_integrity_and_atomic_rollback(entity, relationship, client, project, provenance, db_url):
    requirement, code = entity(), entity("CodeArtifact")
    relationship(requirement, code, type="IMPLEMENTED_BY", evidence=[{"id": requirement["id"], "version": 1}], expected=422)
    relationship(requirement, code, type="IMPLEMENTED_BY", evidence=[{"id": uuid4().hex, "version": 1}], expected=404)
    other_project = client.post("/entities", json={"kind": "Project", "name": "Other", "provenance": provenance}).json()
    evidence = entity("Evidence", project_id=other_project["id"], attributes={"category": "document", "uri": "urn:other:doc", "revision": "1"})
    relationship(requirement, code, type="IMPLEMENTED_BY", evidence=[{"id": evidence["id"], "version": 1}], expected=422)
    bad_evidence = {"kind": "Evidence", "project_id": project["id"], "name": "Bad subject", "provenance": provenance,
                    "attributes": {"category": "test_execution", "uri": "urn:test:run", "revision": "1",
                                   "subject_id": code["id"], "subject_version": 1, "outcome": "passed"}}
    assert client.post("/entities", json=bad_evidence).status_code == 422
    # All failed relationship creations rolled back identity rows, not only revisions.
    with connect(db_url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM relationship WHERE project_id=%s", (project["id"],)).fetchone()["n"] == 0
    relationship(requirement, code, type="IMPLEMENTED_BY")


def test_entity_evidence_is_pinned(entity, client, provenance):
    evidence = entity("Evidence", attributes={"category": "document", "uri": "urn:fixture:prd", "revision": "v1"})
    requirement = entity(evidence=[{"id": evidence["id"], "version": 1}])
    response = client.put(f"/entities/{evidence['id']}", json=entity_update(evidence, provenance, name="New evidence"))
    assert response.status_code == 200
    assert client.get(f"/entities/{requirement['id']}").json()["evidence"] == [{"id": evidence["id"], "version": 1}]


def test_archived_project_blocks_writes(client, project, entity, provenance, relationship):
    a, b = entity(), entity("CodeArtifact")
    link = relationship(a, b)
    response = client.put(f"/entities/{project['id']}", json=entity_update(project, provenance, status="archived"))
    assert response.status_code == 200
    assert client.put(f"/entities/{a['id']}", json=entity_update(a, provenance)).status_code == 409
    assert client.post("/entities", json={"kind": "Domain", "name": "Blocked", "project_id": project["id"], "provenance": provenance}).status_code == 409
    assert client.put(f"/relationships/{link['id']}", json=relation_update(link, provenance)).status_code == 409
    relationship(b, a, expected=409)
    assert client.get(f"/entities/{a['id']}").status_code == 200


def test_concurrent_updates_allow_exactly_one_writer(service, entity, provenance):
    item = entity()
    request = EntityUpdate(**entity_update(item, provenance, name="Concurrent edit"))
    def write():
        try:
            return service.update_entity(UUID(item["id"]), request)["version"]
        except KnowledgeError as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: write(), range(2))) == [2, 409]


def test_concurrent_opposing_edges_cannot_form_cycle(service, entity, provenance):
    a, b = entity("Domain"), entity("Domain")
    def write(pair):
        source, target = pair
        request = RelationCreate(type="CONTAINS", source_id=source["id"], target_id=target["id"],
                                source_version=1, target_version=1, rationale="Concurrent containment", provenance=provenance)
        try:
            service.create_relationship(request)
            return 201
        except KnowledgeError as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, [(a, b), (b, a)])) == [201, 409]
