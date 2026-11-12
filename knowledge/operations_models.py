"""第二阶段采集与变更提案的输入契约。"""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator

from .models import EntityUpdate, RelationUpdate, StrictModel, Text, Version, validate_uri
from .source_spans import SourceSpan


class SourceSnapshotCreate(StrictModel):
    project_id: UUID = Field(title="所属项目 ID")
    source_uri: Text = Field(title="来源地址", description="只作身份定位，服务不会访问该地址。")
    source_revision: Text = Field(title="外部来源版本", description="同一来源版本不能对应不同内容或格式。")
    content: Annotated[str, StringConstraints(strip_whitespace=False), Field(min_length=1, max_length=1048576)] = Field(title="来源原文", description="原样保留空白，UTF-8 编码不超过 1 MiB。")
    actor: Text = Field(title="采集提交者", description="调用方声明，不代表已完成身份认证。")
    _uri = field_validator("source_uri")(validate_uri)


class IngestionRequest(SourceSnapshotCreate):
    format: Literal["json", "markdown"] = Field(title="来源格式")
    title: Annotated[str, Field(min_length=1, max_length=300)] = Field(default="采集知识变更提案", title="提案标题")


class SourceEvidenceTarget(StrictModel):
    table: Literal["entity", "relationship"] = Field(title="引用方类型")
    id: UUID = Field(title="引用方 ID")
    version: Version = Field(title="引用方当前版本")


class SourceEvidenceProposal(StrictModel):
    project_id: UUID
    target: SourceEvidenceTarget
    span: SourceSpan
    actor: Text = Field(title="操作署名")
    reason: Text = Field(title="证据用途与依据")


class EntityRevisionChange(StrictModel):
    op: Literal["update_entity"] = "update_entity"
    id: UUID = Field(title="实体 ID")
    data: EntityUpdate = Field(title="实体完整修订内容")


class RelationshipRevisionChange(StrictModel):
    op: Literal["update_relationship"] = "update_relationship"
    id: UUID = Field(title="关系 ID")
    data: RelationUpdate = Field(title="关系完整修订内容")


Change = Annotated[EntityRevisionChange | RelationshipRevisionChange, Field(discriminator="op")]


class ProposalCreate(StrictModel):
    project_id: UUID = Field(title="所属项目 ID")
    title: Annotated[str, Field(min_length=1, max_length=300)] = Field(title="提案标题")
    actor: Text = Field(title="提案提交者")
    changes: Annotated[list[Change], Field(min_length=1, max_length=200)] = Field(title="待修订的实体或关系", description="适用于已有知识的内容、生命周期及证据修订。")


class ProposalReview(StrictModel):
    expected_version: Version = Field(title="预期提案版本", description="校验或审核前读取的最新提案事件版本。")
    actor: Text = Field(title="操作人", description="仅记录调用方声明的身份，MVP 不指定组织审批权限。")
    reason: Text = Field(title="操作依据")
