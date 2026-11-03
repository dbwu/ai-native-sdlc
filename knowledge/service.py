from contextlib import contextmanager
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from .db import connect
from .models import (
    ATTRIBUTE_MODELS, EntityCreate, EntityUpdate, Kind, RelationCreate,
    RelationType, RelationUpdate,
)
from .semantics import (
    ENTITY_TRANSITIONS, RELATION_TRANSITIONS, KnowledgeError,
    validate_transition, validate_types,
)


class KnowledgeService:
    """所有知识写入都必须经过此处的事务和语义校验。

    项目级咨询锁将图关系校验和修订版本分配串行化。
    expected_version 用于避免无提示地覆盖其他提交者的并发修改。
    """

    def __init__(self, database_url: str, *, connection=None):
        self.database_url = database_url
        # 第二阶段批量提案共用外层事务；单个操作仍通过保存点隔离失败。
        self._connection = connection

    @contextmanager
    def _transaction(self):
        try:
            if self._connection is not None:
                with self._connection.transaction():
                    yield self._connection
            else:
                with connect(self.database_url) as conn:
                    yield conn
        except psycopg.errors.UniqueViolation as exc:
            raise KnowledgeError("实体标识或关系已存在", 409) from exc
        except psycopg.IntegrityError as exc:
            raise KnowledgeError("写入不满足数据库完整性约束") from exc

    @staticmethod
    def _lock(conn, project_id):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(project_id),))

    @staticmethod
    def _get(conn, table, record_id, version=None):
        # table 仅使用内部常量，不接受 API 参数输入。
        if version is None:
            view = "current_entities" if table == "entity" else "current_relationships"
            row = conn.execute(f"SELECT * FROM {view} WHERE id=%s", (record_id,)).fetchone()
        else:
            extras = ", r.source_version, r.target_version" if table == "relationship" else ""
            row = conn.execute(f"""SELECT e.*, r.version, r.metamodel_version, r.status,
                r.document, r.recorded_at {extras}
                FROM {table} e JOIN {table}_revision r ON e.id=r.{table}_id
                WHERE e.id=%s AND r.version=%s""", (record_id, version)).fetchone()
        if row is None:
            label = "实体" if table == "entity" else "关系"
            raise KnowledgeError(f"未找到指定{label}或修订版本", 404)
        evidence = conn.execute(f"""SELECT evidence_id AS id, evidence_version AS version
            FROM {table}_evidence WHERE {table}_id=%s AND version=%s
            ORDER BY evidence_id, evidence_version""", (row["id"], row["version"])).fetchall()
        document = row.pop("document")
        return {**row, **document, "evidence": evidence}

    @staticmethod
    def _assert_project(conn, project_id):
        project = KnowledgeService._get(conn, "entity", project_id)
        if project["kind"] != Kind.PROJECT:
            raise KnowledgeError("project_id 必须指向项目类型 Project 的实体")
        if project["status"] == "archived":
            raise KnowledgeError("项目已归档，不能再写入知识", 409)

    @staticmethod
    def _check_evidence(conn, project_id, refs):
        records = []
        for ref in refs:
            record = KnowledgeService._get(conn, "entity", ref.id, ref.version)
            if record["kind"] != Kind.EVIDENCE or record["project_id"] != project_id:
                raise KnowledgeError("证据必须引用同一项目内 Evidence 实体的具体修订版本")
            records.append(record)
        return records

    @staticmethod
    def _append_evidence(conn, table, record_id, version, refs):
        for ref in refs:
            conn.execute(f"""INSERT INTO {table}_evidence
                ({table}_id,version,evidence_id,evidence_version) VALUES (%s,%s,%s,%s)""",
                (record_id, version, ref.id, ref.version))

    def _entity_revision(self, conn, identity, version, content):
        attributes = ATTRIBUTE_MODELS[Kind(identity["kind"])].model_validate(content.attributes)
        self._check_evidence(conn, identity["project_id"], content.evidence)
        if identity["kind"] == Kind.EVIDENCE and attributes.subject_id:
            subject = self._get(conn, "entity", attributes.subject_id, attributes.subject_version)
            if subject["project_id"] != identity["project_id"]:
                raise KnowledgeError("证据所指实体必须属于同一项目")
            if attributes.category == "test_execution" and subject["kind"] != Kind.TEST:
                raise KnowledgeError("测试执行证据必须指向测试用例 TestCase 的具体修订版本")
            if attributes.category == "code" and subject["kind"] not in {Kind.CODE, Kind.COMPONENT}:
                raise KnowledgeError("代码证据必须指向代码资产 CodeArtifact 或架构组件 ArchitectureComponent 的具体修订版本")
        document = {
            "name": content.name, "description": content.description,
            "attributes": attributes.model_dump(mode="json"),
            "provenance": content.provenance.model_dump(mode="json"),
        }
        conn.execute("""INSERT INTO entity_revision (entity_id,version,status,document)
            VALUES (%s,%s,%s,%s)""", (identity["id"], version, content.status.value, Jsonb(document)))
        self._append_evidence(conn, "entity", identity["id"], version, content.evidence)

    def create_entity(self, request: EntityCreate, *, _record_id: UUID | None = None):
        record_id = _record_id or uuid4()
        project_id = record_id if request.kind == Kind.PROJECT else request.project_id
        with self._transaction() as conn:
            self._lock(conn, project_id)
            if request.kind != Kind.PROJECT:
                self._assert_project(conn, project_id)
            identity = {"id": record_id, "project_id": project_id, "kind": request.kind.value}
            conn.execute("INSERT INTO entity (id,project_id,kind) VALUES (%s,%s,%s)",
                         (record_id, project_id, request.kind.value))
            self._entity_revision(conn, identity, 1, request)
            return self._get(conn, "entity", record_id)

    def update_entity(self, record_id: UUID, request: EntityUpdate):
        with self._transaction() as conn:
            original = self._get(conn, "entity", record_id)
            self._lock(conn, original["project_id"])
            original = self._get(conn, "entity", record_id)
            self._assert_project(conn, original["project_id"])
            if original["version"] != request.expected_version:
                raise KnowledgeError("expected_version 已过期，请读取最新修订后再提交", 409)
            validate_transition(original["status"], request.status, ENTITY_TRANSITIONS)
            self._entity_revision(conn, original, original["version"] + 1, request)
            return self._get(conn, "entity", record_id)

    def get_entity(self, record_id: UUID, version: int | None = None):
        with connect(self.database_url) as conn:
            return self._get(conn, "entity", record_id, version)

    def get_relationship(self, record_id: UUID, version: int | None = None):
        with connect(self.database_url) as conn:
            return self._get(conn, "relationship", record_id, version)

    def history(self, table, record_id: UUID, limit=50, offset=0):
        if table not in {"entity", "relationship"}:
            raise ValueError("不支持的历史记录表")
        self._page(limit, offset)
        with connect(self.database_url) as conn:
            self._get(conn, table, record_id)
            versions = conn.execute(f"""SELECT version FROM {table}_revision
                WHERE {table}_id=%s ORDER BY version DESC LIMIT %s OFFSET %s""",
                (record_id, limit, offset)).fetchall()
            return [self._get(conn, table, record_id, row["version"]) for row in versions]

    @staticmethod
    def _page(limit, offset):
        if not 1 <= limit <= 200 or offset < 0:
            raise KnowledgeError("limit 必须为 1 至 200，offset 必须大于或等于 0")

    def list_entities(self, project_id: UUID, kind=None, status=None, q=None, limit=50, offset=0):
        self._page(limit, offset)
        clauses, params = ["project_id=%s"], [project_id]
        if kind is not None:
            clauses.append("kind=%s")
            params.append(str(kind))
        if status is not None:
            clauses.append("status=%s")
            params.append(str(status))
        if q:
            clauses.append("strpos(lower((document->>'name') || ' ' || (document->>'description')),lower(%s)) > 0")
            params.append(q)
        with connect(self.database_url) as conn:
            rows = conn.execute(f"""SELECT id FROM current_entities WHERE {' AND '.join(clauses)}
                ORDER BY created_at, id LIMIT %s OFFSET %s""", (*params, limit, offset)).fetchall()
            return [self._get(conn, "entity", row["id"]) for row in rows]

    def list_relationships(self, project_id: UUID, entity_id=None, relation_type=None,
                           status=None, direction="both", limit=50, offset=0):
        self._page(limit, offset)
        if direction not in {"both", "outbound", "inbound"}:
            raise KnowledgeError("不支持的关系方向，请使用 both、outbound 或 inbound")
        clauses, params = ["project_id=%s"], [project_id]
        if entity_id is not None:
            if direction == "both":
                clauses.append("(source_id=%s OR target_id=%s)")
                params.extend([entity_id, entity_id])
            else:
                clauses.append("source_id=%s" if direction == "outbound" else "target_id=%s")
                params.append(entity_id)
        if relation_type is not None:
            clauses.append("type=%s")
            params.append(str(relation_type))
        if status is not None:
            clauses.append("status=%s")
            params.append(str(status))
        with connect(self.database_url) as conn:
            rows = conn.execute(f"""SELECT id FROM current_relationships WHERE {' AND '.join(clauses)}
                ORDER BY created_at, id LIMIT %s OFFSET %s""", (*params, limit, offset)).fetchall()
            return [self._get(conn, "relationship", row["id"]) for row in rows]

    @staticmethod
    def _no_cycle(conn, identity):
        if identity["type"] not in {RelationType.CONTAINS, RelationType.SUPERSEDES}:
            return
        cycle = conn.execute("""WITH RECURSIVE reachable(id) AS (
            SELECT %s::uuid
            UNION
            SELECT r.target_id FROM current_relationships r JOIN reachable p ON r.source_id=p.id
            WHERE r.project_id=%s AND r.type=%s AND r.status IN ('proposed','verified') AND r.id<>%s
        ) SELECT 1 FROM reachable WHERE id=%s LIMIT 1""",
            (identity["target_id"], identity["project_id"], identity["type"], identity["id"], identity["source_id"])).fetchone()
        if cycle:
            raise KnowledgeError(f"该 {identity['type']} 关系会形成环路", 409)

    def _relationship_revision(self, conn, identity, version, content):
        source = self._get(conn, "entity", identity["source_id"], content.source_version)
        target = self._get(conn, "entity", identity["target_id"], content.target_version)
        validate_types(RelationType(identity["type"]), Kind(source["kind"]), Kind(target["kind"]))
        evidence = self._check_evidence(conn, identity["project_id"], content.evidence)
        if content.status in {"proposed", "verified"}:
            if source["status"] == "archived" or target["status"] == "archived":
                raise KnowledgeError("待确认或已验证的关系不能引用已归档的端点修订版本")
            self._no_cycle(conn, identity)
        if content.status == "verified":
            if content.provenance.nature not in {"fact", "human_confirmed"}:
                raise KnowledgeError("已验证关系的来源性质不能是推断或假设")
            active_evidence = [e for e in evidence if e["status"] == "active"
                               and e["provenance"]["nature"] in {"fact", "human_confirmed"}]
            if not active_evidence:
                raise KnowledgeError("已验证关系需要生效且性质为事实或人工确认的证据")
            required_category = {"IMPLEMENTED_BY": "code", "VERIFIED_BY": "test_execution"}.get(identity["type"])
            if required_category:
                matching = [e for e in active_evidence if (
                    e["attributes"]["category"] == required_category
                    and e["attributes"]["subject_id"] == str(target["id"])
                    and e["attributes"]["subject_version"] == target["version"]
                    and (required_category != "test_execution" or e["attributes"]["outcome"] == "passed")
                )]
                if not matching:
                    category_label = "代码证据" if required_category == "code" else "结果为通过的测试执行证据"
                    raise KnowledgeError(f"已验证的 {identity['type']} 关系需要与目标实体具体版本匹配的{category_label}")
        document = {"rationale": content.rationale, "provenance": content.provenance.model_dump(mode="json")}
        conn.execute("""INSERT INTO relationship_revision
            (relationship_id,version,source_id,target_id,source_version,target_version,status,document)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (
                identity["id"], version, source["id"], target["id"], content.source_version,
                content.target_version, content.status.value, Jsonb(document)))
        self._append_evidence(conn, "relationship", identity["id"], version, content.evidence)

    def create_relationship(self, request: RelationCreate, *, _record_id: UUID | None = None):
        if request.source_id == request.target_id:
            raise KnowledgeError("不允许实体与自身建立关系")
        # 对称关系采用统一的存储方向，同时避免反向重复。
        if request.type == RelationType.CONFLICTS_WITH and request.source_id > request.target_id:
            request = request.model_copy(update={
                "source_id": request.target_id, "target_id": request.source_id,
                "source_version": request.target_version, "target_version": request.source_version,
            })
        with self._transaction() as conn:
            source = self._get(conn, "entity", request.source_id)
            self._lock(conn, source["project_id"])
            self._assert_project(conn, source["project_id"])
            target = self._get(conn, "entity", request.target_id)
            if source["project_id"] != target["project_id"]:
                raise KnowledgeError("不允许建立跨项目关系")
            identity = {"id": _record_id or uuid4(), "project_id": source["project_id"], "type": request.type.value,
                        "source_id": request.source_id, "target_id": request.target_id}
            conn.execute("""INSERT INTO relationship (id,project_id,type,source_id,target_id)
                VALUES (%s,%s,%s,%s,%s)""", tuple(identity.values()))
            self._relationship_revision(conn, identity, 1, request)
            return self._get(conn, "relationship", identity["id"])

    def update_relationship(self, record_id: UUID, request: RelationUpdate):
        with self._transaction() as conn:
            original = self._get(conn, "relationship", record_id)
            self._lock(conn, original["project_id"])
            self._assert_project(conn, original["project_id"])
            original = self._get(conn, "relationship", record_id)
            if original["version"] != request.expected_version:
                raise KnowledgeError("expected_version 已过期，请读取最新修订后再提交", 409)
            validate_transition(original["status"], request.status, RELATION_TRANSITIONS)
            self._relationship_revision(conn, original, original["version"] + 1, request)
            return self._get(conn, "relationship", record_id)
