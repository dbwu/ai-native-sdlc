import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .db import connect, database_url
from .models import (
    ATTRIBUTE_MODELS, EntityContent, EntityCreate, EntityStatus, EntityUpdate, Kind,
    RelationContent, RelationCreate, RelationStatus, RelationType, RelationUpdate,
)
from .semantics import KnowledgeError, RULES
from .service import KnowledgeService


class EntityView(EntityContent):
    id: UUID
    project_id: UUID
    kind: Kind
    version: int
    metamodel_version: int
    created_at: datetime
    recorded_at: datetime


class RelationView(RelationContent):
    id: UUID
    project_id: UUID
    type: RelationType
    source_id: UUID
    target_id: UUID
    version: int
    metamodel_version: int
    created_at: datetime
    recorded_at: datetime


def create_app(url: str | None = None) -> FastAPI:
    app = FastAPI(title="Engineering Knowledge API", version="0.1.0",
                  description="Phase 1, metamodel v1. Local/trusted use; caller-supplied attribution is not authentication.")

    def service():
        return KnowledgeService(url or database_url())

    @app.exception_handler(KnowledgeError)
    async def knowledge_error(_request: Request, exc: KnowledgeError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(ValidationError)
    async def model_error(_request: Request, exc: ValidationError):
        # Updates validate kind-specific attributes in the service, once kind is known.
        return JSONResponse(status_code=422, content={"detail": exc.errors(include_context=False, include_input=False)})

    @app.exception_handler(psycopg.OperationalError)
    async def database_error(_request: Request, exc: psycopg.OperationalError):
        logging.getLogger(__name__).warning("Knowledge database unavailable: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Knowledge database unavailable"})

    @app.get("/health")
    def health():
        with connect(url or database_url()) as conn:
            conn.execute("SELECT 1 FROM schema_migrations LIMIT 1")
        return {"status": "ok", "metamodel_version": 1}

    @app.get("/metamodel")
    def metamodel():
        return {"version": 1, "entity_attributes": {k: v.model_json_schema() for k, v in ATTRIBUTE_MODELS.items()},
                "relationships": {r: {"source_kinds": sorted(s), "target_kinds": sorted(t)} for r, (s, t) in RULES.items()},
                "additional_rules": ["no self or cross-project links", "SUPERSEDES requires same kind; CONTAINS and SUPERSEDES are acyclic",
                    "ArchitectureComponent CONTAINS technical entities only", "CONFLICTS_WITH is symmetric and canonicalized",
                    "verified links need active fact/human_confirmed evidence and attribution",
                    "verified IMPLEMENTED_BY needs matching code evidence; VERIFIED_BY needs a matching passed test execution"]}

    @app.post("/entities", response_model=EntityView, status_code=201)
    def create_entity(body: EntityCreate):
        return service().create_entity(body)

    @app.get("/entities", response_model=list[EntityView])
    def list_entities(project_id: UUID, kind: Kind | None = None, status: EntityStatus | None = None,
                      q: str | None = Query(None, max_length=300), limit: int = Query(50, ge=1, le=200),
                      offset: int = Query(0, ge=0)):
        return service().list_entities(project_id, kind, status, q, limit, offset)

    @app.get("/entities/{entity_id}", response_model=EntityView)
    def get_entity(entity_id: UUID, version: int | None = Query(None, ge=1)):
        return service().get_entity(entity_id, version)

    @app.get("/entities/{entity_id}/history", response_model=list[EntityView])
    def entity_history(entity_id: UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return service().history("entity", entity_id, limit, offset)

    @app.put("/entities/{entity_id}", response_model=EntityView)
    def update_entity(entity_id: UUID, body: EntityUpdate):
        return service().update_entity(entity_id, body)

    @app.post("/relationships", response_model=RelationView, status_code=201)
    def create_relationship(body: RelationCreate):
        return service().create_relationship(body)

    @app.get("/relationships", response_model=list[RelationView])
    def list_relationships(project_id: UUID, entity_id: UUID | None = None, type: RelationType | None = None,
                           status: RelationStatus | None = None, direction: Literal["both", "outbound", "inbound"] = "both",
                           limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return service().list_relationships(project_id, entity_id, type, status, direction, limit, offset)

    @app.get("/relationships/{relationship_id}", response_model=RelationView)
    def get_relationship(relationship_id: UUID, version: int | None = Query(None, ge=1)):
        return service().get_relationship(relationship_id, version)

    @app.get("/relationships/{relationship_id}/history", response_model=list[RelationView])
    def relationship_history(relationship_id: UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return service().history("relationship", relationship_id, limit, offset)

    @app.put("/relationships/{relationship_id}", response_model=RelationView)
    def update_relationship(relationship_id: UUID, body: RelationUpdate):
        return service().update_relationship(relationship_id, body)

    return app


app = create_app()
