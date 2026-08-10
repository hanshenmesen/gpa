"""Practical GPA demo: turn incoming incidents into reusable ticket intake.

This demo keeps the heavy desktop/vision stack out of the critical path so it can
run on a fresh machine, while still producing a real GPA workflow artifact and a
visible local web result.
"""
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
WORKFLOW_ID = "demo_incident_to_ticket"
REPORT_PATH = ROOT / "demo_output" / "practical_ticket_demo_report.md"

TICKETS = [
    {
        "title": "Checkout payment timeout for APAC customers",
        "priority": "high",
        "priority_key": "h",
        "assignee": "Mina Chen",
        "desc": (
            "Stripe charge confirmation is taking more than 30 seconds for "
            "Singapore and Japan customers. Revenue-impacting, started after "
            "the 09:20 deployment."
        ),
    },
    {
        "title": "Enterprise SSO users intermittently loop on login",
        "priority": "medium",
        "priority_key": "m",
        "assignee": "Devon Park",
        "desc": (
            "Several Okta tenants report being redirected back to the login "
            "screen after MFA. Affected accounts can still use password login."
        ),
    },
    {
        "title": "Exported CSV contains duplicate header row",
        "priority": "low",
        "priority_key": "l",
        "assignee": "Ari Patel",
        "desc": (
            "Weekly account export includes the header row twice when filtered "
            "by region. Workaround: delete the first row before upload."
        ),
    },
]


def substitute_vars(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def make_workflow() -> Workflow:
    variables = [
        WorkflowVariable("title", TICKETS[0]["title"], "Short incident title"),
        WorkflowVariable("priority", TICKETS[0]["priority"], "Ticket severity"),
        WorkflowVariable("priority_key", TICKETS[0]["priority_key"], "Keyboard shortcut for severity select"),
        WorkflowVariable("assignee", TICKETS[0]["assignee"], "Owner who should triage the ticket"),
        WorkflowVariable("desc", TICKETS[0]["desc"], "Incident detail and reproduction context"),
    ]
    steps = [
        WorkflowStep(1, "Click Bug title field", id="ticket-title-click", action_type="click"),
        WorkflowStep(2, "Type {{title}}", id="ticket-title-type", action_type="type", value="{{title}}"),
        WorkflowStep(3, "Move to priority select", id="ticket-priority-tab", action_type="hotkey", value="tab"),
        WorkflowStep(4, "Choose priority {{priority}}", id="ticket-priority-type", action_type="type", value="{{priority_key}}"),
        WorkflowStep(5, "Move to assignee field", id="ticket-assignee-tab", action_type="hotkey", value="tab"),
        WorkflowStep(6, "Type {{assignee}}", id="ticket-assignee-type", action_type="type", value="{{assignee}}"),
        WorkflowStep(7, "Move to description field", id="ticket-desc-tab", action_type="hotkey", value="tab"),
        WorkflowStep(8, "Type incident details", id="ticket-desc-type", action_type="type", value="{{desc}}"),
        WorkflowStep(9, "Submit the bug report", id="ticket-submit-click", action_type="click"),
    ]
    return Workflow(
        workflow_id=WORKFLOW_ID,
        workflow_name="incident_to_ticket",
        workflow_title="Incident To Ticket",
        description="Create structured bug tickets from incoming support or alert incidents.",
        variables=variables,
        steps=steps,
        category="operations",
    )


def server_is_running(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/submissions", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    last_error = None
    for _ in range(3):
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"POST failed for {url}: {last_error}")


def start_demo_server():
    from demo_web import server as demo_server

    base_url = f"http://127.0.0.1:{demo_server.PORT}"
    if server_is_running(base_url):
        return base_url, None
    server = demo_server.start_server()
    deadline = time.time() + 3
    while time.time() < deadline:
        if server_is_running(base_url):
            return base_url, server
        time.sleep(0.05)
    raise RuntimeError("Demo web server did not become ready.")


def render_steps(workflow: Workflow, values: dict[str, str]) -> list[str]:
    rendered = []
    for step in workflow.steps:
        value = substitute_vars(step.value, values)
        if step.action_type in {"type", "hotkey"}:
            rendered.append(f"{step.step_number}. {step.action_type}: {value}")
        else:
            rendered.append(f"{step.step_number}. {step.action_type}: {step.action}")
    return rendered


def write_report(workflow: Workflow, submitted: list[dict], base_url: str) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Practical GPA Demo: Incident To Ticket",
        "",
        "## Why this matters",
        "",
        "Support teams often receive repeated incident reports from alerts, chat, and email. "
        "The useful automation pattern is to record the ticket form once, keep the field values "
        "as variables, and replay it for each incoming case.",
        "",
        "## Workflow artifact",
        "",
        f"- Workflow ID: `{workflow.workflow_id}`",
        f"- Workflow name: `{workflow.workflow_name}`",
        f"- Stored at: `storage/workflows/{workflow.workflow_id}/workflow.yaml`",
        f"- Local demo page: {base_url}",
        "",
        "## Submitted tickets",
        "",
        "| ID | Priority | Assignee | Title |",
        "| --- | --- | --- | --- |",
    ]
    for item in submitted:
        lines.append(
            f"| {item['id']} | {item['priority']} | {item['assignee']} | {item['title']} |"
        )
    lines.extend(
        [
            "",
            "## One replay plan example",
            "",
            "```text",
            *render_steps(workflow, TICKETS[0]),
            "```",
            "",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    workflow_module.WORKFLOWS_DIR = WORKFLOWS_DIR
    workflow = make_workflow()
    WorkflowStorage().save(workflow, {})

    base_url, server = start_demo_server()
    post_json(f"{base_url}/reset", {})

    submitted = []
    for ticket in TICKETS:
        payload = {
            "title": ticket["title"],
            "priority": ticket["priority"],
            "assignee": ticket["assignee"],
            "desc": ticket["desc"],
            "time": time.strftime("%H:%M:%S"),
        }
        response = post_json(f"{base_url}/submit", payload)
        submitted.append({**payload, "id": response["id"]})

    write_report(workflow, submitted, base_url)

    print("Practical GPA demo complete")
    print(f"Workflow: storage/workflows/{WORKFLOW_ID}/workflow.yaml")
    print(f"Submitted tickets: {len(submitted)}")
    print(f"Report: {REPORT_PATH}")
    print(f"Open: {base_url}")

    if server is not None:
        server.shutdown()


if __name__ == "__main__":
    main()
