-- 第四阶段：SDLC 流程、阶段任务关联、人工门禁与可信参考执行记录。
CREATE TABLE workflow_run (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES entity(id),
    title text NOT NULL,
    definition jsonb NOT NULL CHECK (jsonb_typeof(definition)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id,project_id)
);
CREATE TABLE workflow_event (
    workflow_id uuid NOT NULL REFERENCES workflow_run(id),
    version integer NOT NULL CHECK (version>0),
    status text NOT NULL CHECK (status IN ('DRAFT','RUNNING','AWAITING_GATE','PAUSED','BLOCKED','COMPLETED','CANCELLED')),
    action text NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    document jsonb NOT NULL CHECK (jsonb_typeof(document)='object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workflow_id,version)
);
CREATE VIEW current_workflows AS
SELECT w.*,e.version,e.status,e.action,e.actor,e.reason,e.document AS state,e.recorded_at
FROM workflow_run w JOIN (
    SELECT DISTINCT ON (workflow_id) * FROM workflow_event ORDER BY workflow_id,version DESC
) e ON e.workflow_id=w.id;

CREATE TABLE workflow_stage_task (
    workflow_id uuid NOT NULL,
    project_id uuid NOT NULL,
    stage integer NOT NULL CHECK (stage BETWEEN 0 AND 3),
    attempt integer NOT NULL CHECK (attempt>0),
    task_id uuid NOT NULL UNIQUE,
    PRIMARY KEY (workflow_id,stage,attempt),
    FOREIGN KEY (workflow_id,project_id) REFERENCES workflow_run(id,project_id),
    FOREIGN KEY (task_id,project_id) REFERENCES runtime_task(id,project_id)
);

CREATE TABLE workflow_execution (
    id uuid PRIMARY KEY,
    workflow_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid NOT NULL,
    code_id uuid NOT NULL,
    code_version integer NOT NULL,
    code_sha256 text NOT NULL,
    test_sha256 text NOT NULL,
    document jsonb NOT NULL CHECK (jsonb_typeof(document)='object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (workflow_id,project_id) REFERENCES workflow_run(id,project_id),
    FOREIGN KEY (task_id,project_id) REFERENCES runtime_task(id,project_id),
    FOREIGN KEY (code_id,code_version) REFERENCES entity_revision(entity_id,version)
);

CREATE TRIGGER immutable_workflow_run BEFORE UPDATE OR DELETE ON workflow_run
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_workflow_event BEFORE UPDATE OR DELETE ON workflow_event
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_workflow_stage_task BEFORE UPDATE OR DELETE ON workflow_stage_task
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_workflow_execution BEFORE UPDATE OR DELETE ON workflow_execution
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
