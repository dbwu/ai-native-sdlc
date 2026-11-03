"""通过真实 HTTP 演示本地 Agent 协作；全部产物与审核署名均为合成示例。"""
import argparse
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Kind
from .runtime_contract import DemonstrationAdapter


def seed(base_url: str):
    def call(path, payload=None):
        request = Request(base_url.rstrip("/") + path, data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode(),
                          headers={"Content-Type": "application/json"}, method="GET" if payload is None else "POST")
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"协作演示请求失败：{exc.code} {exc.read().decode()}") from exc

    provenance = {"actor": "合成演示创建者", "source_uri": "urn:demo:phase3", "source_revision": "demo-v1",
                  "change_reason": "建立第三阶段协议演示数据", "nature": "human_confirmed"}
    project = call("/entities", {"kind": "Project", "name": "Agent 协作演示（合成示例）", "status": "active", "provenance": provenance})
    requirement = call("/entities", {"kind": "Requirement", "project_id": project["id"], "name": "待补充说明的示例需求",
                                      "status": "active", "provenance": provenance})
    code = call("/entities", {"kind": "CodeArtifact", "project_id": project["id"], "name": "仅用于上下文的合成代码资产",
                               "status": "active", "provenance": provenance})
    scopes = {"Product": ["Requirement"], "Architect": ["ArchitectureComponent", "Interface", "DataModel", "Decision", "Constraint"],
              "Developer": ["CodeArtifact"], "Test": ["TestCase", "Evidence"],
              "Curator": [kind.value for kind in Kind if kind != Kind.PROJECT], "Orchestrator": []}
    agents = {}
    for role, write_kinds in scopes.items():
        capability = {"Product": "demo.refine", "Test": "demo.review"}.get(role, "demo." + role.lower())
        agents[role] = call("/runtime/agents", {"project_id": project["id"], "name": "合成示例-" + role,
            "role": role, "capabilities": [capability], "read_kinds": [kind.value for kind in Kind],
            "write_kinds": write_kinds, "actor": "合成演示注册者"})

    def task(capability, title, inputs, dependencies=None):
        return call("/runtime/tasks", {"project_id": project["id"], "title": title, "goal": "验证协作协议（不代表真实研发活动）",
            "required_capability": capability, "acceptance_criteria": ["产物明确标记为合成示例"],
            "input_knowledge": [{"id": item["id"], "version": item["version"]} for item in inputs],
            "dependencies": dependencies or [], "actor": "合成编排者"})

    def control(task, action):
        return call(f"/runtime/tasks/{task['id']}/{action}", {"expected_version": task["version"],
                    "actor": "合成人工确认者", "reason": "审阅合成演示产物并显式执行状态操作"})

    first = control(task("demo.refine", "产品 Agent 提出知识修订", [requirement]), "ready")
    second = control(task("demo.review", "测试 Agent 在前置任务完成后报告产物", [code], [first["id"]]), "ready")
    assert call("/runtime/claim", {"agent_id": agents["Test"]["id"], "task_id": second["id"]})["task"] is None
    adapter = DemonstrationAdapter()

    def execute(registered_agent, queued_task):
        lease = call("/runtime/claim", {"agent_id": registered_agent["id"], "task_id": queued_task["id"]})
        running = lease["task"]
        assert running and running["status"] == "RUNNING"
        package = call(f"/runtime/tasks/{running['id']}/context")
        context = adapter.get_context(package)
        assert running["required_capability"] in adapter.get_capabilities()
        result = adapter.execute_task(running, context)
        report = adapter.report_result(running, result)
        report.update(agent_id=registered_agent["id"], lease_token=lease["lease_token"], expected_version=running["version"],
                      changes=adapter.propose_changes(running, context, result))
        proposed = call(f"/runtime/tasks/{running['id']}/report", report)
        validating = control(proposed, "validate")
        assert validating["status"] == "VALIDATING"
        if validating["runtime"]["proposal_id"]:
            proposal = call("/operations/proposals/" + validating["runtime"]["proposal_id"])
            assert proposal["status"] == "validated"
            call(f"/operations/proposals/{proposal['id']}/commit", {"expected_version": proposal["version"],
                 "actor": "合成人工确认者", "reason": "明确批准该合成知识提案，未调用真实外部系统"})
        return control(validating, "complete")

    completed_first = execute(agents["Product"], first)
    completed_second = execute(agents["Test"], second)
    latest = call("/entities/" + requirement["id"])
    events = call("/runtime/events?" + urlencode({"project_id": project["id"], "limit": 200}))
    return {"project_id": project["id"], "registered_roles": list(agents),
            "tasks": [{"id": completed_first["id"], "status": completed_first["status"]},
                      {"id": completed_second["id"], "status": completed_second["status"]}],
            "requirement_version": latest["version"], "requirement_nature": latest["provenance"]["nature"],
            "event_count": len(events["events"]), "next_cursor": events["next_cursor"],
            "note": "Agent 适配器、产物和审核均为合成演示；未调用外部模型或执行源码、测试脚本。"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, add_help=False, usage="%(prog)s [-h] [--url 服务地址]")
    options = parser.add_argument_group("参数")
    options.add_argument("-h", "--help", action="help", help="显示帮助信息并退出")
    options.add_argument("--url", default="http://127.0.0.1:8000", metavar="服务地址", help="本地知识 API 服务地址")
    print(json.dumps(seed(parser.parse_args().url), ensure_ascii=False, indent=2))
