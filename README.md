# Engineering Knowledge Model — Phase 1

Runnable knowledge foundation for AI Native SDLC: 12 entity kinds, 9 relationship
types, stable UUIDs, provenance, pinned evidence, immutable revision history,
lifecycle validation, and an HTTP/service API. No Agents or orchestration.

The initial folder contained only project instructions and an empty read-only
`sources/` directory. There was no codebase or stack to preserve. All implementation
files are new; synced reference material is unchanged.

## Run

With Docker Compose:

```sh
docker compose up --build
```

Open [interactive API docs](http://127.0.0.1:8000/docs) or
[health](http://127.0.0.1:8000/health). The database and API bind only to localhost.
Compose uses explicitly local development credentials and persists data in a named
volume. Migrations run transactionally before the API starts. Use `docker compose
down` to stop; it preserves the volume.

Alternatively, use Python 3.11+ and an existing PostgreSQL 17+ database. Create a
virtual environment and install the pinned packages, then the project:

```sh
python -m venv .venv
# Activate .venv for your shell.
python -m pip install -r requirements-test.lock
python -m pip install --no-deps -e .
```

Set `DATABASE_URL` in the shell (the app does not automatically load `.env`):

```powershell
$env:DATABASE_URL = 'postgresql://knowledge:local-dev-only@127.0.0.1:5432/knowledge'
python -m knowledge.db
python -m uvicorn knowledge.api:app --host 127.0.0.1 --port 8000
```

POSIX shells: `export DATABASE_URL='postgresql://…'`, then the same Python commands.
To run only the Compose database: `docker compose up -d db`. Runtime-only
installations can use `requirements.lock`; `requirements-test.lock` includes tests.

In another terminal, seed a **synthetic** example covering all entity and
relationship types:

```sh
python -m knowledge.demo
```

It prints IDs for the created project, requirement, and relationships. Each run
creates a separate project. Sample code and passed-test evidence are fabricated
examples, not actual code verification or test execution.

## API

| Method | Route | Behavior |
|---|---|---|
| GET | `/metamodel` | Attribute JSON Schemas, relationship matrix, extra rules |
| POST | `/entities` | Create Project or project-scoped entity at revision 1 |
| GET | `/entities?project_id=…` | Filter latest revisions by `kind`, `status`, `q` |
| GET | `/entities/{id}?version=…` | Latest or exact immutable revision |
| PUT | `/entities/{id}` | Full replacement revision with `expected_version` |
| GET | `/entities/{id}/history` | Immutable revisions, newest first |
| POST | `/relationships` | Create link with exact source/target versions |
| GET | `/relationships?project_id=…` | Filter by `entity_id`, `direction`, `type`, `status` |
| GET/PUT | `/relationships/{id}` | Read latest/exact revision or append revision |
| GET | `/relationships/{id}/history` | Relationship revision history |

List/history endpoints support `limit` (1–200, default 50) and `offset` (default 0).
Status filters are explicit; lists include all lifecycle states by default. Search
is case-insensitive literal substring search over name and description, not vector
search. `direction` is `both`, `outbound`, or `inbound` and applies to `entity_id`.

Start with a Project (`project_id` omitted):

```json
{
  "kind": "Project",
  "name": "Example project",
  "status": "active",
  "provenance": {
    "actor": "local-author",
    "source_uri": "urn:example:planning",
    "source_revision": "meeting-001",
    "change_reason": "Establish the project knowledge boundary",
    "nature": "human_confirmed"
  }
}
```

Use its returned `id` as `project_id` for subsequent entities. Entity attributes
are typed per kind; `/metamodel` exposes their schemas. No business-specific fields
are mandatory except the Evidence contract. Optional attributes include acceptance
criteria, rule expressions, interface contracts, data schema, code locators,
test steps, decision rationale, and constraint expressions. Add domain-specific
JSON in `attributes.extensions`; unknown core fields are rejected.

Every write requires provenance. `nature` is `fact`, `human_confirmed`, `inference`,
or `assumption` (default); optional confidence is bounded 0–1. URI strings are
locators only and are never fetched. A changed title/path never changes an ID.
PUT replaces the complete mutable content; omitted optional values reset to their
defaults. Identity/kind/project and relationship endpoints/type cannot be changed.

Evidence is an entity with `category`, absolute `uri`, and external `revision`;
optional SHA-256 identifies bytes. Code and test-execution evidence must identify
`subject_id` and `subject_version`; test evidence must include an outcome.
Entity/relationship `evidence` lists pin `{ "id": "<Evidence UUID>", "version": 1 }`.

Errors: 422 for invalid schema/semantics, 404 for missing identity/revision, 409
for stale writes, duplicates, cycles, or invalid lifecycle transitions; 503 if the
database cannot be reached. Archive/retire instead of deleting.

## Verify

```powershell
$env:TEST_DATABASE_URL = $env:DATABASE_URL
python -m pytest -q
```

Integration tests use real PostgreSQL, create a randomly named isolated schema,
and drop only that schema afterward. Use a disposable development database and a
role permitted to create schemas. Without `TEST_DATABASE_URL`, integration tests
are explicitly skipped and only pure model/semantic tests run.

Coverage includes all entity/relationship kinds, provenance and typed attributes,
revision/evidence pinning, optimistic concurrency, history, project boundaries,
cycle prevention (including concurrent edges), verification evidence, rollback,
immutable database records, migration replay, and API search/filtering.

See [design and policy decisions](docs/phase1-design.md) for relationship directions,
lifecycle rules, assumptions, extension points, and decisions for project owners.
