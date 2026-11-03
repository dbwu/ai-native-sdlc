"""通过 HTTP 手动写入合成示例知识；服务启动时不会自动执行。"""
import argparse
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def seed(base_url: str):
    provenance = {"actor": "synthetic-demo", "source_uri": "urn:demo:phase1", "source_revision": "demo-v1",
                  "change_reason": "创建明确标注为虚构的合成示例数据", "nature": "human_confirmed"}

    def post(path, payload):
        req = Request(base_url.rstrip("/") + path, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=10) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"接口拒绝写入示例数据：{exc.code} {exc.read().decode()}") from exc

    def entity(kind, name, attributes=None, project_id=None):
        body = {"kind": kind, "name": name, "attributes": attributes or {},
                "status": "active", "provenance": provenance}
        if project_id:
            body["project_id"] = project_id
        return post("/entities", body)

    project = entity("Project", "订单取消演示项目（合成示例）")
    records = {"Project": project}
    attributes = {
        "Requirement": {"acceptance_criteria": ["未支付订单可以取消"]},
        "BusinessRule": {"expression": {"field": "payment_status", "equals": "unpaid"}},
        "CodeArtifact": {"path": "synthetic/order_service.py", "revision": "demo-commit-1"},
        "TestCase": {"steps": ["创建未支付订单", "取消订单"], "expected_result": "订单已取消"},
    }
    labels = {
        "Domain": "领域", "Requirement": "需求", "BusinessRule": "业务规则",
        "ArchitectureComponent": "架构组件", "Interface": "接口", "DataModel": "数据模型",
        "CodeArtifact": "代码资产", "TestCase": "测试用例", "Decision": "决策", "Constraint": "约束",
    }
    for kind in ["Domain", "Requirement", "BusinessRule", "ArchitectureComponent", "Interface",
                 "DataModel", "CodeArtifact", "TestCase", "Decision", "Constraint"]:
        records[kind] = entity(kind, f"合成示例：{labels[kind]}", attributes.get(kind), project["id"])
    document = entity("Evidence", "设计依据（合成示例）", {
        "category": "document", "uri": "urn:demo:design", "revision": "demo-v1"}, project["id"])
    code_evidence = entity("Evidence", "代码证据（合成示例）", {
        "category": "code", "uri": "urn:demo:code", "revision": "demo-commit-1",
        "subject_id": records["CodeArtifact"]["id"], "subject_version": 1}, project["id"])
    test_evidence = entity("Evidence", "测试通过记录（合成示例，非真实测试执行结果）", {
        "category": "test_execution", "uri": "urn:demo:run", "revision": "demo-run-1",
        "subject_id": records["TestCase"]["id"], "subject_version": 1, "outcome": "passed"}, project["id"])
    previous = entity("Decision", "历史决策（合成示例）", project_id=project["id"])

    def link(type, source, target, evidence=None):
        return post("/relationships", {"type": type, "source_id": source["id"], "target_id": target["id"],
            "source_version": source["version"], "target_version": target["version"],
            "status": "verified" if evidence else "proposed", "rationale": "通过合成示例演示具有明确依据的知识追溯关系",
            "provenance": provenance, "evidence": [{"id": evidence["id"], "version": 1}] if evidence else []})

    links = [
        link("CONTAINS", project, records["Domain"]),
        link("DERIVED_FROM", records["Requirement"], records["BusinessRule"], document),
        link("IMPLEMENTED_BY", records["Requirement"], records["CodeArtifact"], code_evidence),
        link("VERIFIED_BY", records["Requirement"], records["TestCase"], test_evidence),
        link("DEPENDS_ON", records["ArchitectureComponent"], records["Interface"]),
        link("CONSTRAINED_BY", records["Interface"], records["Constraint"]),
        link("IMPACTS", records["Decision"], records["DataModel"]),
        link("SUPERSEDES", records["Decision"], previous),
        link("CONFLICTS_WITH", records["BusinessRule"], records["Constraint"]),
    ]
    return {"project_id": project["id"], "requirement_id": records["Requirement"]["id"],
            "relationship_ids": [r["id"] for r in links], "note": "所有示例数据与证据均为合成内容，不代表真实代码验证或测试执行结果。"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, add_help=False,
                                     usage="%(prog)s [-h] [--url 服务地址]")
    options = parser.add_argument_group("参数")
    options.add_argument("-h", "--help", action="help", help="显示帮助信息并退出")
    options.add_argument("--url", default="http://127.0.0.1:8000", metavar="服务地址",
                         help="知识接口服务地址（默认：http://127.0.0.1:8000）")
    args = parser.parse_args()
    print(json.dumps(seed(args.url), ensure_ascii=False, indent=2))
