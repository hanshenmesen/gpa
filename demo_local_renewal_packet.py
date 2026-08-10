"""Local software GPA demo: build a customer-renewal action packet.

This case uses local macOS software instead of a web form:
- Finder shows the generated packet folder.
- TextEdit opens the executive brief and customer reply draft.
- CSV/JSON files provide an action tracker and audit record for handoff.
"""
from __future__ import annotations

import csv
import json
import subprocess
import textwrap
import time
from datetime import datetime
from pathlib import Path

import gpa.storage.workflow as workflow_module
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowStorage, WorkflowVariable

ROOT = Path(__file__).parent
PACKET_ROOT = ROOT / "demo_output" / "local_renewal_packet"
WORKFLOWS_DIR = ROOT / "storage" / "workflows"
WORKFLOW_ID = "demo_local_renewal_packet"

CASE = {
    "account": "Meridian Bank",
    "owner": "Nora Li",
    "executive_sponsor": "Lena Ortiz",
    "arr": "$610,000",
    "renewal_date": "2026-07-24",
    "health": "31",
    "risk": "High",
    "signal": (
        "Security review is stalled, API error rate increased after SSO migration, "
        "and the economic buyer asked for a three-month renewal delay."
    ),
    "save_plan": (
        "Escalate to VP Customer Success, schedule a security architecture review, "
        "prepare SSO incident analysis, and draft commercial bridge options."
    ),
    "customer_reply": (
        "Hi Meridian team,\n\n"
        "I saw the SSO and security-review blockers ahead of renewal. I can bring "
        "our security architect and customer-success lead into a focused session "
        "tomorrow with a concrete remediation plan.\n\n"
        "Proposed agenda:\n"
        "1. Confirm the SSO failure pattern and affected teams.\n"
        "2. Review the open security questionnaire items.\n"
        "3. Agree on a recovery timeline and commercial bridge options.\n\n"
        "Best,\nNora"
    ),
}


def shell_quote_for_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def write_textedit_document(path: Path, content: str) -> None:
    """Create a document through TextEdit so the local-app workflow is visible."""
    script = f'''
    tell application "TextEdit"
        activate
        set docRef to make new document with properties {{text:"{shell_quote_for_applescript(content)}"}}
        save docRef in POSIX file "{shell_quote_for_applescript(str(path))}"
        close docRef saving yes
        open POSIX file "{shell_quote_for_applescript(str(path))}"
    end tell
    '''
    subprocess.run(["osascript", "-e", script], check=True)


def open_in_finder(path: Path) -> None:
    subprocess.run(["open", str(path)], check=True)


def build_workflow() -> Workflow:
    variables = [
        WorkflowVariable("account", CASE["account"], "Customer account name"),
        WorkflowVariable("owner", CASE["owner"], "Internal customer-success owner"),
        WorkflowVariable("arr", CASE["arr"], "Annual recurring revenue at risk"),
        WorkflowVariable("renewal_date", CASE["renewal_date"], "Renewal date"),
        WorkflowVariable("risk", CASE["risk"], "Renewal risk rating"),
        WorkflowVariable("signal", CASE["signal"], "Observed risk signals"),
        WorkflowVariable("save_plan", CASE["save_plan"], "Internal save-plan actions"),
        WorkflowVariable("customer_reply", CASE["customer_reply"], "Customer-facing reply draft"),
    ]
    steps = [
        WorkflowStep(1, "Create packet folder in Finder", id="local-create-folder", action_type="hotkey", value="cmd+shift+n"),
        WorkflowStep(2, "Create executive brief in TextEdit", id="local-brief-textedit", action_type="type", value="{{signal}}"),
        WorkflowStep(3, "Create customer reply draft in TextEdit", id="local-reply-textedit", action_type="type", value="{{customer_reply}}"),
        WorkflowStep(4, "Write local CSV action tracker", id="local-action-tracker", action_type="type", value="{{save_plan}}"),
        WorkflowStep(5, "Open packet folder in Finder for review", id="local-open-finder", action_type="click"),
    ]
    return Workflow(
        workflow_id=WORKFLOW_ID,
        workflow_name="local_renewal_packet",
        workflow_title="Local Renewal Packet",
        description="Create a local handoff packet for a high-risk customer renewal using Finder and TextEdit.",
        variables=variables,
        steps=steps,
        category="local-productivity",
    )


def build_packet_dir() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    packet_dir = PACKET_ROOT / f"{CASE['account'].replace(' ', '_')}_{timestamp}"
    packet_dir.mkdir(parents=True, exist_ok=True)
    return packet_dir


def executive_brief() -> str:
    return textwrap.dedent(
        f"""\
        Renewal Risk Executive Brief
        ============================

        Account: {CASE['account']}
        Owner: {CASE['owner']}
        Executive sponsor: {CASE['executive_sponsor']}
        ARR at risk: {CASE['arr']}
        Renewal date: {CASE['renewal_date']}
        Health score: {CASE['health']}
        Risk rating: {CASE['risk']}

        Risk signal
        -----------
        {CASE['signal']}

        Recommended save plan
        ---------------------
        {CASE['save_plan']}

        Decision needed
        ---------------
        Approve executive escalation and customer recovery session within 24 hours.
        """
    )


def write_action_tracker(path: Path) -> None:
    rows = [
        ["owner", "priority", "due_date", "action", "status"],
        [CASE["owner"], "P0", "2026-07-09", "Schedule security architecture review", "open"],
        [CASE["executive_sponsor"], "P0", "2026-07-09", "Join executive renewal escalation", "open"],
        ["Solutions Engineering", "P1", "2026-07-10", "Analyze SSO error pattern", "open"],
        ["RevOps", "P1", "2026-07-11", "Draft commercial bridge options", "open"],
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


def write_audit(path: Path, packet_dir: Path) -> None:
    audit = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "case": CASE,
        "packet_dir": str(packet_dir),
        "local_apps": ["Finder", "TextEdit"],
        "artifacts": [
            "executive_brief.txt",
            "customer_reply_draft.txt",
            "action_tracker.csv",
            "audit_record.json",
        ],
    }
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    workflow_module.WORKFLOWS_DIR = WORKFLOWS_DIR
    WorkflowStorage().save(build_workflow(), {})

    packet_dir = build_packet_dir()
    brief_path = packet_dir / "executive_brief.txt"
    reply_path = packet_dir / "customer_reply_draft.txt"
    tracker_path = packet_dir / "action_tracker.csv"
    audit_path = packet_dir / "audit_record.json"

    write_textedit_document(brief_path, executive_brief())
    time.sleep(0.5)
    write_textedit_document(reply_path, CASE["customer_reply"])
    write_action_tracker(tracker_path)
    write_audit(audit_path, packet_dir)
    open_in_finder(packet_dir)

    print("Local renewal packet demo complete")
    print(f"Packet folder: {packet_dir}")
    print(f"Executive brief: {brief_path}")
    print(f"Reply draft: {reply_path}")
    print(f"Action tracker: {tracker_path}")
    print(f"Audit record: {audit_path}")
    print(f"Workflow: storage/workflows/{WORKFLOW_ID}/workflow.yaml")


if __name__ == "__main__":
    main()
