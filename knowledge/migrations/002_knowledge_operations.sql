-- 第二阶段：来源快照、稳定键映射、不可变变更提案和审核事件。
-- 不改写第一阶段的知识修订，也不为组织预设审核权限。
CREATE TABLE knowledge_source (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES entity(id),
    uri text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id, uri),
    UNIQUE (id, project_id)
);

CREATE TABLE source_revision (
    source_id uuid NOT NULL REFERENCES knowledge_source(id),
    version integer NOT NULL CHECK (version > 0),
    external_revision text NOT NULL,
    format text NOT NULL CHECK (format IN ('json','markdown')),
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    content text NOT NULL,
    actor text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (source_id, version),
    UNIQUE (source_id, external_revision)
);

CREATE TABLE change_proposal (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES entity(id),
    source_id uuid,
    source_version integer,
    title text NOT NULL,
    actor text NOT NULL,
    plan jsonb NOT NULL CHECK (jsonb_typeof(plan) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source_id, source_version),
    FOREIGN KEY (source_id, project_id) REFERENCES knowledge_source(id, project_id),
    FOREIGN KEY (source_id, source_version) REFERENCES source_revision(source_id, version),
    CHECK ((source_id IS NULL) = (source_version IS NULL))
);
CREATE INDEX proposal_project ON change_proposal(project_id, created_at, id);

CREATE TABLE proposal_event (
    proposal_id uuid NOT NULL REFERENCES change_proposal(id),
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL CHECK (status IN ('pending','validated','committed','rejected')),
    action text NOT NULL CHECK (action IN ('created','validated','committed','rejected')),
    actor text NOT NULL,
    reason text NOT NULL,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (proposal_id, version)
);

CREATE TABLE source_entity_binding (
    source_id uuid NOT NULL,
    project_id uuid NOT NULL,
    external_key text NOT NULL,
    entity_id uuid NOT NULL,
    PRIMARY KEY (source_id, external_key),
    UNIQUE (source_id, entity_id),
    FOREIGN KEY (source_id, project_id) REFERENCES knowledge_source(id, project_id),
    FOREIGN KEY (entity_id, project_id) REFERENCES entity(id, project_id)
);

CREATE VIEW current_proposals AS
SELECT p.*, e.version, e.status, e.action, e.result, e.actor AS review_actor,
       e.reason AS review_reason, e.recorded_at
FROM change_proposal p JOIN (
    SELECT DISTINCT ON (proposal_id) * FROM proposal_event
    ORDER BY proposal_id, version DESC
) e ON p.id=e.proposal_id;

CREATE TRIGGER immutable_knowledge_source BEFORE UPDATE OR DELETE ON knowledge_source
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_source_revision BEFORE UPDATE OR DELETE ON source_revision
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_change_proposal BEFORE UPDATE OR DELETE ON change_proposal
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_proposal_event BEFORE UPDATE OR DELETE ON proposal_event
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_source_entity_binding BEFORE UPDATE OR DELETE ON source_entity_binding
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
