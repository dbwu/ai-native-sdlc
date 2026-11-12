"""工程知识元模型第 1 版的只读类型目录。"""

from .localization import LABELS
from .models import ATTRIBUTE_MODELS, EntityStatus, EvidenceRef, Kind, Provenance, RelationType
from .semantics import ENTITY_TRANSITIONS, RELATION_TRANSITIONS, RULES, KnowledgeError, validate_types
from .source_spans import SourceSpan


def _allowed_targets(relation):
    sources, targets = RULES[relation]
    result = {}
    for source in sorted(sources):
        result[source] = []
        for target in sorted(targets):
            try:
                validate_types(relation, source, target)
            except KnowledgeError:
                continue
            result[source].append(target)
    return result


def _lifecycle(transitions, labels):
    return {
        status: {
            "label": labels[status],
            "allowed_next": sorted({status, *targets}) if targets else [],
        }
        for status, targets in transitions.items()
    }


def metamodel_registry():
    return {
        "metamodel_version": 1,
        "entity_types": {
            kind: {"label": LABELS["entity_kinds"][kind], "attributes_schema": model.model_json_schema()}
            for kind, model in ATTRIBUTE_MODELS.items()
        },
        "relationship_types": {
            relation: {
                "label": LABELS["relationship_types"][relation],
                "symmetric": relation == RelationType.CONFLICTS_WITH,
                "targets_by_source": _allowed_targets(relation),
            }
            for relation in RelationType
        },
        "relationship_policy": {
            "same_project": True,
            "distinct_endpoints": True,
            "versioned_endpoints": True,
            "reject_archived_endpoints_for": ["proposed", "verified"],
            "acyclic_types": [RelationType.CONTAINS, RelationType.SUPERSEDES],
            "cycle_check_statuses": ["proposed", "verified"],
        },
        "entity_lifecycle": _lifecycle(ENTITY_TRANSITIONS, LABELS["entity_statuses"]),
        "relationship_lifecycle": _lifecycle(RELATION_TRANSITIONS, LABELS["relationship_statuses"]),
        "provenance_schema": Provenance.model_json_schema(),
        "evidence_policy": {
            "reference_schema": EvidenceRef.model_json_schema(),
            "same_project": True,
            "source_span": {
                "category": "document",
                "attributes_path": "extensions.source_span",
                "offset_unit": "unicode_code_point",
                "range": "zero_based_half_open",
                "schema": SourceSpan.model_json_schema(),
            },
            "subject_kinds_by_category": {"code": [Kind.COMPONENT, Kind.CODE], "test_execution": [Kind.TEST]},
            "verified_relationship": {
                "allowed_provenance_natures": ["fact", "human_confirmed"],
                "allowed_evidence_natures": ["fact", "human_confirmed"],
                "evidence_status": EntityStatus.ACTIVE,
                "min_evidence": 1,
                "additional_requirements": {
                    RelationType.IMPLEMENTED_BY: {"category": "code", "matches_target_version": True},
                    RelationType.VERIFIED_BY: {
                        "category": "test_execution", "outcome": "passed", "matches_target_version": True,
                    },
                },
            },
        },
    }
