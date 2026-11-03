"""第四阶段：显式选择的 SDLC 参考流程与阶段门禁输入。"""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from .models import EvidenceRef, Text
from .runtime_models import LeaseRequest, RuntimeInput, TaskControl


class WorkflowCreate(RuntimeInput):
    project_id: UUID = Field(title="所属项目 ID")
    title: Annotated[str, Field(min_length=1, max_length=200)] = Field(title="流程标题")
    goal: Text = Field(title="本次目标", description="由调用方明确提供，不从示例推定项目要求。")
    scenario: Literal["order_cancellation_v1"] = Field(title="明确选择的参考场景", description="MVP 只内置这一可信执行适配器；不是通用代码执行入口。")
    input_knowledge: list[EvidenceRef] = Field(default_factory=list, max_length=20, title="额外固定版本知识")
    max_stage_retries: int = Field(default=2, ge=0, le=5, title="每阶段人工重建任务次数上限")
    actor: Text = Field(title="创建者")


class WorkflowControl(TaskControl):
    """expected_version 指流程事件版本，不是任务或知识提案版本。"""


class ReferenceExecutionRequest(LeaseRequest):
    """仅当前验证任务的租约持有方可以请求固定可信参考执行器。"""


STAGES = [
    {"key": "requirements", "title": "需求分析", "role": "Product", "capability": "sdlc.requirements",
     "acceptance": "结构化需求和验收条件已形成，待人工审阅", "outputs": ["requirement"]},
    {"key": "architecture", "title": "架构设计", "role": "Architect", "capability": "sdlc.architecture",
     "acceptance": "组件职责、接口及数据结构与需求目标一致，待人工审阅", "outputs": ["architecture", "interface", "data"]},
    {"key": "implementation", "title": "代码实现", "role": "Developer", "capability": "sdlc.implementation",
     "acceptance": "代码内容和摘要相符，且实现证据对应同一代码版本", "outputs": ["code", "code-evidence"]},
    {"key": "verification", "title": "测试验证", "role": "Test", "capability": "sdlc.verification",
     "acceptance": "当前代码已通过可信参考执行器，测试内容和执行证据可追溯", "outputs": ["test", "test-evidence"]},
]
