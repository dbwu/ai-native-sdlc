"""第二阶段的只读知识分析：发现待复核线索，不自动改写工程事实。"""
from contextlib import contextmanager
from uuid import UUID

from .db import connect
from .semantics import KnowledgeError
from .service import KnowledgeService


# 正向表示源实体变化可能影响目标；反向表示依赖项变化需复核依赖方。
FORWARD_TYPES = ["CONTAINS", "IMPACTS", "IMPLEMENTED_BY", "VERIFIED_BY", "CONFLICTS_WITH"]
REVERSE_TYPES = ["DEPENDS_ON", "DERIVED_FROM", "CONSTRAINED_BY", "IMPLEMENTED_BY",
                 "VERIFIED_BY", "CONFLICTS_WITH", "SUPERSEDES"]


class KnowledgeAnalysis:
    """在同一只读快照中分析项目当前图谱，结果均为提示而非自动裁决。"""

    def __init__(self, database_url: str):
        self.database_url = database_url

    @contextmanager
    def _snapshot(self, project_id):
        with connect(self.database_url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            project = KnowledgeService._get(conn, "entity", project_id)
            if project["kind"] != "Project":
                raise KnowledgeError("project_id 必须指向项目类型 Project 的实体")
            yield conn

    @staticmethod
    def _neighbors(conn, project_id, frontier, seen, limit):
        # 每轮仅读取未访问的有限候选，避免将完整项目图谱加载到内存。
        return conn.execute("""WITH oriented AS (
            SELECT r.*, r.source_id AS from_id, r.target_id AS to_id, 'forward' AS direction
            FROM current_relationships r
            WHERE r.project_id=%s AND r.status IN ('proposed','verified') AND r.type=ANY(%s::text[])
            UNION ALL
            SELECT r.*, r.target_id AS from_id, r.source_id AS to_id, 'reverse' AS direction
            FROM current_relationships r
            WHERE r.project_id=%s AND r.status IN ('proposed','verified') AND r.type=ANY(%s::text[])
        ) SELECT DISTINCT ON (r.to_id) r.id AS relationship_id, r.type, r.status AS relationship_status,
            r.source_version, r.target_version, r.from_id, r.to_id, r.direction,
            e.version, e.kind, e.status, e.document->>'name' AS name
        FROM oriented r JOIN current_entities e ON e.id=r.to_id AND e.project_id=%s
        WHERE r.from_id=ANY(%s::uuid[]) AND NOT (r.to_id=ANY(%s::uuid[]))
        ORDER BY r.to_id, r.id, r.direction LIMIT %s""",
            (project_id, FORWARD_TYPES, project_id, REVERSE_TYPES, project_id, frontier, list(seen), limit)).fetchall()

    def impact(self, project_id: UUID, entity_id: UUID, max_depth=4, max_nodes=200):
        """按关系语义做有界广度优先遍历；max_nodes 包含根实体。

        SUPERSEDES 仅反向传播：旧实体变更时提示复核其替代实体。
        每个候选保留一条确定的最短解释路径，历史版本差异在路径中显式保留。
        """
        if not 0 <= max_depth <= 12 or not 1 <= max_nodes <= 1000:
            raise KnowledgeError("max_depth 必须为 0 至 12，max_nodes 必须为 1 至 1000")
        project_id, entity_id = UUID(str(project_id)), UUID(str(entity_id))
        with self._snapshot(project_id) as conn:
            root = KnowledgeService._get(conn, "entity", entity_id)
            if root["project_id"] != project_id:
                raise KnowledgeError("指定实体不属于该项目", 404)
            seen, frontier = {entity_id}, [entity_id]
            paths, affected = {entity_id: []}, []
            truncated = False
            for depth in range(1, max_depth + 1):
                remaining = max_nodes - len(seen)
                rows = self._neighbors(conn, project_id, frontier, seen, remaining + 1)
                if len(rows) > remaining:
                    truncated = True
                next_frontier = []
                for row in rows[:remaining]:
                    edge = {key: row[key] for key in ("relationship_id", "type", "from_id", "to_id", "direction",
                                                       "source_version", "target_version")}
                    edge["status"] = row["relationship_status"]
                    path = paths[row["from_id"]] + [edge]
                    paths[row["to_id"]] = path
                    affected.append({"id": row["to_id"], "version": row["version"], "kind": row["kind"],
                                     "name": row["name"], "status": row["status"], "depth": depth, "path": path,
                                     "code": "POTENTIAL_IMPACT", "message": "关系图谱表明该实体可能需要复核，不代表变更影响已被证实。"})
                    seen.add(row["to_id"])
                    next_frontier.append(row["to_id"])
                frontier = next_frontier
                if truncated or not frontier:
                    break
            # 深度达到边界时，只有确实存在未访问节点才标记结果被截断。
            if not truncated and frontier:
                truncated = bool(self._neighbors(conn, project_id, frontier, seen, 1))
            return {"project_id": project_id,
                    "root": {key: root[key] for key in ("id", "version", "kind", "name", "status")},
                    "affected": affected, "max_depth": max_depth, "max_nodes": max_nodes,
                    "truncated": truncated,
                    "message": "仅基于当前待确认和已验证关系给出潜在影响线索；请结合具体版本与证据人工复核。"}

    def consistency(self, project_id: UUID, limit=100):
        """检查陈旧版本、失效引用、冲突及当前需求的实现／验证缺口。

        limit 限定发现项数，truncated 仅在存在更多发现项时为真。
        所有发现均为警告；检查不会改变状态、关系或证据。
        """
        if not 1 <= limit <= 1000:
            raise KnowledgeError("limit 必须为 1 至 1000")
        project_id = UUID(str(project_id))
        findings = []
        with self._snapshot(project_id) as conn:
            def collect(query, params, code, message, scope="relationship"):
                if len(findings) > limit:
                    return
                rows = conn.execute(query + " LIMIT %s", (*params, limit + 1 - len(findings))).fetchall()
                for row in rows:
                    record_id, version = row.pop("id"), row.pop("version")
                    findings.append({"code": code, "severity": "warning", "message": message,
                                     f"{scope}_id": record_id, "version": version, "context": row})

            endpoints = """FROM current_relationships r
                JOIN current_entities s ON s.id=r.source_id
                JOIN current_entities t ON t.id=r.target_id
                WHERE r.project_id=%s AND r.status IN ('proposed','verified')"""
            collect("""SELECT r.id,r.version,r.source_id,r.target_id,r.source_version,r.target_version,
                s.version AS current_source_version,t.version AS current_target_version """ + endpoints + """
                AND (r.source_version<>s.version OR r.target_version<>t.version) ORDER BY r.id""",
                (project_id,), "STALE_ENDPOINT", "关系适用的实体版本已不是最新版本，需要复核关系是否仍然成立。")
            collect("""SELECT r.id,r.version,r.source_id,r.target_id,s.status AS source_status,t.status AS target_status """
                + endpoints + """ AND (s.status IN ('deprecated','archived') OR t.status IN ('deprecated','archived')) ORDER BY r.id""",
                (project_id,), "INACTIVE_ENDPOINT", "关系关联的实体已弃用或归档，需要复核关系的适用性。")

            # 统一检查当前实体／关系引用的证据，保留引用方类型和固定版本。
            evidence = """WITH refs AS (
                SELECT e.id,e.version,'entity' AS owner_type,e.status AS owner_status,
                    p.evidence_id,p.evidence_version
                FROM current_entities e JOIN entity_evidence p ON p.entity_id=e.id AND p.version=e.version
                WHERE e.project_id=%s AND e.status<>'archived'
                UNION ALL
                SELECT r.id,r.version,'relationship' AS owner_type,r.status AS owner_status,
                    p.evidence_id,p.evidence_version
                FROM current_relationships r JOIN relationship_evidence p ON p.relationship_id=r.id AND p.version=r.version
                WHERE r.project_id=%s AND r.status IN ('proposed','verified')
            ) SELECT p.id,p.version,p.owner_type,p.evidence_id,p.evidence_version,
                e.version AS current_evidence_version,e.status AS current_evidence_status
                FROM refs p JOIN current_entities e ON e.id=p.evidence_id WHERE """
            # 引用方可能是实体或关系，发现项通过 context.owner_type 和 record_id 表示。
            collect(evidence + "e.version<>p.evidence_version ORDER BY p.owner_type,p.id,p.evidence_id",
                    (project_id, project_id), "STALE_EVIDENCE", "证据已有新修订，当前引用仍固定在历史版本，需要复核。", "record")
            collect(evidence + "e.status<>'active' ORDER BY p.owner_type,p.id,p.evidence_id",
                    (project_id, project_id), "INACTIVE_EVIDENCE", "所引用证据的最新修订尚未生效、已弃用或已归档。", "record")
            collect(evidence + """p.owner_type='relationship' AND p.owner_status='verified' AND (
                    e.document->'provenance'->>'nature' NOT IN ('fact','human_confirmed')
                    OR (e.document->'attributes'->>'category'='test_execution'
                        AND e.document->'attributes'->>'outcome'<>'passed'))
                    ORDER BY p.id,p.evidence_id""",
                    (project_id, project_id), "UNQUALIFIED_EVIDENCE", "已验证关系所引用证据的最新修订不再满足事实／人工确认或测试通过条件。", "record")

            collect("""SELECT r.id,r.version,r.source_id,r.target_id,r.status
                FROM current_relationships r WHERE r.project_id=%s AND r.type='CONFLICTS_WITH'
                AND r.status IN ('proposed','verified') ORDER BY r.id""",
                (project_id,), "UNRESOLVED_CONFLICT", "存在尚未退役或拒绝的显式冲突关系，请人工处理。")
            for relation_type, code, message in [
                ("IMPLEMENTED_BY", "MISSING_IMPLEMENTATION", "生效需求缺少关联当前生效实体版本的已验证实现关系。"),
                ("VERIFIED_BY", "MISSING_VERIFICATION", "生效需求缺少关联当前生效测试用例版本的已验证验证关系。"),
            ]:
                collect("""SELECT e.id,e.version,e.document->>'name' AS name FROM current_entities e
                    WHERE e.project_id=%s AND e.kind='Requirement' AND e.status='active'
                    AND NOT EXISTS (SELECT 1 FROM current_relationships r
                        JOIN current_entities t ON t.id=r.target_id
                        WHERE r.source_id=e.id AND r.source_version=e.version AND r.type=%s AND r.status='verified'
                        AND r.target_version=t.version AND t.status='active') ORDER BY e.id""",
                    (project_id, relation_type), code, message, "entity")
        return {"project_id": project_id, "findings": findings[:limit], "limit": limit,
                "truncated": len(findings) > limit,
                "message": "检查结果均为待复核警告，不会自动改写、弃用或删除知识。"}
