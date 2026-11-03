"""Metamodel v1 policy. No organization-specific approval workflow."""
from .models import EntityStatus, Kind as K, RelationStatus, RelationType as R


class KnowledgeError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


ALL = set(K)
ENGINEERING = ALL - {K.PROJECT, K.EVIDENCE}
TECHNICAL = {K.COMPONENT, K.INTERFACE, K.DATA_MODEL, K.CODE, K.TEST}
RULES = {
    R.CONTAINS: ({K.PROJECT, K.DOMAIN, K.COMPONENT}, ALL - {K.PROJECT}),
    R.DERIVED_FROM: (ENGINEERING, ALL - {K.PROJECT}),
    R.DEPENDS_ON: (ENGINEERING, ENGINEERING),
    R.IMPLEMENTED_BY: ({K.REQUIREMENT, K.BUSINESS_RULE, K.COMPONENT, K.INTERFACE, K.DATA_MODEL}, {K.COMPONENT, K.CODE}),
    R.VERIFIED_BY: (ENGINEERING - {K.TEST}, {K.TEST}),
    R.CONSTRAINED_BY: (ENGINEERING, {K.CONSTRAINT, K.BUSINESS_RULE, K.DECISION}),
    R.IMPACTS: (ENGINEERING, ENGINEERING),
    R.SUPERSEDES: (ALL - {K.PROJECT}, ALL - {K.PROJECT}),
    R.CONFLICTS_WITH: (ENGINEERING, ENGINEERING),
}


def validate_types(relation_type: R, source: K, target: K):
    sources, targets = RULES[relation_type]
    if source not in sources or target not in targets:
        raise KnowledgeError(f"{relation_type}: {source} -> {target} is not permitted")
    if relation_type == R.CONTAINS and source == K.COMPONENT and target not in TECHNICAL:
        raise KnowledgeError("ArchitectureComponent may contain only technical entities")
    if relation_type == R.SUPERSEDES and source != target:
        raise KnowledgeError("SUPERSEDES requires entities of the same kind")


ENTITY_TRANSITIONS = {
    EntityStatus.DRAFT: {EntityStatus.ACTIVE, EntityStatus.ARCHIVED},
    EntityStatus.ACTIVE: {EntityStatus.DEPRECATED, EntityStatus.ARCHIVED},
    EntityStatus.DEPRECATED: {EntityStatus.ACTIVE, EntityStatus.ARCHIVED},
    EntityStatus.ARCHIVED: set(),
}
RELATION_TRANSITIONS = {
    RelationStatus.PROPOSED: {RelationStatus.VERIFIED, RelationStatus.REJECTED, RelationStatus.RETIRED},
    RelationStatus.VERIFIED: {RelationStatus.PROPOSED, RelationStatus.RETIRED},
    RelationStatus.REJECTED: {RelationStatus.PROPOSED, RelationStatus.RETIRED},
    RelationStatus.RETIRED: set(),
}


def validate_transition(old, new, transitions):
    if not transitions[old]:
        raise KnowledgeError(f"{old} revisions are terminal", 409)
    if new != old and new not in transitions[old]:
        raise KnowledgeError(f"invalid lifecycle transition: {old} -> {new}", 409)
