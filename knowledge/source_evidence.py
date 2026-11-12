"""通过普通知识提案把一段原文关联到实体或关系。"""
from uuid import uuid4

from .db import connect
from .models import EntityCreate, EntityUpdate, Kind, RelationUpdate
from .operations import KnowledgeOperations
from .semantics import KnowledgeError
from .service import KnowledgeService as KS
from .source_spans import SourceSpan, read_source_span


def _locate(record):
    if record.get("kind") == Kind.EVIDENCE:
        return record["attributes"]["extensions"].get("source_span")
    return None


class SourceEvidenceService:
    def __init__(self, database_url):
        self.database_url = database_url

    def propose(self, request):
        with connect(self.database_url) as conn:
            project_id, target, span = request.project_id, request.target, request.span
            KS._lock(conn, project_id)
            KS._assert_project(conn, project_id)
            record = KS._get(conn, target.table, target.id)
            if record["project_id"] != project_id:
                raise KnowledgeError("未找到本项目的证据引用方", 404)
            if record["version"] != target.version or record["status"] in {"archived", "retired"}:
                raise KnowledgeError("知识已更新或归档，请刷新后再提出证据", 409)
            source = read_source_span(conn, project_id, span)
            span_data = span.model_dump(mode="json")
            for ref in record["evidence"]:
                previous = KS._get(conn, "entity", ref["id"], ref["version"])
                if _locate(previous) == span_data:
                    raise KnowledgeError("此知识已关联同一段原文证据", 409)

            evidence_id = uuid4()
            evidence = EntityCreate(project_id=project_id, kind=Kind.EVIDENCE, name="原文证据", status="active",
                                    description=request.reason, provenance={
                                        "actor": request.actor, "source_uri": source["uri"],
                                        "source_revision": source["external_revision"], "change_reason": request.reason,
                                        "nature": "human_confirmed",
                                    }, attributes={
                                        "category": "document", "uri": source["uri"], "revision": source["external_revision"],
                                        "sha256": span.sha256, "extensions": {"source_span": span_data},
                                    })
            if target.table == "entity":
                fields = ("name", "description", "status", "attributes", "provenance")
                update_model = EntityUpdate
            else:
                fields = ("source_version", "target_version", "status", "rationale", "provenance")
                update_model = RelationUpdate
            update = update_model(**{field: record[field] for field in fields}, expected_version=target.version,
                                  evidence=[*record["evidence"], {"id": evidence_id, "version": 1}])
            plan = {"operations": [
                {"op": "create_entity", "id": str(evidence_id), "data": evidence.model_dump(mode="json")},
                {"op": f"update_{target.table}", "id": str(target.id), "data": update.model_dump(mode="json")},
            ], "baselines": [{"table": target.table, "id": str(target.id), "version": target.version}],
                "entity_mapping": [], "removed_keys": []}
            return KnowledgeOperations(self.database_url, connection=conn)._create(conn, project_id, "关联原文证据", request.actor, plan)

    def read(self, project_id, table, record_id, version):
        with connect(self.database_url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            record = KS._get(conn, table, record_id, version)
            if record["project_id"] != project_id:
                raise KnowledgeError("未找到本项目的证据引用方", 404)
            evidence = [KS._get(conn, "entity", ref["id"], ref["version"]) for ref in record["evidence"]]
            if record.get("kind") == Kind.EVIDENCE:
                evidence.insert(0, record)
            items = []
            for item in evidence:
                location = _locate(item)
                if location is not None:
                    span = SourceSpan.model_validate(location)
                    items.append({"id": item["id"], "version": item["version"], "name": item["name"],
                                  "span": span.model_dump(mode="json"), "source": read_source_span(conn, project_id, span)})
            return {"items": items}
