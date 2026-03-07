"""Task monitoring commands."""

import time
from typing import Optional

import typer

from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.output import output_result, print_error, print_success, print_info, create_progress

app = typer.Typer(help="Monitor async tasks")


@app.command()
def status(
    task_id: str = typer.Argument(..., help="Task ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Check the status of a task."""
    try:
        http = HTTPAdapter()
        result = http.get_task_status(task_id)

        output_result(result, json_output=json_output, title=f"Task {task_id}")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def wait(
    task_id: str = typer.Argument(..., help="Task ID"),
    timeout: float = typer.Option(300.0, "--timeout", "-t", help="Maximum wait time in seconds"),
    poll_interval: float = typer.Option(2.0, "--interval", "-i", help="Poll interval in seconds"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Wait for a task to complete."""
    try:
        http = HTTPAdapter()

        print_info(f"Waiting for task {task_id}...")

        result = http.wait_for_task(
            task_id,
            poll_interval=poll_interval,
            max_wait=timeout,
            verbose=True,
        )

        print_success("Task completed")
        output_result(result, json_output=json_output, title="Task result")

    except TimeoutError as e:
        print_error(str(e))
        raise typer.Exit(1)
    except RuntimeError as e:
        print_error(str(e))
        raise typer.Exit(1)
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def watch(
    task_id: str = typer.Argument(..., help="Task ID"),
    timeout: float = typer.Option(300.0, "--timeout", "-t", help="Maximum watch time in seconds"),
    poll_interval: float = typer.Option(2.0, "--interval", "-i", help="Poll interval in seconds"),
):
    """Watch a task with live progress updates."""
    try:
        http = HTTPAdapter()

        with create_progress() as progress:
            task = progress.add_task(f"[cyan]Task {task_id}...", total=None)

            start_time = time.time()
            last_state = None

            while True:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    progress.stop()
                    print_error(f"Timeout after {timeout}s")
                    raise typer.Exit(1)

                try:
                    status = http.get_task_status(task_id)
                except Exception as e:
                    progress.update(task, description=f"[red]Error: {e}")
                    time.sleep(poll_interval)
                    continue

                state = status.get("state", "UNKNOWN")
                status_msg = status.get("status", "")
                pct = status.get("progress", status.get("percent", 0))

                if state != last_state:
                    last_state = state

                desc = f"[cyan]{state}"
                if status_msg:
                    desc += f" - {status_msg}"
                progress.update(task, description=desc, completed=pct if pct else None)

                if state in ("SUCCESS", "COMPLETED"):
                    progress.update(task, description="[green]Completed", completed=100)
                    break

                if state in ("FAILURE", "FAILED", "ERROR"):
                    error_msg = status.get("error") or status.get("message") or "Unknown error"
                    progress.update(task, description=f"[red]Failed: {error_msg}")
                    progress.stop()
                    raise typer.Exit(1)

                if state == "REVOKED":
                    progress.update(task, description="[yellow]Revoked")
                    progress.stop()
                    raise typer.Exit(1)

                time.sleep(poll_interval)

        print_success(f"Task {task_id} completed successfully")

    except typer.Exit:
        raise
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("list-recent")
def list_recent(
    limit: int = typer.Option(10, "--limit", "-l", help="Number of recent tasks"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List recent tasks (if available in backend)."""
    print_info("Recent task listing not available - tasks are ephemeral in Celery.")
    print_info("Use 'stemmy tasks status <task_id>' to check a specific task.")
    raise typer.Exit(0)
