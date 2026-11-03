"""第二阶段的并发重放与乐观锁回归。"""
from concurrent.futures import ThreadPoolExecutor
import json
from uuid import UUID

import pytest

from knowledge.operations import KnowledgeOperations
from knowledge.operations_models import IngestionRequest, ProposalCreate, ProposalReview
from knowledge.semantics import KnowledgeError

pytestmark = pytest.mark.integration


def review(version):
    return ProposalReview(expected_version=version, actor="并发测试审核者", reason="验证并发下的幂等性与基线保护")


def source(project):
    return IngestionRequest(project_id=project["id"], source_uri="urn:fixture:concurrent", source_revision="v1",
        actor="并发测试采集者", format="json", content=json.dumps({"entities": [
            {"key": "req", "kind": "Requirement", "name": "并发采集示例"}], "relationships": []}))


def test_concurrent_ingestions_share_one_source_snapshot_and_proposal(db_url, project):
    operations = KnowledgeOperations(db_url)
    request = source(project)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: operations.ingest(request), range(2)))
    assert results[0]["proposal"]["id"] == results[1]["proposal"]["id"]
    assert sorted(result["replayed"] for result in results) == [False, True]
    assert len(operations.list_sources(UUID(project["id"]))) == 1
    assert len(operations.list_proposals(UUID(project["id"]))) == 1


def test_concurrent_commit_replays_without_duplicating_revisions(db_url, project):
    operations = KnowledgeOperations(db_url)
    proposal = operations.ingest(source(project))["proposal"]
    checked = operations.review(proposal["id"], review(1), "validate")["proposal"]
    assert checked["result"]["valid"] is True
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: operations.review(proposal["id"], review(2), "commit"), range(2)))
    assert sorted(result["replayed"] for result in results) == [False, True]
    assert all(result["proposal"]["version"] == 3 for result in results)
    assert len(operations.history(proposal["id"])) == 3


def test_competing_validated_proposals_cannot_overwrite_the_winner(db_url, project, entity, provenance):
    record = entity()
    operations = KnowledgeOperations(db_url)
    proposals = []
    for name in ("候选修改一", "候选修改二"):
        content = {key: record[key] for key in ("description", "status", "attributes", "evidence")}
        content.update(name=name, provenance=provenance, expected_version=1)
        proposal = operations.create_proposal(ProposalCreate(project_id=project["id"], title=name,
            actor="提案提交者", changes=[{"op": "update_entity", "id": record["id"], "data": content}]))
        operations.review(proposal["id"], review(1), "validate")
        proposals.append(proposal)

    def commit(proposal):
        try:
            operations.review(proposal["id"], review(2), "commit")
            return 200
        except KnowledgeError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(commit, proposals)) == [200, 409]
