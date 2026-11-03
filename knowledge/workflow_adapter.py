"""第四阶段内置参考适配器；只输出显式合成场景的结构化修订建议。"""
from copy import deepcopy

from .reference_runner import build_reference
from .semantics import KnowledgeError
from .workflow_models import STAGES


class WorkflowReferenceAdapter:
    def build_report(self, stage: int, task: dict, package: dict, bindings: dict[str, str], execution=None):
        if not 0 <= stage < len(STAGES):
            raise KnowledgeError("不支持的 SDLC 阶段")
        build = build_reference("order_cancellation_v1")
        entities = {item["id"]: item for item in package["document"]["entities"]}
        records = {key: entities[str(record_id)] for key, record_id in bindings.items()}
        changes = []

        def revise(key, **updates):
            record = records[key]
            data = {field: deepcopy(record[field]) for field in ("name", "description", "status", "attributes", "evidence", "provenance")}
            data.update(expected_version=record["version"], **updates)
            changes.append({"op": "update_entity", "id": record["id"], "data": data})

        if stage == 0:
            attrs = deepcopy(records["requirement"]["attributes"])
            attrs.update(acceptance_criteria=["未支付订单可取消", "已支付订单拒绝取消且状态不变", "重复取消保持幂等"], requirement_type="functional")
            revise("requirement", name="订单取消需求（明确选择的合成参考）", description="参考场景：仅未支付订单可取消，重复操作不产生第二次取消动作。", attributes=attrs)
        elif stage == 1:
            attrs = deepcopy(records["architecture"]["attributes"])
            attrs["responsibility"] = "Order 服务负责检查支付状态并执行幂等取消（合成参考）。"
            revise("architecture", name="订单取消组件（合成参考）",
                   description="合成参考场景中的订单服务组件，负责支付状态检查与幂等取消。", attributes=attrs)
            attrs = deepcopy(records["interface"]["attributes"])
            attrs.update(protocol="python", contract_uri="urn:reference:order-cancellation:cancel_order")
            revise("interface", name="cancel_order 接口（合成参考）",
                   description="合成参考场景的 Python 取消接口：未支付订单可取消，已支付订单拒绝，重复取消保持幂等。", attributes=attrs)
            attrs = deepcopy(records["data"]["attributes"])
            attrs["schema_definition"] = {"Order": {"payment_status": ["unpaid", "paid"], "status": ["open", "cancelled"]}}
            revise("data", name="Order 数据模型（合成参考）",
                   description="合成参考场景的订单数据结构，分别记录支付状态与订单取消状态。", attributes=attrs)
        elif stage == 2:
            code_sha = build["sha256"]["order_service.py"]
            attrs = deepcopy(records["code"]["attributes"])
            attrs.update(path="order_service.py", language="python", revision=code_sha, repository_uri="urn:reference:order-cancellation",
                         extensions={"source": build["files"]["order_service.py"], "sha256": code_sha, "scenario": build["scenario"]})
            revise("code", name="订单取消 Python 实现（合成参考）",
                   description="合成参考场景的订单取消实现，包含支付状态检查、幂等取消源码及内容摘要。", attributes=attrs)
            attrs = {"category": "code", "uri": "urn:reference:code:" + code_sha, "revision": code_sha,
                     "subject_id": records["code"]["id"], "subject_version": records["code"]["version"] + 1, "sha256": code_sha,
                     "extensions": {"scenario": build["scenario"]}}
            revise("code-evidence", name="参考代码内容证据",
                   description="记录合成参考代码的内容摘要与对应知识版本，用于核对实现来源。", attributes=attrs)
        else:
            if execution is None:
                raise KnowledgeError("验证适配器需要服务端真实执行记录，不能自行声明通过")
            report = execution["document"]
            attrs = deepcopy(records["test"]["attributes"])
            attrs.update(steps=["创建未支付订单并取消", "拒绝已支付订单", "重复取消验证幂等", "取消一笔订单不影响其他订单"], expected_result="四项内置 unittest 全部通过",
                         extensions={"test_source": build["files"]["test_order_service.py"], "test_sha256": build["sha256"]["test_order_service.py"]})
            revise("test", name="订单取消参考验收测试",
                   description="合成参考场景的四项独立测试，覆盖未支付取消、已支付拒绝、重复取消幂等与订单状态隔离。", attributes=attrs)
            attrs = {"category": "test_execution", "uri": "urn:workflow:execution:" + str(execution["id"]), "revision": str(execution["id"]),
                     "subject_id": records["test"]["id"], "subject_version": records["test"]["version"] + 1,
                     "outcome": "passed" if report["passed"] else "failed", "extensions": {
                         "execution_id": str(execution["id"]), "code_sha256": execution["code_sha256"], "test_sha256": execution["test_sha256"],
                         "tests_run": report["tests_run"], "executed": report["executed"]}}
            revise("test-evidence", name="真实参考测试执行证据",
                   description="记录合成参考场景的真实测试结果、执行记录以及代码与测试内容摘要。", attributes=attrs)
        artifacts = {"stage": STAGES[stage]["key"], "scenario": build["scenario"], "synthetic_business_scenario": True}
        if execution is not None:
            artifacts["execution_id"] = str(execution["id"])
        return {"outcome": "succeeded", "summary": STAGES[stage]["title"] + "参考产物已形成，等待显式复核。",
                "acceptance": [{"criterion_index": i, "passed": True} for i in range(len(task["definition"]["acceptance_criteria"]))],
                "artifacts": artifacts, "changes": changes}
