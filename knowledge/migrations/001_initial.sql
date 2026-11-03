CREATE TABLE entity (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN (
        'Project','Domain','Requirement','BusinessRule','ArchitectureComponent','Interface',
        'DataModel','CodeArtifact','TestCase','Decision','Constraint','Evidence')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, project_id),
    FOREIGN KEY (project_id) REFERENCES entity(id),
    CHECK ((kind = 'Project') = (id = project_id))
);
CREATE INDEX entity_project_kind ON entity(project_id, kind);

CREATE TABLE entity_revision (
    entity_id uuid NOT NULL REFERENCES entity(id),
    version integer NOT NULL CHECK (version > 0),
    metamodel_version integer NOT NULL DEFAULT 1 CHECK (metamodel_version = 1),
    status text NOT NULL CHECK (status IN ('draft','active','deprecated','archived')),
    document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (entity_id, version)
);

CREATE TABLE relationship (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES entity(id),
    type text NOT NULL CHECK (type IN ('CONTAINS','DERIVED_FROM','DEPENDS_ON',
        'IMPLEMENTED_BY','VERIFIED_BY','CONSTRAINED_BY','IMPACTS','SUPERSEDES','CONFLICTS_WITH')),
    source_id uuid NOT NULL,
    target_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (source_id, project_id) REFERENCES entity(id, project_id),
    FOREIGN KEY (target_id, project_id) REFERENCES entity(id, project_id),
    UNIQUE (source_id, type, target_id),
    UNIQUE (id, source_id, target_id),
    CHECK (source_id <> target_id),
    CHECK (type <> 'CONFLICTS_WITH' OR source_id < target_id)
);
CREATE INDEX relationship_target ON relationship(target_id, type);
CREATE INDEX relationship_project ON relationship(project_id, type);

CREATE TABLE relationship_revision (
    relationship_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    metamodel_version integer NOT NULL DEFAULT 1 CHECK (metamodel_version = 1),
    source_id uuid NOT NULL,
    target_id uuid NOT NULL,
    source_version integer NOT NULL,
    target_version integer NOT NULL,
    status text NOT NULL CHECK (status IN ('proposed','verified','rejected','retired')),
    document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (relationship_id, version),
    FOREIGN KEY (relationship_id, source_id, target_id) REFERENCES relationship(id, source_id, target_id),
    FOREIGN KEY (source_id, source_version) REFERENCES entity_revision(entity_id, version),
    FOREIGN KEY (target_id, target_version) REFERENCES entity_revision(entity_id, version)
);

CREATE TABLE entity_evidence (
    entity_id uuid NOT NULL,
    version integer NOT NULL,
    evidence_id uuid NOT NULL,
    evidence_version integer NOT NULL,
    PRIMARY KEY (entity_id, version, evidence_id, evidence_version),
    FOREIGN KEY (entity_id, version) REFERENCES entity_revision(entity_id, version),
    FOREIGN KEY (evidence_id, evidence_version) REFERENCES entity_revision(entity_id, version)
);
CREATE TABLE relationship_evidence (
    relationship_id uuid NOT NULL,
    version integer NOT NULL,
    evidence_id uuid NOT NULL,
    evidence_version integer NOT NULL,
    PRIMARY KEY (relationship_id, version, evidence_id, evidence_version),
    FOREIGN KEY (relationship_id, version) REFERENCES relationship_revision(relationship_id, version),
    FOREIGN KEY (evidence_id, evidence_version) REFERENCES entity_revision(entity_id, version)
);

CREATE VIEW current_entities AS
SELECT e.*, r.version, r.metamodel_version, r.status, r.document, r.recorded_at
FROM entity e JOIN (
    SELECT DISTINCT ON (entity_id) * FROM entity_revision ORDER BY entity_id, version DESC
) r ON e.id = r.entity_id;

CREATE VIEW current_relationships AS
SELECT e.*, r.version, r.metamodel_version, r.status, r.source_version, r.target_version,
       r.document, r.recorded_at
FROM relationship e JOIN (
    SELECT DISTINCT ON (relationship_id) * FROM relationship_revision
    ORDER BY relationship_id, version DESC
) r ON e.id = r.relationship_id;

CREATE FUNCTION reject_knowledge_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Knowledge records are append-only; create a new revision instead';
END;
$$;

CREATE TRIGGER immutable_entity BEFORE UPDATE OR DELETE ON entity
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_entity_revision BEFORE UPDATE OR DELETE ON entity_revision
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_relationship BEFORE UPDATE OR DELETE ON relationship
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_relationship_revision BEFORE UPDATE OR DELETE ON relationship_revision
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_entity_evidence BEFORE UPDATE OR DELETE ON entity_evidence
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_relationship_evidence BEFORE UPDATE OR DELETE ON relationship_evidence
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
