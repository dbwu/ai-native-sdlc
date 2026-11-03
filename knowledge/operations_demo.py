"""演示第二阶段闭环；所有内容和审核署名均为本地合成示例。"""
import argparse
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def seed(base_url: str):
    def call(path, payload=None):
        request = Request(base_url.rstrip("/") + path,
                          data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="GET" if payload is None else "POST")
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"第二阶段演示请求失败：{exc.code} {exc.read().decode('utf-8')}") from exc

    provenance = {"actor": "合成示例作者", "source_uri": "urn:demo:phase2", "source_revision": "demo-v1",
                  "change_reason": "建立第二阶段合成演示项目", "nature": "human_confirmed"}
    project = call("/entities", {"kind": "Project", "name": "第二阶段知识操作演示（合成示例）",
                                "status": "active", "provenance": provenance})
    document = {"entities": [
        {"key": "cancel-rule", "kind": "BusinessRule", "name": "支付前允许取消（合成示例）"},
        {"key": "cancel-requirement", "kind": "Requirement", "name": "支持订单取消（合成示例）",
         "attributes": {"acceptance_criteria": ["未支付订单允许取消"]}},
    ], "relationships": [{"type": "DERIVED_FROM", "source_key": "cancel-requirement", "target_key": "cancel-rule",
                          "rationale": "示例需求由支付前取消规则派生"}]}

    def ingest(revision):
        return call("/operations/ingestions", {
            "project_id": project["id"], "source_uri": "urn:demo:phase2:source", "source_revision": revision,
            "format": "json", "content": json.dumps(document, ensure_ascii=False), "actor": "合成示例采集者",
        })["proposal"]

    def confirm(proposal):
        checked = call(f"/operations/proposals/{proposal['id']}/validate", {
            "expected_version": proposal["version"], "actor": "合成示例校验者", "reason": "试运行演示变更",
        })["proposal"]
        if not checked["result"]["valid"]:
            raise RuntimeError("演示提案校验失败：" + json.dumps(checked["result"], ensure_ascii=False))
        return call(f"/operations/proposals/{proposal['id']}/commit", {
            "expected_version": checked["version"], "actor": "合成示例确认者", "reason": "显式确认写入合成演示知识",
        })["proposal"]

    initial = confirm(ingest("demo-v1"))
    document["entities"][1]["name"] = "支持支付前取消订单（修改后的合成示例）"
    updated = confirm(ingest("demo-v2"))
    mapping = {item["key"]: item for item in updated["plan"]["entity_mapping"]}
    requirement_id = mapping["cancel-requirement"]["entity_id"]
    requirement = call(f"/entities/{requirement_id}")
    data = {field: requirement[field] for field in ("name", "description", "attributes", "evidence")}
    data.update(status="active", provenance={**provenance, "change_reason": "显式将演示需求置为生效状态"},
                expected_version=requirement["version"])
    lifecycle = call("/operations/proposals", {"project_id": project["id"], "title": "演示需求生命周期修订",
        "actor": "合成示例作者", "changes": [{"op": "update_entity", "id": requirement_id, "data": data}]})
    confirm(lifecycle)
    impact = call("/operations/impact?" + urlencode({"project_id": project["id"], "entity_id": mapping["cancel-rule"]["entity_id"]}))
    consistency = call("/operations/consistency?" + urlencode({"project_id": project["id"]}))
    return {"project_id": project["id"], "initial_proposal_id": initial["id"], "updated_proposal_id": updated["id"],
            "requirement_id": requirement_id, "potential_impact_count": len(impact["affected"]),
            "finding_codes": sorted({item["code"] for item in consistency["findings"]}),
            "note": "全部内容为合成示例。流程演示采集、身份复用、变更检测、校验、显式提交、生命周期修订和只读检查。"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, add_help=False, usage="%(prog)s [-h] [--url 服务地址]")
    options = parser.add_argument_group("参数")
    options.add_argument("-h", "--help", action="help", help="显示帮助信息并退出")
    options.add_argument("--url", default="http://127.0.0.1:8000", metavar="服务地址", help="本地知识 API 服务地址")
    print(json.dumps(seed(parser.parse_args().url), ensure_ascii=False, indent=2))
