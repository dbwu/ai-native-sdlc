-- 扩展固定隔离执行方式和阶段结果，保留已有 Python 执行记录。
ALTER TABLE engineering_execution DROP CONSTRAINT engineering_execution_profile_check,
    ADD CONSTRAINT engineering_execution_profile_check
    CHECK (profile IN ('python_unittest','c_native','cpp_native','java_native'));

ALTER TABLE engineering_execution_event DROP CONSTRAINT engineering_execution_event_status_check,
    ADD CONSTRAINT engineering_execution_event_status_check
    CHECK (status IN ('RUNNING','SUCCEEDED','FAILED','COMPILE_FAILED','TEST_FAILED','TIMED_OUT','CANCELLED','STALE'));
