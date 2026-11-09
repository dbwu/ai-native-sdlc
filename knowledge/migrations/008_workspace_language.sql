-- 固定工程工作区的代码生成语言，已有工作区沿用 Python。
ALTER TABLE engineering_workspace ADD COLUMN language text NOT NULL DEFAULT 'python'
    CHECK (language IN ('python', 'c', 'cpp', 'java'));

CREATE OR REPLACE VIEW current_engineering_workspaces AS
SELECT w.id,w.project_id,w.design_flow_id,w.name,w.actor,w.created_at,
       e.version,e.action,e.reason,e.recorded_at,w.language
FROM engineering_workspace w JOIN (
    SELECT DISTINCT ON(workspace_id) * FROM engineering_workspace_event
    ORDER BY workspace_id,version DESC
) e ON e.workspace_id=w.id;
