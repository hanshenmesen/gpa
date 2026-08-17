"""CLI for GPA — GUI Process Automation.

Commands:
  gpa record [NAME]   — start recording, hit Enter to stop
  gpa run NAME        — replay a workflow
  gpa list            — list all stored workflows
  gpa show NAME       — show workflow steps
  gpa export NAME     — export workflow as a community record package
  gpa import PACKAGE  — import a community record package
  gpa delete NAME     — delete a workflow
  gpa download-models — pre-download all required models
  gpa mcp-serve       — start MCP server (stdio)
"""
from __future__ import annotations

import logging
import os
import sys
import time

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from gpa import __version__
from gpa.config import MAX_RETRIES_LIMIT
from gpa.runtime_config import RuntimeConfigurationError, env_bool
from gpa.storage.workflow import storage as wf_storage

console = Console()
diagnostic_console = Console(stderr=True)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
DESKTOP_AUTOMATION_ENV = "GPA_ENABLE_DESKTOP_AUTOMATION"


def _setup_verbose(verbose: bool) -> None:
    if verbose:
        logging.getLogger("gpa").setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.INFO)


# ──────────────────────────────────────────────────────────────────────────── #
# CLI group                                                                    #
# ──────────────────────────────────────────────────────────────────────────── #

@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose output.")
@click.version_option(version=__version__, prog_name="gpa")
@click.pass_context
def main(ctx, verbose):
    """GPA — local-first GUI workflow recording and safe Replay."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_verbose(verbose)


# ──────────────────────────────────────────────────────────────────────────── #
# record                                                                       #
# ──────────────────────────────────────────────────────────────────────────── #

@main.command()
@click.argument("name", required=False)
@click.pass_context
def record(ctx, name):
    """Record a GUI workflow demonstration.

    Starts capturing mouse clicks and keyboard input until you press Enter.
    Then builds a workflow template (with LLM) and saves it.
    """
    from gpa.recording.builder import build_workflow
    from gpa.recording.recorder import Recorder
    from gpa.storage.workflow import storage

    console.print("[bold green]GPA Recorder[/bold green] — perform your workflow, then press Enter to stop.")
    console.print("Recording will start in 3 seconds …")
    time.sleep(3)

    rec = Recorder()
    rec.start()
    console.print("[yellow]Recording …[/yellow] (press Enter in this terminal to stop)")

    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass

    recording = rec.stop()
    console.print(f"Captured [bold]{len(recording.events)}[/bold] events.")

    if not recording.events:
        console.print("[red]No events recorded. Exiting.[/red]")
        return

    console.print("[yellow]Building workflow (parsing screenshots + LLM) …[/yellow]")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        task = prog.add_task("Building …", total=None)
        build_result = build_workflow(recording, workflow_id=name)
        prog.update(task, completed=True)

    wf = build_result.workflow
    saved_path = storage.save(wf, build_result.step_subgraphs)

    console.print("\n[bold green]Workflow saved![/bold green]")
    console.print(f"  ID:    {wf.workflow_id}")
    console.print(f"  Name:  {wf.workflow_name}")
    console.print(f"  Steps: {len(wf.steps)}")
    console.print(f"  Vars:  {[v.name for v in wf.variables]}")
    console.print(f"  Path:  {saved_path}")


# ──────────────────────────────────────────────────────────────────────────── #
# run                                                                          #
# ──────────────────────────────────────────────────────────────────────────── #

@main.command()
@click.argument("workflow_id_or_name")
@click.option("--var", "-v", multiple=True, metavar="KEY=VALUE",
              help="Variable override (repeatable, e.g. -v email=foo@bar.com).")
@click.option("--threshold", default=0.5, show_default=True,
              type=click.FloatRange(min=0.0, max=1.0),
              help="Readiness confidence threshold (0–1).")
@click.option("--retries", default=5, show_default=True,
              type=click.IntRange(min=0, max=MAX_RETRIES_LIMIT),
              help="Max retries per step before failing.")
@click.option("--metrics", is_flag=True, help="Show per-step parser and decision timings.")
@click.pass_context
def run(ctx, workflow_id_or_name, var, threshold, retries, metrics):
    """Replay a recorded workflow on the live desktop."""
    try:
        desktop_enabled = env_bool(DESKTOP_AUTOMATION_ENV, False)
    except RuntimeConfigurationError as exc:
        raise click.ClickException(str(exc)) from exc
    if not desktop_enabled:
        raise click.ClickException(
            "Desktop automation is disabled. Set "
            f"{DESKTOP_AUTOMATION_ENV}=1 only before running a trusted, reviewed Replay."
        )

    # Resolve name → id
    wf_id = _resolve_id(workflow_id_or_name)
    if wf_id is None:
        console.print(f"[red]Workflow not found: {workflow_id_or_name}[/red]")
        sys.exit(1)

    # Parse variable overrides
    variables: dict[str, str] = {}
    for kv in var:
        if "=" not in kv:
            console.print(f"[red]Invalid --var format (use KEY=VALUE): {kv}[/red]")
            sys.exit(1)
        k, v = kv.split("=", 1)
        key = k.strip()
        if not key:
            raise click.ClickException(f"Invalid --var key: {kv}")
        variables[key] = v.strip()

    workflow, subgraphs = wf_storage.load(wf_id)
    from gpa.execution.executor import Executor

    console.print(
        f"[bold green]Running[/bold green] '{workflow.workflow_title}' "
        f"({len(workflow.steps)} steps)"
    )
    if variables:
        console.print(f"  Variables: {variables}")

    executor = Executor(
        workflow, subgraphs,
        variables=variables,
        readiness_threshold=threshold,
        max_retries=retries,
    )
    result = executor.run()

    if metrics:
        _print_run_metrics(result)

    if result.success:
        console.print("[bold green]✓ Workflow completed successfully.[/bold green]")
    else:
        console.print(f"[bold red]✗ Workflow failed: {result.error}[/bold red]")
        sys.exit(1)


def _print_run_metrics(result) -> None:
    table = Table(title="Run Metrics", show_header=True, header_style="bold cyan")
    table.add_column("Step", justify="right")
    table.add_column("State")
    table.add_column("Duration", justify="right")
    table.add_column("Agent", justify="right")
    table.add_column("Observations", justify="right")
    table.add_column("Parser", justify="right")
    table.add_column("Cache", justify="right")

    for step in result.step_results:
        observations = step.observation_metrics or []
        parser_ms = sum(float(item.get("total_ms") or 0.0) for item in observations)
        cache_hits = sum(1 for item in observations if item.get("cache_hit"))
        table.add_row(
            str(step.step_number),
            step.state.name,
            f"{step.duration_seconds:.3f}s",
            f"{step.agent_decision_ms:.1f}ms",
            str(len(observations)),
            f"{parser_ms:.1f}ms",
            f"{cache_hits}/{len(observations)}",
        )

    console.print(table)


# ──────────────────────────────────────────────────────────────────────────── #
# list                                                                         #
# ──────────────────────────────────────────────────────────────────────────── #

@main.command(name="list")
def list_workflows():
    """List all stored workflows."""
    workflows = wf_storage.list_workflows()
    if not workflows:
        console.print("No workflows found. Run [bold]gpa record[/bold] to create one.")
        return

    table = Table(title="Stored Workflows", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Title")
    table.add_column("Steps", justify="right")
    table.add_column("Description")

    for wf in workflows:
        table.add_row(
            wf["id"][:16] + "…" if len(wf["id"]) > 16 else wf["id"],
            wf["name"],
            wf["title"],
            str(wf["steps"]),
            (wf["description"][:60] + "…") if len(wf["description"]) > 60 else wf["description"],
        )
    console.print(table)


# ──────────────────────────────────────────────────────────────────────────── #
# show                                                                         #
# ──────────────────────────────────────────────────────────────────────────── #

@main.command()
@click.argument("workflow_id_or_name")
def show(workflow_id_or_name):
    """Show detailed info about a workflow."""
    wf_id = _resolve_id(workflow_id_or_name)
    if wf_id is None:
        console.print(f"[red]Workflow not found: {workflow_id_or_name}[/red]")
        sys.exit(1)

    workflow, subgraphs = wf_storage.load(wf_id)
    console.print(f"\n[bold]{workflow.workflow_title}[/bold] ({workflow.workflow_name})")
    console.print(f"ID: {workflow.workflow_id}")
    console.print(f"Description: {workflow.description}")
    console.print(f"Created: {workflow.created_at}")

    if workflow.variables:
        console.print("\n[bold cyan]Variables:[/bold cyan]")
        for v in workflow.variables:
            console.print(f"  {v.name} = {v.default_value!r}  # {v.description}")

    console.print(f"\n[bold cyan]Steps ({len(workflow.steps)}):[/bold cyan]")
    for step in workflow.steps:
        has_sg = step.id in subgraphs
        icon = "●" if has_sg else "○"
        console.print(f"  {icon} {step.step_number:3d}. {step.action}")


# ──────────────────────────────────────────────────────────────────────────── #
# community package export/import                                             #
# ──────────────────────────────────────────────────────────────────────────── #

@main.command("export")
@click.argument("workflow_id_or_name")
@click.argument("destination", required=False, type=click.Path())
def export_record(workflow_id_or_name, destination):
    """Export a workflow as a portable community record package."""
    from gpa.community.package import export_workflow_package

    wf_id = _resolve_id(workflow_id_or_name)
    if wf_id is None:
        console.print(f"[red]Workflow not found: {workflow_id_or_name}[/red]")
        sys.exit(1)

    output = export_workflow_package(wf_id, destination or ".")
    console.print(f"[bold green]Exported community package:[/bold green] {output}")


@main.command("import")
@click.argument("package_path", type=click.Path(exists=True))
@click.option("--workflow-id", help="Import under a specific local workflow ID.")
@click.option("--overwrite", is_flag=True, help="Replace an existing workflow with the same ID.")
def import_record(package_path, workflow_id, overwrite):
    """Import a portable community record package."""
    from gpa.community.package import import_workflow_package

    try:
        result = import_workflow_package(
            package_path,
            workflow_id=workflow_id,
            overwrite=overwrite,
        )
    except Exception as exc:
        console.print(f"[bold red]Import failed:[/bold red] {exc}")
        sys.exit(1)

    renamed = " (renamed to avoid collision)" if result.was_renamed else ""
    console.print(
        f"[bold green]Imported[/bold green] {result.workflow_name} "
        f"as {result.workflow_id}{renamed}"
    )


# ──────────────────────────────────────────────────────────────────────────── #
# delete                                                                       #
# ──────────────────────────────────────────────────────────────────────────── #

@main.command()
@click.argument("workflow_id_or_name")
@click.confirmation_option(prompt="Are you sure you want to delete this workflow?")
def delete(workflow_id_or_name):
    """Delete a stored workflow."""
    wf_id = _resolve_id(workflow_id_or_name)
    if wf_id is None:
        console.print(f"[red]Workflow not found: {workflow_id_or_name}[/red]")
        sys.exit(1)
    wf_storage.delete(wf_id)
    console.print(f"[green]Deleted workflow: {wf_id}[/green]")


# ──────────────────────────────────────────────────────────────────────────── #
# download-models                                                              #
# ──────────────────────────────────────────────────────────────────────────── #

@main.command("download-models")
def download_models():
    """Pre-download all required ML models."""
    from gpa.models.model_loader import ModelDependencyError, ensure_all_models

    console.print("[yellow]Downloading models (this may take a while) …[/yellow]")
    try:
        ensure_all_models()
    except ModelDependencyError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(f"Model download failed: {exc}") from exc
    console.print("[bold green]All models downloaded.[/bold green]")


# ──────────────────────────────────────────────────────────────────────────── #
# mcp-serve                                                                    #
# ──────────────────────────────────────────────────────────────────────────── #

@main.command("mcp-serve")
@click.option(
    "--allow-execution",
    is_flag=True,
    help="Explicitly allow MCP clients to run trusted Replays.",
)
def mcp_serve(allow_execution):
    """Start the GPA MCP server (stdio transport).

    The default server exposes workflow schemas but refuses execution. Use
    --allow-execution together with GPA_ENABLE_DESKTOP_AUTOMATION=1 only for
    trusted local clients and reviewed Replays.
    """
    import asyncio

    from gpa.integration.mcp_server import MCP_EXECUTION_ENV, run_server

    # The command-line choice overrides inherited and .env values so an old
    # permissive shell cannot silently turn a discovery server into an executor.
    os.environ[MCP_EXECUTION_ENV] = "1" if allow_execution else "0"
    mode = "execution enabled" if allow_execution else "discovery only"
    # stdout is reserved for the MCP stdio protocol.
    diagnostic_console.print(f"[bold green]Starting GPA MCP server[/bold green] ({mode})")
    asyncio.run(run_server())


# ──────────────────────────────────────────────────────────────────────────── #
# Helper                                                                       #
# ──────────────────────────────────────────────────────────────────────────── #

def _resolve_id(name_or_id: str) -> str | None:
    """Resolve an exact ID, unique name, or unique ID prefix."""
    all_wf = wf_storage.list_workflows()
    # Exact ID match
    for wf in all_wf:
        if wf["id"] == name_or_id:
            return wf["id"]

    name_matches = [wf["id"] for wf in all_wf if wf["name"] == name_or_id]
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        raise click.ClickException(
            f"Workflow name is ambiguous: {name_or_id!r}. Use an exact workflow ID."
        )

    prefix_matches = [wf["id"] for wf in all_wf if wf["id"].startswith(name_or_id)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        choices = ", ".join(prefix_matches[:5])
        raise click.ClickException(
            f"Workflow ID prefix is ambiguous: {name_or_id!r}. Matches: {choices}"
        )
    return None


if __name__ == "__main__":
    main()
