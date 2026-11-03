from uuid import uuid4

import pytest
from pydantic import ValidationError

from knowledge.api import create_app
from knowledge.models import ATTRIBUTE_MODELS, EntityCreate, EvidenceAttributes, Kind, Provenance, RelationType
from knowledge.semantics import KnowledgeError, validate_types


def test_all_requested_types_and_api_are_discoverable():
    assert len(ATTRIBUTE_MODELS) == 12
    assert len(RelationType) == 9
    schema = create_app("unused").openapi()
    assert schema["info"]["version"] == "0.1.0"
    assert "/relationships/{relationship_id}/history" in schema["paths"]


@pytest.mark.parametrize("type,source,target", [
    ("CONTAINS", "Project", "Domain"),
    ("CONTAINS", "ArchitectureComponent", "CodeArtifact"),
    ("DERIVED_FROM", "Requirement", "BusinessRule"),
    ("DEPENDS_ON", "Requirement", "ArchitectureComponent"),
    ("IMPLEMENTED_BY", "Requirement", "CodeArtifact"),
    ("VERIFIED_BY", "Requirement", "TestCase"),
    ("CONSTRAINED_BY", "Interface", "Constraint"),
    ("IMPACTS", "Decision", "DataModel"),
    ("SUPERSEDES", "Decision", "Decision"),
    ("CONFLICTS_WITH", "BusinessRule", "Constraint"),
])
def test_valid_directions(type, source, target):
    validate_types(RelationType(type), Kind(source), Kind(target))


@pytest.mark.parametrize("type,source,target", [
    ("CONTAINS", "Requirement", "Domain"),
    ("CONTAINS", "ArchitectureComponent", "BusinessRule"),
    ("CONTAINS", "Domain", "Project"),
    ("DERIVED_FROM", "Evidence", "Requirement"),
    ("DEPENDS_ON", "Project", "Domain"),
    ("IMPLEMENTED_BY", "CodeArtifact", "Requirement"),
    ("IMPLEMENTED_BY", "TestCase", "CodeArtifact"),
    ("VERIFIED_BY", "TestCase", "Requirement"),
    ("CONSTRAINED_BY", "Requirement", "TestCase"),
    ("IMPACTS", "Evidence", "Requirement"),
    ("SUPERSEDES", "Requirement", "Decision"),
    ("CONFLICTS_WITH", "Project", "Requirement"),
])
def test_invalid_directions(type, source, target):
    with pytest.raises(KnowledgeError):
        validate_types(RelationType(type), Kind(source), Kind(target))


@pytest.mark.parametrize("attributes", [
    {"category": "code", "uri": "git:repo", "revision": "abc"},
    {"category": "document", "uri": "relative/path", "revision": "v1"},
    {"category": "test_execution", "uri": "urn:run:1", "revision": "1", "subject_id": str(uuid4()), "subject_version": 1},
    {"category": "document", "uri": "urn:doc:1", "revision": "1", "subject_version": 1},
    {"category": "document", "uri": "urn:doc:1", "revision": "1", "sha256": "bad"},
])
def test_incomplete_evidence_rejected(attributes):
    with pytest.raises(ValidationError):
        EvidenceAttributes.model_validate(attributes)


def test_provenance_and_kind_attributes_are_required_and_typed(provenance):
    with pytest.raises(ValidationError):
        EntityCreate(kind="Requirement", name="Missing project", provenance=provenance)
    with pytest.raises(ValidationError):
        EntityCreate(kind="Project", name="Typo", attributes={"external_keey": "p"}, provenance=provenance)
    with pytest.raises(ValidationError):
        Provenance(**{**provenance, "confidence": float("nan")})
