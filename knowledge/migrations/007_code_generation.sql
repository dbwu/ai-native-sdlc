-- 模型代码候选和审阅记录独立于已保存的工程快照。
CREATE TABLE engineering_code_generation (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES engineering_workspace(id),
    base_snapshot_id uuid,
    design_flow_id uuid NOT NULL REFERENCES design_flow(id),
    design_version integer NOT NULL CHECK (design_version > 0),
    actor text NOT NULL,
    reason text NOT NULL,
    request jsonb NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(id, workspace_id),
    FOREIGN KEY (base_snapshot_id, workspace_id) REFERENCES engineering_snapshot(id, workspace_id)
);
CREATE INDEX engineering_code_generation_workspace ON engineering_code_generation(workspace_id, created_at DESC, id);

CREATE TABLE engineering_code_generation_event (
    generation_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL CHECK (status IN ('QUEUED','RUNNING','REVIEW_PENDING','APPLIED','FAILED','STALE')),
    applied_snapshot_id uuid,
    deadline_at timestamptz,
    actor text NOT NULL,
    reason text NOT NULL,
    result jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (generation_id, version),
    FOREIGN KEY (generation_id, workspace_id) REFERENCES engineering_code_generation(id, workspace_id),
    FOREIGN KEY (applied_snapshot_id, workspace_id) REFERENCES engineering_snapshot(id, workspace_id),
    CHECK ((status = 'RUNNING') = (deadline_at IS NOT NULL)),
    CHECK ((status = 'APPLIED') = (applied_snapshot_id IS NOT NULL))
);
CREATE VIEW current_engineering_code_generations AS
SELECT g.*, e.version, e.status, e.applied_snapshot_id, e.deadline_at, e.result, e.recorded_at AS updated_at
FROM engineering_code_generation g JOIN (
    SELECT DISTINCT ON (generation_id) * FROM engineering_code_generation_event ORDER BY generation_id, version DESC
) e ON e.generation_id = g.id;

CREATE TRIGGER immutable_engineering_code_generation BEFORE UPDATE OR DELETE ON engineering_code_generation
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_engineering_code_generation_event BEFORE UPDATE OR DELETE ON engineering_code_generation_event
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
