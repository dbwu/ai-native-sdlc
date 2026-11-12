"""知识操作：来源快照 → 不可变提案 → 事务校验 → 显式审核提交。"""
import hashlib
from contextlib import contextmanager
from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from .db import connect
from .ingestion import MAX_SOURCE_BYTES, parse_source
from .localization import localize_validation_errors
from .models import EntityCreate, EntityUpdate, RelationCreate, RelationType, RelationUpdate
from .operations_models import IngestionRequest, ProposalCreate, ProposalReview
from .semantics import KnowledgeError
from .service import KnowledgeService


class _PreviewRollback(Exception):
    """试运行成功后，通过保存点回滚全部知识写入。"""


class KnowledgeOperations:
    def __init__(self, database_url: str, *, connection=None):
        self.database_url = database_url
        self._connection = connection

    @contextmanager
    def _transaction(self):
        if self._connection is None:
            with connect(self.database_url) as conn:
                yield conn
        else:
            with self._connection.transaction():
                yield self._connection

    @staticmethod
    def _proposal(conn, proposal_id):
        proposal = conn.execute("SELECT * FROM current_proposals WHERE id=%s", (proposal_id,)).fetchone()
        if proposal is None:
            raise KnowledgeError("未找到指定变更提案", 404)
        return proposal

    @staticmethod
    def _event(conn, proposal_id, version, status, action, actor, reason, result):
        conn.execute("""INSERT INTO proposal_event
            (proposal_id,version,status,action,actor,reason,result) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (proposal_id, version, status, action, actor, reason, Jsonb(result)))

    def _create(self, conn, project_id, title, actor, plan, source_id=None, source_version=None):
        proposal_id = uuid4()
        conn.execute("""INSERT INTO change_proposal
            (id,project_id,source_id,source_version,title,actor,plan) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (proposal_id, project_id, source_id, source_version, title, actor, Jsonb(plan)))
        self._event(conn, proposal_id, 1, "pending", "created", actor, "创建待审查的变更提案", {})
        return self._proposal(conn, proposal_id)

    def ingest(self, request: IngestionRequest):
        candidates = parse_source(request.format, request.content)
        with self._transaction() as conn:
            revision, replayed = self._save_source_revision(conn, request, request.format)
            source_id, version = revision["source_id"], revision["version"]
            if replayed:
                proposal = conn.execute("SELECT id FROM change_proposal WHERE source_id=%s AND source_version=%s",
                                        (source_id, version)).fetchone()
                return {"proposal": self._proposal(conn, proposal["id"]), "replayed": True}
            plan = self._source_plan(conn, source_id, request, candidates)
            proposal = self._create(conn, request.project_id, request.title, request.actor, plan, source_id, version)
            return {"proposal": proposal, "replayed": False}

    def register_source_snapshot(self, request):
        with self._transaction() as conn:
            row, replayed = self._save_source_revision(conn, request, "text")
            return {key: value for key, value in row.items() if key != "content"} | {"replayed": replayed}

    @staticmethod
    def _save_source_revision(conn, request, source_format):
        try:
            raw = request.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise KnowledgeError("来源内容包含无效的 Unicode 字符") from exc
        if len(raw) > MAX_SOURCE_BYTES or "\x00" in request.content:
            raise KnowledgeError("来源原文须为无 NUL 的 UTF-8 文本，最多 1 MiB")
        digest = hashlib.sha256(raw).hexdigest()
        KnowledgeService._lock(conn, request.project_id)
        KnowledgeService._assert_project(conn, request.project_id)
        source = conn.execute("SELECT id FROM knowledge_source WHERE project_id=%s AND uri=%s",
                              (request.project_id, request.source_uri)).fetchone()
        if source is None:
            source = {"id": uuid4()}
            conn.execute("INSERT INTO knowledge_source (id,project_id,uri) VALUES (%s,%s,%s)",
                         (source["id"], request.project_id, request.source_uri))
        previous = conn.execute("SELECT * FROM source_revision WHERE source_id=%s AND external_revision=%s",
                                (source["id"], request.source_revision)).fetchone()
        if previous is not None:
            if previous["sha256"] != digest or previous["format"] != source_format:
                raise KnowledgeError("同一来源版本不能对应不同内容或格式，请提供新的 source_revision", 409)
            return previous, True
        version = conn.execute("SELECT coalesce(max(version),0)+1 AS v FROM source_revision WHERE source_id=%s",
                               (source["id"],)).fetchone()["v"]
        row = conn.execute("""INSERT INTO source_revision
            (source_id,version,external_revision,format,sha256,content,actor) VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING *""", (source["id"], version, request.source_revision, source_format, digest,
                              request.content, request.actor)).fetchone()
        return row, False

    def _source_plan(self, conn, source_id, request, candidates):
        bindings = {row["external_key"]: row["entity_id"] for row in conn.execute(
            "SELECT external_key,entity_id FROM source_entity_binding WHERE source_id=%s", (source_id,)).fetchall()}
        provenance = {"actor": request.actor, "source_uri": request.source_uri,
                      "source_revision": request.source_revision, "change_reason": "从来源快照提取待确认知识",
                      "nature": "inference"}
        operations, baselines, resolved, mapping = [], [], {}, []
        for candidate in candidates["entities"]:
            key = candidate["key"]
            content = {k: v for k, v in candidate.items() if k not in {"key", "kind"}}
            content.update(provenance=provenance, evidence=[])
            record_id = bindings.get(key)
            if record_id is None:
                record_id = uuid5(source_id, "entity:" + key)
                data = EntityCreate(**content, kind=candidate["kind"], project_id=request.project_id)
                operations.append({"op": "create_entity", "id": str(record_id), "data": data.model_dump(mode="json")})
                target_version = 1
            else:
                current = KnowledgeService._get(conn, "entity", record_id)
                if current["kind"] != candidate["kind"]:
                    raise KnowledgeError(f"来源键 {key} 已绑定其他实体类型，不能静默改变身份")
                baselines.append({"table": "entity", "id": str(record_id), "version": current["version"]})
                content["status"] = current["status"]
                changed = any(content[field] != current[field] for field in ("name", "description", "attributes"))
                target_version = current["version"] + int(changed)
                if changed:
                    data = EntityUpdate(**content, expected_version=current["version"])
                    operations.append({"op": "update_entity", "id": str(record_id), "data": data.model_dump(mode="json")})
            resolved[key] = (record_id, target_version)
            mapping.append({"key": key, "entity_id": str(record_id), "version": target_version,
                            "resolution": "existing" if key in bindings else "new"})
        for candidate in candidates["relationships"]:
            source_id_value, source_version = resolved[candidate["source_key"]]
            target_id, target_version = resolved[candidate["target_key"]]
            if candidate["type"] == RelationType.CONFLICTS_WITH and source_id_value > target_id:
                source_id_value, target_id = target_id, source_id_value
                source_version, target_version = target_version, source_version
            existing = conn.execute("""SELECT * FROM current_relationships
                WHERE source_id=%s AND type=%s AND target_id=%s""", (source_id_value, candidate["type"], target_id)).fetchone()
            content = {"source_version": source_version, "target_version": target_version,
                       "status": "proposed", "rationale": candidate["rationale"], "provenance": provenance, "evidence": []}
            if existing:
                baselines.append({"table": "relationship", "id": str(existing["id"]), "version": existing["version"]})
                changed = (source_version != existing["source_version"] or target_version != existing["target_version"]
                           or content["rationale"] != existing["document"]["rationale"])
                if changed:
                    data = RelationUpdate(**content, expected_version=existing["version"])
                    operations.append({"op": "update_relationship", "id": str(existing["id"]), "data": data.model_dump(mode="json")})
            else:
                data = RelationCreate(**content, type=candidate["type"], source_id=source_id_value, target_id=target_id)
                operations.append({"op": "create_relationship", "id": str(uuid4()), "data": data.model_dump(mode="json")})
        return {"operations": operations, "baselines": baselines, "entity_mapping": mapping,
                "removed_keys": sorted(set(bindings) - set(resolved)),
                "message": "候选知识尚未入库；缺失来源键仅作提示，不自动删除或归档。"}

    def create_proposal(self, request: ProposalCreate):
        with self._transaction() as conn:
            KnowledgeService._lock(conn, request.project_id)
            KnowledgeService._assert_project(conn, request.project_id)
            seen = set()
            operations, baselines = [], []
            for change in request.changes:
                table = "entity" if change.op == "update_entity" else "relationship"
                key = (table, change.id)
                if key in seen:
                    raise KnowledgeError("同一提案不能多次修订同一个实体或关系")
                seen.add(key)
                current = KnowledgeService._get(conn, table, change.id)
                if current["project_id"] != request.project_id:
                    raise KnowledgeError("提案不能修改其他项目的知识")
                operations.append(change.model_dump(mode="json"))
                baselines.append({"table": table, "id": str(change.id), "version": change.data.expected_version})
            return self._create(conn, request.project_id, request.title, request.actor,
                                {"operations": operations, "baselines": baselines, "entity_mapping": [], "removed_keys": []})

    def _apply(self, conn, proposal):
        KnowledgeService._assert_project(conn, proposal["project_id"])
        if proposal["source_id"]:
            latest = conn.execute("SELECT max(version) AS v FROM source_revision WHERE source_id=%s",
                                  (proposal["source_id"],)).fetchone()["v"]
            if latest != proposal["source_version"]:
                raise KnowledgeError("来源已有更新快照，请基于最新来源版本生成提案", 409)
        plan = proposal["plan"]
        if 'design_baseline_version' not in plan and conn.execute('''SELECT 1 FROM current_design_flows
                WHERE project_id=%s AND definition ? 'project_model_version' AND state->>'design_proposal_id'=%s''',
                (proposal['project_id'], str(proposal['id']))).fetchone():
            raise KnowledgeError('请先在需求设计页重新关联固定的项目基线，再校验或提交设计', 409)
        extraction = plan.get("extraction")
        binding_source = proposal['source_id']
        if extraction:
            if guard := extraction.get('project_state_token'):
                from .extraction_context import project_state_token
                if project_state_token(conn, proposal['project_id']) != guard:
                    raise KnowledgeError('知识或项目基线已变化，请重新抽取并审阅增量及影响；不会沿用过期决定', 409)
            binding_source = UUID(extraction['source_id'])
            row = conn.execute('''SELECT r.version,r.sha256 FROM source_revision r JOIN knowledge_source s ON s.id=r.source_id
                WHERE s.project_id=%s AND s.id=%s ORDER BY r.version DESC LIMIT 1''',
                (proposal['project_id'], binding_source)).fetchone()
            if row is None or row['version'] != extraction['source_version'] or row['sha256'] != extraction['source_sha256']:
                raise KnowledgeError('抽取原文已更新，请基于最新原文重新抽取和审阅', 409)
            # 与知识写入处于同一事务，防止两个来源提案互相覆盖稳定键绑定。
            for mapping in plan['entity_mapping']:
                rows = conn.execute('''SELECT external_key,entity_id FROM source_entity_binding
                    WHERE source_id=%s AND (external_key=%s OR entity_id=%s)''',
                    (binding_source, mapping['key'], UUID(mapping['entity_id']))).fetchall()
                if any(row['external_key'] != mapping['key'] or str(row['entity_id']) != mapping['entity_id'] for row in rows):
                    raise KnowledgeError('来源知识稳定键的绑定已变化，请重新抽取和审阅', 409)
            for mapping in extraction['relationship_mapping']:
                rows = conn.execute('''SELECT external_key,relationship_id FROM source_relationship_binding
                    WHERE source_id=%s AND (external_key=%s OR relationship_id=%s)''',
                    (binding_source, mapping['key'], UUID(mapping['relationship_id']))).fetchall()
                if any(row['external_key'] != mapping['key'] or str(row['relationship_id']) != mapping['relationship_id'] for row in rows):
                    raise KnowledgeError('来源关系稳定键的绑定已变化，请重新抽取和审阅', 409)
        project_model = plan.get("project_model")
        if project_model:
            latest = conn.execute("SELECT coalesce(max(version), 0) AS version FROM project_model_revision WHERE project_id=%s",
                                  (proposal["project_id"],)).fetchone()["version"]
            if latest + 1 != project_model["version"]:
                raise KnowledgeError("项目基线已更新，请刷新并在最新版上重新提交", 409)
        for baseline in plan["baselines"]:
            current = KnowledgeService._get(conn, baseline["table"], UUID(baseline["id"]))
            if current["version"] != baseline["version"]:
                raise KnowledgeError("提案基线已过期，请重新采集或创建新的修订提案", 409)
        service = KnowledgeService(self.database_url, connection=conn)
        result = []
        for operation in plan["operations"]:
            record_id, data = UUID(operation["id"]), operation["data"]
            if operation["op"] == "create_entity":
                record = service.create_entity(EntityCreate(**data), _record_id=record_id)
            elif operation["op"] == "update_entity":
                record = service.update_entity(record_id, EntityUpdate(**data))
            elif operation["op"] == "create_relationship":
                record = service.create_relationship(RelationCreate(**data), _record_id=record_id)
            else:
                record = service.update_relationship(record_id, RelationUpdate(**data))
            if record["project_id"] != proposal["project_id"]:
                raise KnowledgeError("提案不能修改其他项目的知识")
            result.append({"op": operation["op"], "id": str(record["id"]), "version": record["version"]})
        if binding_source:
            for mapping in plan["entity_mapping"]:
                existing = conn.execute("SELECT entity_id FROM source_entity_binding WHERE source_id=%s AND external_key=%s",
                                        (binding_source, mapping["key"])).fetchone()
                if existing is None:
                    conn.execute("""INSERT INTO source_entity_binding
                        (source_id,project_id,external_key,entity_id) VALUES (%s,%s,%s,%s)""",
                        (binding_source, proposal["project_id"], mapping["key"], UUID(mapping["entity_id"])))
                elif str(existing["entity_id"]) != mapping["entity_id"]:
                    raise KnowledgeError("来源键的稳定实体映射发生冲突", 409)
        if extraction:
            for mapping in extraction['relationship_mapping']:
                conn.execute('''INSERT INTO source_relationship_binding(source_id,project_id,external_key,relationship_id)
                    VALUES (%s,%s,%s,%s) ON CONFLICT (source_id,external_key) DO NOTHING''',
                    (binding_source, proposal['project_id'], mapping['key'], UUID(mapping['relationship_id'])))
        if project_model:
            conn.execute("INSERT INTO project_model_revision (project_id,version,proposal_id,document) VALUES (%s,%s,%s,%s)",
                         (proposal["project_id"], project_model["version"], proposal["id"], Jsonb(project_model)))
        return {"operations": result, "operation_count": len(result), "entity_mapping": plan["entity_mapping"]}

    def _preview(self, conn, proposal):
        try:
            with conn.transaction():
                result = self._apply(conn, proposal)
                raise _PreviewRollback()
        except _PreviewRollback:
            return {"valid": True, "errors": [], "preview": result}
        except KnowledgeError as exc:
            return {"valid": False, "errors": [{"status_code": exc.status_code, "message": exc.message}]}
        except ValidationError as exc:
            errors = localize_validation_errors(exc.errors(include_input=False))
            for error in errors:
                error.pop("ctx", None)
            return {"valid": False, "errors": [{"status_code": 422, "message": "实体或关系内容未通过结构校验", "details": errors}]}

    def review(self, proposal_id: UUID, request: ProposalReview, action: str):
        if action not in {"validate", "commit", "reject"}:
            raise ValueError("不支持的提案操作")
        with self._transaction() as conn:
            proposal = self._proposal(conn, proposal_id)
            KnowledgeService._lock(conn, proposal["project_id"])
            proposal = self._proposal(conn, proposal_id)
            if action == "commit" and proposal["status"] == "committed":
                return {"proposal": proposal, "replayed": True}
            if proposal["version"] != request.expected_version:
                raise KnowledgeError("提案 expected_version 已过期，请读取最新提案版本", 409)
            if proposal["status"] in {"committed", "rejected"}:
                raise KnowledgeError("已提交或已拒绝的提案不能再修改", 409)
            KnowledgeService._assert_project(conn, proposal["project_id"])
            if action == "validate":
                result = self._preview(conn, proposal)
                status, event_action = ("validated" if result["valid"] else "pending"), "validated"
            elif action == "commit":
                if proposal["status"] != "validated":
                    raise KnowledgeError("提案必须先通过校验，再显式提交", 409)
                result = self._apply(conn, proposal)
                status = event_action = "committed"
            else:
                result = {}
                status = event_action = "rejected"
            self._event(conn, proposal_id, proposal["version"] + 1, status, event_action,
                        request.actor, request.reason, result)
            return {"proposal": self._proposal(conn, proposal_id), "replayed": False}

    def get_proposal(self, proposal_id: UUID):
        with connect(self.database_url) as conn:
            return self._proposal(conn, proposal_id)

    def list_proposals(self, project_id: UUID, status=None, limit=50, offset=0):
        KnowledgeService._page(limit, offset)
        if status is not None and status not in {"pending", "validated", "committed", "rejected"}:
            raise KnowledgeError("不支持的提案状态")
        with connect(self.database_url) as conn:
            return conn.execute("""SELECT * FROM current_proposals WHERE project_id=%s
                AND (%s::text IS NULL OR status=%s) ORDER BY created_at DESC,id LIMIT %s OFFSET %s""",
                (project_id, status, status, limit, offset)).fetchall()

    def history(self, proposal_id: UUID, limit=50, offset=0):
        KnowledgeService._page(limit, offset)
        with connect(self.database_url) as conn:
            self._proposal(conn, proposal_id)
            return conn.execute("""SELECT * FROM proposal_event WHERE proposal_id=%s
                ORDER BY version DESC LIMIT %s OFFSET %s""", (proposal_id, limit, offset)).fetchall()

    def list_sources(self, project_id: UUID, limit=50, offset=0):
        KnowledgeService._page(limit, offset)
        with connect(self.database_url) as conn:
            return conn.execute("""SELECT s.*, r.version, r.external_revision, r.format, r.sha256, r.recorded_at
                FROM knowledge_source s JOIN LATERAL (SELECT * FROM source_revision WHERE source_id=s.id
                ORDER BY version DESC LIMIT 1) r ON true WHERE s.project_id=%s
                ORDER BY s.created_at,s.id LIMIT %s OFFSET %s""", (project_id, limit, offset)).fetchall()

    def source_version(self, source_id: UUID, version: int):
        with connect(self.database_url) as conn:
            result = conn.execute("SELECT * FROM source_revision WHERE source_id=%s AND version=%s", (source_id, version)).fetchone()
            if result is None:
                raise KnowledgeError("未找到来源快照", 404)
            return result
