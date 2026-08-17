"""Practical GPA demo: customer renewal-risk operations workflow."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import gpa.storage.workflow as workflow_module
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowStorage, WorkflowVariable

ROOT = Path(__file__).parent
WORKFLOWS_DIR = ROOT / "storage" / "workflows"
WORKFLOW_ID = "demo_renewal_risk_desk"
REPORT_PATH = ROOT / "demo_output" / "renewal_risk_demo_report.md"

CASES = [
    {
        "account": "Acme Health",
        "owner": "Lena Ortiz",
        "arr": "420000",
        "renewal_date": "2026-07-29",
        "health": "38",
        "risk": "high",
        "risk_key": "h",
        "signal": (
            "Admin activity down 72%, data export job has failed for six days, "
            "and the procurement owner changed last week."
        ),
        "playbook": (
            "Create exec review, attach adoption trend, schedule solution engineer "
            "for export reliability review, and prepare a commercial save plan."
        ),
        "reply": (
            "Hi Acme team, we noticed adoption and export reliability issues before "
            "renewal. I would like to bring our CSM and solution engineer into a "
            "focused recovery session this week."
        ),
        "next_touch": "2026-07-10",
        "stage": "exec-review",
        "stage_key": "e",
    },
    {
        "account": "Northstar Retail",
        "owner": "Devon Park",
        "arr": "185000",
        "renewal_date": "2026-08-22",
        "health": "54",
        "risk": "medium",
        "risk_key": "m",
        "signal": (
            "Support cases rose 38% after the inventory connector upgrade. Store "
            "operations still use the product daily, but escalation sentiment is negative."
        ),
        "playbook": (
            "Open reliability review, route connector logs to engineering, and "
            "book a success review with the store operations lead."
        ),
        "reply": (
            "Thanks for flagging the connector issues. We can review logs with our "
            "engineering partner and agree on a recovery plan before your renewal review."
        ),
        "next_touch": "2026-07-13",
        "stage": "customer-meeting",
        "stage_key": "c",
    },
    {
        "account": "Atlas Logistics",
        "owner": "Mina Chen",
        "arr": "72000",
        "renewal_date": "2026-09-26",
        "health": "71",
        "risk": "low",
        "risk_key": "l",
        "signal": (
            "Procurement contact changed and two admin seats are inactive, but weekly "
            "shipment dashboard usage remains stable."
        ),
        "playbook": (
            "Confirm new procurement stakeholder, refresh admin roster, and schedule "
            "a lightweight value recap."
        ),
        "reply": (
            "I saw your procurement contact changed. I can help update the renewal "
            "stakeholder map and send a brief usage recap for the new owner."
        ),
        "next_touch": "2026-07-17",
        "stage": "triage",
        "stage_key": "t",
    },
]


def make_workflow() -> Workflow:
    variables = [
        WorkflowVariable("account", CASES[0]["account"], "Customer account name"),
        WorkflowVariable("owner", CASES[0]["owner"], "Customer success owner"),
        WorkflowVariable("arr", CASES[0]["arr"], "Annual recurring revenue at risk"),
        WorkflowVariable("renewal_date", CASES[0]["renewal_date"], "Renewal date"),
        WorkflowVariable("health", CASES[0]["health"], "Customer health score"),
        WorkflowVariable("risk_key", CASES[0]["risk_key"], "Keyboard shortcut for risk level"),
        WorkflowVariable("signal", CASES[0]["signal"], "Observed renewal-risk signals"),
        WorkflowVariable("playbook", CASES[0]["playbook"], "Internal save-plan actions"),
        WorkflowVariable("reply", CASES[0]["reply"], "External customer reply draft"),
        WorkflowVariable("next_touch", CASES[0]["next_touch"], "Next follow-up date"),
        WorkflowVariable("stage_key", CASES[0]["stage_key"], "Keyboard shortcut for workflow stage"),
    ]
    steps = [
        WorkflowStep(1, "Click account field", id="renewal-account-click", action_type="click"),
        WorkflowStep(2, "Type {{account}}", id="renewal-account-type", action_type="type", value="{{account}}"),
        WorkflowStep(3, "Move to owner", id="renewal-owner-tab", action_type="hotkey", value="tab"),
        WorkflowStep(4, "Type {{owner}}", id="renewal-owner-type", action_type="type", value="{{owner}}"),
        WorkflowStep(5, "Move to ARR", id="renewal-arr-tab", action_type="hotkey", value="tab"),
        WorkflowStep(6, "Type {{arr}}", id="renewal-arr-type", action_type="type", value="{{arr}}"),
        WorkflowStep(7, "Move to renewal date", id="renewal-date-tab", action_type="hotkey", value="tab"),
        WorkflowStep(8, "Type {{renewal_date}}", id="renewal-date-type", action_type="type", value="{{renewal_date}}"),
        WorkflowStep(9, "Move to health", id="renewal-health-tab", action_type="hotkey", value="tab"),
        WorkflowStep(10, "Type {{health}}", id="renewal-health-type", action_type="type", value="{{health}}"),
        WorkflowStep(11, "Move to risk", id="renewal-risk-tab", action_type="hotkey", value="tab"),
        WorkflowStep(12, "Choose risk", id="renewal-risk-type", action_type="type", value="{{risk_key}}"),
        WorkflowStep(13, "Move to signal", id="renewal-signal-tab", action_type="hotkey", value="tab"),
        WorkflowStep(14, "Type risk signal", id="renewal-signal-type", action_type="type", value="{{signal}}"),
        WorkflowStep(15, "Move to playbook", id="renewal-playbook-tab", action_type="hotkey", value="tab"),
        WorkflowStep(16, "Type save plan", id="renewal-playbook-type", action_type="type", value="{{playbook}}"),
        WorkflowStep(17, "Move to customer reply", id="renewal-reply-tab", action_type="hotkey", value="tab"),
        WorkflowStep(18, "Type customer reply", id="renewal-reply-type", action_type="type", value="{{reply}}"),
        WorkflowStep(19, "Move to next touch", id="renewal-next-tab", action_type="hotkey", value="tab"),
        WorkflowStep(20, "Type {{next_touch}}", id="renewal-next-type", action_type="type", value="{{next_touch}}"),
        WorkflowStep(21, "Move to stage", id="renewal-stage-tab", action_type="hotkey", value="tab"),
        WorkflowStep(22, "Choose stage", id="renewal-stage-type", action_type="type", value="{{stage_key}}"),
        WorkflowStep(23, "Submit renewal-risk case", id="renewal-submit-click", action_type="click"),
    ]
    return Workflow(
        workflow_id=WORKFLOW_ID,
        workflow_name="renewal_risk_desk",
        workflow_title="Renewal Risk Desk",
        description="Create customer renewal-risk cases with ARR impact, playbook, reply draft, and audit trail.",
        variables=variables,
        steps=steps,
        category="customer-success",
    )


def post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    last_error = None
    for _ in range(4):
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            time.sleep(0.15)
    raise RuntimeError(f"POST failed for {url}: {last_error}")


def get_json(url: str) -> list[dict]:
    with urllib.request.urlopen(url, timeout=4) as response:
        return json.loads(response.read().decode("utf-8"))


def start_demo_server():
    from demo_web import server as demo_server

    base_url = f"http://127.0.0.1:{demo_server.PORT}"
    try:
        get_json(f"{base_url}/submissions")
        return base_url, None
    except Exception as initial_error:
        srv = demo_server.start_server()
        last_error = initial_error
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                get_json(f"{base_url}/submissions")
                return base_url, srv
            except Exception as exc:
                last_error = exc
                time.sleep(0.05)
        raise RuntimeError("Demo web server did not become ready.") from last_error


def write_report(workflow: Workflow, submitted: list[dict], base_url: str) -> None:
    total_arr = sum(int(item["arr"]) for item in submitted)
    high_count = sum(1 for item in submitted if item["risk"] == "high")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Renewal Risk Desk Demo",
        "",
        "## Practical scenario",
        "",
        "A customer-success team needs to convert renewal warning signals into a structured case: "
        "ARR exposure, health score, risk reason, internal save plan, customer-facing reply, next touch, and audit trail.",
        "",
        "## Workflow artifact",
        "",
        f"- Workflow ID: `{workflow.workflow_id}`",
        f"- Workflow name: `{workflow.workflow_name}`",
        f"- Stored at: `storage/workflows/{workflow.workflow_id}/workflow.yaml`",
        f"- Demo page: {base_url}",
        f"- Steps in replay template: {len(workflow.steps)}",
        "",
        "## Generated cases",
        "",
        f"- Cases: {len(submitted)}",
        f"- High-risk cases: {high_count}",
        f"- ARR at risk: ${total_arr:,}",
        "",
        "| ID | Risk | ARR | Health | Account | Owner | Stage |",
        "| --- | --- | ---: | ---: | --- | --- | --- |",
    ]
    for item in submitted:
        lines.append(
            f"| {item['id']} | {item['risk']} | ${int(item['arr']):,} | {item['health']} | "
            f"{item['account']} | {item['owner']} | {item['stage']} |"
        )
    lines.extend(["", "## Why this is a stronger GPA case", ""])
    lines.extend(
        [
            "- It models a cross-functional operating workflow, not only one simple form.",
            "- It preserves internal action plan and external customer communication as separate fields.",
            "- It has measurable business impact through ARR, health score, renewal date, and risk stage.",
            "- It produces an audit trail suitable for Customer Success, RevOps, and leadership review.",
            "",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    workflow_module.WORKFLOWS_DIR = WORKFLOWS_DIR
    workflow = make_workflow()
    WorkflowStorage().save(workflow, {})

    base_url, srv = start_demo_server()
    post_json(f"{base_url}/reset", {})
    submitted = []
    for case in CASES:
        payload = {k: v for k, v in case.items() if not k.endswith("_key")}
        payload["time"] = time.strftime("%H:%M:%S")
        response = post_json(f"{base_url}/submit", payload)
        submitted.append({**payload, "id": response["id"]})

    write_report(workflow, submitted, base_url)
    print("Renewal-risk demo complete")
    print(f"Workflow: storage/workflows/{WORKFLOW_ID}/workflow.yaml")
    print(f"Cases: {len(submitted)}")
    print(f"Report: {REPORT_PATH}")
    print(f"Open: {base_url}")

    if srv is not None:
        srv.shutdown()


if __name__ == "__main__":
    main()
