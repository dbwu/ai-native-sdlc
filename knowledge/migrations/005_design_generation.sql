-- 需求设计里程碑：固定需求基线、只追加流程与模型任务，不替用户批准知识。
CREATE TABLE design_flow (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES entity(id),
    title text NOT NULL,
    definition jsonb NOT NULL CHECK (jsonb_typeof(definition)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id,project_id)
);
CREATE INDEX design_flow_project ON design_flow(project_id,created_at,id);
CREATE TABLE design_flow_event (
    flow_id uuid NOT NULL REFERENCES design_flow(id),
    version integer NOT NULL CHECK(version>0),
    status text NOT NULL CHECK(status IN ('DRAFT','READY','GENERATING','NEEDS_CLARIFICATION','REVIEW_PENDING','DESIGN_COMMITTED','COMPLETED','BLOCKED','CANCELLED')),
    action text NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    document jsonb NOT NULL CHECK(jsonb_typeof(document)='object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(flow_id,version)
);
CREATE VIEW current_design_flows AS
SELECT f.*,e.version,e.status,e.action,e.actor,e.reason,e.document AS state,e.recorded_at
FROM design_flow f JOIN (
    SELECT DISTINCT ON(flow_id) * FROM design_flow_event ORDER BY flow_id,version DESC
) e ON e.flow_id=f.id;

CREATE TABLE design_generation (
    id uuid PRIMARY KEY,
    flow_id uuid NOT NULL,
    project_id uuid NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    request jsonb NOT NULL CHECK(jsonb_typeof(request)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(flow_id,project_id) REFERENCES design_flow(id,project_id)
);
CREATE TABLE design_generation_event (
    job_id uuid NOT NULL REFERENCES design_generation(id),
    version integer NOT NULL CHECK(version>0),
    status text NOT NULL CHECK(status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED','STALE')),
    document jsonb NOT NULL CHECK(jsonb_typeof(document)='object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(job_id,version)
);
CREATE VIEW current_design_generations AS
SELECT j.*,e.version,e.status,e.document AS result,e.document->>'diagnostic' AS diagnostic,
       e.document->'trace' AS trace,e.recorded_at AS updated_at,
       CASE WHEN e.status NOT IN ('QUEUED','RUNNING') THEN e.recorded_at END AS finished_at
FROM design_generation j JOIN (
    SELECT DISTINCT ON(job_id) * FROM design_generation_event ORDER BY job_id,version DESC
) e ON e.job_id=j.id;
CREATE TRIGGER immutable_design_flow BEFORE UPDATE OR DELETE ON design_flow
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_design_flow_event BEFORE UPDATE OR DELETE ON design_flow_event
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_design_generation BEFORE UPDATE OR DELETE ON design_generation
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_design_generation_event BEFORE UPDATE OR DELETE ON design_generation_event
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
