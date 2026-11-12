-- 保留从已保存代码开始新一轮需求实现时的明确来源。
ALTER TABLE engineering_workspace ADD COLUMN source_snapshot_id uuid REFERENCES engineering_snapshot(id);

CREATE OR REPLACE VIEW current_engineering_workspaces AS
SELECT w.id,w.project_id,w.design_flow_id,w.name,w.actor,w.created_at,
       e.version,e.action,e.reason,e.recorded_at,w.language,w.source_snapshot_id
FROM engineering_workspace w JOIN (
    SELECT DISTINCT ON(workspace_id) * FROM engineering_workspace_event
    ORDER BY workspace_id,version DESC
) e ON e.workspace_id=w.id;
