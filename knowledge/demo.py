"""Explicitly seed synthetic sample knowledge via HTTP (never runs on startup)."""
import argparse
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def seed(base_url: str):
    provenance = {"actor": "synthetic-demo", "source_uri": "urn:demo:phase1", "source_revision": "demo-v1",
                  "change_reason": "Create explicitly synthetic sample data", "nature": "human_confirmed"}

    def post(path, payload):
        req = Request(base_url.rstrip("/") + path, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=10) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"API rejected demo: {exc.code} {exc.read().decode()}") from exc

    def entity(kind, name, attributes=None, project_id=None):
        body = {"kind": kind, "name": name, "attributes": attributes or {},
                "status": "active", "provenance": provenance}
        if project_id:
            body["project_id"] = project_id
        return post("/entities", body)

    project = entity("Project", "Synthetic order cancellation demo")
    records = {"Project": project}
    attributes = {
        "Requirement": {"acceptance_criteria": ["An unpaid order can be cancelled"]},
        "BusinessRule": {"expression": {"field": "payment_status", "equals": "unpaid"}},
        "CodeArtifact": {"path": "synthetic/order_service.py", "revision": "demo-commit-1"},
        "TestCase": {"steps": ["Create unpaid order", "Cancel order"], "expected_result": "Order cancelled"},
    }
    for kind in ["Domain", "Requirement", "BusinessRule", "ArchitectureComponent", "Interface",
                 "DataModel", "CodeArtifact", "TestCase", "Decision", "Constraint"]:
        records[kind] = entity(kind, f"Synthetic {kind}", attributes.get(kind), project["id"])
    document = entity("Evidence", "Synthetic design reference", {
        "category": "document", "uri": "urn:demo:design", "revision": "demo-v1"}, project["id"])
    code_evidence = entity("Evidence", "Synthetic code evidence", {
        "category": "code", "uri": "urn:demo:code", "revision": "demo-commit-1",
        "subject_id": records["CodeArtifact"]["id"], "subject_version": 1}, project["id"])
    test_evidence = entity("Evidence", "Synthetic passed execution (not a real test run)", {
        "category": "test_execution", "uri": "urn:demo:run", "revision": "demo-run-1",
        "subject_id": records["TestCase"]["id"], "subject_version": 1, "outcome": "passed"}, project["id"])
    previous = entity("Decision", "Synthetic previous decision", project_id=project["id"])

    def link(type, source, target, evidence=None):
        return post("/relationships", {"type": type, "source_id": source["id"], "target_id": target["id"],
            "source_version": source["version"], "target_version": target["version"],
            "status": "verified" if evidence else "proposed", "rationale": "Synthetic demonstration of explicit traceability",
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
            "relationship_ids": [r["id"] for r in links], "note": "All demo data and evidence are synthetic."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    print(json.dumps(seed(args.url), indent=2))
