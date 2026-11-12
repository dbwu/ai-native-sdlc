from itertools import product

from fastapi.testclient import TestClient

from knowledge.api import create_app
from knowledge.models import Kind, RelationType
from knowledge.semantics import ENTITY_TRANSITIONS, RELATION_TRANSITIONS, KnowledgeError, validate_transition, validate_types
from knowledge.source_spans import SourceSpan


def test_registry_lists_implemented_schemas_and_exact_relationship_pairs():
    with TestClient(create_app("unused")) as client:
        response = client.get("/metamodel/registry")
        legacy = client.get("/metamodel").json()
        assert response.status_code == 200
        assert client.post("/metamodel/registry").status_code == 405

    registry = response.json()
    assert registry["metamodel_version"] == legacy["version"] == 1
    assert set(legacy) == {"version", "entity_attributes", "relationships", "labels", "additional_rules"}
    assert set(registry["entity_types"]) == set(Kind)
    assert {kind: item["attributes_schema"] for kind, item in registry["entity_types"].items()} == legacy["entity_attributes"]
    assert set(registry["relationship_types"]) == set(RelationType)
    for relation, source, target in product(RelationType, Kind, Kind):
        try:
            validate_types(relation, source, target)
            allowed = True
        except KnowledgeError:
            allowed = False
        targets = registry["relationship_types"][relation]["targets_by_source"].get(source, [])
        assert (target in targets) == allowed

    assert registry["relationship_types"]["CONFLICTS_WITH"]["symmetric"]
    assert registry["relationship_types"]["SUPERSEDES"]["targets_by_source"]["Requirement"] == ["Requirement"]
    assert registry["relationship_types"]["CONTAINS"]["targets_by_source"]["ArchitectureComponent"] == [
        "ArchitectureComponent", "CodeArtifact", "DataModel", "Interface", "TestCase",
    ]


def test_registry_reports_same_status_revision_and_verified_evidence_requirements():
    with TestClient(create_app("unused")) as client:
        registry = client.get("/metamodel/registry").json()
    for policy, transitions in [("entity_lifecycle", ENTITY_TRANSITIONS), ("relationship_lifecycle", RELATION_TRANSITIONS)]:
        for old, new in product(transitions, repeat=2):
            try:
                validate_transition(old, new, transitions)
                allowed = True
            except KnowledgeError:
                allowed = False
            assert (new in registry[policy][old]["allowed_next"]) == allowed

    evidence = registry["evidence_policy"]
    assert evidence["reference_schema"]["required"] == ["id", "version"]
    assert evidence["source_span"]["schema"] == SourceSpan.model_json_schema()
    assert evidence["source_span"]["offset_unit"] == "unicode_code_point"
    assert evidence["source_span"]["range"] == "zero_based_half_open"
    assert evidence["verified_relationship"]["evidence_status"] == "active"
    assert evidence["verified_relationship"]["additional_requirements"] == {
        "IMPLEMENTED_BY": {"category": "code", "matches_target_version": True},
        "VERIFIED_BY": {"category": "test_execution", "matches_target_version": True, "outcome": "passed"},
    }
