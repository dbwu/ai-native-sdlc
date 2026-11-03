from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class Kind(StrEnum):
    PROJECT = "Project"
    DOMAIN = "Domain"
    REQUIREMENT = "Requirement"
    BUSINESS_RULE = "BusinessRule"
    COMPONENT = "ArchitectureComponent"
    INTERFACE = "Interface"
    DATA_MODEL = "DataModel"
    CODE = "CodeArtifact"
    TEST = "TestCase"
    DECISION = "Decision"
    CONSTRAINT = "Constraint"
    EVIDENCE = "Evidence"


class RelationType(StrEnum):
    CONTAINS = "CONTAINS"
    DERIVED_FROM = "DERIVED_FROM"
    DEPENDS_ON = "DEPENDS_ON"
    IMPLEMENTED_BY = "IMPLEMENTED_BY"
    VERIFIED_BY = "VERIFIED_BY"
    CONSTRAINED_BY = "CONSTRAINED_BY"
    IMPACTS = "IMPACTS"
    SUPERSEDES = "SUPERSEDES"
    CONFLICTS_WITH = "CONFLICTS_WITH"


class EntityStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class RelationStatus(StrEnum):
    PROPOSED = "proposed"
    VERIFIED = "verified"
    REJECTED = "rejected"
    RETIRED = "retired"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Text = Annotated[str, Field(min_length=1, max_length=4000)]
Version = Annotated[int, Field(strict=True, ge=1)]


def validate_uri(value: str) -> str:
    # A locator only: never fetched or executed by the MVP.
    if not urlsplit(value).scheme:
        raise ValueError("use an absolute URI, e.g. https:, git:, file:, or urn:")
    return value


class Provenance(StrictModel):
    actor: Text
    source_uri: Text
    source_revision: Text
    change_reason: Text
    nature: Literal["fact", "human_confirmed", "inference", "assumption"] = "assumption"
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    _uri = field_validator("source_uri")(validate_uri)


class EvidenceRef(StrictModel):
    id: UUID
    version: Version


class Attributes(StrictModel):
    # Explicit extension point, without accepting misspelled core fields.
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class ProjectAttributes(Attributes):
    external_key: Text | None = None


class DomainAttributes(Attributes):
    glossary: dict[str, str] = Field(default_factory=dict)


class RequirementAttributes(Attributes):
    acceptance_criteria: list[Text] = Field(default_factory=list)
    requirement_type: Literal["functional", "nonfunctional"] | None = None


class RuleAttributes(Attributes):
    expression: dict[str, JsonValue] = Field(default_factory=dict)


class ComponentAttributes(Attributes):
    responsibility: Text | None = None


class InterfaceAttributes(Attributes):
    protocol: Text | None = None
    contract_uri: Text | None = None


class DataModelAttributes(Attributes):
    schema_definition: dict[str, JsonValue] = Field(default_factory=dict)


class CodeAttributes(Attributes):
    repository_uri: Text | None = None
    path: Text | None = None
    symbol: Text | None = None
    revision: Text | None = None
    language: Text | None = None


class TestAttributes(Attributes):
    steps: list[Text] = Field(default_factory=list)
    expected_result: Text | None = None


class DecisionAttributes(Attributes):
    rationale: Text | None = None
    alternatives: list[Text] = Field(default_factory=list)


class ConstraintAttributes(Attributes):
    expression: dict[str, JsonValue] = Field(default_factory=dict)


class EvidenceAttributes(Attributes):
    category: Literal["document", "code", "test_execution", "decision", "analysis"]
    uri: Text
    revision: Text
    subject_id: UUID | None = None
    subject_version: Version | None = None
    outcome: Literal["passed", "failed", "unknown"] | None = None
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None

    _uri = field_validator("uri")(validate_uri)

    @model_validator(mode="after")
    def consistent_subject(self):
        if (self.subject_id is None) != (self.subject_version is None):
            raise ValueError("subject_id and subject_version must be supplied together")
        if self.category in {"code", "test_execution"} and self.subject_id is None:
            raise ValueError("code and test_execution evidence require a versioned subject")
        if self.category == "test_execution" and self.outcome is None:
            raise ValueError("test_execution evidence requires an outcome")
        if self.category != "test_execution" and self.outcome is not None:
            raise ValueError("outcome applies only to test_execution evidence")
        return self


ATTRIBUTE_MODELS = dict(zip(Kind, [
    ProjectAttributes, DomainAttributes, RequirementAttributes, RuleAttributes,
    ComponentAttributes, InterfaceAttributes, DataModelAttributes, CodeAttributes,
    TestAttributes, DecisionAttributes, ConstraintAttributes, EvidenceAttributes,
], strict=True))


class RevisionContent(StrictModel):
    provenance: Provenance
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=100)

    @field_validator("evidence")
    @classmethod
    def distinct_evidence(cls, refs):
        if len({(r.id, r.version) for r in refs}) != len(refs):
            raise ValueError("duplicate evidence reference")
        return refs


class EntityContent(RevisionContent):
    name: Annotated[str, Field(min_length=1, max_length=300)]
    description: str = Field(default="", max_length=100000)
    status: EntityStatus = EntityStatus.DRAFT
    attributes: dict[str, Any] = Field(default_factory=dict)


class EntityCreate(EntityContent):
    kind: Kind
    project_id: UUID | None = None

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind == Kind.PROJECT and self.project_id is not None:
            raise ValueError("Project creates its own scope; omit project_id")
        if self.kind != Kind.PROJECT and self.project_id is None:
            raise ValueError("project_id is required for non-Project entities")
        # Surface field errors as API 422 responses before reaching the database.
        self.attributes = ATTRIBUTE_MODELS[self.kind].model_validate(self.attributes).model_dump(mode="json")
        return self


class EntityUpdate(EntityContent):
    expected_version: Version


class RelationContent(RevisionContent):
    source_version: Version
    target_version: Version
    status: RelationStatus = RelationStatus.PROPOSED
    rationale: Text


class RelationCreate(RelationContent):
    type: RelationType
    source_id: UUID
    target_id: UUID


class RelationUpdate(RelationContent):
    expected_version: Version
