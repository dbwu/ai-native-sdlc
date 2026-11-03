# Phase 1 design and decisions

## Scope and structure

This implements the Engineering Knowledge Model described in the referenced
“AI原生SDLC知识驱动” conversation: a unified knowledge metamodel and query/write
API, before ingestion, inference, impact analysis, Agents, and orchestration.

- `knowledge/models.py`: typed API inputs, kind-specific attributes, provenance.
- `knowledge/semantics.py`: metamodel v1 relationship and lifecycle policy.
- `knowledge/service.py`: PostgreSQL transaction service; HTTP is a thin adapter,
  not the owner of semantics.
- `knowledge/migrations/`: ordered, checksummed SQL migrations packaged with the app.
- `knowledge/api.py`: REST, OpenAPI docs, bounded queries, validation errors.

PostgreSQL stores a graph as relational identities and append-only revisions.
Common revision content and typed attributes use JSONB; identity, scope, versions,
endpoints, lifecycle state, timestamps, and evidence references are structured.
This avoids twelve nearly identical persistence implementations while preserving
typed domain attributes in the service. No independent graph/vector database,
object storage, external source connector, or event infrastructure is needed here.

## Identity, revisions, and provenance

Server-generated UUIDv4 IDs are globally unique and independent of names/paths.
A Project scopes itself (`project_id = id`); every other entity belongs to exactly
one existing Project. Cross-project relationships and evidence are rejected.

Entity and relationship IDs are immutable. Integer content revisions start at 1;
all changes, including lifecycle changes, append a full snapshot. Evidence links
and endpoint applicability reference exact revisions. Database triggers reject
UPDATE/DELETE on knowledge tables. `recorded_at` is server time; `created_at` is
identity creation time. Metamodel version 1 is stored separately from content
versions; schema migrations have their own names/checksums. There is no global
point-in-time graph snapshot or backdated validity interval in this MVP.

Provenance records actor, absolute source locator, external source revision, change
reason, nature, and optional confidence on every revision. These are **caller
assertions**, not independently authenticated claims. Evidence payloads point at
external artifacts; original bytes remain in their authoritative source system.
No link or URI is executed/fetched. Optional digests are recorded, not verified.

Project-level transaction advisory locks protect read/validate/write sequences,
including cycle checks and revision assignment. `expected_version` detects stale
updates (409). Writes within a project serialize; projects can write independently.
This favors correctness over throughput for the MVP. Callers may retry after a
conflict only after reading and reconciling current content.

## Relationship semantics (v1 defaults)

Canonical storage uses the direction shown below. All relations prohibit self
links; cardinality is many-to-many. Only one stable relationship identity exists
per `(source_id, type, target_id)`, including retired/rejected identities. Revise
that identity instead of creating a duplicate. There is no separate uniqueness
policy on external project keys, names, or aliases.

“Engineering” means all kinds except Project/Evidence; “technical” means
ArchitectureComponent, Interface, DataModel, CodeArtifact, TestCase.

| Type | Source → target | Additional validation |
|---|---|---|
| CONTAINS | Project/Domain → any non-Project; ArchitectureComponent → technical | No cycles across live edges |
| DERIVED_FROM | Engineering → any non-Project | Rationale required, including derivation basis |
| DEPENDS_ON | Engineering → Engineering | Cycles allowed; mutual dependencies may be legitimate |
| IMPLEMENTED_BY | Requirement/BusinessRule/ArchitectureComponent/Interface/DataModel → ArchitectureComponent/CodeArtifact | Verified link needs matching code evidence |
| VERIFIED_BY | Engineering except TestCase → TestCase | Verified link needs matching **passed** execution evidence |
| CONSTRAINED_BY | Engineering → Constraint/BusinessRule/Decision | Does not evaluate expressions |
| IMPACTS | Engineering → Engineering | Explicitly asserted impact; no computed impact analysis |
| SUPERSEDES | Non-Project → same kind | New entity replaces old entity; no cycles |
| CONFLICTS_WITH | Engineering ↔ Engineering | Symmetric: lower UUID stored as source, paired versions swapped accordingly |

Containment/replacement cycle checks include proposed and verified edges; rejected
and retired edges are excluded. Reactivation rechecks the graph. A replacement
link does not automatically deprecate the replaced entity. Multiple containers
and multiple implementations are permitted. Rationale is required for every link.

Every **verified** link requires fact/human-confirmed provenance and at least one
active, fact/human-confirmed Evidence revision. `IMPLEMENTED_BY` additionally
requires Evidence category `code` whose subject is the exact target ID/version;
`VERIFIED_BY` requires category `test_execution`, same subject pin, outcome
`passed`. Code evidence subjects are CodeArtifact/ArchitectureComponent; execution
evidence subjects are TestCase. Proposed links may carry no evidence. These rules
establish traceability, not independent proof of artifact truth or test coverage.

Live links cannot pin an archived endpoint revision. Later endpoint/evidence
changes do not rewrite or automatically invalidate earlier links: a verified link
still applies only to its recorded endpoint/evidence versions. Queries return
these explicit pins, never imply verification of a newer entity revision. Detecting
stale evidence and revalidating links belongs to later knowledge operations.

## Lifecycle defaults

Entities:

```text
draft → active | archived
active → deprecated | archived
deprecated → active | archived
archived → terminal
```

Relationships:

```text
proposed → verified | rejected | retired
verified → proposed | retired
rejected → proposed | retired
retired → terminal
```

Same-state content updates are allowed except terminal states. Verified updates
revalidate all evidence; to remove evidence or move to unsupported endpoint versions,
submit the revised content with status `proposed`. New entities may start in
any entity state, and new relationships in any relationship state, provided all
applicable rules pass. Creating a verified link does not require a prior proposal
revision; no approval workflow is implied.

Archiving a Project freezes new writes throughout its scope while preserving
reads/history. Archived entities and retired links remain available historically;
there is no hard delete API or automatic propagation to related objects.

## Assumptions and decisions for the project owner

These are reversible implementation defaults, not claims about your organization:

1. **Stack:** Python/FastAPI/Pydantic + psycopg + PostgreSQL, because the folder has
   no established application stack. No source repositories are inferred or imported.
2. **Semantics:** The matrix above, same-project scoping, lifecycle vocabulary,
   many-to-many containment, and evidence thresholds are initial metamodel policy.
   Review them before real engineering knowledge is imported. No product-specific
   rule or acceptance criterion is invented; the demo is clearly synthetic.
3. **Approval/authentication — owner decision required before shared use:** who may
   confirm knowledge or verify links, trusted source identities, and project access
   control are unspecified. The MVP runs locally without authentication; an `actor`
   value is attribution only. No approval authority has been invented.
4. **Governance — owner decision required before production data:** retention,
   personal/confidential data handling, project tenancy, identifier interoperability,
   deployment/backup policy, and artifact storage authority remain open. No external
   writes, deployment, or integration configuration has been performed.

Semantic validation must go through the service. Foreign keys, uniqueness, endpoint
revision pins, scope consistency for links, and append-only records are enforced
by PostgreSQL too; the database does not duplicate the full Python type/evidence
policy. A direct SQL writer can bypass application semantics. For shared deployment,
use separate migration/service roles and prohibit other writers; authentication and
authorization need an explicit owner-approved design first.

## Extension and migration path

Use `attributes.extensions` for experimental domain fields. Promote stable fields
into a kind's Pydantic model with compatibility tests. Add new kinds/relations by
updating the schema via a **new** SQL migration, model registry, policy, metamodel
version strategy, tests, and documentation together. Never modify an applied SQL
migration: checksums detect drift. Existing revision snapshots must retain their
meaning; a future incompatible metamodel needs explicit readers/migration policy.
Current history is enough for audit of content, not a general event bus.

The intentionally deferred phases include source ingestion/extraction, proposals
with approval routing, automated consistency checks, computed impact analysis,
vector search, context packages, source-code analysis, Agents, and orchestration.
