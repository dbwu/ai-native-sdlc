-- 第三阶段：本地 Agent 注册、任务队列、不可变上下文和协作事件。
CREATE TABLE runtime_agent (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES entity(id),
    name text NOT NULL,
    role text NOT NULL CHECK (role IN ('Product','Architect','Developer','Test','Curator','Orchestrator')),
    capabilities jsonb NOT NULL CHECK (jsonb_typeof(capabilities)='array'),
    read_kinds jsonb NOT NULL CHECK (jsonb_typeof(read_kinds)='array'),
    write_kinds jsonb NOT NULL CHECK (jsonb_typeof(write_kinds)='array'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id,name),
    UNIQUE (id,project_id)
);
CREATE TABLE runtime_agent_event (
    agent_id uuid NOT NULL REFERENCES runtime_agent(id),
    version integer NOT NULL CHECK (version>0),
    status text NOT NULL CHECK (status IN ('active','disabled')),
    actor text NOT NULL,
    reason text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (agent_id,version)
);
CREATE VIEW current_runtime_agents AS
SELECT a.*,e.version,e.status,e.actor,e.reason,e.recorded_at FROM runtime_agent a JOIN (
    SELECT DISTINCT ON (agent_id) * FROM runtime_agent_event ORDER BY agent_id,version DESC
) e ON e.agent_id=a.id;

CREATE TABLE runtime_task (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES entity(id),
    title text NOT NULL,
    required_capability text NOT NULL,
    document jsonb NOT NULL CHECK (jsonb_typeof(document)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id,project_id)
);
CREATE INDEX runtime_task_project ON runtime_task(project_id,created_at,id);

CREATE TABLE context_package (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    task_id uuid NOT NULL,
    document jsonb NOT NULL CHECK (jsonb_typeof(document)='object'),
    sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (task_id,project_id) REFERENCES runtime_task(id,project_id),
    UNIQUE (id,task_id)
);
CREATE TABLE runtime_task_event (
    event_id bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    task_id uuid NOT NULL REFERENCES runtime_task(id),
    version integer NOT NULL CHECK (version>0),
    status text NOT NULL CHECK (status IN ('CREATED','READY','RUNNING','PROPOSED','VALIDATING',
        'COMPLETED','FAILED','BLOCKED','NEEDS_HUMAN_REVIEW','CANCELLED')),
    action text NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    document jsonb NOT NULL CHECK (jsonb_typeof(document)='object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (task_id,version)
);
CREATE VIEW current_runtime_tasks AS
SELECT t.id,t.project_id,t.title,t.required_capability,t.document AS definition,t.created_at,
       e.version,e.status,e.action,e.actor,e.reason,e.document AS runtime,e.recorded_at,e.event_id
FROM runtime_task t JOIN (
    SELECT DISTINCT ON (task_id) * FROM runtime_task_event ORDER BY task_id,version DESC
) e ON e.task_id=t.id;

CREATE TRIGGER immutable_runtime_agent BEFORE UPDATE OR DELETE ON runtime_agent
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_runtime_agent_event BEFORE UPDATE OR DELETE ON runtime_agent_event
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_runtime_task BEFORE UPDATE OR DELETE ON runtime_task
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_runtime_task_event BEFORE UPDATE OR DELETE ON runtime_task_event
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_context_package BEFORE UPDATE OR DELETE ON context_package
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
