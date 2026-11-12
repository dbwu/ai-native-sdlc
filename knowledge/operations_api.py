"""第二阶段知识操作的中文 HTTP 接口。"""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query

from .analysis import KnowledgeAnalysis
from .operations import KnowledgeOperations
from .operations_models import IngestionRequest, ProposalCreate, ProposalReview, SourceEvidenceProposal, SourceSnapshotCreate
from .source_evidence import SourceEvidenceService


def operations_router(url_provider):
    router = APIRouter(prefix="/operations", tags=["知识操作（第二阶段）"])

    def service():
        return KnowledgeOperations(url_provider())

    @router.post("/ingestions", status_code=201, summary="采集来源并生成变更提案",
                 description="接收 JSON 或显式 knowledge-json Markdown 块；记录原文快照并生成提案，不写入正式知识。相同来源版本和内容重放返回原提案。")
    def ingest(body: IngestionRequest):
        return service().ingest(body)

    @router.post("/source-snapshots", status_code=201, summary="登记普通文本或 Markdown 原文快照")
    def register_source_snapshot(body: SourceSnapshotCreate):
        return service().register_source_snapshot(body)

    @router.post("/source-evidence/proposals", status_code=201, summary="提出实体或关系的原文证据关联")
    def propose_source_evidence(body: SourceEvidenceProposal):
        return SourceEvidenceService(url_provider()).propose(body)

    @router.get("/projects/{project_id}/source-evidence", summary="读取指定知识版本的原文证据位置")
    def source_evidence(project_id: UUID, table: Literal["entity", "relationship"], id: UUID, version: int = Query(ge=1)):
        return SourceEvidenceService(url_provider()).read(project_id, table, id, version)

    @router.get("/sources", summary="查询项目的来源快照索引",
                description="列出每个来源及其最新快照版本、外部版本和原文 SHA-256；系统不会抓取来源 URI。")
    def sources(project_id: UUID = Query(description="所属项目 ID"),
                limit: int = Query(50, ge=1, le=200, description="本页最多返回的来源数"),
                offset: int = Query(0, ge=0, description="跳过的来源数")):
        return service().list_sources(project_id, limit, offset)

    @router.get("/sources/{source_id}/versions/{version}", summary="读取不可变来源原文",
                description="读取精确来源版本，包含原始内容、摘要、格式、提交者和记录时间。")
    def source_version(source_id: UUID, version: int):
        return service().source_version(source_id, version)

    @router.post("/proposals", status_code=201, summary="提出已有知识的修订",
                 description="对同一项目内已有实体或关系提出完整修订，支持内容、生命周期及证据调整；仅创建待审核提案，不改动知识。")
    def propose(body: ProposalCreate):
        return service().create_proposal(body)

    @router.get("/proposals", summary="查询项目变更提案",
                description="返回最新提案事件版本及不可变操作计划，可按状态筛选，按创建时间从新到旧分页。")
    def proposals(project_id: UUID = Query(description="所属项目 ID"),
                  status: Literal["pending", "validated", "committed", "rejected"] | None = Query(None, description="提案状态"),
                  limit: int = Query(50, ge=1, le=200, description="本页最多返回的提案数"),
                  offset: int = Query(0, ge=0, description="跳过的提案数")):
        return service().list_proposals(project_id, status, limit, offset)

    @router.get("/proposals/{proposal_id}", summary="读取提案与最新校验结果",
                description="返回不可变操作计划、身份解析结果、基线版本及最新审核事件。")
    def proposal(proposal_id: UUID):
        return service().get_proposal(proposal_id)

    @router.get("/proposals/{proposal_id}/history", summary="读取提案审核历史",
                description="按版本从新到旧返回创建、校验、提交或拒绝事件；审核人仅作调用方署名记录。")
    def history(proposal_id: UUID, limit: int = Query(50, ge=1, le=200, description="本页最多返回的事件数"),
                offset: int = Query(0, ge=0, description="跳过的事件数")):
        return service().history(proposal_id, limit, offset)

    @router.post("/proposals/{proposal_id}/validate", summary="试运行提案并记录校验结果",
                 description="在事务保存点内执行全部操作并回滚，检查基线、关系语义和证据等；失败时返回 valid=false，正式知识保持不变。")
    def validate(proposal_id: UUID, body: ProposalReview):
        return service().review(proposal_id, body, "validate")

    @router.post("/proposals/{proposal_id}/commit", summary="显式确认并原子提交提案",
                 description="仅允许提交已通过校验的提案；事务内重新检查全部约束。全部操作及审核事件一起提交或回滚；重复提交已完成提案会重放原结果。")
    def commit(proposal_id: UUID, body: ProposalReview):
        return service().review(proposal_id, body, "commit")

    @router.post("/proposals/{proposal_id}/reject", summary="拒绝变更提案",
                 description="记录拒绝原因并终止提案，不改变工程知识。修正内容后应提交新的来源版本或新提案。")
    def reject(proposal_id: UUID, body: ProposalReview):
        return service().review(proposal_id, body, "reject")

    @router.get("/impact", summary="分析实体变更的潜在影响",
                description="按关系语义进行有界遍历，返回候选影响及最短解释路径。结果是待复核线索，不自动变更实体或关系。")
    def impact(project_id: UUID = Query(description="所属项目 ID"), entity_id: UUID = Query(description="变更起点实体 ID"),
               max_depth: int = Query(4, ge=0, le=12, description="最多遍历的关系层数"),
               max_nodes: int = Query(200, ge=1, le=1000, description="包含起点在内的节点上限")):
        return KnowledgeAnalysis(url_provider()).impact(project_id, entity_id, max_depth, max_nodes)

    @router.get("/consistency", summary="检查项目知识一致性",
                description="报告陈旧版本、失效证据、冲突及需求实现或验证缺口。仅返回中文警告，不自动修复知识。")
    def consistency(project_id: UUID = Query(description="所属项目 ID"),
                    limit: int = Query(100, ge=1, le=1000, description="最多返回的发现项数")):
        return KnowledgeAnalysis(url_provider()).consistency(project_id, limit)

    return router
