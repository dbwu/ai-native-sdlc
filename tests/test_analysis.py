"""第二阶段只读分析的真实 PostgreSQL 回归测试。"""
from uuid import UUID, uuid4

import pytest

from knowledge.analysis import KnowledgeAnalysis
from knowledge.semantics import KnowledgeError

pytestmark = pytest.mark.integration


def update(client, record, provenance, **changes):
    body = {key: record[key] for key in ("name", "description", "status", "attributes", "evidence")}
    response = client.put(f"/entities/{record['id']}", json={**body, "expected_version": record["version"],
                                                          "provenance": provenance, **changes})
    assert response.status_code == 200, response.text
    return response.json()


def test_impact_follows_semantic_directions_and_explains_paths(db_url, project, entity, relationship):
    rule, requirement = entity("BusinessRule"), entity("Requirement")
    code, test, component = entity("CodeArtifact"), entity("TestCase"), entity("ArchitectureComponent")
    relation = relationship(requirement, rule, type="DERIVED_FROM")
    relationship(requirement, code, type="IMPLEMENTED_BY")
    relationship(requirement, test, type="VERIFIED_BY")
    relationship(component, requirement, type="DEPENDS_ON")
    unrelated = entity("DataModel")
    result = KnowledgeAnalysis(db_url).impact(UUID(project["id"]), UUID(rule["id"]))
    by_id = {str(item["id"]): item for item in result["affected"]}
    assert set(by_id) == {item["id"] for item in (requirement, code, test, component)}
    assert unrelated["id"] not in by_id
    assert result["truncated"] is False
    assert by_id[requirement["id"]]["depth"] == 1
    assert by_id[code["id"]]["depth"] == 2
    assert str(by_id[code["id"]]["path"][0]["relationship_id"]) == relation["id"]
    assert by_id[code["id"]]["path"][0]["direction"] == "reverse"
    assert by_id[code["id"]]["path"][-1]["direction"] == "forward"
    assert by_id[code["id"]]["code"] == "POTENTIAL_IMPACT"
    # 组件依赖需求时，组件变更不会通过 DEPENDS_ON 倒推其依赖项也受影响。
    assert KnowledgeAnalysis(db_url).impact(project["id"], component["id"])["affected"] == []


def test_impact_contains_conflicts_and_supersession_directions(db_url, project, entity, relationship):
    domain, requirement, rule = entity("Domain"), entity(), entity("BusinessRule")
    relationship(domain, requirement, type="CONTAINS")
    relationship(requirement, rule, type="CONFLICTS_WITH")
    analysis = KnowledgeAnalysis(db_url)
    result = analysis.impact(project["id"], domain["id"])
    assert {str(item["id"]) for item in result["affected"]} == {requirement["id"], rule["id"]}
    reverse = analysis.impact(project["id"], rule["id"])
    assert {str(item["id"]) for item in reverse["affected"]} == {requirement["id"]}
    previous, replacement = entity("Decision"), entity("Decision")
    relationship(replacement, previous, type="SUPERSEDES")
    assert [str(item["id"]) for item in analysis.impact(project["id"], previous["id"])["affected"]] == [replacement["id"]]
    assert analysis.impact(project["id"], replacement["id"])["affected"] == []


def test_impact_bounds_cycles_and_ignores_rejected_links(db_url, project, entity, relationship, client, provenance):
    a, b, c, d = [entity("Domain") for _ in range(4)]
    relationship(a, b, type="IMPACTS")
    relationship(b, c, type="IMPACTS")
    relationship(c, a, type="IMPACTS")
    relationship(a, d, type="IMPACTS", status="rejected")
    analysis = KnowledgeAnalysis(db_url)
    limited = analysis.impact(project["id"], a["id"], max_depth=1)
    assert [str(item["id"]) for item in limited["affected"]] == [b["id"]]
    assert limited["truncated"] is True
    assert analysis.impact(project["id"], a["id"], max_depth=0)["truncated"] is True
    assert analysis.impact(project["id"], d["id"], max_depth=0)["truncated"] is False
    bounded = analysis.impact(project["id"], a["id"], max_nodes=2)
    assert len(bounded["affected"]) == 1
    assert bounded["truncated"] is True
    full = analysis.impact(project["id"], a["id"])
    assert len(full["affected"]) == 2
    assert {str(item["id"]) for item in full["affected"]} == {b["id"], c["id"]}
    assert full["truncated"] is False
    # 不会把根实体因环路重复加入结果。
    assert a["id"] not in {str(item["id"]) for item in full["affected"]}


def test_analysis_rejects_wrong_project_and_invalid_bounds(db_url, project, entity, client, provenance):
    item = entity()
    other = client.post("/entities", json={"kind": "Project", "name": "其他项目", "provenance": provenance}).json()
    analysis = KnowledgeAnalysis(db_url)
    with pytest.raises(KnowledgeError) as error:
        analysis.impact(other["id"], item["id"])
    assert error.value.status_code == 404
    with pytest.raises(KnowledgeError):
        analysis.impact(item["id"], item["id"])
    with pytest.raises(KnowledgeError):
        analysis.impact(project["id"], uuid4())
    for depth, nodes in [(-1, 10), (13, 10), (2, 0), (2, 1001)]:
        with pytest.raises(KnowledgeError):
            analysis.impact(project["id"], item["id"], depth, nodes)
    with pytest.raises(KnowledgeError):
        analysis.consistency(project["id"], limit=0)


def test_consistency_detects_stale_pins_inactive_endpoints_and_conflicts(db_url, project, entity, relationship, client, provenance):
    requirement, code, rule = entity(), entity("CodeArtifact"), entity("BusinessRule")
    link = relationship(requirement, code, type="IMPLEMENTED_BY")
    conflict = relationship(requirement, rule, type="CONFLICTS_WITH")
    update(client, code, provenance, status="deprecated")
    result = KnowledgeAnalysis(db_url).consistency(project["id"])
    findings = result["findings"]
    assert any(item["code"] == "STALE_ENDPOINT" and str(item["relationship_id"]) == link["id"] for item in findings)
    assert any(item["code"] == "INACTIVE_ENDPOINT" and str(item["relationship_id"]) == link["id"] for item in findings)
    assert any(item["code"] == "UNRESOLVED_CONFLICT" and str(item["relationship_id"]) == conflict["id"] for item in findings)
    assert {item["code"] for item in findings} >= {"MISSING_IMPLEMENTATION", "MISSING_VERIFICATION"}
    assert result["truncated"] is False
    small = KnowledgeAnalysis(db_url).consistency(project["id"], limit=1)
    assert len(small["findings"]) == 1
    assert small["truncated"] is True
    # 只读检查不会创建新的实体或关系版本。
    assert len(client.get(f"/relationships/{link['id']}/history").json()) == 1


def test_consistency_evidence_changes_and_requirement_coverage(db_url, project, entity, relationship, client, provenance):
    requirement, code, test = entity(), entity("CodeArtifact"), entity("TestCase")
    code_evidence = entity("Evidence", attributes={"category": "code", "uri": "urn:analysis:code", "revision": "1",
                                                   "subject_id": code["id"], "subject_version": 1})
    run = entity("Evidence", attributes={"category": "test_execution", "uri": "urn:analysis:test", "revision": "run1",
                                         "subject_id": test["id"], "subject_version": 1, "outcome": "passed"})
    implementation = relationship(requirement, code, type="IMPLEMENTED_BY", status="verified",
                                  evidence=[{"id": code_evidence["id"], "version": 1}])
    verification = relationship(requirement, test, type="VERIFIED_BY", status="verified",
                                evidence=[{"id": run["id"], "version": 1}])
    analysis = KnowledgeAnalysis(db_url)
    assert analysis.consistency(project["id"])["findings"] == []
    update(client, code_evidence, provenance, status="archived")
    update(client, run, provenance, attributes={**run["attributes"], "revision": "run2", "outcome": "failed"})
    findings = analysis.consistency(project["id"])["findings"]
    assert {item["code"] for item in findings} >= {"STALE_EVIDENCE", "INACTIVE_EVIDENCE", "UNQUALIFIED_EVIDENCE"}
    assert any(item["code"] == "UNQUALIFIED_EVIDENCE" and str(item["record_id"]) == verification["id"] for item in findings)
    assert any(item["code"] == "INACTIVE_EVIDENCE" and str(item["record_id"]) == implementation["id"] for item in findings)


def test_consistency_entity_evidence_and_project_isolation(db_url, project, entity, client, provenance):
    document = entity("Evidence", attributes={"category": "document", "uri": "urn:analysis:doc", "revision": "1"})
    item = entity("Decision", evidence=[{"id": document["id"], "version": 1}])
    update(client, document, provenance, name="新证据名称")
    analysis = KnowledgeAnalysis(db_url)
    findings = analysis.consistency(project["id"])["findings"]
    assert len(findings) == 1
    assert findings[0]["code"] == "STALE_EVIDENCE"
    assert str(findings[0]["record_id"]) == item["id"]
    assert findings[0]["context"]["owner_type"] == "entity"
    other = client.post("/entities", json={"kind": "Project", "name": "空白项目", "provenance": provenance}).json()
    assert analysis.consistency(other["id"])["findings"] == []
    assert analysis.consistency(other["id"])["truncated"] is False
    # 项目归档后仍可检查历史知识，不会调用禁止写入的项目保护规则。
    update(client, project, provenance, status="archived")
    assert analysis.consistency(project["id"])["findings"] == findings
