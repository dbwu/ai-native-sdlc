-- M1 文档语义抽取。原文、模型任务、审核提案与稳定绑定分别留档。
CREATE TABLE extraction_job (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES entity(id),
    source_id uuid NOT NULL,
    source_version integer NOT NULL,
    actor text NOT NULL,
    request jsonb NOT NULL CHECK(jsonb_typeof(request)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(source_id,project_id) REFERENCES knowledge_source(id,project_id),
    FOREIGN KEY(source_id,source_version) REFERENCES source_revision(source_id,version)
);
CREATE INDEX extraction_job_project ON extraction_job(project_id,created_at,id);
CREATE TABLE extraction_job_event (
    job_id uuid NOT NULL REFERENCES extraction_job(id),
    version integer NOT NULL CHECK(version>0),
    status text NOT NULL CHECK(status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','STALE')),
    document jsonb NOT NULL CHECK(jsonb_typeof(document)='object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(job_id,version)
);
CREATE VIEW current_extraction_jobs AS
SELECT j.*,e.version,e.status,e.document AS result,e.recorded_at AS updated_at
FROM extraction_job j JOIN (
    SELECT DISTINCT ON(job_id) * FROM extraction_job_event ORDER BY job_id,version DESC
) e ON e.job_id=j.id;
CREATE TABLE extraction_proposal (
    job_id uuid NOT NULL REFERENCES extraction_job(id),
    proposal_id uuid NOT NULL UNIQUE REFERENCES change_proposal(id),
    PRIMARY KEY(job_id,proposal_id)
);
ALTER TABLE relationship ADD CONSTRAINT relationship_id_project_unique UNIQUE(id,project_id);
CREATE TABLE source_relationship_binding (
    source_id uuid NOT NULL,
    project_id uuid NOT NULL,
    external_key text NOT NULL,
    relationship_id uuid NOT NULL,
    PRIMARY KEY(source_id,external_key),
    UNIQUE(source_id,relationship_id),
    FOREIGN KEY(source_id,project_id) REFERENCES knowledge_source(id,project_id),
    FOREIGN KEY(relationship_id,project_id) REFERENCES relationship(id,project_id)
);
CREATE TRIGGER immutable_extraction_job BEFORE UPDATE OR DELETE ON extraction_job
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_extraction_job_event BEFORE UPDATE OR DELETE ON extraction_job_event
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_extraction_proposal BEFORE UPDATE OR DELETE ON extraction_proposal
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
CREATE TRIGGER immutable_source_relationship_binding BEFORE UPDATE OR DELETE ON source_relationship_binding
FOR EACH ROW EXECUTE FUNCTION reject_knowledge_mutation();
