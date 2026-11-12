-- 普通文本和 Markdown 原文可登记为不可变快照，供逐条引用。
ALTER TABLE source_revision DROP CONSTRAINT source_revision_format_check;
ALTER TABLE source_revision ADD CONSTRAINT source_revision_format_check
    CHECK (format IN ('json', 'markdown', 'text'));
