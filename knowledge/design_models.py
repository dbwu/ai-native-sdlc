"""仅需求启动的设计流程及模型输出契约。"""
from typing import Annotated

from pydantic import Field, JsonValue, model_validator

from .models import EvidenceRef, Text, Version
from .runtime_models import RuntimeInput

Key = Annotated[str, Field(min_length=1, max_length=80, pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')]
RequirementIds = Annotated[list[str], Field(min_length=1, max_length=20)]


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


class TestPlan(DesignItem):
    steps: list[Text] = Field(min_length=1, max_length=30)
    expected_result: Text


class DesignDocument(RuntimeInput):
    summary: Text
    questions: list[Clarification] = Field(default_factory=list, max_length=20)
    assumptions: list[Text] = Field(default_factory=list, max_length=20)
    components: list[ComponentDesign] = Field(default_factory=list, max_length=20)
    interfaces: list[InterfaceDesign] = Field(default_factory=list, max_length=20)
    data_models: list[DataDesign] = Field(default_factory=list, max_length=20)
    test_plan: list[TestPlan] = Field(default_factory=list, max_length=30)

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
        return self
