"""元模型第 1 版的语义策略，不包含组织特定的审批流程。"""
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
        raise KnowledgeError(f"关系 {relation_type} 不允许从 {source} 指向 {target}")
    if relation_type == R.CONTAINS and source == K.COMPONENT and target not in TECHNICAL:
        raise KnowledgeError("架构组件 ArchitectureComponent 只能包含技术类实体")
    if relation_type == R.SUPERSEDES and source != target:
        raise KnowledgeError("替代关系 SUPERSEDES 的两个实体必须属于同一类型")


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
        raise KnowledgeError(f"{old} 是终止状态，不能再提交修订", 409)
    if new != old and new not in transitions[old]:
        raise KnowledgeError(f"不允许的生命周期状态迁移：{old} → {new}", 409)
