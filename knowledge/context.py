"""为任务组装有版本、来源及证据依据的不可变上下文包。"""
import hashlib
import json
from collections import deque
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from .db import connect
from .models import EvidenceRef, Kind
from .semantics import KnowledgeError
from .service import KnowledgeService


MAX_CONTEXT_BYTES = 1024 * 1024
MAX_CONTEXT_RELATIONSHIPS = 1000


class ContextBuilder:
    """由调用方在项目锁与同一事务中构建上下文，不访问外部来源。

    图遍历使用关系中明确固定的两端版本，避免把历史关系与当前实体内容
    拼接成看似一致的事实。同一实体的不同版本会作为独立快照保留。
    """

    def __init__(self, database_url: str):
        self.database_url = database_url

    @staticmethod
    def _package(conn, package_id):
        row = conn.execute("SELECT * FROM context_package WHERE id=%s", (package_id,)).fetchone()
        if row is None:
            raise KnowledgeError("未找到指定上下文包", 404)
        return row

    def get(self, package_id: UUID):
        with connect(self.database_url) as conn:
            return self._package(conn, package_id)

    @staticmethod
    def _relations(conn, project_id, record, kinds, limit, excluded):
        # 只返回精确匹配当前展开版本的边，且两端类型均在声明的读取范围内。
        return conn.execute("""SELECT r.id FROM current_relationships r
            JOIN entity s ON s.id=r.source_id AND s.project_id=r.project_id
            JOIN entity t ON t.id=r.target_id AND t.project_id=r.project_id
            WHERE r.project_id=%s AND r.status IN ('proposed','verified')
            AND s.kind=ANY(%s::text[]) AND t.kind=ANY(%s::text[])
            AND NOT (r.id=ANY(%s::uuid[]))
            AND ((r.source_id=%s AND r.source_version=%s)
                OR (r.target_id=%s AND r.target_version=%s))
            ORDER BY r.id LIMIT %s""", (project_id, kinds, kinds, list(excluded), record["id"], record["version"],
                                        record["id"], record["version"], limit)).fetchall()

    def build(self, conn, project_id: UUID, task_id: UUID, seeds: list[EvidenceRef],
              allowed_kinds: list[Kind], max_depth=2, max_entities=100):
        if not 0 <= max_depth <= 3 or not 1 <= max_entities <= 200:
            raise KnowledgeError("上下文 max_depth 必须为 0 至 3，max_entities 必须为 1 至 200")
        project_id, task_id = UUID(str(project_id)), UUID(str(task_id))
        try:
            kinds = sorted({Kind(kind).value for kind in allowed_kinds})
        except ValueError as exc:
            raise KnowledgeError("上下文读取范围包含不支持的实体类型") from exc
        if not kinds:
            raise KnowledgeError("上下文读取范围不能为空")
        normalized_seeds = [EvidenceRef.model_validate(seed) for seed in seeds]
        if len(normalized_seeds) > max_entities:
            raise KnowledgeError("上下文种子数不能超过实体数量上限")
        if len({(seed.id, seed.version) for seed in normalized_seeds}) != len(normalized_seeds):
            raise KnowledgeError("上下文种子不能重复引用同一实体版本")
        KnowledgeService._assert_project(conn, project_id)
        task = conn.execute("SELECT project_id FROM runtime_task WHERE id=%s", (task_id,)).fetchone()
        if task is None or task["project_id"] != project_id:
            raise KnowledgeError("任务不存在或不属于上下文的项目", 404)

        entities, relationships, snapshots, depths = {}, {}, {}, {}
        baselines = {"entities": {}, "relationships": {}}
        graph_queue, evidence_queue = deque(), deque()
        truncated = False

        def entity_snapshot(record_id, version):
            cache_key = (UUID(str(record_id)), version)
            if cache_key in snapshots:
                return snapshots[cache_key]
            record = KnowledgeService._get(conn, "entity", record_id, version)
            if record["project_id"] != project_id:
                raise KnowledgeError("上下文不能读取其他项目的实体或证据")
            baselines["entities"][(record["id"], record["version"])] = {
                "id": record["id"], "version": record["version"], "status": record["status"],
            }
            snapshots[cache_key] = record
            return record

        def add_entity(record, depth, *, seed=False):
            nonlocal truncated
            key = (record["id"], record["version"])
            if record["kind"] not in kinds:
                if seed:
                    raise KnowledgeError("任务种子类型不在 Agent 声明的读取范围内")
                return False
            if key in entities:
                if depth is not None and (key not in depths or depth < depths[key]):
                    depths[key] = depth
                    graph_queue.append((record, depth))
                return True
            if len(entities) >= max_entities:
                if seed:
                    raise KnowledgeError("上下文种子数超过可用的实体数量上限")
                truncated = True
                return False
            entities[key] = record
            if depth is not None:
                depths[key] = depth
                graph_queue.append((record, depth))
            evidence_queue.extend(record["evidence"])
            return True

        for seed in normalized_seeds:
            add_entity(entity_snapshot(seed.id, seed.version), 0, seed=True)

        while graph_queue or evidence_queue:
            # 证据不消耗额外图深度，但仍计入实体数量和内容大小限制。
            while evidence_queue:
                ref = EvidenceRef.model_validate(evidence_queue.popleft())
                record = entity_snapshot(ref.id, ref.version)
                if record["kind"] != Kind.EVIDENCE:
                    raise KnowledgeError("上下文证据引用必须指向 Evidence 实体")
                add_entity(record, None)
            if not graph_queue:
                continue
            record, depth = graph_queue.popleft()
            if depth != depths[(record["id"], record["version"])]:
                continue
            if depth >= max_depth:
                if self._relations(conn, project_id, record, kinds, 1, relationships):
                    truncated = True
                continue
            remaining = MAX_CONTEXT_RELATIONSHIPS - len(relationships)
            rows = self._relations(conn, project_id, record, kinds, remaining + 1, relationships)
            if len(rows) > remaining:
                truncated = True
            for row in rows[:remaining]:
                if row["id"] in relationships:
                    continue
                relation = KnowledgeService._get(conn, "relationship", row["id"])
                source = entity_snapshot(relation["source_id"], relation["source_version"])
                target = entity_snapshot(relation["target_id"], relation["target_version"])
                unseen = {(item["id"], item["version"]) for item in (source, target)} - entities.keys()
                if len(entities) + len(unseen) > max_entities:
                    truncated = True
                    continue
                add_entity(source, depth + 1)
                add_entity(target, depth + 1)
                relationships[relation["id"]] = relation
                baselines["relationships"][relation["id"]] = {
                    "id": relation["id"], "version": relation["version"], "status": relation["status"],
                }
                evidence_queue.extend(relation["evidence"])

        document = jsonable_encoder({
            "metamodel_version": 1,
            "seeds": [seed.model_dump(mode="json") for seed in normalized_seeds],
            "allowed_kinds": kinds,
            "limits": {"max_depth": max_depth, "max_entities": max_entities,
                       "max_relationships": MAX_CONTEXT_RELATIONSHIPS, "max_bytes": MAX_CONTEXT_BYTES},
            "entities": sorted(entities.values(), key=lambda item: (str(item["id"]), item["version"])),
            "relationships": sorted(relationships.values(), key=lambda item: str(item["id"])),
            "baselines": {
                "entities": sorted(baselines["entities"].values(), key=lambda item: (str(item["id"]), item["version"])),
                "relationships": sorted(baselines["relationships"].values(), key=lambda item: str(item["id"])),
            },
            "truncated": truncated,
            "message": "上下文内容固定到具体知识版本。来源性质保持原值，不将推断或假设升级为事实。",
        })
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_CONTEXT_BYTES:
            raise KnowledgeError("上下文内容超过 1 MiB，请缩小任务种子或展开范围")
        package_id = uuid4()
        conn.execute("""INSERT INTO context_package (id,project_id,task_id,document,sha256)
            VALUES (%s,%s,%s,%s,%s)""", (package_id, project_id, task_id, Jsonb(document), hashlib.sha256(encoded).hexdigest()))
        return self._package(conn, package_id)

    def freshness(self, conn, package_id: UUID, accepted_versions: dict[tuple[str, str], int] | None = None):
        package = self._package(conn, package_id)
        stale = []
        accepted_versions = accepted_versions or {}
        for table, key in (("entity", "entities"), ("relationship", "relationships")):
            for baseline in package["document"]["baselines"][key]:
                current = KnowledgeService._get(conn, table, UUID(baseline["id"]))
                accepted = accepted_versions.get((table, baseline["id"]))
                expected_version = accepted if accepted is not None else baseline["version"]
                if (current["project_id"] != package["project_id"] or current["version"] != expected_version
                        or (accepted is None and current["status"] != baseline["status"])):
                    stale.append({"table": table, "id": baseline["id"], "version": baseline["version"],
                                  "expected_version": expected_version,
                                  "current_version": current["version"], "expected_status": baseline["status"],
                                  "current_status": current["status"], "message": "上下文所依据的知识版本或状态已变化，需要重新组装。"})
        return {"fresh": not stale, "stale": stale}
