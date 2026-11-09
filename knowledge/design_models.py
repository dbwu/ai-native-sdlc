"""仅需求启动的设计流程及模型输出契约。"""
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from .models import EvidenceRef, Text, Version
from .runtime_models import RuntimeInput

Key = Annotated[str, Field(min_length=1, max_length=80, pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')]
RequirementIds = Annotated[list[str], Field(min_length=1, max_length=20)]
DiagramLabel = Annotated[str, Field(min_length=1, max_length=160, pattern=r'^[^\x00-\x1f\x7f\u2028\u2029]+$')]


class DesignFlowCreate(RuntimeInput):
    title: str = Field(min_length=1, max_length=200, title="设计流程名称")
    goal: Text = Field(title="设计目标")
    requirements: list[EvidenceRef] = Field(min_length=1, max_length=20, title="已有需求的具体版本")
    input_knowledge: list[EvidenceRef] = Field(default_factory=list, max_length=25, title="额外设计依据")
    actor: str = Field(min_length=1, max_length=100, title="创建署名")


class DesignAction(RuntimeInput):
    expected_version: Version = Field(title="预期设计流程版本")
    proposal_version: Version | None = Field(default=None, title="预期设计提案版本")
    actor: str = Field(min_length=1, max_length=100, title="操作署名")
    reason: Text = Field(title="操作依据")


class Clarification(RuntimeInput):
    key: Key
    question: Text
    requirement_ids: RequirementIds


class AcceptanceClarification(Clarification):
    acceptance_criterion: Text = Field(title="原样引用会受影响的现有验收条件")
    acceptance_impact: Text = Field(title="不同规则会如何改变验收结果")
    options: list[Text] = Field(min_length=2, max_length=5, title="会导致不同验收结果的待确认选项")


class Answer(RuntimeInput):
    key: Key
    answer: Text


class AnswerSubmission(DesignAction):
    answers: list[Answer] = Field(min_length=1, max_length=20, title="逐项澄清答复")


class DesignItem(RuntimeInput):
    key: Key
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10000)
    requirement_ids: RequirementIds


class ComponentDesign(DesignItem):
    responsibility: Text


class InterfaceDesign(DesignItem):
    protocol: str = Field(min_length=1, max_length=100)
    component_key: Key
    contract: dict[str, JsonValue] = Field(min_length=1)


class DataDesign(DesignItem):
    component_key: Key
    schema_definition: dict[str, JsonValue] = Field(min_length=1)


class CollaborationEndpoint(RuntimeInput):
    kind: Literal["component", "interface", "data_model"] = Field(title="本轮设计对象类型")
    key: Key = Field(title="本轮设计对象标识")


class CollaborationLink(RuntimeInput):
    source: CollaborationEndpoint = Field(title="发起调用、读写或依赖的一方")
    target: CollaborationEndpoint = Field(title="被调用、读写或依赖的一方")
    type: Literal["calls", "reads", "writes", "depends_on"] = Field(title="协作关系类型")
    rationale: Text = Field(title="说明该关系如何满足关联需求或设计依据")
    requirement_ids: RequirementIds = Field(title="此关系服务的本轮需求标识")

    @model_validator(mode="after")
    def consistent_link(self):
        if self.source == self.target:
            raise ValueError("协作关系的两端必须是不同的设计对象")
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("同一协作关系不能重复引用需求")
        action_kinds = {"component", "interface"}
        if self.type == "calls" and not {self.source.kind, self.target.kind}.issubset(action_kinds):
            raise ValueError("调用关系的两端必须是组件或接口")
        if self.type in {"reads", "writes"} and (self.source.kind not in action_kinds or self.target.kind != "data_model"):
            raise ValueError("读写关系必须从组件或接口指向数据结构")
        return self


class TestPlan(DesignItem):
    steps: list[Text] = Field(min_length=1, max_length=30)
    expected_result: Text


class SequenceParticipant(RuntimeInput):
    key: Key = Field(title="参与者标识")
    name: DiagramLabel = Field(max_length=96, title="参与者名称")


class SequenceMessage(RuntimeInput):
    source: Key = Field(title="发送方标识")
    target: Key = Field(title="接收方标识")
    label: DiagramLabel = Field(title="单行交互说明")


class SequenceDiagram(RuntimeInput):
    title: DiagramLabel = Field(max_length=120, title="业务交互标题")
    requirement_ids: RequirementIds = Field(title="图所解释的需求标识")
    participants: list[SequenceParticipant] = Field(min_length=2, max_length=8, title="参与交互的角色、组件或外部系统")
    messages: list[SequenceMessage] = Field(min_length=1, max_length=20, title="按发生顺序记录的交互")

    @model_validator(mode="after")
    def consistent_participants(self):
        keys = {participant.key for participant in self.participants}
        if len(keys) != len(self.participants):
            raise ValueError("同一时序图的参与者标识不能重复")
        if any(message.source not in keys or message.target not in keys for message in self.messages):
            raise ValueError("时序图交互必须引用本图定义的参与者")
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("同一时序图不能重复引用需求")
        return self


class UseCaseNode(RuntimeInput):
    key: Key = Field(title="图内标识")
    name: DiagramLabel = Field(max_length=96, title="参与者或用例名称")


class UseCaseAssociation(RuntimeInput):
    actor: Key = Field(title="参与者标识")
    use_case: Key = Field(title="用例标识")


class UseCaseDiagram(RuntimeInput):
    title: DiagramLabel = Field(max_length=120, title="需求用例图标题")
    requirement_ids: RequirementIds = Field(title="图所解释的需求标识")
    actors: list[UseCaseNode] = Field(min_length=1, max_length=8, title="系统边界外的业务参与者")
    use_cases: list[UseCaseNode] = Field(min_length=1, max_length=12, title="参与者通过系统达成的目标")
    associations: list[UseCaseAssociation] = Field(min_length=1, max_length=24, title="参与者与用例的关联")

    @model_validator(mode="after")
    def consistent_associations(self):
        actors = {actor.key for actor in self.actors}
        cases = {case.key for case in self.use_cases}
        if len(actors) != len(self.actors) or len(cases) != len(self.use_cases):
            raise ValueError("同一用例图内同类节点标识不能重复")
        pairs = {(item.actor, item.use_case) for item in self.associations}
        if len(pairs) != len(self.associations):
            raise ValueError("同一用例图不能重复声明同一关联")
        if any(actor not in actors or case not in cases for actor, case in pairs):
            raise ValueError("用例图关联必须引用本图的参与者和用例")
        if {actor for actor, _ in pairs} != actors or {case for _, case in pairs} != cases:
            raise ValueError("用例图中的参与者和用例都必须出现在关联中")
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("同一用例图不能重复引用需求")
        return self


class DesignDocument(RuntimeInput):
    summary: Text
    questions: list[Clarification] = Field(default_factory=list, max_length=20)
    assumptions: list[Text] = Field(default_factory=list, max_length=20)
    deferred_items: list[Text] = Field(default_factory=list, max_length=20, title="后续事项，不属于当前设计范围")
    components: list[ComponentDesign] = Field(default_factory=list, max_length=20)
    interfaces: list[InterfaceDesign] = Field(default_factory=list, max_length=20)
    data_models: list[DataDesign] = Field(default_factory=list, max_length=20)
    test_plan: list[TestPlan] = Field(default_factory=list, max_length=30)
    sequence_diagrams: list[SequenceDiagram] = Field(default_factory=list, max_length=6, title="业务时序图，可选")
    use_case_diagrams: list[UseCaseDiagram] = Field(default_factory=list, max_length=4, title="需求分析用例图，可选")
    collaboration_links: list[CollaborationLink] = Field(default_factory=list, max_length=50, title="架构协作关系，可选")

    @model_validator(mode="after")
    def complete_and_consistent(self):
        groups = (self.components, self.interfaces, self.data_models, self.test_plan)
        if sum(map(len, groups)) > 50:
            raise ValueError("本里程碑最多生成50项设计知识")
        if not self.questions and (not self.components or not self.test_plan):
            raise ValueError("无待澄清问题时必须形成组件设计和测试计划")
        for group in (*groups, self.questions):
            keys = [item.key for item in group]
            if len(keys) != len(set(keys)):
                raise ValueError("同类设计条目的稳定键不能重复")
            if any(len(item.requirement_ids) != len(set(item.requirement_ids)) for item in group):
                raise ValueError("同一条目不能重复引用需求")
        components = {item.key for item in self.components}
        if any(item.component_key not in components for item in [*self.interfaces, *self.data_models]):
            raise ValueError("接口和数据模型必须引用本次设计中存在的组件")
        endpoint_groups = (("component", self.components), ("interface", self.interfaces), ("data_model", self.data_models))
        nodes = {(kind, item.key): set(item.requirement_ids) for kind, items in endpoint_groups for item in items}
        seen = set()
        for link in self.collaboration_links:
            source, target = (link.source.kind, link.source.key), (link.target.kind, link.target.key)
            if source not in nodes or target not in nodes:
                raise ValueError("协作关系必须引用本轮设计中存在的组件、接口或数据结构")
            if not set(link.requirement_ids).issubset(nodes[source] | nodes[target]):
                raise ValueError("协作关系只能引用至少一个端点关联的需求")
            identity = (source, target, link.type)
            if identity in seen:
                raise ValueError("同一方向和类型的协作关系不能重复，请合并关联需求")
            seen.add(identity)
        return self


class ConfirmedScopeDesign(DesignDocument):
    questions: list[AcceptanceClarification] = Field(default_factory=list, max_length=20)
