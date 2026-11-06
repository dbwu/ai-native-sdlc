-- 工程工作区：稳定身份、不可变文件快照和隔离执行事件。
CREATE TABLE engineering_workspace (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES entity(id),
    design_flow_id uuid REFERENCES design_flow(id),
    name text NOT NULL,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(id,project_id)
);
CREATE TABLE engineering_workspace_event (
    workspace_id uuid NOT NULL REFERENCES engineering_workspace(id),
    version integer NOT NULL CHECK(version>0),
    action text NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(workspace_id,version)
);
CREATE VIEW current_engineering_workspaces AS
SELECT w.*,e.version,e.action,e.reason,e.recorded_at
FROM engineering_workspace w JOIN (
    SELECT DISTINCT ON(workspace_id) * FROM engineering_workspace_event
    ORDER BY workspace_id,version DESC
) e ON e.workspace_id=w.id;
CREATE TABLE engineering_snapshot (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES engineering_workspace(id),
    version integer NOT NULL CHECK(version>0),
    workspace_version integer NOT NULL CHECK(workspace_version>0),
    sha256 text NOT NULL CHECK(length(sha256)=64),
    files jsonb NOT NULL CHECK(jsonb_typeof(files)='array'),
    actor text NOT NULL,
    reason text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(workspace_id,version),
    UNIQUE(id,workspace_id)
);
CREATE TABLE engineering_execution (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES engineering_workspace(id),
    snapshot_id uuid NOT NULL,
    snapshot_sha256 text NOT NULL,
    workspace_version integer NOT NULL,
    profile text NOT NULL CHECK(profile='python_unittest'),
    actor text NOT NULL,
    reason text NOT NULL,
    environment jsonb NOT NULL CHECK(jsonb_typeof(environment)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(snapshot_id,workspace_id) REFERENCES engineering_snapshot(id,workspace_id)
);
CREATE TABLE engineering_execution_event (
    execution_id uuid NOT NULL REFERENCES engineering_execution(id),
    version integer NOT NULL CHECK(version>0),
    status text NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','FAILED','TIMED_OUT','CANCELLED','STALE')),
    actor text NOT NULL,
    reason text NOT NULL,
    result jsonb NOT NULL CHECK(jsonb_typeof(result)='object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(execution_id,version)
);
CREATE VIEW current_engineering_executions AS
SELECT x.*,e.version,e.status,e.result,e.recorded_at
FROM engineering_execution x JOIN (
    SELECT DISTINCT ON(execution_id) * FROM engineering_execution_event
    ORDER BY execution_id,version DESC
) e ON e.execution_id=x.id;
CREATE TRIGGER immutable_engineering_workspace BEFORE UPDATE OR DELETE ON engineering_workspace
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_engineering_workspace_event BEFORE UPDATE OR DELETE ON engineering_workspace_event
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_engineering_snapshot BEFORE UPDATE OR DELETE ON engineering_snapshot
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_engineering_execution BEFORE UPDATE OR DELETE ON engineering_execution
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_engineering_execution_event BEFORE UPDATE OR DELETE ON engineering_execution_event
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
