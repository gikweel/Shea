from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from shea.app.facade import SheaApp
from shea.bootstrap import build_runtime


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

    sub.add_parser("listen", help="Start offline voice interaction")

    update_p = sub.add_parser("update", help="Update the system securely")
    update_p.add_argument("--channel", default="stable", help="Update channel")

    args = parser.parse_args(argv)
    Path(args.workspace).mkdir(parents=True, exist_ok=True)

    runtime = build_runtime(
        args.db,
        workspace=args.workspace,
        register_builtins=True,
    )

    if args.command is None or args.command == "boot":
        print("shea ready; app-plane reconcile done")
        return

    if args.command == "listen":
        from shea.audio.service import AudioInteractionService

        try:
            print("Starting offline audio interaction. Press Ctrl-C to stop.")
            AudioInteractionService(runtime).start_loop()
        finally:
            runtime.conn.close()
        return

    app = SheaApp(runtime)

    if args.command == "update":
        from shea.system.updater import UpdateService
        # The update service operates from the root project directory in this setup
        updater = UpdateService(Path("."))
        print(f"Checking for updates on channel {args.channel}...")
        manifest = updater.check_for_updates("0.1.0", channel=args.channel)
        if not manifest:
            print("System is up to date.")
            return
            
        print(f"Update found: {manifest.version}")
        print("Changelog:")
        print(manifest.changelog)
        
        reply = input("Apply update now? [y/N] ")
        if reply.lower() == "y":
            success = updater.apply_update(manifest.version)
            if success:
                print("Update applied successfully. Please restart SHEA.")
            else:
                print("Update failed and was rolled back securely.")
        else:
            print("Update cancelled.")
        return

    if args.command == "chat":
        print("Shea Interactive Chat. Type \"exit\" or \"quit\" to stop.")
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