"""外部执行方可实现的统一 Agent 契约与无网络的确定性演示适配器。"""
from typing import Protocol


class AgentAdapter(Protocol):
    def get_capabilities(self) -> list[str]: ...
    def get_context(self, package: dict) -> dict: ...
    def execute_task(self, task: dict, context: dict) -> dict: ...
    def propose_changes(self, task: dict, context: dict, result: dict) -> list[dict]: ...
    def report_result(self, task: dict, result: dict) -> dict: ...
    def request_review(self, reason: str) -> dict: ...


class DemonstrationAdapter:
    """仅产生明确标注的合成结果，用于验证运行时协议，不判断真实工程成果。"""

    def get_capabilities(self):
        return ["demo.refine", "demo.review"]

    def get_context(self, package):
        return package["document"]

    def execute_task(self, task, context):
        return {"summary": "确定性合成适配器已读取固定版本上下文；结果仅用于协议演示。",
                "knowledge_count": len(context["entities"]), "synthetic": True}

    def propose_changes(self, task, context, result):
        if task["required_capability"] != "demo.refine":
            return []
        requirement = next(item for item in context["entities"] if item["kind"] == "Requirement")
        data = {key: requirement[key] for key in ("name", "description", "status", "attributes", "evidence", "provenance")}
        data.update(expected_version=requirement["version"], description="通过 Agent 提案补充的说明（合成示例）。")
        return [{"op": "update_entity", "id": requirement["id"], "data": data}]

    def report_result(self, task, result):
        return {"outcome": "succeeded", "summary": result["summary"], "artifacts": result,
                "acceptance": [{"criterion_index": i, "passed": True} for i in range(len(task["definition"]["acceptance_criteria"]))]}

    def request_review(self, reason):
        return {"outcome": "review", "summary": reason, "acceptance": [], "artifacts": {}}
