from __future__ import annotations

import click
import uvicorn

from shea.audio.service import AudioInteractionService
from shea.bootstrap import build_runtime


@click.group()
def cli() -> None:
    """Shea Agent - Unified Command Line Interface"""
    pass


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option("--port", default=8000, help="Port to bind to.", type=int)
def serve(host: str, port: int) -> None:
    """Start the Shea unified web server."""
    click.secho(f"Starting Shea API and Web GUI on http://{host}:{port}", fg="green")
    uvicorn.run("shea.api.server:create_app", host=host, port=port, factory=True, reload=True)


@cli.command()
@click.option("--host", default="127.0.0.1", help="Internal host for the background API.")
@click.option("--port", default=8000, help="Internal port for the background API.", type=int)
def desktop(host: str, port: int) -> None:
    """Launch the Shea desktop native application."""
    from shea.gui.desktop import start_desktop_app
    
    click.secho("Starting Shea Desktop App...", fg="blue")
    start_desktop_app(host=host, port=port)


@cli.command()
@click.option("--db-path", default="shea.db", help="Path to SQLite database.")
@click.option("--workspace", default="./workspace", help="Path to workspace directory.")
def chat(db_path: str, workspace: str) -> None:
    """Start an interactive terminal chat session with the agent."""

    click.secho("Booting Shea Runtime...", fg="yellow")
    runtime = build_runtime(db_path=db_path, workspace=workspace)
    runtime.model_settings_service.load_saved()

    click.secho("System ready. Type 'exit' to quit.", fg="green", bold=True)

    while True:
        try:
            user_input = input("\n\033[1mYou>\033[0m ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break

            click.secho(
                "\n[Agent] Processing request...\n",
                fg="cyan",
            )
            
            # Follows the unified pipeline: Text -> Intent -> Plan -> Execute
            result = runtime.interaction_service.handle_text(
                text=user_input,
                session_id="cli_session",
                actor="cli_user",
                explicit_user_ack=True, # In CLI, we can prompt or assume auto-ack depending on risk
            )
            
            if result.error:
                click.secho(f"Error: {result.error}", fg="red")
            elif result.message is not None:
                click.secho(f"[Agent] {result.message}", fg="green")
            elif result.task is not None:
                click.secho(f"Task Complete: {result.task.id}", fg="green")
        except (KeyboardInterrupt, EOFError):
            break

    click.echo("\nGoodbye!")


@cli.command(name="listen")
@click.option("--db-path", default="shea.db", help="Path to SQLite database.")
def listen_cmd(db_path: str) -> None:
    """Start the audio wake-word interaction loop."""
    
    runtime = build_runtime(db_path=db_path)
    runtime.model_settings_service.load_saved()
    
    # We need to run the runtime event bus in the background if it has async listeners,
    # but the audio loop is strictly synchronous right now (mock input).
    # Since event_bus.publish is sync and handles it via MemoryChannel directly, it works.
    
    click.secho("Starting Audio Interaction Service...", fg="cyan", bold=True)
    audio_service = AudioInteractionService(runtime)
    audio_service.start_loop()


if __name__ == "__main__":
    cli()