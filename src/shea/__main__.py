from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from shea import __version__
from shea.app.facade import SheaApp
from shea.bootstrap import build_runtime
from shea.system.updater import UpdateError, UpdateService


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Shea runtime")
    parser.add_argument("--db", default="shea.db", help="SQLite database path")
    parser.add_argument(
        "--workspace",
        default="./workspace",
        help="Allowed filesystem root for builtin tools",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("boot", help="Start runtime and reconcile only")

    run_p = sub.add_parser("run", help="Plan and execute a text request")
    run_p.add_argument("text", help="User request text")
    run_p.add_argument(
        "--no-execute",
        action="store_true",
        help="Plan only (leave task READY)",
    )
    run_p.add_argument("--session", default="cli")

    chat_p = sub.add_parser("chat", help="Start interactive chat session")
    chat_p.add_argument("--session", default="cli-chat")

    desktop_p = sub.add_parser("desktop", help="Launch the Shea desktop application")
    desktop_p.add_argument("--host", default="127.0.0.1", help="Host for the background API")
    desktop_p.add_argument("--port", type=int, default=8000, help="Port for the background API")

    sub.add_parser("listen", help="Start offline voice interaction")
    sub.add_parser("configure-model", help="Configure a model provider and API key")

    update_p = sub.add_parser("update", help="Check for and install a GitHub release")
    update_p.add_argument(
        "--channel",
        choices=("stable", "prerelease"),
        default="stable",
        help="Release channel",
    )
    sub.add_parser("backup", help="Create a code, database, and config snapshot")
    sub.add_parser("snapshots", help="List available restore snapshots")
    restore_p = sub.add_parser("restore", help="Restore code and config from a snapshot")
    restore_p.add_argument("snapshot_id", help="Snapshot ID shown by `shea snapshots`")

    args = parser.parse_args(argv)

    if args.command is None or args.command == "boot":
        Path(args.workspace).mkdir(parents=True, exist_ok=True)
        runtime = build_runtime(
            args.db,
            workspace=args.workspace,
            register_builtins=True,
        )
        runtime.conn.close()
        print("shea ready; app-plane reconcile done")
        return

    if args.command == "listen":
        from shea.audio.service import AudioInteractionService

        Path(args.workspace).mkdir(parents=True, exist_ok=True)
        runtime = build_runtime(
            args.db,
            workspace=args.workspace,
            register_builtins=True,
        )
        runtime.model_settings_service.load_saved()
        try:
            print("Starting offline audio interaction. Press Ctrl-C to stop.")
            AudioInteractionService(runtime).start_loop()
        finally:
            runtime.conn.close()
        return

    if args.command == "desktop":
        try:
            from shea.gui.desktop import start_desktop_app
        except ModuleNotFoundError as exc:
            if exc.name in {"uvicorn", "webview", "fastapi"}:
                print(
                    "Desktop support is not installed. Reinstall Shea without "
                    "`--cli-only` to include the web and desktop dependencies.",
                    file=sys.stderr,
                )
                sys.exit(2)
            raise

        start_desktop_app(
            host=args.host,
            port=args.port,
            db_path=None if args.db == "shea.db" else args.db,
            workspace=None if args.workspace == "./workspace" else args.workspace,
        )
        return

    if args.command == "configure-model":
        runtime = build_runtime(
            args.db,
            workspace=args.workspace,
            register_builtins=True,
        )
        try:
            runtime.model_settings_service.load_saved()
            provider = input(
                "Provider (openai-compatible/ollama) [openai-compatible]: "
            ).strip() or "openai-compatible"
            default_base_url = (
                "http://localhost:11434/v1"
                if provider == "ollama"
                else "https://api.openai.com/v1"
            )
            base_url = input(f"Base URL [{default_base_url}]: ").strip() or default_base_url
            default_model = "llama3.2" if provider == "ollama" else "gpt-4o-mini"
            model_name = input(f"Model name [{default_model}]: ").strip() or default_model
            api_key = (
                getpass.getpass("API key (hidden; leave blank to keep an existing key): ")
                if provider == "openai-compatible"
                else ""
            )
            try:
                settings = runtime.model_settings_service.save(
                    provider=provider,
                    base_url=base_url,
                    model_name=model_name,
                    api_key=api_key,
                )
            except ValueError as exc:
                print(f"Model configuration error: {exc}", file=sys.stderr)
                sys.exit(2)
            print(
                f"Configured {settings.provider} model {settings.model_name}. "
                "The API key is stored in the OS keychain."
            )
        finally:
            runtime.conn.close()
        return

    if args.command in {"update", "backup", "snapshots", "restore"}:
        db_path = Path(args.db).expanduser()
        updater = UpdateService(db_path.parent, db_path=db_path)

        try:
            if args.command == "backup":
                backup_snapshot = updater.create_snapshot()
                print(f"Snapshot created: {backup_snapshot.name}")
                return

            if args.command == "snapshots":
                available_snapshots = updater.list_snapshots()
                if not available_snapshots:
                    print("No snapshots available.")
                    return
                for snapshot_info in available_snapshots:
                    print(
                        f"{snapshot_info.id} version={snapshot_info.version} "
                        f"reason={snapshot_info.reason}"
                    )
                return

            if args.command == "restore":
                updater.restore(args.snapshot_id)
                print(
                    "Application code and config restored. "
                    "The current database and .env were preserved."
                )
                return

            print(f"Checking for updates on channel {args.channel}...")
            manifest = updater.check_for_updates(__version__, channel=args.channel)
        except UpdateError as exc:
            print(f"Update error: {exc}", file=sys.stderr)
            sys.exit(1)

        if not manifest:
            print(f"Shea {__version__} is up to date; no newer GitHub release is available.")
            return

        print(f"Update found: {manifest.version}")
        print("Changelog:")
        print(manifest.changelog)

        reply = input("Apply update now? [y/N] ")
        if reply.lower() == "y":
            try:
                success = updater.apply_update(manifest)
            except UpdateError as exc:
                print(f"Update error: {exc}", file=sys.stderr)
                sys.exit(1)
            if success:
                print("Update installed successfully. Please restart SHEA.")
            else:
                print("Update failed; the prior installation was restored.")
        else:
            print("Update cancelled.")
        return

    Path(args.workspace).mkdir(parents=True, exist_ok=True)
    runtime = build_runtime(
        args.db,
        workspace=args.workspace,
        register_builtins=True,
    )
    runtime.model_settings_service.load_saved()
    app = SheaApp(runtime)

    if args.command == "chat":
        print('Shea Interactive Chat. Type "exit" or "quit" to stop.')
        while True:
            try:
                text = input("> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if text.strip().lower() in ("exit", "quit"):
                break

            if not text.strip():
                continue

            result = app.chat(
                text,
                session_id=args.session,
                explicit_user_ack=True,
                run=True,
            )

            if result.error:
                print(f"Error: {result.error}", file=sys.stderr)
                continue

            if result.run is not None:
                for step_result in result.run.step_results:
                    data = step_result.response.data
                    if isinstance(data, dict):
                        typed_data = cast(dict[str, object], data)
                        content = typed_data.get("content")
                        if isinstance(content, str) and content.strip():
                            print(content)
                    elif isinstance(data, str) and data.strip():
                        print(data)
                    if step_result.response.error:
                        print(f"Step Error: {step_result.response.error}", file=sys.stderr)
            else:
                print("No execution occurred.")
        return

    if args.command == "run":
        result = app.chat(
            args.text,
            session_id=args.session,
            explicit_user_ack=True,
            run=not args.no_execute,
        )
        if result.task is not None:
            print(f"task={result.task.id} state={result.task.state.value}")
        elif result.message is not None:
            print(f"reply={result.message!r}")
        if result.error:
            print(f"error: {result.error}", file=sys.stderr)
            sys.exit(1)

        if result.planning is not None:
            plan = result.planning.plan
            print(f"plan_steps={len(plan.steps)}")
            for i, step in enumerate(plan.steps, start=1):
                print(f"  {i}. {step.tool} {step.description if step.description else ''}".rstrip())

        if result.run is not None:
            for step_result in result.run.step_results:
                data = step_result.response.data
                if isinstance(data, dict):
                    typed_data = cast(dict[str, object], data)
                    content = typed_data.get("content")
                    if isinstance(content, str) and content.strip():
                        print(content)
                if step_result.response.error:
                    print(
                        f"step_error: {step_result.response.error}",
                        file=sys.stderr,
                    )
            print(
                f"completed_steps={result.run.completed_steps} "
                f"stopped_early={result.run.stopped_early}"
            )
        elif args.no_execute:
            print("plan_only=true (use run without --no-execute to execute)")
        return

    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":
    main()
