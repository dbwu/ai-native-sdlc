import logging
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import FastAPI, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError

from .db import connect, database_url
from .localization import LABELS, localize_validation_errors
from .models import (
    ATTRIBUTE_MODELS, EntityContent, EntityCreate, EntityStatus, EntityUpdate, Kind,
    RelationContent, RelationCreate, RelationStatus, RelationType, RelationUpdate,
)
from .semantics import KnowledgeError, RULES
from .service import KnowledgeService
from .operations_api import operations_router
from .runtime_api import runtime_router


class EntityView(EntityContent):
    """实体的指定修订版本及其稳定身份。"""

    id: UUID = Field(title="实体标识", description="服务端生成的稳定 UUID，不随名称或路径变化。")
    project_id: UUID = Field(title="所属项目", description="该实体所属项目的稳定 UUID。")
    kind: Kind = Field(title="实体类型", description="类型标识保持英文；中文名称见 /metamodel 的 labels。")
    version: int = Field(title="内容版本", description="从 1 开始递增的实体修订版本。")
    metamodel_version: int = Field(title="元模型版本", description="解释该修订内容所使用的元模型版本。")
    created_at: datetime = Field(title="身份创建时间", description="实体身份首次创建的服务端时间。")
    recorded_at: datetime = Field(title="版本记录时间", description="该修订写入数据库的服务端时间。")


class RelationView(RelationContent):
    """关系的指定修订版本及其稳定身份。"""

    id: UUID = Field(title="关系标识", description="关系身份的稳定 UUID。")
    project_id: UUID = Field(title="所属项目", description="关系两端及证据必须属于同一项目。")
    type: RelationType = Field(title="关系类型", description="规范存储方向对应的关系类型标识。")
    source_id: UUID = Field(title="源实体标识", description="关系起点实体的稳定 UUID。")
    target_id: UUID = Field(title="目标实体标识", description="关系终点实体的稳定 UUID。")
    version: int = Field(title="内容版本", description="从 1 开始递增的关系修订版本。")
    metamodel_version: int = Field(title="元模型版本", description="解释该修订所使用的元模型版本。")
    created_at: datetime = Field(title="身份创建时间", description="关系身份首次创建的服务端时间。")
    recorded_at: datetime = Field(title="版本记录时间", description="该关系修订写入数据库的服务端时间。")


def create_app(url: str | None = None) -> FastAPI:
    app = FastAPI(
        title="工程知识模型 API", version="0.3.0",
        description="AI 原生 SDLC 第一至三阶段，元模型版本 1。提供知识模型、知识操作与 Agent 协作运行时。"
                    "供本地或可信环境使用；调用方提交的操作者信息仅用于归属记录，不代表已完成身份认证。"
                    "字段名、枚举值和路由保持稳定，中文名称可通过 /metamodel 查询。",
        openapi_tags=[
            {"name": "系统与元模型", "description": "查看服务状态、实体属性模式及关系规则。"},
            {"name": "知识实体", "description": "创建、查询和修订项目内的工程知识实体。"},
            {"name": "知识关系", "description": "管理显式关系、适用版本、验证状态和证据关联。"},
        ],
    )

    def service():
        return KnowledgeService(url or database_url())

    @app.exception_handler(KnowledgeError)
    async def knowledge_error(_request: Request, exc: KnowledgeError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(ValidationError)
    async def model_error(_request: Request, exc: ValidationError):
        # 更新时先确定实体类型，再由服务层校验该类型的专有属性。
        errors = localize_validation_errors(exc.errors(include_input=False))
        for error in errors:
            error.pop("ctx", None)
        return JSONResponse(status_code=422, content=jsonable_encoder({"detail": errors}))

    @app.exception_handler(RequestValidationError)
    async def request_error(_request: Request, exc: RequestValidationError):
        errors = localize_validation_errors(exc.errors())
        return JSONResponse(status_code=422, content=jsonable_encoder({"detail": errors}))

    @app.exception_handler(psycopg.OperationalError)
    async def database_error(_request: Request, exc: psycopg.OperationalError):
        logging.getLogger(__name__).warning("工程知识数据库暂不可用：%s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "工程知识数据库暂不可用"})

    @app.get("/health", tags=["系统与元模型"], summary="检查服务与数据库状态",
             description="确认数据库可连接且迁移记录可读取；成功时返回 ok 和元模型版本。")
    def health():
        with connect(url or database_url()) as conn:
            conn.execute("SELECT 1 FROM schema_migrations LIMIT 1")
        return {"status": "ok", "metamodel_version": 1}

    @app.get("/metamodel", tags=["系统与元模型"], summary="查询工程知识元模型",
             description="返回元模型版本、各实体类型的属性 JSON Schema、关系方向约束和中文名称对照。")
    def metamodel():
        return {"version": 1, "entity_attributes": {k: v.model_json_schema() for k, v in ATTRIBUTE_MODELS.items()},
                "relationships": {r: {"source_kinds": sorted(s), "target_kinds": sorted(t)} for r, (s, t) in RULES.items()},
                "labels": LABELS,
                "additional_rules": ["不允许自关联或跨项目关系", "SUPERSEDES 两端须为同类实体；CONTAINS 和 SUPERSEDES 不允许形成环",
                    "ArchitectureComponent 的 CONTAINS 关系只能指向技术实体", "CONFLICTS_WITH 为对称关系，按 UUID 顺序规范化存储",
                    "已验证关系需要生效的事实或人工确认证据，关系来源也必须标记为事实或人工确认",
                    "已验证的 IMPLEMENTED_BY 需要匹配目标版本的代码证据；VERIFIED_BY 需要匹配目标版本且通过的测试执行证据"]}

    @app.post("/entities", response_model=EntityView, status_code=201, tags=["知识实体"], summary="创建知识实体",
              description="创建稳定身份和第 1 个修订版本。创建 Project 时省略 project_id；其他实体必须指定所属项目。")
    def create_entity(body: EntityCreate):
        return service().create_entity(body)

    @app.get("/entities", response_model=list[EntityView], tags=["知识实体"], summary="查询项目内的知识实体",
             description="查询实体的最新修订，可按类型、状态、名称或描述筛选。默认包含全部生命周期状态，按身份创建时间及 UUID 排序。")
    def list_entities(project_id: Annotated[UUID, Query(description="所属项目的稳定 UUID。")],
                      kind: Kind | None = Query(None, description="按实体类型筛选；省略时返回所有类型。"),
                      status: EntityStatus | None = Query(None, description="按生命周期状态筛选；省略时包含所有状态。"),
                      q: str | None = Query(None, max_length=300, description="名称和描述中的字面子串，不区分大小写。"),
                      limit: int = Query(50, ge=1, le=200, description="本页最多返回的记录数，范围为 1～200。"),
                      offset: int = Query(0, ge=0, description="跳过的记录数，从 0 开始。")):
        return service().list_entities(project_id, kind, status, q, limit, offset)

    @app.get("/entities/{entity_id}", response_model=EntityView, tags=["知识实体"], summary="读取知识实体的指定版本",
             description="省略 version 时返回最新修订；指定版本时返回对应的不可变历史快照。")
    def get_entity(entity_id: Annotated[UUID, Path(description="实体的稳定 UUID。")],
                   version: int | None = Query(None, ge=1, description="指定内容版本；省略时读取最新修订。")):
        return service().get_entity(entity_id, version)

    @app.get("/entities/{entity_id}/history", response_model=list[EntityView], tags=["知识实体"], summary="查询知识实体的修订历史",
             description="按版本从新到旧返回不可变修订快照，支持分页。")
    def entity_history(entity_id: Annotated[UUID, Path(description="实体的稳定 UUID。")],
                       limit: int = Query(50, ge=1, le=200, description="本页最多返回的修订数，范围为 1～200。"),
                       offset: int = Query(0, ge=0, description="跳过的修订数，从 0 开始。")):
        return service().history("entity", entity_id, limit, offset)

    @app.put("/entities/{entity_id}", response_model=EntityView, tags=["知识实体"], summary="修订知识实体",
             description="完整替换可变内容并追加新版本，不修改历史快照。expected_version 必须等于当前版本；身份、类型和所属项目保持稳定。")
    def update_entity(entity_id: Annotated[UUID, Path(description="实体的稳定 UUID。")], body: EntityUpdate):
        return service().update_entity(entity_id, body)

    @app.post("/relationships", response_model=RelationView, status_code=201, tags=["知识关系"], summary="创建知识关系",
              description="创建显式关系并固定两端适用版本。服务校验关系类型、方向、项目边界和证据；CONFLICTS_WITH 按 UUID 顺序规范化存储。")
    def create_relationship(body: RelationCreate):
        return service().create_relationship(body)

    @app.get("/relationships", response_model=list[RelationView], tags=["知识关系"], summary="查询项目内的知识关系",
             description="查询关系的最新修订，支持按关联实体、方向、类型和状态筛选。默认包含全部关系状态；返回显式记录的两端版本和证据版本。")
    def list_relationships(project_id: Annotated[UUID, Query(description="所属项目的稳定 UUID。")],
                           entity_id: UUID | None = Query(None, description="筛选与该实体关联的关系。"),
                           type: RelationType | None = Query(None, description="按关系类型筛选。"),
                           status: RelationStatus | None = Query(None, description="按关系状态筛选；省略时包含所有状态。"),
                           direction: Literal["both", "outbound", "inbound"] = Query("both", description="相对 entity_id 的方向：both 为双向，outbound 为出向，inbound 为入向。"),
                           limit: int = Query(50, ge=1, le=200, description="本页最多返回的记录数，范围为 1～200。"),
                           offset: int = Query(0, ge=0, description="跳过的记录数，从 0 开始。")):
        return service().list_relationships(project_id, entity_id, type, status, direction, limit, offset)

    @app.get("/relationships/{relationship_id}", response_model=RelationView, tags=["知识关系"], summary="读取知识关系的指定版本",
             description="省略 version 时返回最新修订；指定版本时返回对应的不可变关系快照。")
    def get_relationship(relationship_id: Annotated[UUID, Path(description="关系的稳定 UUID。")],
                         version: int | None = Query(None, ge=1, description="指定关系内容版本；省略时读取最新修订。")):
        return service().get_relationship(relationship_id, version)

    @app.get("/relationships/{relationship_id}/history", response_model=list[RelationView], tags=["知识关系"], summary="查询知识关系的修订历史",
             description="按版本从新到旧返回关系历史及其固定的实体和证据版本，支持分页。")
    def relationship_history(relationship_id: Annotated[UUID, Path(description="关系的稳定 UUID。")],
                             limit: int = Query(50, ge=1, le=200, description="本页最多返回的修订数，范围为 1～200。"),
                             offset: int = Query(0, ge=0, description="跳过的修订数，从 0 开始。")):
        return service().history("relationship", relationship_id, limit, offset)

    @app.put("/relationships/{relationship_id}", response_model=RelationView, tags=["知识关系"], summary="修订知识关系",
             description="追加关系修订并重新校验证据和生命周期。expected_version 必须等于当前版本；关系类型及两端实体身份保持稳定。")
    def update_relationship(relationship_id: Annotated[UUID, Path(description="关系的稳定 UUID。")], body: RelationUpdate):
        return service().update_relationship(relationship_id, body)

    app.include_router(operations_router(lambda: url or database_url()))
    app.include_router(runtime_router(lambda: url or database_url()))
    return app


app = create_app()
