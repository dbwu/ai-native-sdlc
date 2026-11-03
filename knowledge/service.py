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
    """All semantic writes go through this transactional boundary.

    Project advisory locks serialize graph validation and revision allocation.
    expected_version prevents silently overwriting a concurrent author's work.
    """

    def __init__(self, database_url: str):
        self.database_url = database_url

    @contextmanager
    def _transaction(self):
        try:
            with connect(self.database_url) as conn:
                yield conn
        except psycopg.errors.UniqueViolation as exc:
            raise KnowledgeError("Identity or relationship already exists", 409) from exc
        except psycopg.IntegrityError as exc:
            raise KnowledgeError("Database integrity constraint rejected the write") from exc

    @staticmethod
    def _lock(conn, project_id):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(project_id),))

    @staticmethod
    def _get(conn, table, record_id, version=None):
        # table is an internal constant, never an API parameter.
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
            raise KnowledgeError(f"{table} or revision not found", 404)
        evidence = conn.execute(f"""SELECT evidence_id AS id, evidence_version AS version
            FROM {table}_evidence WHERE {table}_id=%s AND version=%s
            ORDER BY evidence_id, evidence_version""", (row["id"], row["version"])).fetchall()
        document = row.pop("document")
        return {**row, **document, "evidence": evidence}

    @staticmethod
    def _assert_project(conn, project_id):
        project = KnowledgeService._get(conn, "entity", project_id)
        if project["kind"] != Kind.PROJECT:
            raise KnowledgeError("project_id must identify a Project")
        if project["status"] == "archived":
            raise KnowledgeError("Project is archived", 409)

    @staticmethod
    def _check_evidence(conn, project_id, refs):
        records = []
        for ref in refs:
            record = KnowledgeService._get(conn, "entity", ref.id, ref.version)
            if record["kind"] != Kind.EVIDENCE or record["project_id"] != project_id:
                raise KnowledgeError("Evidence must reference an Evidence revision in the same project")
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
                raise KnowledgeError("Evidence subject must belong to the same project")
            if attributes.category == "test_execution" and subject["kind"] != Kind.TEST:
                raise KnowledgeError("test_execution evidence must refer to a TestCase revision")
            if attributes.category == "code" and subject["kind"] not in {Kind.CODE, Kind.COMPONENT}:
                raise KnowledgeError("code evidence must refer to a CodeArtifact or ArchitectureComponent revision")
        document = {
            "name": content.name, "description": content.description,
            "attributes": attributes.model_dump(mode="json"),
            "provenance": content.provenance.model_dump(mode="json"),
        }
        conn.execute("""INSERT INTO entity_revision (entity_id,version,status,document)
            VALUES (%s,%s,%s,%s)""", (identity["id"], version, content.status.value, Jsonb(document)))
        self._append_evidence(conn, "entity", identity["id"], version, content.evidence)

    def create_entity(self, request: EntityCreate):
        record_id = uuid4()
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
                raise KnowledgeError("Stale expected_version; reload the current revision", 409)
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
            raise ValueError("invalid history table")
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
            raise KnowledgeError("limit must be 1..200 and offset must be nonnegative")

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
            raise KnowledgeError("invalid relationship direction")
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
            raise KnowledgeError(f"{identity['type']} would form a cycle", 409)

    def _relationship_revision(self, conn, identity, version, content):
        source = self._get(conn, "entity", identity["source_id"], content.source_version)
        target = self._get(conn, "entity", identity["target_id"], content.target_version)
        validate_types(RelationType(identity["type"]), Kind(source["kind"]), Kind(target["kind"]))
        evidence = self._check_evidence(conn, identity["project_id"], content.evidence)
        if content.status in {"proposed", "verified"}:
            if source["status"] == "archived" or target["status"] == "archived":
                raise KnowledgeError("Live relationships cannot pin archived endpoint revisions")
            self._no_cycle(conn, identity)
        if content.status == "verified":
            if content.provenance.nature not in {"fact", "human_confirmed"}:
                raise KnowledgeError("Verified relationships cannot be an inference or assumption")
            active_evidence = [e for e in evidence if e["status"] == "active"
                               and e["provenance"]["nature"] in {"fact", "human_confirmed"}]
            if not active_evidence:
                raise KnowledgeError("Verified relationships require active evidence")
            required_category = {"IMPLEMENTED_BY": "code", "VERIFIED_BY": "test_execution"}.get(identity["type"])
            if required_category:
                matching = [e for e in active_evidence if (
                    e["attributes"]["category"] == required_category
                    and e["attributes"]["subject_id"] == str(target["id"])
                    and e["attributes"]["subject_version"] == target["version"]
                    and (required_category != "test_execution" or e["attributes"]["outcome"] == "passed")
                )]
                if not matching:
                    raise KnowledgeError(f"Verified {identity['type']} requires matching {required_category} evidence for the pinned target revision")
        document = {"rationale": content.rationale, "provenance": content.provenance.model_dump(mode="json")}
        conn.execute("""INSERT INTO relationship_revision
            (relationship_id,version,source_id,target_id,source_version,target_version,status,document)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (
                identity["id"], version, source["id"], target["id"], content.source_version,
                content.target_version, content.status.value, Jsonb(document)))
        self._append_evidence(conn, "relationship", identity["id"], version, content.evidence)

    def create_relationship(self, request: RelationCreate):
        if request.source_id == request.target_id:
            raise KnowledgeError("Self-relationships are not permitted")
        # Canonical symmetric storage also prevents reverse duplicates.
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
                raise KnowledgeError("Cross-project relationships are not permitted")
            identity = {"id": uuid4(), "project_id": source["project_id"], "type": request.type.value,
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
                raise KnowledgeError("Stale expected_version; reload the current revision", 409)
            validate_transition(original["status"], request.status, RELATION_TRANSITIONS)
            self._relationship_revision(conn, original, original["version"] + 1, request)
            return self._get(conn, "relationship", record_id)
