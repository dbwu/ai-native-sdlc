"""确定性解析采集内容，只生成候选知识，不访问外部来源或写入数据库。

结构化 JSON 和 Markdown 中显式的 knowledge-json 围栏共用同一输入契约。
来源署名、来源版本和可信性质由采集服务统一注入，不能由源文档自行声明。
"""
import json
import math
import re
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from .models import ATTRIBUTE_MODELS, Kind, RelationType, StrictModel, Text
from .semantics import KnowledgeError, validate_types


MAX_SOURCE_BYTES = 1024 * 1024
SourceFormat = Literal["json", "markdown"]
SourceKey = Annotated[str, StringConstraints(strip_whitespace=False), Field(min_length=1, max_length=200)]


def _validate_key(value: str) -> str:
    if any(character.isspace() for character in value):
        raise ValueError("来源 key 不能包含空白字符")
    return value


class SourceEntity(StrictModel):
    """显式来源条目；key 在同一来源内稳定，不能使用标题自动猜测身份。"""

    key: SourceKey = Field(title="来源内稳定标识", description="最多 200 字符，区分大小写，不能包含空白。")
    kind: Kind = Field(title="候选实体类型", description="不允许导入 Project；项目范围由采集请求指定。")
    name: Annotated[str, Field(min_length=1, max_length=300)] = Field(title="名称")
    description: str = Field(default="", max_length=100000, title="说明")
    status: Literal["draft"] = Field(default="draft", title="候选状态", description="来源只能提出草稿，不能自行确认知识。")
    attributes: dict[str, Any] = Field(default_factory=dict, title="类型专有属性")

    _key = field_validator("key")(_validate_key)

    @model_validator(mode="after")
    def validate_entity(self):
        if self.kind == Kind.PROJECT:
            raise ValueError("来源内容不能创建或修订 Project，请先通过项目接口建立项目范围")
        self.attributes = ATTRIBUTE_MODELS[self.kind].model_validate(self.attributes).model_dump(mode="json")
        return self


class SourceRelationship(StrictModel):
    """以本批来源 key 声明显式关系，不自动推断未提供的关系。"""

    type: RelationType = Field(title="关系类型")
    source_key: SourceKey = Field(title="源实体的来源标识")
    target_key: SourceKey = Field(title="目标实体的来源标识")
    status: Literal["proposed"] = Field(default="proposed", title="候选关系状态")
    rationale: Text = Field(title="关系依据")

    _keys = field_validator("source_key", "target_key")(_validate_key)


class SourceKnowledge(StrictModel):
    """单次采集的完整候选批次，实体和关系规模都有明确上限。"""

    entities: list[SourceEntity] = Field(min_length=1, max_length=100, title="候选实体")
    relationships: list[SourceRelationship] = Field(default_factory=list, max_length=200, title="候选关系")

    @model_validator(mode="after")
    def validate_graph(self):
        kinds = {}
        for entity in self.entities:
            if entity.key in kinds:
                raise ValueError(f"来源内 key 重复：{entity.key}")
            kinds[entity.key] = entity.kind
        relationships = set()
        acyclic = {RelationType.CONTAINS: {}, RelationType.SUPERSEDES: {}}
        for relation in self.relationships:
            for key in (relation.source_key, relation.target_key):
                if key not in kinds:
                    raise ValueError(f"关系引用了本批未定义的来源 key：{key}")
            if relation.source_key == relation.target_key:
                raise ValueError("来源内容不允许实体与自身建立关系")
            validate_types(relation.type, kinds[relation.source_key], kinds[relation.target_key])
            ends = (relation.source_key, relation.target_key)
            if relation.type == RelationType.CONFLICTS_WITH:
                ends = tuple(sorted(ends))
            signature = (relation.type, *ends)
            if signature in relationships:
                raise ValueError("来源内容包含重复关系，或包含同一对称关系的反向重复")
            relationships.add(signature)
            if relation.type in acyclic:
                acyclic[relation.type].setdefault(relation.source_key, []).append(relation.target_key)
        for relation_type, edges in acyclic.items():
            _reject_cycle(relation_type, edges)
        return self


def _reject_cycle(relation_type: RelationType, edges: dict[str, list[str]]):
    # 最多 100 个实体；递归深度不会随来源文本的长度无限增长。
    complete, active = set(), set()

    def visit(node):
        if node in active:
            raise ValueError(f"来源内容中的 {relation_type} 关系会形成环路")
        if node in complete:
            return
        active.add(node)
        for child in edges.get(node, []):
            visit(child)
        active.remove(node)
        complete.add(node)

    for node in edges:
        visit(node)


def _markdown_payload(content: str) -> str:
    """识别显式围栏并跳过其他代码块，避免把示例中的伪围栏当作知识。"""
    fence = None
    current, blocks = [], []
    for line in content.splitlines(keepends=True):
        if fence is None:
            opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})([^\r\n]*)(?:\r?\n)?", line)
            if opening is None:
                continue
            marker, info = opening.groups()
            fence = (marker[0], len(marker), info.strip() == "knowledge-json")
            current = []
        else:
            character, minimum, is_knowledge = fence
            closing = re.fullmatch(r" {0,3}" + re.escape(character) + "{" + str(minimum) + r",}[ \t]*(?:\r?\n)?", line)
            if closing:
                if is_knowledge:
                    blocks.append("".join(current))
                fence = None
            elif is_knowledge:
                current.append(line)
    if fence and fence[2]:
        raise KnowledgeError("Markdown 的 knowledge-json 代码围栏未闭合")
    if len(blocks) != 1:
        raise KnowledgeError("Markdown 必须且只能包含一个显式 knowledge-json 代码块；普通正文不会自动抽取知识")
    return blocks[0]


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KnowledgeError(f"JSON 对象含有重复字段：{key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise KnowledgeError(f"JSON 不允许非有限数值：{value}")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise KnowledgeError("JSON 数值超出有限浮点数范围")
    return number


def _validate_decoded_text(document):
    # PostgreSQL JSONB 不接受 NUL 或未配对的代理字符，即使它们以 JSON 转义输入。
    pending = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            if "\x00" in value:
                raise KnowledgeError("来源 JSON 字符串不能包含 NUL 字符")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise KnowledgeError("来源 JSON 字符串包含无效的 Unicode 字符") from exc


def parse_source(format: SourceFormat, content: str) -> dict:
    """解析为规范化候选批次；不分配正式 ID，也不改变任何已存在知识。

    来源内 key 精确匹配且区分大小写。实际 ID 分配、跨修订映射、来源追踪、
    审核和事务提交由第二阶段操作服务负责。
    """
    if format not in {"json", "markdown"}:
        raise KnowledgeError("不支持的来源格式，请使用 json 或 markdown")
    if not isinstance(content, str):
        raise KnowledgeError("来源内容必须为 UTF-8 文本字符串")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise KnowledgeError("来源内容包含无效的 Unicode 字符") from exc
    if size > MAX_SOURCE_BYTES:
        raise KnowledgeError("单次来源内容不能超过 1 MiB（按 UTF-8 字节计算）")
    payload = _markdown_payload(content) if format == "markdown" else content
    try:
        document = json.loads(payload, object_pairs_hook=_json_object, parse_constant=_invalid_constant,
                              parse_float=_finite_float)
    except json.JSONDecodeError as exc:
        raise KnowledgeError(f"来源内容不是有效 JSON：第 {exc.lineno} 行，第 {exc.colno} 列") from exc
    except (RecursionError, ValueError) as exc:
        raise KnowledgeError("JSON 结构嵌套过深或数值超出可解析范围") from exc
    _validate_decoded_text(document)
    try:
        return SourceKnowledge.model_validate(document).model_dump(mode="json")
    except RecursionError as exc:
        raise KnowledgeError("来源内容的结构嵌套过深") from exc
