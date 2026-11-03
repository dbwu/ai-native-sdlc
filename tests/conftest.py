import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from knowledge.api import create_app
from knowledge.db import migrate
from knowledge.service import KnowledgeService


@pytest.fixture(scope="session")
def db_url():
    base_url = os.environ.get("TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("Set TEST_DATABASE_URL for real PostgreSQL integration tests")
    # Isolated, generated schema: never truncate or drop the caller's tables.
    schema = "knowledge_test_" + uuid4().hex
    with psycopg.connect(base_url, autocommit=True, connect_timeout=5) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    url = make_conninfo(base_url, options=f"-csearch_path={schema}")
    try:
        migrate(url)
        yield url
    finally:
        with psycopg.connect(base_url, autocommit=True, connect_timeout=5) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def service(db_url):
    return KnowledgeService(db_url)


@pytest.fixture
def client(db_url):
    with TestClient(create_app(db_url)) as client:
        yield client


@pytest.fixture
def provenance():
    return {"actor": "test-author", "source_uri": "urn:fixture:phase1", "source_revision": "fixture-v1",
            "change_reason": "Record test fixture", "nature": "human_confirmed"}


@pytest.fixture
def project(client, provenance):
    response = client.post("/entities", json={"kind": "Project", "name": "Synthetic test project",
                                             "status": "active", "provenance": provenance})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def entity(client, project, provenance):
    def create(kind="Requirement", name=None, attributes=None, **kwargs):
        payload = {"kind": kind, "project_id": project["id"], "name": name or kind,
                   "status": "active", "attributes": attributes or {}, "provenance": provenance, **kwargs}
        response = client.post("/entities", json=payload)
        assert response.status_code == 201, response.text
        return response.json()
    return create


@pytest.fixture
def relationship(client, provenance):
    def create(source, target, type="DEPENDS_ON", expected=201, **kwargs):
        payload = {"type": type, "source_id": source["id"], "target_id": target["id"],
                   "source_version": source["version"], "target_version": target["version"],
                   "rationale": "Explicit relationship fixture", "provenance": provenance, **kwargs}
        response = client.post("/relationships", json=payload)
        assert response.status_code == expected, response.text
        return response.json()
    return create
