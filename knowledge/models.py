from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator, model_validator


class Kind(StrEnum):
    """核心实体类型；枚举值作为稳定的接口标识保留英文。"""
    PROJECT = "Project"
    DOMAIN = "Domain"
    REQUIREMENT = "Requirement"
    BUSINESS_RULE = "BusinessRule"
    COMPONENT = "ArchitectureComponent"
    INTERFACE = "Interface"
    DATA_MODEL = "DataModel"
    CODE = "CodeArtifact"
    TEST = "TestCase"
    DECISION = "Decision"
    CONSTRAINT = "Constraint"
    EVIDENCE = "Evidence"


class RelationType(StrEnum):
    """标准关系类型，含义参见元模型及中文设计说明。"""
    CONTAINS = "CONTAINS"
    DERIVED_FROM = "DERIVED_FROM"
    DEPENDS_ON = "DEPENDS_ON"
    IMPLEMENTED_BY = "IMPLEMENTED_BY"
    VERIFIED_BY = "VERIFIED_BY"
    CONSTRAINED_BY = "CONSTRAINED_BY"
    IMPACTS = "IMPACTS"
    SUPERSEDES = "SUPERSEDES"
    CONFLICTS_WITH = "CONFLICTS_WITH"


class EntityStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class RelationStatus(StrEnum):
    PROPOSED = "proposed"
    VERIFIED = "verified"
    REJECTED = "rejected"
    RETIRED = "retired"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Text = Annotated[str, Field(min_length=1, max_length=4000)]
Version = Annotated[int, Field(strict=True, ge=1)]
_JSON_EXTENSIONS = TypeAdapter(dict[str, JsonValue], config=ConfigDict(str_strip_whitespace=False))


def validate_uri(value: str) -> str:
    # URI 仅用于定位来源；MVP 不会获取或执行其中的内容。
    if not urlsplit(value).scheme:
        raise ValueError("请使用绝对 URI，例如以 https:、git:、file: 或 urn: 开头的地址")
    return value


class Provenance(StrictModel):
    """来源追踪：记录提交者、来源版本、变更原因及知识性质。"""

    actor: Text = Field(title="提交者", description="调用方声明的提交者身份，不代表已经完成身份认证。")
    source_uri: Text = Field(title="来源地址", description="来源的绝对 URI；系统不会自动访问该地址。")
    source_revision: Text = Field(title="来源版本", description="外部来源的提交号、文档版本或记录编号。")
    change_reason: Text = Field(title="变更原因", description="说明本次创建或修订的依据和目的。")
    nature: Literal["fact", "human_confirmed", "inference", "assumption"] = Field(
        default="assumption", title="知识性质", description="fact：事实；human_confirmed：人工确认；inference：推断；assumption：假设。")
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, title="置信度", description="可选，取值为 0 到 1。")

    _uri = field_validator("source_uri")(validate_uri)


class EvidenceRef(StrictModel):
    """证据引用：固定到某个证据实体的具体修订版本。"""

    id: UUID = Field(title="证据 ID")
    version: Version = Field(title="证据版本")


class Attributes(StrictModel):
    """实体属性的公共扩展结构。"""

    # 通过明确的扩展入口承载领域属性，不接受拼错的核心字段。
    extensions: dict[str, JsonValue] = Field(default_factory=dict, title="扩展属性", description="存放尚未纳入核心元模型的领域 JSON 属性。")

    @field_validator("extensions", mode="wrap")
    @classmethod
    def preserve_json_strings(cls, value, handler):
        # 扩展内容可能包含源码。仍校验 JSON 类型，但不沿用说明文字的去空白规则。
        return _JSON_EXTENSIONS.validate_python(value)


class ProjectAttributes(Attributes):
    """项目属性。"""

    external_key: Text | None = Field(default=None, title="外部项目标识")


class DomainAttributes(Attributes):
    """业务领域属性。"""

    glossary: dict[str, str] = Field(default_factory=dict, title="领域术语表")


class RequirementAttributes(Attributes):
    """需求属性。"""

    acceptance_criteria: list[Text] = Field(default_factory=list, title="验收标准")
    requirement_type: Literal["functional", "nonfunctional"] | None = Field(default=None, title="需求类别", description="functional：功能需求；nonfunctional：非功能需求。")


class RuleAttributes(Attributes):
    """业务规则属性；表达式仅保存，不自动执行。"""

    expression: dict[str, JsonValue] = Field(default_factory=dict, title="规则表达式")


class ComponentAttributes(Attributes):
    """架构组件属性。"""

    responsibility: Text | None = Field(default=None, title="组件职责")


class InterfaceAttributes(Attributes):
    """接口契约属性。"""

    protocol: Text | None = Field(default=None, title="接口协议")
    contract_uri: Text | None = Field(default=None, title="契约地址")


class DataModelAttributes(Attributes):
    """数据模型属性。"""

    schema_definition: dict[str, JsonValue] = Field(default_factory=dict, title="数据结构定义")


class CodeAttributes(Attributes):
    """代码资产属性。"""

    repository_uri: Text | None = Field(default=None, title="代码仓库地址")
    path: Text | None = Field(default=None, title="文件路径")
    symbol: Text | None = Field(default=None, title="代码符号")
    revision: Text | None = Field(default=None, title="代码版本")
    language: Text | None = Field(default=None, title="编程语言")


class TestAttributes(Attributes):
    """测试用例属性。"""

    steps: list[Text] = Field(default_factory=list, title="测试步骤")
    expected_result: Text | None = Field(default=None, title="预期结果")


class DecisionAttributes(Attributes):
    """工程决策属性。"""

    rationale: Text | None = Field(default=None, title="决策依据")
    alternatives: list[Text] = Field(default_factory=list, title="备选方案")


class ConstraintAttributes(Attributes):
    """工程约束属性；表达式仅保存，不自动执行。"""

    expression: dict[str, JsonValue] = Field(default_factory=dict, title="约束表达式")


class EvidenceAttributes(Attributes):
    """证据属性：外部产物定位信息及其适用的实体版本。"""

    category: Literal["document", "code", "test_execution", "decision", "analysis"] = Field(
        title="证据类别", description="document：文档；code：代码；test_execution：测试执行；decision：决策；analysis：分析。")
    uri: Text = Field(title="证据地址")
    revision: Text = Field(title="外部证据版本")
    subject_id: UUID | None = Field(default=None, title="证据所指实体 ID")
    subject_version: Version | None = Field(default=None, title="证据所指实体版本")
    outcome: Literal["passed", "failed", "unknown"] | None = Field(default=None, title="测试执行结果", description="passed：通过；failed：失败；unknown：未知。仅用于测试执行证据。")
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = Field(default=None, title="内容 SHA-256 摘要", description="可选的 64 位小写十六进制摘要；MVP 不自动核验文件内容。")

    _uri = field_validator("uri")(validate_uri)

    @model_validator(mode="after")
    def consistent_subject(self):
        if (self.subject_id is None) != (self.subject_version is None):
            raise ValueError("subject_id 与 subject_version 必须同时提供")
        if self.category in {"code", "test_execution"} and self.subject_id is None:
            raise ValueError("代码和测试执行证据必须指定所指实体及其具体版本")
        if self.category == "test_execution" and self.outcome is None:
            raise ValueError("测试执行证据必须提供执行结果 outcome")
        if self.category != "test_execution" and self.outcome is not None:
            raise ValueError("outcome 字段仅适用于测试执行证据")
        return self


ATTRIBUTE_MODELS = dict(zip(Kind, [
    ProjectAttributes, DomainAttributes, RequirementAttributes, RuleAttributes,
    ComponentAttributes, InterfaceAttributes, DataModelAttributes, CodeAttributes,
    TestAttributes, DecisionAttributes, ConstraintAttributes, EvidenceAttributes,
], strict=True))


class RevisionContent(StrictModel):
    """实体与关系修订共用的来源和证据信息。"""

    provenance: Provenance = Field(title="来源追踪")
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=100, title="证据引用", description="引用具体证据 ID 及版本，最多 100 项。")

    @field_validator("evidence")
    @classmethod
    def distinct_evidence(cls, refs):
        if len({(r.id, r.version) for r in refs}) != len(refs):
            raise ValueError("同一证据版本不能重复引用")
        return refs


class EntityContent(RevisionContent):
    """知识实体的完整可修订内容。"""

    name: Annotated[str, Field(min_length=1, max_length=300)] = Field(title="名称")
    description: str = Field(default="", max_length=100000, title="说明")
    status: EntityStatus = Field(default=EntityStatus.DRAFT, title="实体状态", description="draft：草稿；active：生效；deprecated：弃用；archived：归档。")
    attributes: dict[str, Any] = Field(default_factory=dict, title="实体属性", description="按 kind 校验；具体属性结构见 /metamodel。")


class EntityCreate(EntityContent):
    """创建实体；创建项目时由系统建立独立的项目范围。"""

    kind: Kind = Field(title="实体类型")
    project_id: UUID | None = Field(default=None, title="所属项目 ID", description="创建 Project 时省略；创建其他类型时必填。")

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind == Kind.PROJECT and self.project_id is not None:
            raise ValueError("Project 会创建自身的项目范围，请省略 project_id")
        if self.kind != Kind.PROJECT and self.project_id is None:
            raise ValueError("创建非 Project 实体时必须提供 project_id")
        # 在访问数据库之前，将属性校验错误作为 API 422 响应返回。
        self.attributes = ATTRIBUTE_MODELS[self.kind].model_validate(self.attributes).model_dump(mode="json")
        return self


class EntityUpdate(EntityContent):
    """提交实体的完整新修订，使用预期版本防止覆盖并发修改。"""

    expected_version: Version = Field(title="预期当前版本", description="必须与数据库中的最新版本一致，否则返回 409。")


class RelationContent(RevisionContent):
    """关系内容及其适用的实体修订版本。"""

    source_version: Version = Field(title="源实体版本")
    target_version: Version = Field(title="目标实体版本")
    status: RelationStatus = Field(default=RelationStatus.PROPOSED, title="关系状态", description="proposed：待确认；verified：已验证；rejected：已拒绝；retired：已退役。")
    rationale: Text = Field(title="关系依据", description="说明关系成立的原因；派生关系还应说明推导依据。")


class RelationCreate(RelationContent):
    """在同一项目内创建有类型的实体关系。"""

    type: RelationType = Field(title="关系类型")
    source_id: UUID = Field(title="源实体 ID")
    target_id: UUID = Field(title="目标实体 ID")


class RelationUpdate(RelationContent):
    """提交关系的新修订，保留原有端点和关系类型。"""

    expected_version: Version = Field(title="预期当前版本", description="必须与关系的最新版本一致，否则返回 409。")
