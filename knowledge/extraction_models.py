"""文档语义抽取候选。候选身份、来源与人工绑定都必须可以复核。"""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue, StringConstraints, model_validator

from .models import ATTRIBUTE_MODELS, Kind, RelationType, StrictModel, Text, Version


Key = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$')]
QuoteText = Annotated[str, StringConstraints(strip_whitespace=False), Field(min_length=1, max_length=4000)]


class ExtractionQuote(StrictModel):
    quote: QuoteText = Field(title='逐字原文引文')
    start: int | None = Field(default=None, ge=0, strict=True, title='可选的 Unicode 码点起始位置')


class ExistingKnowledge(StrictModel):
    id: UUID = Field(title='已有知识的稳定 ID')
    version: Version = Field(title='本轮读取的知识版本')


class AcceptanceItem(StrictModel):
    key: Key = Field(title='验收条件稳定键')
    text: Text = Field(title='验收条件')
    evidence: list[ExtractionQuote] = Field(min_length=1, max_length=5, title='验收条件原文依据')


class ExtractionEntity(StrictModel):
    key: Key = Field(title='来源内稳定知识键')
    kind: Literal['Requirement', 'BusinessRule', 'Domain'] = Field(title='知识类型')
    name: Annotated[str, Field(min_length=1, max_length=300)] = Field(title='知识名称')
    description: str = Field(default='', max_length=12000, title='知识说明')
    attributes: dict[str, JsonValue] = Field(default_factory=dict, title='该类型的知识属性')
    existing: ExistingKnowledge | None = Field(default=None, title='建议关联的已有知识版本')
    evidence: list[ExtractionQuote] = Field(min_length=1, max_length=5, title='原文依据')
    acceptance_items: list[AcceptanceItem] | None = Field(default=None, max_length=30, title='带稳定键的验收条件，空值保留已有条件')

    @model_validator(mode='after')
    def check_attributes(self):
        ATTRIBUTE_MODELS[Kind(self.kind)].model_validate(self.attributes)
        if self.acceptance_items is not None and self.kind != 'Requirement':
            raise ValueError('只有需求可以填写验收条件')
        keys = [item.key for item in self.acceptance_items or []]
        if len(keys) != len(set(keys)):
            raise ValueError('同一需求的验收条件键不能重复')
        if 'acceptance_criteria' in self.attributes:
            raise ValueError('请使用 acceptance_items 编辑验收条件，不能在 attributes 中另行设置或清空 acceptance_criteria')
        reserved = {'source_span', 'extraction', 'acceptance_items'}
        if reserved & set(self.attributes.get('extensions', {})):
            raise ValueError('抽取追溯和验收条件扩展由服务端生成，请使用专用字段')
        return self


class ExtractionRelationship(StrictModel):
    key: Key = Field(title='来源内稳定关系键')
    type: RelationType = Field(title='关系类型')
    source_key: Key = Field(title='起点候选键')
    target_key: Key = Field(title='终点候选键')
    rationale: Text = Field(title='关系依据')
    evidence: list[ExtractionQuote] = Field(min_length=1, max_length=5, title='关系原文依据')


class ExtractionReviewHint(StrictModel):
    kind: Literal['possible_duplicate', 'possible_conflict', 'retirement'] = Field(title='模型建议类型')
    candidate_key: Key | None = Field(default=None, title='本轮候选键，移除建议可为空')
    existing: ExistingKnowledge = Field(title='比较的已有知识固定版本')
    rationale: Text = Field(title='相同性、冲突或明确移除的语义依据')
    evidence: list[ExtractionQuote] = Field(min_length=1, max_length=5, title='本轮原文依据')
    existing_quote: QuoteText = Field(title='已有知识名称、说明或验收条件中的逐字引文')


class ExtractionCandidate(StrictModel):
    summary: Text = Field(title='抽取摘要')
    entities: list[ExtractionEntity] = Field(default_factory=list, max_length=40, title='候选知识')
    relationships: list[ExtractionRelationship] = Field(default_factory=list, max_length=60, title='候选关系')
    warnings: list[Text] = Field(default_factory=list, max_length=30, title='待审阅事项')
    review_hints: list[ExtractionReviewHint] = Field(default_factory=list, max_length=60, title='需人工裁决的增量检查建议')

    @model_validator(mode='after')
    def unique_keys(self):
        for items in (self.entities, self.relationships):
            keys = [item.key for item in items]
            if len(keys) != len(set(keys)):
                raise ValueError('同类候选的稳定键不能重复')
        return self


class ExtractionStart(StrictModel):
    source_id: UUID = Field(title='固定原文来源 ID')
    source_version: Version = Field(title='固定原文版本')
    actor: Text = Field(title='操作署名')
    guidance: str = Field(default='', max_length=4000, title='本轮抽取范围说明')
    source_scope: Literal['full', 'delta'] = Field(default='full', title='完整文档或增量补充')


class ExtractionReviewDecision(StrictModel):
    finding_id: Text = Field(title='审阅发现标识')
    action: Text = Field(title='处理选择')
    reason: Text = Field(title='处理依据')


class ExtractionReviewRequest(StrictModel):
    candidate: ExtractionCandidate = Field(title='当前人工选择及编辑后的候选')


class ExtractionProposal(StrictModel):
    actor: Text = Field(title='审阅署名')
    reason: Text = Field(title='修改与选择依据')
    candidate: ExtractionCandidate = Field(title='人工选择和修改后的候选')
    review_digest: str | None = Field(default=None, title='已审阅增量报告摘要')
    decisions: list[ExtractionReviewDecision] = Field(default_factory=list, max_length=500, title='逐项处理决定')
