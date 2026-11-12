-- 项目级架构与业务流程使用经提案确认的不可变版本。
CREATE TABLE project_model_revision (
    project_id uuid NOT NULL REFERENCES entity(id),
    version integer NOT NULL CHECK (version > 0),
    proposal_id uuid NOT NULL UNIQUE REFERENCES change_proposal(id),
    document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, version)
);

CREATE TRIGGER immutable_project_model_revision BEFORE UPDATE OR DELETE ON project_model_revision
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
