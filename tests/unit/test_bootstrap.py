from __future__ import annotations

import os
from pathlib import Path

import pytest

from shea.bootstrap import _load_dotenv, build_runtime
from shea.contracts.enums import TaskState
from shea.contracts.models import ModelResponse
from shea.model.scripted import ScriptedModelProvider


def test_load_dotenv_uses_explicit_file_without_overriding_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "shea.env"
    env_file.write_text(
        "SHEA_TEST_CONFIG_FROM_FILE=loaded\nSHEA_TEST_CONFIG_PRECEDENCE=file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHEA_ENV_FILE", str(env_file))
    monkeypatch.setenv("SHEA_TEST_CONFIG_PRECEDENCE", "environment")

    _load_dotenv()

    assert os.environ["SHEA_TEST_CONFIG_FROM_FILE"] == "loaded"
    assert os.environ["SHEA_TEST_CONFIG_PRECEDENCE"] == "environment"


def test_build_runtime_starts(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    runtime = build_runtime(tmp_path / "shea.db", workspace=workspace)

    assert runtime.reconcile_app_plane() == []
    names = {tool.name for tool in runtime.tool_registry.list_tools()}
    assert {"filesystem.read", "filesystem.write"} <= names
    runtime.conn.close()


def test_handle_text_write_note_plan_only(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    runtime = build_runtime(tmp_path / "shea.db", workspace=workspace)

    result = runtime.interaction_service.handle_text(
        "write a note",
        run=False,
    )
    assert result.error is None
    assert result.task.state is TaskState.READY
    assert result.planning.plan is not None
    assert len(result.planning.plan.steps) == 1
    assert result.planning.plan.steps[0].tool == "filesystem.write"
    runtime.conn.close()


def test_handle_text_write_note_executes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    runtime = build_runtime(
        tmp_path / "shea.db",
        workspace=workspace,
        allow_unsafe_execution=True,
    )

    result = runtime.interaction_service.handle_text(
        "write a note",
        explicit_user_ack=True,
        run=True,
    )
    assert result.error is None
    assert result.run is not None
    assert result.task.state is TaskState.COMPLETED
    assert result.run.stopped_early is False
    note = workspace / "note.txt"
    assert note.is_file()
    assert note.read_text(encoding="utf-8") == "hello from shea"
    runtime.conn.close()


def test_handle_text_conversational_query_skips_task(tmp_path: Path) -> None:
    """A pure `type == "query"` intent never becomes a Task — no Plan,
    no Decision, no Execution/Verification ceremony for a greeting."""
    workspace = tmp_path / "ws"
    provider = ScriptedModelProvider()
    # 1st call: intent classification. 2nd call: the direct reply.
    provider.queue_response(
        ModelResponse(
            content="",
            structured_data={
                "type": "query",
                "goal": "respond conversationally",
                "confidence": 1.0,
            },
        )
    )
    provider.queue_response(ModelResponse(content="Hey! How can I help?"))

    runtime = build_runtime(tmp_path / "shea.db", workspace=workspace, model_provider=provider)

    result = runtime.interaction_service.handle_text("hi there", run=True)

    assert result.error is None
    assert result.task is None
    assert result.message == "Hey! How can I help?"
    assert result.planning is None
    assert result.run is None
    runtime.conn.close()


def test_handle_text_conversational_query_persists_request(tmp_path: Path) -> None:
    """Even the task-less fast path leaves a durable Request/Session
    trail — the whole point is Request no longer needs a Task to have
    somewhere to live."""
    workspace = tmp_path / "ws"
    provider = ScriptedModelProvider()
    provider.queue_response(
        ModelResponse(
            content="",
            structured_data={"type": "query", "goal": "respond conversationally", "confidence": 1.0},
        )
    )
    provider.queue_response(ModelResponse(content="Sure thing."))

    runtime = build_runtime(tmp_path / "shea.db", workspace=workspace, model_provider=provider)

    runtime.interaction_service.handle_text("hi there", session_id="convo-1", run=True)

    requests = runtime.request_repository.list_by_session("convo-1")
    assert len(requests) == 1
    assert requests[0].input == "hi there"

    # The Intent is owned by the Request even though no Task ever existed.
    intent = runtime.intent_repository.get_by_request(requests[0].request_id)
    assert intent is not None
    assert intent.type == "query"
    runtime.conn.close()


def test_conversational_turn_is_remembered_under_the_requests_profile(tmp_path: Path) -> None:
    """End to end: chat (no Task) -> reply recorded -> memory extracted
    and stored under the profile the request was made with."""
    provider = ScriptedModelProvider()
    provider.queue_response(
        ModelResponse(
            content="",
            structured_data={"type": "query", "goal": "chat", "confidence": 1.0},
        )
    )
    provider.queue_response(ModelResponse(content="Got it, JSON from now on."))
    provider.queue_response(
        ModelResponse(
            content="",
            structured_data={
                "memories": [{"type": "PREFERENCE", "content": "user wants JSON answers"}]
            },
        )
    )
    runtime = build_runtime(tmp_path / "shea.db", workspace=tmp_path / "ws", model_provider=provider)

    result = runtime.interaction_service.handle_text(
        "from now on answer in JSON", profile_id="work", run=True
    )

    assert result.task is None
    request = runtime.request_repository.list_by_session("cli")[0]
    assert request.profile_id == "work"
    assert request.response == "Got it, JSON from now on."
    stored = runtime.memory_service.list_active("work")
    assert [m.content for m in stored] == ["user wants JSON answers"]
    assert stored[0].provenance == request.request_id
    runtime.conn.close()


def _queue_conversational_turn(provider: ScriptedModelProvider, reply: str) -> None:
    """One model call: classification and the answer come back together."""
    provider.queue_response(
        ModelResponse(
            content="",
            structured_data={
                "type": "query",
                "goal": "chat",
                "confidence": 1.0,
                "reply": reply,
            },
        )
    )


def test_conversational_follow_up_sees_prior_turn(tmp_path: Path) -> None:
    """The second message references "its" and only makes sense with the
    first turn's Q&A in the prompt the model sees."""
    provider = ScriptedModelProvider()
    _queue_conversational_turn(provider, "Paris is the capital of France.")
    _queue_conversational_turn(provider, "About 2.1 million people.")

    runtime = build_runtime(tmp_path / "shea.db", workspace=tmp_path / "ws", model_provider=provider)

    first = runtime.interaction_service.handle_text("what's the capital of france", run=True)
    assert first.task is None
    second = runtime.interaction_service.handle_text("what's its population", run=True)
    assert second.task is None
    assert second.message == "About 2.1 million people."

    # Turn 2's single combined call carried turn 1's question and answer.
    turn_two_prompt = provider.calls[-1]
    assert "capital of france" in turn_two_prompt
    assert "Paris is the capital of France." in turn_two_prompt
    runtime.conn.close()


def test_plain_chat_costs_exactly_one_model_call_per_turn(tmp_path: Path) -> None:
    """Provider spend: classify + answer in one call, and nothing
    memorable means no extraction call either."""
    provider = ScriptedModelProvider()
    for i in range(3):
        _queue_conversational_turn(provider, f"reply-{i}")
    runtime = build_runtime(tmp_path / "shea.db", workspace=tmp_path / "ws", model_provider=provider)

    for question in ("what's the capital of france", "how tall is everest", "who wrote hamlet"):
        runtime.interaction_service.handle_text(question, run=True)

    assert len(provider.calls) == 3
    runtime.conn.close()


def test_memorable_turn_costs_one_extra_call(tmp_path: Path) -> None:
    provider = ScriptedModelProvider()
    _queue_conversational_turn(provider, "Noted.")
    provider.queue_response(
        ModelResponse(
            content="",
            structured_data={"memories": [{"type": "PREFERENCE", "content": "likes tea"}]},
        )
    )
    runtime = build_runtime(tmp_path / "shea.db", workspace=tmp_path / "ws", model_provider=provider)

    runtime.interaction_service.handle_text("I always drink tea in the morning", run=True)

    assert len(provider.calls) == 2  # combined classify+answer, then extraction
    assert [m.content for m in runtime.memory_service.list_active("system")] == ["likes tea"]
    runtime.conn.close()


def test_reply_missing_from_combined_call_falls_back_to_a_dedicated_answer_call(
    tmp_path: Path,
) -> None:
    provider = ScriptedModelProvider()
    provider.queue_response(
        ModelResponse(
            content="", structured_data={"type": "query", "goal": "chat", "confidence": 1.0}
        )
    )
    provider.queue_response(ModelResponse(content="fallback answer"))
    runtime = build_runtime(tmp_path / "shea.db", workspace=tmp_path / "ws", model_provider=provider)

    result = runtime.interaction_service.handle_text("how tall is everest", run=True)

    assert result.message == "fallback answer"
    assert len(provider.calls) == 2
    runtime.conn.close()


def test_thinking_from_the_combined_call_is_still_shown(tmp_path: Path) -> None:
    provider = ScriptedModelProvider()
    provider.queue_response(
        ModelResponse(
            content="",
            structured_data={"type": "query", "goal": "chat", "confidence": 1.0, "reply": "Hi!"},
            metadata={"thinking": "user greeted me"},
        )
    )
    runtime = build_runtime(tmp_path / "shea.db", workspace=tmp_path / "ws", model_provider=provider)

    result = runtime.interaction_service.handle_text("hello", run=True)

    assert result.message is not None
    assert "user greeted me" in result.message
    assert result.message.endswith("Hi!")
    runtime.conn.close()


def _decision_audits(runtime: object) -> list[tuple[str, str]]:
    rows = runtime.conn.execute(  # type: ignore[attr-defined]
        "SELECT result, metadata FROM audit_events WHERE event_type = 'decision.evaluated'"
    ).fetchall()
    return [(r["result"], r["metadata"]) for r in rows]


def test_conversational_memory_write_is_policy_checked_and_audited(tmp_path: Path) -> None:
    provider = ScriptedModelProvider()
    _queue_conversational_turn(provider, "Noted.")
    provider.queue_response(
        ModelResponse(content="", structured_data={"memories": [{"type": "FACT", "content": "x"}]})
    )
    runtime = build_runtime(tmp_path / "shea.db", workspace=tmp_path / "ws", model_provider=provider)

    runtime.interaction_service.handle_text("remember that I live in Nairobi", run=True)

    audits = _decision_audits(runtime)
    assert len(audits) == 1
    result, metadata = audits[0]
    assert result == "allowed"
    assert "REQUIRES_AUTHORIZATION" in metadata and "SAFE" in metadata
    runtime.conn.close()


def test_withheld_acknowledgement_blocks_remembering(tmp_path: Path) -> None:
    provider = ScriptedModelProvider()
    _queue_conversational_turn(provider, "Noted.")
    runtime = build_runtime(tmp_path / "shea.db", workspace=tmp_path / "ws", model_provider=provider)

    result = runtime.interaction_service.handle_text(
        "remember that I live in Nairobi", explicit_user_ack=False, run=True
    )

    assert result.message == "Noted."  # the reply itself is unaffected
    assert len(provider.calls) == 1  # no extraction call was made
    assert runtime.memory_service.list_active("system") == []
    assert [r for r, _ in _decision_audits(runtime)] == ["blocked"]
    runtime.conn.close()


def test_task_planning_prompt_includes_recent_conversation(tmp_path: Path) -> None:
    """Session context reaches the task path too: a follow-up task can
    say "it" and the planner prompt carries what "it" was."""
    provider = ScriptedModelProvider()
    _queue_conversational_turn(provider, "The file is called report.txt.")
    provider.queue_response(
        ModelResponse(
            content="",
            structured_data={"type": "task", "goal": "delete that file", "confidence": 1.0},
        )
    )
    provider.queue_response(
        ModelResponse(
            content="",
            structured_data={
                "steps": [
                    {"tool": "system.reply", "action": "reply", "arguments": {"message": "ok"}}
                ]
            },
        )
    )
    runtime = build_runtime(tmp_path / "shea.db", workspace=tmp_path / "ws", model_provider=provider)

    runtime.interaction_service.handle_text("what is the file called", run=False)
    result = runtime.interaction_service.handle_text("delete that file", run=False)

    assert result.task is not None
    plan_prompt = provider.calls[-1]
    assert "report.txt" in plan_prompt
    assert "Goal: delete that file" in plan_prompt
    runtime.conn.close()


def test_conversational_context_window_is_bounded(tmp_path: Path) -> None:
    provider = ScriptedModelProvider()
    for i in range(8):
        _queue_conversational_turn(provider, f"reply-{i}")
    runtime = build_runtime(tmp_path / "shea.db", workspace=tmp_path / "ws", model_provider=provider)

    for i in range(8):
        runtime.interaction_service.handle_text(f"turn {i}", run=True)

    last_prompt = provider.calls[-1]
    assert "turn 0" not in last_prompt  # outside the 6-turn window
    assert "turn 1" in last_prompt
    runtime.conn.close()
