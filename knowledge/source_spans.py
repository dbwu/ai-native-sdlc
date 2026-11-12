"""固定原文快照中的一段连续引文。"""
from typing import Annotated
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from .models import StrictModel, Version
from .semantics import KnowledgeError


class SourceSpan(StrictModel):
    source_id: UUID = Field(title="来源 ID")
    source_version: Version = Field(title="固定来源版本")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$", title="原文快照 SHA-256")
    start: int = Field(ge=0, strict=True, title="起始位置（从零计数的 Unicode 码点）")
    end: int = Field(gt=0, strict=True, title="结束位置（不含该位置）")
    quote: Annotated[str, StringConstraints(strip_whitespace=False), Field(min_length=1, max_length=4000)]

    @model_validator(mode="after")
    def check_length(self):
        if not self.quote.strip():
            raise ValueError("原文引文须包含可见文字")
        if self.end - self.start != len(self.quote):
            raise ValueError("引文长度须等于结束位置减去起始位置")
        return self


def read_source_span(conn, project_id, span):
    source = conn.execute("""SELECT s.uri, r.external_revision, r.format, r.sha256, r.content
        FROM knowledge_source s JOIN source_revision r ON r.source_id=s.id
        WHERE s.project_id=%s AND s.id=%s AND r.version=%s""",
        (project_id, span.source_id, span.source_version)).fetchone()
    if source is None:
        raise KnowledgeError("未找到本项目的指定来源快照", 404)
    if source["sha256"] != span.sha256:
        raise KnowledgeError("原文快照摘要不匹配，请重新读取固定来源版本", 409)
    if source["content"][span.start:span.end] != span.quote:
        raise KnowledgeError("引文与原文在指定位置的文字不一致")
    return {key: source[key] for key in ("uri", "external_revision", "format")}
