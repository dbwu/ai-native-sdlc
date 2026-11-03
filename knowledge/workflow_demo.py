"""通过 HTTP 演示需求→架构→代码→真实参考测试；业务场景为显式合成示例。"""
import argparse
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Kind
from .workflow_adapter import WorkflowReferenceAdapter
from .workflow_models import STAGES


def seed(base_url: str):
    def call(path, payload=None):
        req = Request(base_url.rstrip("/") + path, data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode(),
                      headers={"Content-Type": "application/json"}, method="GET" if payload is None else "POST")
        try:
            with urlopen(req, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"第四阶段演示请求失败：{exc.code} {exc.read().decode()}") from exc

    def approve(proposal_id):
        proposal = call("/operations/proposals/" + proposal_id)
        checked = call(f"/operations/proposals/{proposal_id}/validate", {"expected_version": proposal["version"],
                       "actor": "合成演示审核者", "reason": "显式审阅参考产物及其版本和证据"})["proposal"]
        if not checked["result"]["valid"]:
            raise RuntimeError("合成提案校验未通过：" + json.dumps(checked["result"], ensure_ascii=False))
        return call(f"/operations/proposals/{proposal_id}/commit", {"expected_version": checked["version"],
                    "actor": "合成演示审核者", "reason": "明确批准本地合成示例中的知识变更"})["proposal"]

    provenance = {"actor": "合成演示创建者", "source_uri": "urn:demo:phase4", "source_revision": "demo-v1",
                  "change_reason": "创建单独的第四阶段参考项目", "nature": "human_confirmed"}
    project = call("/entities", {"kind": "Project", "name": "四阶段 SDLC 闭环（合成参考）", "status": "active", "provenance": provenance})
    workflow = call("/workflows", {"project_id": project["id"], "title": "订单取消参考功能闭环",
                    "goal": "验证四阶段知识驱动流程，不推定任何真实项目已有业务要求。", "scenario": "order_cancellation_v1", "actor": "合成流程发起者"})
    workflow_id = workflow["id"]

    def flow(action):
        current = call("/workflows/" + workflow_id)
        return call(f"/workflows/{workflow_id}/actions/{action}", {"expected_version": current["version"],
                    "actor": "合成流程审核者", "reason": "依据参考产物、显式门禁和当前知识版本推进"})

    def task_control(task, action):
        return call(f"/runtime/tasks/{task['id']}/{action}", {"expected_version": task["version"],
                    "actor": "合成任务审核者", "reason": "审阅阶段产物并显式确认任务状态"})

    approve(workflow["state"]["bootstrap_proposal_id"])
    workflow = flow("start")
    adapter = WorkflowReferenceAdapter()
    write_scopes = [["Requirement"], ["ArchitectureComponent", "Interface", "DataModel"], ["CodeArtifact", "Evidence"], ["TestCase", "Evidence"]]
    execution_id = None
    for index, stage in enumerate(STAGES):
        agent = call("/runtime/agents", {"project_id": project["id"], "name": "参考-" + stage["role"], "role": stage["role"],
            "capabilities": [stage["capability"]], "read_kinds": [kind.value for kind in Kind], "write_kinds": write_scopes[index], "actor": "合成注册者"})
        lease = call("/runtime/claim", {"agent_id": agent["id"], "task_id": workflow["state"]["task_id"], "lease_seconds": 120})
        task = lease["task"]
        if task is None:
            raise RuntimeError("当前阶段未能领取任务，请检查上下文和依赖。")
        package = call(f"/runtime/tasks/{task['id']}/context")
        execution = None
        lease_fields = {"agent_id": agent["id"], "lease_token": lease["lease_token"], "expected_version": task["version"]}
        if index == 3:
            execution = call(f"/workflows/{workflow_id}/reference-executions", lease_fields)
            execution_id = execution["id"]
            if not execution["document"]["passed"]:
                raise RuntimeError("真实参考测试未通过，演示不会继续放行。")
        report = adapter.build_report(index, task, package, workflow["state"]["bindings"], execution)
        proposed = call(f"/runtime/tasks/{task['id']}/report", {**report, **lease_fields})
        validating = task_control(proposed, "validate")
        if validating["status"] != "VALIDATING":
            raise RuntimeError("任务校验失败：" + json.dumps(validating["runtime"].get("review"), ensure_ascii=False))
        approve(validating["runtime"]["proposal_id"])
        task_control(validating, "complete")
        workflow = flow("prepare_gate")
        if workflow["status"] != "AWAITING_GATE":
            raise RuntimeError("阶段门禁未通过：" + str(workflow["state"].get("diagnostic")))
        approve(workflow["state"]["gate_proposal_id"])
        workflow = flow("advance")
    executions = call(f"/workflows/{workflow_id}/executions")
    relationships = call("/relationships?" + urlencode({"project_id": project["id"], "status": "verified"}))
    return {"project_id": project["id"], "workflow_id": workflow_id, "status": workflow["status"],
            "completed_stages": len(workflow["state"]["completed_stages"]), "execution_id": execution_id,
            "tests_run": executions[-1]["document"]["tests_run"], "tests_passed": executions[-1]["document"]["passed"],
            "verified_trace_types": sorted(item["type"] for item in relationships),
            "code_sha256": executions[-1]["code_sha256"], "test_sha256": executions[-1]["test_sha256"],
            "note": "业务场景与署名为合成示例；Python 参考代码和四项 unittest 已实际执行。未修改业务仓库或执行任意上传代码。"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, add_help=False, usage="%(prog)s [-h] [--url 服务地址]")
    options = parser.add_argument_group("参数")
    options.add_argument("-h", "--help", action="help", help="显示帮助信息并退出")
    options.add_argument("--url", default="http://127.0.0.1:8000", metavar="服务地址", help="本地知识 API 服务地址")
    print(json.dumps(seed(parser.parse_args().url), ensure_ascii=False, indent=2))
