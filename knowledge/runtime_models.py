"""第三阶段的 Agent 契约和任务控制输入。"""
from enum import StrEnum
import math
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue, model_validator

from .models import EvidenceRef, Kind, StrictModel, Text, Version
from .operations_models import Change


class AgentRole(StrEnum):
    PRODUCT = "Product"
    ARCHITECT = "Architect"
    DEVELOPER = "Developer"
    TEST = "Test"
    CURATOR = "Curator"
    ORCHESTRATOR = "Orchestrator"


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    READY = "READY"
    RUNNING = "RUNNING"
    PROPOSED = "PROPOSED"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    CANCELLED = "CANCELLED"


class RuntimeInput(StrictModel):
    """提前拒绝 PostgreSQL JSONB 无法保存的文本和非有限数值。"""

    @model_validator(mode="before")
    @classmethod
    def valid_json_content(cls, data):
        pending = [data]
        while pending:
            value = pending.pop()
            if isinstance(value, BaseModel):
                pending.append(value.model_dump(mode="python"))
            elif isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, (list, tuple)):
                pending.extend(value)
            elif isinstance(value, str):
                if "\x00" in value:
                    raise ValueError("运行时输入不能包含 NUL 字符")
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ValueError("运行时输入包含无效的 Unicode 字符") from exc
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("运行时输入只能包含有限数值")
        return data


class AgentCreate(RuntimeInput):
    project_id: UUID = Field(title="所属项目 ID")
    name: Annotated[str, Field(min_length=1, max_length=200)] = Field(title="Agent 名称")
    role: AgentRole = Field(title="职责角色", description="职责标签，不自动授予组织权限。")
    capabilities: list[Text] = Field(min_length=1, max_length=50, title="能力标识")
    read_kinds: list[Kind] = Field(min_length=1, max_length=12, title="上下文允许读取的类型")
    write_kinds: list[Kind] = Field(default_factory=list, max_length=11, title="允许提出修订的类型")
    actor: Text = Field(title="注册操作者")

    @model_validator(mode="after")
    def scopes(self):
        for field in ("capabilities", "read_kinds", "write_kinds"):
            values = getattr(self, field)
            if len(set(values)) != len(values):
                raise ValueError(f"{field} 不能包含重复项")
        if not set(self.write_kinds) <= set(self.read_kinds):
            raise ValueError("允许修订的知识类型必须同时允许读取")
        if Kind.PROJECT in self.write_kinds:
            raise ValueError("本阶段 Agent 不能提出项目实体的修订")
        return self


class TaskCreate(RuntimeInput):
    project_id: UUID = Field(title="所属项目 ID")
    title: Annotated[str, Field(min_length=1, max_length=300)] = Field(title="任务标题")
    goal: Text = Field(title="任务目标")
    required_capability: Text = Field(title="所需能力标识")
    acceptance_criteria: list[Text] = Field(min_length=1, max_length=50, title="验收条件")
    input_knowledge: list[EvidenceRef] = Field(default_factory=list, max_length=50, title="输入知识版本")
    dependencies: list[UUID] = Field(default_factory=list, max_length=50, title="前置任务 ID")
    max_attempts: int = Field(default=3, ge=1, le=10, title="最大领取次数")
    actor: Text = Field(title="任务创建者")

    @model_validator(mode="after")
    def distinct_inputs(self):
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("前置任务不能重复")
        if len({(r.id, r.version) for r in self.input_knowledge}) != len(self.input_knowledge):
            raise ValueError("输入知识版本不能重复")
        return self


class TaskControl(RuntimeInput):
    expected_version: Version = Field(title="预期任务事件版本")
    actor: Text = Field(title="操作者", description="仅作本地署名，不代表组织授权。")
    reason: Text = Field(title="操作依据")


class AgentStateChange(TaskControl):
    status: Literal["active", "disabled"] = Field(title="Agent 状态")


class ClaimRequest(RuntimeInput):
    agent_id: UUID = Field(title="领取任务的 Agent ID")
    task_id: UUID | None = Field(default=None, title="指定任务 ID", description="省略时按能力和依赖状态领取最早的可执行任务。")
    lease_seconds: int = Field(default=60, ge=5, le=3600, title="租约秒数")


class LeaseRequest(RuntimeInput):
    agent_id: UUID = Field(title="持有租约的 Agent ID")
    lease_token: UUID = Field(title="领取时返回的租约令牌")
    expected_version: Version = Field(title="预期任务事件版本")


class AcceptanceResult(StrictModel):
    criterion_index: int = Field(ge=0, title="验收条件序号", description="从 0 开始。")
    passed: bool = Field(title="执行方声明是否满足", description="声明需复核，不等于独立验证。")
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=20, title="支持本项声明的证据版本")


class TaskReport(LeaseRequest):
    outcome: Literal["succeeded", "failed", "blocked", "review"] = Field(title="执行结果")
    summary: Text = Field(title="结果说明")
    acceptance: list[AcceptanceResult] = Field(default_factory=list, max_length=50, title="逐项验收声明")
    artifacts: dict[str, JsonValue] = Field(default_factory=dict, title="结构化产物", description="只存储 JSON，不执行脚本或抓取地址。")
    changes: list[Change] = Field(default_factory=list, max_length=100, title="建议的知识修订", description="仅创建第二阶段提案，不直接提交知识。")

    @model_validator(mode="after")
    def valid_report(self):
        if len({r.criterion_index for r in self.acceptance}) != len(self.acceptance):
            raise ValueError("验收条件序号不能重复")
        if self.outcome != "succeeded" and self.changes:
            raise ValueError("仅成功报告可以携带待审核的知识修订")
        return self
