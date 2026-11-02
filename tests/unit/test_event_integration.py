from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shea.audit.recorder import AuditRecorder
from shea.contracts.models import ToolRequest
from shea.core.orchestrator import Orchestrator
from shea.events.bus import EventBus
from shea.events.channels import SecurityChannel, TaskChannel
from shea.events.contracts import Event
from shea.persistence.sqlite.audit_sink import SqliteAuditSink
from shea.persistence.sqlite.connection import open_connection
from shea.persistence.sqlite.migrator import run_migrations
from shea.persistence.sqlite.task_repository import SqliteTaskRepository
from shea.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from shea.security.filesystem_policy import FilesystemPolicy
from shea.security.gate import SecurityGate
from shea.security.injection import PromptInjectionDetector
from shea.security.network_policy import NetworkPolicy
from shea.security.service import SecurityService
from shea.state_machine.transitions import IllegalTransitionError


class FakeClock:
    def now(self) -> datetime:
        return datetime(2025, 1, 1, tzinfo=UTC)


class SequentialIdGenerator:
    def __init__(self) -> None:
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return f"id-{self._counter}"


@pytest.fixture
def wired() -> tuple[EventBus, Orchestrator, SecurityService]:
    """Wire up a minimal Orchestrator + SecurityService with EventBus."""
    conn = open_connection(":memory:")
    run_migrations(conn)
    uow = SqliteUnitOfWork(conn)
    clock = FakeClock()
    ids = SequentialIdGenerator()
    bus = EventBus()
    task_channel = TaskChannel(bus, clock, ids)
    security_channel = SecurityChannel(bus, clock, ids)

    task_repo = SqliteTaskRepository(conn, unit_of_work=uow)
    audit_sink = SqliteAuditSink(conn, unit_of_work=uow)
    audit = AuditRecorder(sink=audit_sink, clock=clock, id_generator=ids)

    orchestrator = Orchestrator(
        task_repository=task_repo,
        audit=audit,
        clock=clock,
        id_generator=ids,
        unit_of_work=uow,
        task_channel=task_channel,
        receipt_acceptance_verifier=lambda task_id, receipt_id: (
            receipt_id == f"accepted:{task_id}"
        ),
    )
    security_service = SecurityService(
        gate=SecurityGate(
            network_policy=NetworkPolicy(),
            filesystem_policy=FilesystemPolicy(allowed_roots=frozenset({"/tmp"})),
        ),
        injection_detector=PromptInjectionDetector(),
        orchestrator=orchestrator,
        audit=audit,
        unit_of_work=uow,
        security_channel=security_channel,
    )

    return bus, orchestrator, security_service


class TestOrchestratorEvents:
    def test_create_task_emits_event(
        self,
        wired: tuple[EventBus, Orchestrator, SecurityService],
    ) -> None:
        bus, orchestrator, _ = wired
        events: list[Event] = []
        bus.subscribe("task.*", events.append)

        task = orchestrator.create_task(session_id="s1", request_id="r1")

        assert len(events) == 1
        assert events[0].event_type == "task.created"
        assert events[0].payload["task_id"] == task.id

    def test_advance_emits_state_changed(
        self,
        wired: tuple[EventBus, Orchestrator, SecurityService],
    ) -> None:
        bus, orchestrator, _ = wired
        events: list[Event] = []
        bus.subscribe("task.state_changed", events.append)

        task = orchestrator.create_task(session_id="s1", request_id="r1")
        orchestrator.advance(task.id, "start_planning")

        assert len(events) == 1
        e = events[0]
        assert e.payload["from_state"] == "CREATED"
        assert e.payload["to_state"] == "PLANNING"
        assert e.payload["trigger"] == "start_planning"

    def test_attach_plan_emits_event(
        self,
        wired: tuple[EventBus, Orchestrator, SecurityService],
    ) -> None:
        bus, orchestrator, _ = wired
        events: list[Event] = []
        bus.subscribe("task.plan_attached", events.append)

        task = orchestrator.create_task(session_id="s1", request_id="r1")
        orchestrator.attach_plan(task.id, "plan-42")

        assert len(events) == 1
        assert events[0].payload["plan_id"] == "plan-42"

    def test_rejected_advance_does_not_emit(
        self,
        wired: tuple[EventBus, Orchestrator, SecurityService],
    ) -> None:
        bus, orchestrator, _ = wired
        events: list[Event] = []
        bus.subscribe("task.*", events.append)

        task = orchestrator.create_task(session_id="s1", request_id="r1")
        events.clear()  # clear creation event

        with pytest.raises(IllegalTransitionError):
            orchestrator.advance(task.id, "verified")  # illegal from CREATED

        # Only illegal transitions should NOT emit a state_changed event
        assert not any(e.event_type == "task.state_changed" for e in events)


class TestSecurityServiceEvents:
    def test_violation_emits_event(
        self,
        wired: tuple[EventBus, Orchestrator, SecurityService],
    ) -> None:
        bus, orchestrator, security_service = wired
        events: list[Event] = []
        bus.subscribe("security.*", events.append)

        # Create a task in RUNNING state
        task = orchestrator.create_task(session_id="s1", request_id="r1")
        orchestrator.advance(task.id, "start_planning")
        orchestrator.advance(task.id, "plan_ready")
        orchestrator.advance(
            task.id, "authorize_and_run", receipt_id=f"accepted:{task.id}"
        )
        task = orchestrator.get_task(task.id)

        # Try to access a private IP (SSRF)
        request = ToolRequest(
            request_id="r1",
            tool="http.fetch",
            action="fetch",
            arguments={"url": "http://169.254.169.254/latest/meta-data/"},
        )

        from shea.security.exceptions import SecurityViolationError
        with pytest.raises(SecurityViolationError):
            security_service.enforce(task, request)

        violation_events = [e for e in events if e.event_type == "security.violation"]
        assert len(violation_events) == 1
        assert violation_events[0].payload["category"] == "ssrf"

    def test_request_cleared_emits_event(
        self,
        wired: tuple[EventBus, Orchestrator, SecurityService],
    ) -> None:
        bus, orchestrator, security_service = wired
        events: list[Event] = []
        bus.subscribe("security.request_cleared", events.append)

        task = orchestrator.create_task(session_id="s1", request_id="r1")
        orchestrator.advance(task.id, "start_planning")
        orchestrator.advance(task.id, "plan_ready")
        orchestrator.advance(
            task.id, "authorize_and_run", receipt_id=f"accepted:{task.id}"
        )
        task = orchestrator.get_task(task.id)

        # A safe request
        request = ToolRequest(
            request_id="r1",
            tool="filesystem.read",
            action="read",
            arguments={"path": "/tmp/test.txt"},
        )

        security_service.enforce(task, request)

        assert len(events) == 1
        assert events[0].payload["tool"] == "filesystem.read"