"""Durable project store for the generic scientific research runtime.

The project directory is the unit of recovery. Mutable snapshots use atomic
replacement, while audit records are append-only JSON Lines files. Artifacts
are copied into a content-addressed area so later edits to a work file cannot
silently change the evidence that a decision reviewed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from .research_contracts import (
    ArtifactHead,
    ArtifactRecord,
    ArtifactVersion,
    AssessmentRecord,
    AssessmentResult,
    AutonomyMode,
    DecisionEvent,
    FailureClass,
    DomainActivityRecord,
    ForkDirective,
    ForkMode,
    PlanBranch,
    PlanBranchStatus,
    ProjectEvent,
    ProjectState,
    RepairAction,
    RepairAuthorization,
    RepairDirective,
    RepairRequest,
    RepairResolution,
    ResearchProjectControl,
    RepairResolutionStatus,
    ResearchPlan,
    ResearchPlanRevision,
    ResearchProjectSpec,
    ReviewTarget,
    WorkAttempt,
    WorkAttemptStatus,
    WorkItemHead,
    WorkItemResult,
    WorkItemStatus,
    WorkerLease,
)
from .research_projection import DomainActivityProjection, trace_event_digest
from .research_repair import (
    DOMAIN_REPAIR_POLICY,
    active_assessments,
    active_item_ids,
    canonical_sha256,
    chain_final_replacement,
    effective_plan,
    fork_affected_item_ids,
    project_snapshot_digest,
    verify_domain_repair_policy,
    work_item_result_digest,
)


T = TypeVar("T", bound=BaseModel)
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Integrity whitelists are derived from the policy layer so a new typed
# domain repair cannot silently fail the store's consistency gates.
DOMAIN_REPAIR_ACTION_VALUES = frozenset(action.value for action in DOMAIN_REPAIR_POLICY)
OVERLAY_REVISION_OPERATIONS = DOMAIN_REPAIR_ACTION_VALUES - {RepairAction.SWITCH_DATASET_SAME_CONTEXT.value}


class ProjectBusyError(RuntimeError):
    pass


LEASE_DURATION = timedelta(hours=4)


class ResearchProjectStore:
    """Filesystem-backed store for one research project."""

    def __init__(self, projects_dir: Path | str, project_id: str):
        self.project_id = self._safe_component(project_id, "project_id")
        self.projects_dir = Path(projects_dir).expanduser().resolve()
        self.project_dir = (self.projects_dir / self.project_id).resolve()
        if not self.project_dir.is_relative_to(self.projects_dir):
            raise ValueError("project directory escapes projects root")
        self._lock = threading.RLock()
        # Process-local JSONL tail cursors: name -> ((size, mtime_ns), count,
        # last_sequence). A cursor is trusted only while the file stat still
        # matches; appends made by this instance refresh it, so repeated
        # appends never re-read the whole ledger. External writers fall back
        # to a full parse (necessary for cross-process consistency).
        self._jsonl_tail: dict[str, tuple[tuple[int, int], int, int]] = {}
        self._domain_activity_by_key: dict[tuple[str, str], DomainActivityRecord] | None = None

    @staticmethod
    def _safe_component(value: str, label: str) -> str:
        if not _SAFE_COMPONENT.fullmatch(value):
            raise ValueError(f"unsafe {label}: {value!r}")
        return value

    @staticmethod
    def _new_contract_id(prefix: str) -> str:
        return f"{prefix}-{uuid4().hex[:24]}"

    @staticmethod
    def _payload(value: BaseModel | dict[str, Any] | list[Any]) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, list):
            return [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value]
        return value

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Persist a rename where the platform permits directory fsync."""
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_json_atomic(self, path: Path, value: BaseModel | dict[str, Any] | list[Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self._payload(value), ensure_ascii=False, indent=2) + "\n"
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            self._fsync_directory(path.parent)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return path

    def _append_jsonl(self, name: str, value: BaseModel) -> None:
        path = self.project_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(value.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _read_jsonl(self, name: str, model: type[T]) -> list[T]:
        path = self.project_dir / name
        if not path.exists():
            return []
        records: list[T] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(model.model_validate_json(line))
                except Exception as exc:
                    raise ValueError(f"invalid {name} record at line {line_number}: {exc}") from exc
        return records

    def _jsonl_tail_cursor(self, name: str, model: type[T]) -> tuple[int, int]:
        """Return (record_count, last_sequence) for a sequence-numbered JSONL file.

        Uses a process-local tail cursor to avoid full re-reads on repeated
        appends; falls back to a full parse only when the file stat no longer
        matches the cursor (e.g. another writer appended).
        """
        path = self.project_dir / name
        try:
            stat = path.stat()
        except FileNotFoundError:
            return 0, 0
        cached = self._jsonl_tail.get(name)
        if cached is not None and cached[0] == (stat.st_size, stat.st_mtime_ns):
            return cached[1], cached[2]
        records = self._read_jsonl(name, model)
        count = len(records)
        last_sequence = records[-1].sequence if records else 0
        self._jsonl_tail[name] = ((stat.st_size, stat.st_mtime_ns), count, last_sequence)
        return count, last_sequence

    def _note_jsonl_appended(self, name: str, count: int, last_sequence: int) -> None:
        """Refresh the tail cursor after this instance appended one row."""
        path = self.project_dir / name
        try:
            stat = path.stat()
        except FileNotFoundError:
            return
        self._jsonl_tail[name] = ((stat.st_size, stat.st_mtime_ns), count, last_sequence)

    def _domain_activity_index(self) -> dict[tuple[str, str], DomainActivityRecord]:
        """Return child_run_id/source_trace_id -> record for the activity ledger.

        Built once per store instance and updated on appends; a changed file
        stat (external writer) forces a full reload.
        """
        name = "domain_activities.jsonl"
        path = self.project_dir / name
        try:
            stat = path.stat()
        except FileNotFoundError:
            self._domain_activity_by_key = {}
            return self._domain_activity_by_key
        cached = self._jsonl_tail.get(name)
        if (self._domain_activity_by_key is not None
                and cached is not None and cached[0] == (stat.st_size, stat.st_mtime_ns)):
            return self._domain_activity_by_key
        records = self._read_jsonl(name, DomainActivityRecord)
        self._domain_activity_by_key = {
            (row.child_run_id, row.source_trace_id): row for row in records
        }
        self._jsonl_tail[name] = (
            (stat.st_size, stat.st_mtime_ns),
            len(records),
            records[-1].sequence if records else 0,
        )
        return self._domain_activity_by_key

    @staticmethod
    def _read_model(path: Path, model: type[T]) -> T | None:
        if not path.exists():
            return None
        return model.model_validate_json(path.read_text(encoding="utf-8"))

    def _save_immutable(self, path: Path, value: BaseModel, label: str) -> None:
        existing = self._read_model(path, type(value))
        if existing is not None:
            if (existing.model_dump(mode="json", exclude={"created_at"})
                    != value.model_dump(mode="json", exclude={"created_at"})):
                raise ValueError(f"{label} is immutable once written")
            return
        self._write_json_atomic(path, value)

    def create(self, spec: ResearchProjectSpec) -> bool:
        """Atomically reserve a project id; return False for an identical existing spec."""
        if spec.project_id != self.project_id:
            raise ValueError("project spec id does not match store project id")
        with self._lock:
            self.project_dir.mkdir(parents=True, exist_ok=True)
            path = self.project_dir / "project_spec.json"
            content = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
            try:
                with path.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._fsync_directory(path.parent)
                return True
            except FileExistsError:
                existing = self.load_spec()
                if (existing is None
                        or existing.model_dump(mode="json", exclude={"created_at"})
                        != spec.model_dump(mode="json", exclude={"created_at"})):
                    raise ValueError("project spec is immutable once written")
                return False

    def load_spec(self) -> ResearchProjectSpec | None:
        return self._read_model(self.project_dir / "project_spec.json", ResearchProjectSpec)

    def save_plan(self, plan: ResearchPlan) -> None:
        if plan.project_id != self.project_id:
            raise ValueError("research plan project id does not match store project id")
        with self._lock:
            self._save_immutable(self.project_dir / "research_plan.json", plan, "research plan")

    def load_plan(self) -> ResearchPlan | None:
        return self._read_model(self.project_dir / "research_plan.json", ResearchPlan)

    def append_repair_request(self, request: RepairRequest) -> None:
        if request.project_id != self.project_id:
            raise ValueError("repair request project id does not match store project id")
        request_id = self._safe_component(request.repair_request_id, "repair request id")
        with self._lock:
            self._save_immutable(
                self.project_dir / "repair_requests" / f"{request_id}.json",
                request,
                "repair request",
            )

    def read_repair_requests(self) -> list[RepairRequest]:
        root = self.project_dir / "repair_requests"
        if not root.exists():
            return []
        return sorted(
            (RepairRequest.model_validate_json(path.read_text(encoding="utf-8")) for path in root.glob("*.json")),
            key=lambda row: (row.created_at, row.repair_request_id),
        )

    def append_plan_revision(self, revision: ResearchPlanRevision) -> None:
        if revision.project_id != self.project_id:
            raise ValueError("plan revision project id does not match store project id")
        revision_id = self._safe_component(revision.revision_id, "plan revision id")
        with self._lock:
            existing = self.read_plan_revisions()
            same_number = next((row for row in existing if row.revision_number == revision.revision_number), None)
            if same_number is not None and same_number.revision_id != revision.revision_id:
                raise ValueError("plan revision number already has different immutable content")
            self._save_immutable(
                self.project_dir / "plan_revisions" / f"{revision_id}.json",
                revision,
                "plan revision",
            )

    def read_plan_revisions(self) -> list[ResearchPlanRevision]:
        root = self.project_dir / "plan_revisions"
        if not root.exists():
            return []
        return sorted(
            (ResearchPlanRevision.model_validate_json(path.read_text(encoding="utf-8"))
             for path in root.glob("*.json")),
            key=lambda row: row.revision_number,
        )

    def load_effective_plan(self) -> ResearchPlan | None:
        base = self.load_plan()
        return effective_plan(base, self.read_plan_revisions()) if base is not None else None

    def append_repair_resolution(self, resolution: RepairResolution) -> None:
        if resolution.project_id != self.project_id:
            raise ValueError("repair resolution project id does not match store project id")
        resolution_id = self._safe_component(resolution.resolution_id, "repair resolution id")
        with self._lock:
            self._save_immutable(
                self.project_dir / "repair_resolutions" / f"{resolution_id}.json",
                resolution,
                "repair resolution",
            )

    def replace_repair_resolution(
        self, resolution: RepairResolution, previous_resolution_id: str,
    ) -> None:
        """Atomically replace an earlier (unresolved) resolution for the same repair.

        A repair whose subgraph is later superseded by another repair revision may
        transition from unresolved to resolved once the final active chain passes
        independent review. The previous file is removed and the new immutable
        resolution is written under the same store lock.
        """
        if resolution.project_id != self.project_id:
            raise ValueError("repair resolution project id does not match store project id")
        previous = self._safe_component(previous_resolution_id, "repair resolution id")
        with self._lock:
            root = self.project_dir / "repair_resolutions"
            old_path = root / f"{previous}.json"
            if not old_path.exists():
                raise ValueError("previous repair resolution is missing")
            old = RepairResolution.model_validate_json(old_path.read_text(encoding="utf-8"))
            if (old.repair_request_id != resolution.repair_request_id
                    or old.revision_id != resolution.revision_id):
                raise ValueError("replacement resolution does not match the previous resolution")
            old_path.unlink()
            self._save_immutable(
                root / f"{self._safe_component(resolution.resolution_id, 'repair resolution id')}.json",
                resolution, "repair resolution",
            )

    def read_repair_resolutions(self) -> list[RepairResolution]:
        root = self.project_dir / "repair_resolutions"
        if not root.exists():
            return []
        return sorted(
            (RepairResolution.model_validate_json(path.read_text(encoding="utf-8"))
             for path in root.glob("*.json")),
            key=lambda row: (row.created_at, row.resolution_id),
        )

    def save_state(self, state: ProjectState) -> None:
        if state.project_id != self.project_id:
            raise ValueError("project state id does not match store project id")
        with self._lock:
            self._write_json_atomic(self.project_dir / "project_state.json", state)

    def load_state(self) -> ProjectState | None:
        return self._read_model(self.project_dir / "project_state.json", ProjectState)
    def save_control(self, control: ResearchProjectControl) -> None:
        """Atomically record the latest user control intent (pause/cancel/resume).

        The request is additionally written to the append-only event ledger by
        the service before it is saved here, so the ledger remains the durable
        audit trail and this file is only the current intent.
        """
        if control.project_id != self.project_id:
            raise ValueError("control project id does not match store project id")
        with self._lock:
            self._write_json_atomic(self.project_dir / "control.json", control)

    def load_control(self) -> ResearchProjectControl | None:
        return self._read_model(self.project_dir / "control.json", ResearchProjectControl)

    def clear_control(self) -> None:
        """Remove the current control intent after the runtime consumed it."""
        with self._lock:
            path = self.project_dir / "control.json"
            if path.exists():
                path.unlink()
            self._fsync_directory(self.project_dir)


    def save_work_item_result(self, result: WorkItemResult) -> None:
        item_id = self._safe_component(result.item_id, "work item id")
        with self._lock:
            self._write_json_atomic(self.project_dir / "work_items" / item_id / "result.json", result)

    def load_work_item_results(self) -> dict[str, WorkItemResult]:
        root = self.project_dir / "work_items"
        if not root.exists():
            return {}
        results: dict[str, WorkItemResult] = {}
        for path in sorted(root.glob("*/result.json")):
            item_id = self._safe_component(path.parent.name, "work item id")
            result = WorkItemResult.model_validate_json(path.read_text(encoding="utf-8"))
            if result.item_id != item_id:
                raise ValueError(f"work item result path/id mismatch: {item_id} != {result.item_id}")
            results[item_id] = result
        return results

    def update_work_item_head(
        self,
        head: WorkItemHead,
        *,
        expected_version: int | None = None,
    ) -> WorkItemHead:
        """CAS-update the committed head of one work item.

        The caller passes the next version it intends to write; the store
        verifies it against the durable head so a stale writer can never
        silently overwrite a newer commit. Replaying the already committed
        attempt/digest is an idempotent no-op.
        """
        if head.project_id != self.project_id:
            raise ValueError("work item head project id does not match store project id")
        item_id = self._safe_component(head.work_item_id, "work item id")
        with self._lock:
            current = self.read_work_item_head(item_id)
            if (current is not None
                    and current.attempt_id == head.attempt_id
                    and current.result_digest == head.result_digest):
                return current
            expected = current.version if (current is not None and expected_version is None) else expected_version
            if current is None:
                if expected_version not in (None, 1) or head.version != 1:
                    raise ValueError("first work item head must be version 1")
            else:
                if current.version != expected:
                    raise ValueError(
                        f"work item head CAS conflict: expected version {expected}, found {current.version}"
                    )
                if head.version != current.version + 1:
                    raise ValueError(
                        f"work item head version must follow the current head: "
                        f"expected {current.version + 1}, got {head.version}"
                    )
            committed = head.model_copy(update={
                "supersedes_head_id": current.head_id if current is not None else None,
            })
            self._write_json_atomic(
                self.project_dir / "work_items" / item_id / "head.json", committed,
            )
            return committed

    def read_work_item_head(self, work_item_id: str) -> WorkItemHead | None:
        item_id = self._safe_component(work_item_id, "work item id")
        return self._read_model(self.project_dir / "work_items" / item_id / "head.json", WorkItemHead)

    def read_work_item_heads(self) -> list[WorkItemHead]:
        root = self.project_dir / "work_items"
        if not root.exists():
            return []
        heads: list[WorkItemHead] = []
        for path in sorted(root.glob("*/head.json")):
            head = WorkItemHead.model_validate_json(path.read_text(encoding="utf-8"))
            if head.work_item_id != path.parent.name:
                raise ValueError("work item head path/id mismatch")
            heads.append(head)
        return heads

    def append_attempt(self, attempt: WorkAttempt) -> None:
        if attempt.project_id != self.project_id:
            raise ValueError("attempt project id does not match store project id")
        attempt_id = self._safe_component(attempt.attempt_id, "attempt id")
        with self._lock:
            # Complexity boundary (P1-3 family): attempts are scanned per work
            # item; the contract caps attempt_number at 3, so this stays
            # effectively O(1) and needs no global tail index.
            prior = self.read_attempts(attempt.work_item_id)
            if any(row.attempt_id == attempt.attempt_id for row in prior):
                raise ValueError("attempt id already exists")
            if attempt.attempt_number != len(prior) + 1:
                raise ValueError(
                    f"attempt number {attempt.attempt_number} does not follow {len(prior)} prior attempts"
                )
            self._save_immutable(
                self.project_dir / "work_items" / attempt.work_item_id / "attempts" / f"{attempt_id}.json",
                attempt,
                "work attempt",
            )

    def read_attempts(self, work_item_id: str | None = None) -> list[WorkAttempt]:
        root = self.project_dir / "work_items"
        if not root.exists():
            return []
        records: list[WorkAttempt] = []
        for path in sorted(root.glob("*/attempts/*.json")):
            if path.name.endswith(".result.json"):
                continue
            record = WorkAttempt.model_validate_json(path.read_text(encoding="utf-8"))
            if work_item_id is not None and record.work_item_id != work_item_id:
                continue
            records.append(record)
        return sorted(records, key=lambda row: (row.work_item_id, row.attempt_number, row.attempt_id))

    def current_attempt(self, work_item_id: str) -> WorkAttempt | None:
        records = self.read_attempts(work_item_id)
        return records[-1] if records else None

    def save_attempt_result(self, attempt: WorkAttempt, result: WorkItemResult) -> None:
        """Persist the immutable result payload bound to one terminal attempt."""
        if attempt.project_id != self.project_id:
            raise ValueError("attempt project id does not match store project id")
        if result.item_id != attempt.work_item_id:
            raise ValueError("attempt result item id does not match its attempt")
        if attempt.output_digest is None:
            raise ValueError("cannot snapshot a non-terminal attempt result")
        if work_item_result_digest(result) != attempt.output_digest:
            raise ValueError("attempt result digest does not match the immutable attempt record")
        path = self.project_dir / "work_items" / attempt.work_item_id / "attempts" / f"{attempt.attempt_id}.result.json"
        with self._lock:
            if path.exists():
                existing = WorkItemResult.model_validate_json(path.read_text(encoding="utf-8"))
                if work_item_result_digest(existing) != attempt.output_digest:
                    raise ValueError("attempt result snapshot is immutable and already differs")
                return
            self._write_json_atomic(path, result)

    def load_attempt_result(self, attempt_id: str) -> WorkItemResult | None:
        attempt = next((row for row in self.read_attempts() if row.attempt_id == attempt_id), None)
        if attempt is None:
            return None
        path = self.project_dir / "work_items" / attempt.work_item_id / "attempts" / f"{attempt.attempt_id}.result.json"
        if not path.exists():
            return None
        return WorkItemResult.model_validate_json(path.read_text(encoding="utf-8"))

    def recover_work_item_results(self) -> dict[str, WorkItemResult]:
        """Return the authoritative committed results after an interrupted run.

        Heads are the deterministic recovery anchor: terminal attempts backfill
        missing heads (legacy projects and crash windows), and each committed
        head repairs the working result.json mirror from its immutable attempt
        snapshot. Business state is never guessed from Trace events. Items
        without any attempt (legacy manual saves) are returned unchanged.
        """
        with self._lock:
            results = self.load_work_item_results()
            attempts = self.read_attempts()
            latest_by_item: dict[str, WorkAttempt] = {}
            for attempt in attempts:
                if attempt.output_digest is None:
                    continue
                current = latest_by_item.get(attempt.work_item_id)
                if current is None or attempt.attempt_number > current.attempt_number:
                    latest_by_item[attempt.work_item_id] = attempt
            for item_id, attempt in sorted(latest_by_item.items()):
                current_head = self.read_work_item_head(item_id)
                if current_head is not None and current_head.attempt_id == attempt.attempt_id:
                    continue
                if current_head is not None and current_head.result_digest == attempt.output_digest:
                    continue
                snapshot = self.load_attempt_result(attempt.attempt_id)
                if snapshot is None:
                    continue
                self.update_work_item_head(WorkItemHead(
                    project_id=self.project_id,
                    work_item_id=item_id,
                    attempt_id=attempt.attempt_id,
                    result_digest=attempt.output_digest,
                    status=snapshot.status,
                    version=(current_head.version + 1) if current_head else 1,
                    supersedes_head_id=current_head.head_id if current_head is not None else None,
                ), expected_version=current_head.version if current_head else None)
            for head in self.read_work_item_heads():
                snapshot = self.load_attempt_result(head.attempt_id)
                if snapshot is None:
                    continue
                path = self.project_dir / "work_items" / head.work_item_id / "result.json"
                existing = self._read_model(path, WorkItemResult)
                if existing is None or work_item_result_digest(existing) != head.result_digest:
                    self._write_json_atomic(path, snapshot)
                results[head.work_item_id] = snapshot
            return results

    def append_lease(self, lease: WorkerLease) -> None:
        if lease.project_id != self.project_id:
            raise ValueError("lease project id does not match store project id")
        with self._lock:
            # Complexity boundary (P1-3 family): leases are re-read per append
            # for cross-process release/heartbeat safety; lease volume per work
            # item is small, so a tail index is deferred.
            existing = self.read_leases(lease.work_item_id)
            active = [row for row in existing if row.released_at is None]
            if active:
                raise ValueError("work item already has an active worker lease")
            if any(row.lease_id == lease.lease_id for row in existing):
                raise ValueError("lease id already exists")
            self._append_jsonl(
                self.project_dir / "work_items" / lease.work_item_id / "leases.jsonl",
                lease,
            )

    def read_leases(self, work_item_id: str | None = None) -> list[WorkerLease]:
        """Return the latest record per lease id (leases are append-only snapshots)."""
        root = self.project_dir / "work_items"
        if not root.exists():
            return []
        records: list[WorkerLease] = []
        for path in sorted(root.glob("*/leases.jsonl")):
            records.extend(self._read_jsonl(path.relative_to(self.project_dir).as_posix(), WorkerLease))
        latest: dict[str, WorkerLease] = {}
        for row in records:
            latest[row.lease_id] = row
        records = list(latest.values())
        if work_item_id is not None:
            records = [row for row in records if row.work_item_id == work_item_id]
        return sorted(records, key=lambda row: (row.work_item_id, row.acquired_at, row.lease_id))

    def release_lease(self, lease_id: str, released_at: str | None = None) -> WorkerLease:
        safe_lease = self._safe_component(lease_id, "lease id")
        with self._lock:
            target = next((row for row in self.read_leases() if row.lease_id == safe_lease), None)
            if target is None:
                raise ValueError("lease not found")
            if target.released_at is not None:
                return target
            released = target.model_copy(update={
                "released_at": released_at or target.heartbeat_at,
                "heartbeat_at": released_at or target.heartbeat_at,
            })
            self._append_jsonl(
                self.project_dir / "work_items" / target.work_item_id / "leases.jsonl",
                released,
            )
            return released

    def heartbeat_lease(self, lease_id: str) -> WorkerLease:
        """Refresh a live worker lease heartbeat and its expiry window."""
        safe_lease = self._safe_component(lease_id, "lease id")
        with self._lock:
            target = next((row for row in self.read_leases() if row.lease_id == safe_lease), None)
            if target is None:
                raise ValueError("lease not found")
            if target.released_at is not None:
                return target
            now = datetime.now(timezone.utc)
            refreshed = target.model_copy(update={
                "heartbeat_at": now.isoformat(),
                "expires_at": (now + LEASE_DURATION).isoformat(),
            })
            self._append_jsonl(
                self.project_dir / "work_items" / target.work_item_id / "leases.jsonl",
                refreshed,
            )
            return refreshed

    def append_artifact_version(self, version: ArtifactVersion) -> None:
        if version.project_id != self.project_id:
            raise ValueError("artifact version project id does not match store project id")
        with self._lock:
            existing = self.read_artifact_versions(version.artifact_id)
            if any(row.version_id == version.version_id for row in existing):
                raise ValueError("artifact version id already exists")
            if version.version != (existing[-1].version + 1 if existing else 1):
                raise ValueError("artifact version sequence must be contiguous")
            if version.sha256 == (existing[-1].sha256 if existing else None):
                raise ValueError("artifact version must change content")
            self._append_jsonl("artifact_versions.jsonl", version)

    def read_artifact_versions(self, artifact_id: str | None = None) -> list[ArtifactVersion]:
        records = self._read_jsonl("artifact_versions.jsonl", ArtifactVersion)
        if artifact_id is None:
            return records
        return [row for row in records if row.artifact_id == artifact_id]

    def current_artifact_version(self, artifact_id: str) -> ArtifactVersion | None:
        records = self.read_artifact_versions(artifact_id)
        return records[-1] if records else None

    def update_artifact_head(
        self,
        head: ArtifactHead,
        *,
        expected_version: int | None = None,
    ) -> ArtifactHead:
        """CAS-update the active version head of one logical artifact.

        Rows are append-only so head transitions remain auditable; the latest
        row per artifact id is the active head. Replaying the already active
        version is an idempotent no-op.
        """
        if head.project_id != self.project_id:
            raise ValueError("artifact head project id does not match store project id")
        with self._lock:
            current = self.current_artifact_head(head.artifact_id)
            if current is not None and current.version_id == head.version_id:
                return current
            expected = current.version if (current is not None and expected_version is None) else expected_version
            if current is None:
                if expected_version not in (None, 1) or head.version != 1:
                    raise ValueError("first artifact head must be version 1")
            else:
                if current.version != expected:
                    raise ValueError(
                        f"artifact head CAS conflict: expected version {expected}, found {current.version}"
                    )
                if head.version != current.version + 1:
                    raise ValueError(
                        f"artifact head version must follow the current head: "
                        f"expected {current.version + 1}, got {head.version}"
                    )
            self._append_jsonl("artifact_heads.jsonl", head)
            return head

    def read_artifact_heads(self) -> list[ArtifactHead]:
        """Return the active head per logical artifact (append-only rows are history)."""
        records = self._read_jsonl("artifact_heads.jsonl", ArtifactHead)
        latest: dict[str, ArtifactHead] = {}
        for row in records:
            latest[row.artifact_id] = row
        return [latest[key] for key in sorted(latest)]

    def current_artifact_head(self, artifact_id: str) -> ArtifactHead | None:
        records = self._read_jsonl("artifact_heads.jsonl", ArtifactHead)
        return next((row for row in reversed(records) if row.artifact_id == artifact_id), None)

    def append_review_target(self, target: ReviewTarget) -> None:
        if target.project_id != self.project_id:
            raise ValueError("review target project id does not match store project id")
        with self._lock:
            existing = self.read_review_targets()
            if any(row.review_target_id == target.review_target_id for row in existing):
                raise ValueError("review target id already exists")
            self._append_jsonl("review_targets.jsonl", target)

    def read_review_targets(self) -> list[ReviewTarget]:
        return self._read_jsonl("review_targets.jsonl", ReviewTarget)

    def append_repair_directive(self, directive: RepairDirective) -> None:
        if directive.project_id != self.project_id:
            raise ValueError("repair directive project id does not match store project id")
        with self._lock:
            existing = self.read_repair_directives()
            if any(row.directive_id == directive.directive_id for row in existing):
                raise ValueError("repair directive id already exists")
            self._append_jsonl("repair_directives.jsonl", directive)

    def read_repair_directives(self) -> list[RepairDirective]:
        return self._read_jsonl("repair_directives.jsonl", RepairDirective)


    def append_fork_directive(self, directive: ForkDirective) -> None:
        """Persist one immutable user-issued fork directive."""
        if directive.project_id != self.project_id:
            raise ValueError("fork directive project id does not match store project id")
        with self._lock:
            existing = self.read_fork_directives()
            if any(row.fork_directive_id == directive.fork_directive_id for row in existing):
                raise ValueError("fork directive id already exists")
            self._append_jsonl("fork_directives.jsonl", directive)

    def read_fork_directives(self) -> list[ForkDirective]:
        return self._read_jsonl("fork_directives.jsonl", ForkDirective)

    def append_branch_snapshot(self, branch: PlanBranch) -> None:
        """Append one immutable branch snapshot; status transitions append new rows."""
        if branch.project_id != self.project_id:
            raise ValueError("plan branch project id does not match store project id")
        with self._lock:
            existing = self.read_branches()
            if any(row.branch_id == branch.branch_id and row.model_dump(mode="json") == branch.model_dump(mode="json")
                   for row in existing):
                raise ValueError("duplicate plan branch snapshot")
            self._append_jsonl("plan_branches.jsonl", branch)

    def read_branches(self) -> list[PlanBranch]:
        """Return the latest snapshot per branch id, ordered by fork_count."""
        latest: dict[str, PlanBranch] = {}
        for row in self._read_jsonl("plan_branches.jsonl", PlanBranch):
            latest[row.branch_id] = row
        return [latest[branch_id] for branch_id in sorted(latest, key=lambda row: (latest[row].fork_count, row))]

    def current_branch(self, branch_id: str) -> PlanBranch | None:
        return next((row for row in self.read_branches() if row.branch_id == branch_id), None)

    def append_event(
        self,
        event_type: str,
        state: str,
        work_item_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> ProjectEvent:
        if work_item_id is not None:
            work_item_id = self._safe_component(work_item_id, "work item id")
        with self._lock:
            count, last_sequence = self._jsonl_tail_cursor("events.jsonl", ProjectEvent)
            event = ProjectEvent(
                sequence=last_sequence + 1,
                project_id=self.project_id,
                event_type=event_type,
                state=state,
                work_item_id=work_item_id,
                detail=detail or {},
            )
            self._append_jsonl("events.jsonl", event)
            self._note_jsonl_appended("events.jsonl", count + 1, last_sequence + 1)
            return event

    def read_events(
        self,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> list[ProjectEvent]:
        """Read ordered events after a client cursor (incremental read support).

        ``after_sequence`` filters events with sequence greater than the cursor
        (default 0 returns everything); ``limit`` caps the page size (1..500).
        """
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if limit is not None and not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        records = self._read_jsonl("events.jsonl", ProjectEvent)
        rows = [row for row in records if row.sequence > after_sequence]
        return rows[:limit] if limit is not None else rows

    def count_events(self) -> int:
        """Return the number of persisted project events (tail-cursor based)."""
        count, _ = self._jsonl_tail_cursor("events.jsonl", ProjectEvent)
        return count

    def append_domain_activity(self, projection: DomainActivityProjection) -> DomainActivityRecord:
        """Assign sequence and append one child-trace projection idempotently."""
        project_id = str(projection.values.get("project_id") or "")
        work_item_id = str(projection.values.get("work_item_id") or "")
        child_run_id = str(projection.values.get("child_run_id") or "")
        source_trace_id = str(projection.values.get("source_trace_id") or "")
        if project_id != self.project_id:
            raise ValueError("domain activity project id does not match store project id")
        self._safe_component(work_item_id, "work item id")
        self._safe_component(child_run_id, "child run id")
        with self._lock:
            index = self._domain_activity_index()
            existing = index.get((child_run_id, source_trace_id))
            if existing is not None:
                if existing != projection.to_record(existing.sequence):
                    raise ValueError("source trace id has a conflicting domain activity projection")
                return existing
            count, _ = self._jsonl_tail_cursor("domain_activities.jsonl", DomainActivityRecord)
            record = projection.to_record(count + 1)
            self._append_jsonl("domain_activities.jsonl", record)
            self._domain_activity_by_key[(child_run_id, source_trace_id)] = record
            self._note_jsonl_appended("domain_activities.jsonl", count + 1, count + 1)
            return record

    def read_domain_activities(
        self,
        after_sequence: int = 0,
        limit: int | None = None,
        work_item_id: str | None = None,
    ) -> list[DomainActivityRecord]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if limit is not None and not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if work_item_id is not None:
            work_item_id = self._safe_component(work_item_id, "work item id")
        records = [
            row for row in self._read_jsonl("domain_activities.jsonl", DomainActivityRecord)
            if row.sequence > after_sequence and (work_item_id is None or row.work_item_id == work_item_id)
        ]
        return records[:limit] if limit is not None else records

    def domain_activity_cursor(self) -> int:
        _, last_sequence = self._jsonl_tail_cursor("domain_activities.jsonl", DomainActivityRecord)
        return last_sequence

    def append_assessment(self, record: AssessmentRecord) -> None:
        if record.project_id != self.project_id:
            raise ValueError("assessment project id does not match store project id")
        with self._lock:
            self._append_jsonl("assessments.jsonl", record)

    def read_assessments(self) -> list[AssessmentRecord]:
        return self._read_jsonl("assessments.jsonl", AssessmentRecord)

    def append_decision(self, record: DecisionEvent) -> None:
        if record.project_id != self.project_id:
            raise ValueError("decision project id does not match store project id")
        with self._lock:
            self._append_jsonl("decisions.jsonl", record)

    def read_decisions(self) -> list[DecisionEvent]:
        return self._read_jsonl("decisions.jsonl", DecisionEvent)

    @contextmanager
    def execution_lock(self):
        """Hold a cross-instance process lock for one project's state transitions."""
        self.project_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.project_dir / ".execution.lock"
        with lock_path.open("a+b") as handle:
            try:
                try:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    backend = "fcntl"
                except ImportError:  # pragma: no cover - Linux is the acceptance environment
                    import msvcrt
                    handle.seek(0)
                    if handle.tell() == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    backend = "msvcrt"
            except (BlockingIOError, OSError) as exc:
                raise ProjectBusyError(f"project is already executing: {self.project_id}") from exc
            try:
                yield
            finally:
                if backend == "fcntl":
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:  # pragma: no cover - Linux is the acceptance environment
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _resolved_project_file(self, path: Path | str) -> Path:
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_relative_to(self.project_dir):
            raise ValueError("artifact source must be inside the project directory")
        if not resolved.is_file():
            raise ValueError("artifact source must be a regular file")
        return resolved

    def register_artifact(
        self,
        path: Path | str,
        work_item_id: str,
        logical_name: str,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRecord:
        work_item_id = self._safe_component(work_item_id, "work item id")
        if not logical_name.strip():
            raise ValueError("artifact logical name cannot be empty")
        source = self._resolved_project_file(path)
        artifact_root = self.project_dir / "artifacts"
        artifact_root.mkdir(parents=True, exist_ok=True)

        with self._lock:
            descriptor, temp_name = tempfile.mkstemp(prefix=".artifact.", suffix=".tmp", dir=artifact_root)
            os.close(descriptor)
            temp_path = Path(temp_name)
            try:
                shutil.copyfile(source, temp_path)
                with temp_path.open("rb") as handle:
                    os.fsync(handle.fileno())
                digest = self._sha256(temp_path)
                suffix = source.suffix if 0 < len(source.suffix) <= 20 else ""
                destination_dir = artifact_root / digest[:2]
                destination_dir.mkdir(parents=True, exist_ok=True)
                destination = destination_dir / f"{digest}{suffix}"
                if destination.exists():
                    if self._sha256(destination) != digest:
                        raise ValueError("content-addressed artifact path has conflicting content")
                    temp_path.unlink()
                else:
                    os.replace(temp_path, destination)
                    self._fsync_directory(destination_dir)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise

            records = self.read_artifacts()
            matching = [
                item for item in records
                if item.work_item_id == work_item_id and item.logical_name == logical_name
            ]
            for existing in reversed(matching):
                if existing.sha256 == digest:
                    self._ensure_artifact_versioned(existing)
                    return existing
            record = ArtifactRecord(
                project_id=self.project_id,
                work_item_id=work_item_id,
                logical_name=logical_name,
                uri=f"project://{destination.relative_to(self.project_dir).as_posix()}",
                media_type=media_type,
                sha256=digest,
                size_bytes=destination.stat().st_size,
                version=max((item.version for item in matching), default=0) + 1,
            )
            self._append_jsonl("artifacts.jsonl", record)
            self._ensure_artifact_versioned(record)
            return record

    def _ensure_artifact_versioned(self, record: ArtifactRecord) -> None:
        """Append the typed immutable version row and CAS-update the active head.

        Idempotent across crash windows: a content-addressed record whose
        version row already exists is only bound to the head, never duplicated.
        """
        # Complexity boundary (P1-3 family): the full artifact_versions ledger
        # is re-read on registration. Per-artifact rows are small in typical
        # projects; a per-artifact tail index remains a future optimization.
        versions = [
            row for row in self.read_artifact_versions()
            if row.work_item_id == record.work_item_id and row.logical_name == record.logical_name
        ]
        existing_row = next((row for row in versions if row.record_id == record.artifact_id), None)
        if existing_row is None:
            logical_id = versions[0].artifact_id if versions else self._new_contract_id("artifact")
            previous = versions[-1] if versions else None
            existing_row = ArtifactVersion(
                version_id=self._new_contract_id("artifact-version"),
                project_id=self.project_id,
                artifact_id=logical_id,
                record_id=record.artifact_id,
                version=record.version,
                sha256=record.sha256,
                size_bytes=record.size_bytes,
                work_item_id=record.work_item_id,
                logical_name=record.logical_name,
                media_type=record.media_type,
                uri=record.uri,
                supersedes_version_id=previous.version_id if previous else None,
            )
            self._append_jsonl("artifact_versions.jsonl", existing_row)
        current_head = self.current_artifact_head(existing_row.artifact_id)
        if current_head is not None and current_head.version_id == existing_row.version_id:
            return
        self.update_artifact_head(ArtifactHead(
            artifact_id=existing_row.artifact_id,
            project_id=self.project_id,
            work_item_id=record.work_item_id,
            logical_name=record.logical_name,
            version_id=existing_row.version_id,
            record_id=record.artifact_id,
            version=(current_head.version + 1) if current_head else 1,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
            media_type=record.media_type,
            uri=record.uri,
            updated_by_attempt_id=None,
        ), expected_version=current_head.version if current_head else None)

    def reconcile_artifact_heads(self, referenced_artifact_ids: set[str]) -> None:
        """Backfill version rows and active heads for legacy content-addressed records.

        New registrations always write the typed ledger; this makes an older
        project's artifacts addressable by the same active-set contract after
        an upgrade, without duplicating or rewriting stored bytes.
        """
        for record in self.read_artifacts():
            if record.artifact_id in referenced_artifact_ids:
                self._ensure_artifact_versioned(record)

    def read_artifacts(self) -> list[ArtifactRecord]:
        return self._read_jsonl("artifacts.jsonl", ArtifactRecord)

    def artifact_path(self, record: ArtifactRecord) -> Path:
        if record.project_id != self.project_id:
            raise ValueError("artifact belongs to a different project")
        relative = record.uri.removeprefix("project://")
        if not relative or Path(relative).is_absolute():
            raise ValueError("invalid project artifact URI")
        resolved = (self.project_dir / relative).resolve()
        if not resolved.is_relative_to(self.project_dir):
            raise ValueError("artifact URI escapes project directory")
        return resolved

    @staticmethod
    def _unique(records: list[BaseModel], field: str, label: str) -> None:
        values = [getattr(record, field) for record in records]
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate {label} identifiers")

    def assert_integrity(self) -> None:
        verify_domain_repair_policy()
        spec = self.load_spec()
        if spec is None:
            raise ValueError("project spec is missing")
        if spec.project_id != self.project_id:
            raise ValueError("project spec id mismatch")

        base_plan = self.load_plan()
        if base_plan is not None and base_plan.project_id != self.project_id:
            raise ValueError("research plan project id mismatch")
        repair_requests = self.read_repair_requests()
        revisions = self.read_plan_revisions()
        resolutions = self.read_repair_resolutions()
        plan = effective_plan(base_plan, revisions) if base_plan is not None else None
        known_items = {item.item_id for item in plan.items} if plan is not None else set()

        state = self.load_state()
        if state is not None:
            if state.project_id != self.project_id:
                raise ValueError("project state id mismatch")
            referenced = set(state.completed_items) | set(state.failed_items) | set(state.attempts)
            if state.current_item_id:
                referenced.add(state.current_item_id)
            if known_items and not referenced.issubset(known_items):
                raise ValueError(f"project state references unknown work items: {sorted(referenced - known_items)}")

        results = self.load_work_item_results()
        if known_items and not set(results).issubset(known_items):
            raise ValueError(f"results reference unknown work items: {sorted(set(results) - known_items)}")

        events = self.read_events()
        expected_sequences = list(range(1, len(events) + 1))
        if [event.sequence for event in events] != expected_sequences:
            raise ValueError("project event sequence is not contiguous and monotonic")
        for event in events:
            if event.project_id != self.project_id:
                raise ValueError("project event id mismatch")
            if known_items and event.work_item_id and event.work_item_id not in known_items:
                raise ValueError(f"event references unknown work item: {event.work_item_id}")

        artifacts = self.read_artifacts()
        activities = self.read_domain_activities()
        expected_activity_sequences = list(range(1, len(activities) + 1))
        if [activity.sequence for activity in activities] != expected_activity_sequences:
            raise ValueError("domain activity sequence is not contiguous and monotonic")
        self._unique(activities, "activity_id", "domain activity")
        self._unique(activities, "source_trace_id", "domain activity source trace")
        trace_events_by_run: dict[str, dict[str, str]] = {}
        for artifact in artifacts:
            if artifact.logical_name != "target_discovery_trace":
                continue
            path = self.artifact_path(artifact)
            if not path.is_file():
                continue
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    run_id = str(payload.get("run_id") or "")
                    event_id = str(payload.get("event_id") or "")
                    if run_id and event_id:
                        trace_events_by_run.setdefault(run_id, {})[event_id] = trace_event_digest(payload)
        for activity in activities:
            if activity.project_id != self.project_id:
                raise ValueError("domain activity project id mismatch")
            if known_items and activity.work_item_id not in known_items:
                raise ValueError(f"domain activity references unknown work item: {activity.work_item_id}")
            run_id = self._safe_component(activity.child_run_id, "child run id")
            if run_id not in trace_events_by_run:
                raise ValueError(f"domain activity source trace artifact is missing: {run_id}")
            source_digest = trace_events_by_run[run_id].get(activity.source_trace_id)
            if source_digest is None:
                raise ValueError(
                    f"domain activity references missing source trace: {activity.source_trace_id}"
                )
            if source_digest != activity.source_event_sha256:
                raise ValueError(
                    f"domain activity source digest mismatch: {activity.source_trace_id}"
                )

        assessments = self.read_assessments()
        decisions = self.read_decisions()
        fork_directives = self.read_fork_directives()
        branches = self.read_branches()
        self._unique(assessments, "assessment_id", "assessment")
        self._unique(decisions, "decision_id", "decision")
        self._unique(artifacts, "artifact_id", "artifact")
        self._unique(repair_requests, "repair_request_id", "repair request")
        self._unique(revisions, "revision_id", "plan revision")
        self._unique(resolutions, "resolution_id", "repair resolution")
        self._unique(fork_directives, "fork_directive_id", "fork directive")
        self._unique(branches, "branch_id", "plan branch")
        for record in [
            *assessments, *decisions, *artifacts, *repair_requests, *revisions, *resolutions,
            *fork_directives, *branches,
        ]:
            if record.project_id != self.project_id:
                raise ValueError("append-only record project id mismatch")

        artifact_ids = {record.artifact_id for record in artifacts}
        assessment_ids = {record.assessment_id for record in assessments}
        request_ids = {record.repair_request_id for record in repair_requests}
        revision_ids = {record.revision_id for record in revisions}
        branch_ids = {record.branch_id for record in branches}
        allowed_decision_targets = (
            known_items | artifact_ids | assessment_ids | request_ids | revision_ids | branch_ids
        )
        if base_plan is not None:
            allowed_decision_targets.add(base_plan.plan_id)
        for decision in decisions:
            unknown_targets = {
                target for target in decision.target_ids
                if target not in allowed_decision_targets
                and not re.fullmatch(r"release:[0-9a-f]{64}", target)
            }
            if unknown_targets:
                raise ValueError(f"decision references unknown targets: {sorted(unknown_targets)}")
        for assessment in assessments:
            if assessment.target_id not in known_items | artifact_ids:
                raise ValueError(f"assessment references unknown target: {assessment.target_id}")
        for result in results.values():
            missing = set(result.artifact_ids) - artifact_ids
            if missing:
                raise ValueError(f"work item {result.item_id} references missing artifacts: {sorted(missing)}")

        attempts = self.read_attempts()
        for attempt in attempts:
            if attempt.project_id != self.project_id:
                raise ValueError("work attempt project id mismatch")
            if known_items and attempt.work_item_id not in known_items:
                raise ValueError(f"work attempt references unknown work item: {attempt.work_item_id}")
            if attempt.output_digest is not None:
                snapshot = self.load_attempt_result(attempt.attempt_id)
                if snapshot is None:
                    raise ValueError(f"work attempt result snapshot is missing: {attempt.attempt_id}")
                if snapshot.item_id != attempt.work_item_id:
                    raise ValueError("work attempt result snapshot item id mismatch")
                if work_item_result_digest(snapshot) != attempt.output_digest:
                    raise ValueError("work attempt result snapshot digest mismatch")

        heads = self.read_work_item_heads()
        self._unique(heads, "head_id", "work item head")
        if len({head.work_item_id for head in heads}) != len(heads):
            raise ValueError("work item head is not unique per work item")
        for head in heads:
            if head.project_id != self.project_id:
                raise ValueError("work item head project id mismatch")
            if known_items and head.work_item_id not in known_items:
                raise ValueError(f"work item head references unknown work item: {head.work_item_id}")
            attempt = next((row for row in attempts if row.attempt_id == head.attempt_id), None)
            if attempt is None or attempt.output_digest != head.result_digest:
                raise ValueError(f"work item head references a mismatched attempt: {head.attempt_id}")
            if attempt.work_item_id != head.work_item_id:
                raise ValueError("work item head attempt binding mismatch")
            snapshot = self.load_attempt_result(attempt.attempt_id)
            if snapshot is None or work_item_result_digest(snapshot) != head.result_digest:
                raise ValueError(f"work item head result snapshot mismatch: {head.attempt_id}")
            if head.status != snapshot.status:
                raise ValueError(f"work item head status mismatch: {head.work_item_id}")

        artifact_versions = self.read_artifact_versions()
        artifact_heads = self.read_artifact_heads()
        records_by_id = {record.artifact_id: record for record in artifacts}
        for head in artifact_heads:
            if head.project_id != self.project_id:
                raise ValueError("artifact head project id mismatch")
            version_row = next(
                (row for row in artifact_versions if row.version_id == head.version_id),
                None,
            )
            if version_row is None or version_row.artifact_id != head.artifact_id:
                raise ValueError("artifact head references a missing version row")
            record = records_by_id.get(head.record_id)
            if record is None or record.sha256 != head.sha256:
                raise ValueError("artifact head references a missing or mismatched record")
        for row in artifact_versions:
            if row.project_id != self.project_id:
                raise ValueError("artifact version project id mismatch")
            if row.record_id not in records_by_id:
                raise ValueError(f"artifact version references a missing record: {row.version_id}")

        branch_by_id = {row.branch_id: row for row in branches}
        directive_by_id = {row.fork_directive_id: row for row in fork_directives}
        if sorted(row.fork_count for row in branches) != list(range(1, len(branches) + 1)):
            raise ValueError("plan branch fork_count is not contiguous")
        if len(branches) > spec.max_forks:
            raise ValueError("plan branch budget exceeded")
        if set(branch_by_id) != {row.branch_id for row in fork_directives}:
            raise ValueError("fork directives and plan branches are not one-to-one")
        prior_branch_id: str | None = None
        for branch in branches:
            if branch.project_id != self.project_id:
                raise ValueError("plan branch project id mismatch")
            if branch.parent_branch_id != prior_branch_id:
                raise ValueError("plan branch parent chain is not contiguous")
            directive = directive_by_id.get(branch.fork_directive_id)
            if directive is None:
                raise ValueError("plan branch references missing fork directive")
            if directive.branch_id != branch.branch_id:
                raise ValueError("fork directive branch binding mismatch")
            if directive.mode != branch.mode or directive.target_work_item_id != branch.fork_point_item_id:
                raise ValueError("fork directive and plan branch intent mismatch")
            if directive.rollback_to_attempt_id != branch.rollback_to_attempt_id:
                raise ValueError("fork directive and plan branch attempt binding mismatch")
            prior_branch_id = branch.branch_id

        request_by_id = {row.repair_request_id: row for row in repair_requests}
        revision_by_id = {row.revision_id: row for row in revisions}
        if [row.revision_number for row in revisions] != list(range(1, len(revisions) + 1)):
            raise ValueError("plan revision sequence is not contiguous")
        known_before = {item.item_id for item in base_plan.items} if base_plan is not None else set()
        prior_revision_id: str | None = None
        for revision in revisions:
            branch = None
            request = None
            if revision.fork_branch_id is not None:
                branch = branch_by_id.get(revision.fork_branch_id)
                if branch is None:
                    raise ValueError("plan revision references missing plan branch")
                directive = directive_by_id.get(branch.fork_directive_id)
                if directive is None:
                    raise ValueError("plan branch references missing fork directive")
            else:
                request = request_by_id.get(revision.repair_request_id)
                if request is None:
                    raise ValueError("plan revision references missing repair request")
            if base_plan is None or revision.base_plan_id != base_plan.plan_id:
                raise ValueError("plan revision base plan mismatch")
            if revision.parent_revision_id != prior_revision_id:
                raise ValueError("plan revision parent chain is not contiguous")
            revision_body = {
                "project_id": revision.project_id,
                "base_plan_id": revision.base_plan_id,
                "parent_revision_id": revision.parent_revision_id,
                "revision_number": revision.revision_number,
                "repair_request_id": revision.repair_request_id,
                "fork_branch_id": revision.fork_branch_id,
                "operation": revision.operation,
                "directive_id": revision.directive_id,
                "payload": revision.payload,
                "added_items": [item.model_dump(mode="json") for item in revision.added_items],
                "superseded_item_ids": revision.superseded_item_ids,
                "superseded_assessment_ids": revision.superseded_assessment_ids,
                "trigger_snapshot_digest": revision.trigger_snapshot_digest,
                "approval_required": revision.approval_required,
            }
            if canonical_sha256(revision_body) != revision.revision_digest:
                raise ValueError("plan revision digest mismatch")
            if revision.trigger_snapshot_digest != (
                branch.before_snapshot_digest if branch is not None else request.trigger_snapshot_digest
            ):
                raise ValueError("plan revision trigger snapshot mismatch")
            if branch is not None:
                if revision.operation != "fork_rollback":
                    raise ValueError("fork revision must use the fork_rollback operation")
                if revision.repair_request_id is not None or revision.directive_id is not None:
                    raise ValueError("fork revision cannot carry repair metadata")
                if branch.revision_id != revision.revision_id:
                    raise ValueError("plan branch revision binding mismatch")
                if directive.snapshot_digest != branch.before_snapshot_digest:
                    raise ValueError("fork directive snapshot digest mismatch")
                plan_before = effective_plan(
                    base_plan, [row for row in revisions if row.revision_number < revision.revision_number]
                )
                active_before = active_item_ids(
                    plan_before, [row for row in revisions if row.revision_number < revision.revision_number]
                )
                expected_affected = set(fork_affected_item_ids(
                    plan_before, branch.fork_point_item_id, branch.mode, active_before,
                ))
                if set(revision.superseded_item_ids) != expected_affected:
                    raise ValueError("fork revision affected items do not match the descendant closure")
                expected_approval = (
                    directive.mode == ForkMode.RESTORE or spec.autonomy_mode != AutonomyMode.AUTONOMOUS
                )
                if revision.approval_required != expected_approval:
                    raise ValueError("fork revision approval requirement mismatch")
                if revision.approval_required and not any(
                    decision.action.value == "accept"
                    and branch.branch_id in decision.target_ids
                    and decision.evidence_snapshot_digest == branch.before_snapshot_digest
                    for decision in decisions
                ):
                    raise ValueError("checkpointed fork revision lacks exact-snapshot approval")
                if any(
                    decision.action.value == "reject"
                    and branch.branch_id in decision.target_ids
                    and decision.evidence_snapshot_digest == branch.before_snapshot_digest
                    for decision in decisions
                ):
                    raise ValueError("fork revision conflicts with an immutable fork rejection")
                if branch.status in {PlanBranchStatus.PROPOSED, PlanBranchStatus.APPROVED, PlanBranchStatus.REJECTED}:
                    raise ValueError("fork revision exists while the branch is not applied")
                if directive.mode == ForkMode.RESTORE:
                    if branch.fork_point_item_id in set(revision.superseded_item_ids):
                        raise ValueError("restore fork must keep its restored fork point active")
                    plan_by_id = {item.item_id: item for item in plan.items}
                    seen_ancestors: set[str] = set()
                    current_item = plan_by_id.get(branch.fork_point_item_id)
                    while current_item is not None and current_item.rerun_of_item_id is not None:
                        if current_item.rerun_of_item_id in seen_ancestors:
                            break
                        seen_ancestors.add(current_item.rerun_of_item_id)
                        current_item = plan_by_id.get(current_item.rerun_of_item_id)
                    attempt = next(
                        (row for row in attempts if row.attempt_id == directive.rollback_to_attempt_id),
                        None,
                    )
                    if attempt is None or attempt.work_item_id not in (
                        {branch.fork_point_item_id} | seen_ancestors
                    ):
                        raise ValueError("restore fork references a missing or mismatched attempt")
                    if attempt.output_digest is None or attempt.status not in {
                        WorkAttemptStatus.COMPLETED, WorkAttemptStatus.COMPLETED_WITH_GAPS,
                    }:
                        raise ValueError("restore fork attempt is not a terminal completed result")
                    restored = self.load_attempt_result(attempt.attempt_id)
                    current_target = results.get(branch.fork_point_item_id)
                    if restored is None or current_target is None:
                        raise ValueError("restore fork target result is missing")
                    if work_item_result_digest(restored) != attempt.output_digest:
                        raise ValueError("restore fork attempt result digest mismatch")
                    if work_item_result_digest(current_target) != work_item_result_digest(restored):
                        normalized = current_target.model_copy(update={
                            "item_id": restored.item_id,
                            "repair_request_id": restored.repair_request_id,
                            "fork_branch_id": restored.fork_branch_id,
                            "supersedes_result_digest": restored.supersedes_result_digest,
                        })
                        if work_item_result_digest(normalized) != work_item_result_digest(restored):
                            raise ValueError("restore fork target result was not restored")
            else:
                domain_repair = request.action.value in DOMAIN_REPAIR_ACTION_VALUES
                if not domain_repair:
                    if request.failure_class != FailureClass.TRANSIENT or request.action != RepairAction.RERUN_SUBGRAPH_SAME_INPUTS:
                        raise ValueError("plan revision is not backed by an eligible same-input transient request")
                elif request.failure_class != FailureClass.SCIENTIFIC_GAP:
                    raise ValueError("plan revision is not backed by an eligible scientific-gap domain request")
                if domain_repair:
                    expected_authorization = DOMAIN_REPAIR_POLICY[request.action][1]
                else:
                    expected_authorization = (
                        RepairAuthorization.AUTOMATIC
                        if spec.autonomy_mode == AutonomyMode.AUTONOMOUS
                        else RepairAuthorization.CHECKPOINT_REQUIRED
                    )
                if request.authorization != expected_authorization:
                    raise ValueError("repair request authorization does not match project autonomy")
                if revision.approval_required != (expected_authorization == RepairAuthorization.CHECKPOINT_REQUIRED):
                    raise ValueError("plan revision approval requirement does not match repair authorization")
                if revision.approval_required and not any(
                    decision.action.value == "accept"
                    and request.repair_request_id in decision.target_ids
                    and decision.evidence_snapshot_digest == request.trigger_snapshot_digest
                    for decision in decisions
                ):
                    raise ValueError("checkpointed plan revision lacks exact-snapshot approval")
                if any(
                    decision.action.value == "reject"
                    and request.repair_request_id in decision.target_ids
                    and decision.evidence_snapshot_digest == request.trigger_snapshot_digest
                    for decision in decisions
                ):
                    raise ValueError("plan revision conflicts with an immutable repair rejection")
                if revision.superseded_item_ids != request.affected_work_item_ids:
                    raise ValueError("plan revision affected work items do not match repair request")
            if not set(revision.superseded_item_ids).issubset(known_before):
                raise ValueError("plan revision supersedes unknown work items")
            added_ids = {item.item_id for item in revision.added_items}
            if added_ids & known_before:
                raise ValueError("plan revision reuses an existing work item id")
            prior_items = {
                item.item_id: item
                for item in [*base_plan.items, *(added for prior in revisions
                                                 if prior.revision_number < revision.revision_number
                                                 for added in prior.added_items)]
            }
            replacement_by_source = {item.rerun_of_item_id: item for item in revision.added_items}
            if set(replacement_by_source) != set(revision.superseded_item_ids):
                raise ValueError("plan revision must replace every and only superseded work item")
            replacement_ids = {
                source_id: replacement.item_id for source_id, replacement in replacement_by_source.items()
            }
            for item in revision.added_items:
                if item.rerun_of_item_id not in revision.superseded_item_ids:
                    raise ValueError("revision item does not rerun a superseded work item")
                source = prior_items[item.rerun_of_item_id]
                source_payload = source.model_dump(mode="json", exclude={
                    "item_id", "dependencies", "rerun_of_item_id", "repair_request_id", "fork_branch_id",
                })
                item_payload = item.model_dump(mode="json", exclude={
                    "item_id", "dependencies", "rerun_of_item_id", "repair_request_id", "fork_branch_id",
                })
                expected_inputs = dict(source.inputs or {})
                if revision.fork_branch_id is not None:
                    expected_inputs.update(directive.input_overrides.get(source.item_id, {}))
                elif revision.operation == "switch_dataset_same_context" and request is not None:
                    directive_payload = request.directive_payload or {}
                    expected_inputs["dataset_override"] = {
                        key: directive_payload[key]
                        for key in ("preferred_dataset_accessions", "excluded_dataset_accessions")
                        if directive_payload.get(key) is not None
                    }
                elif revision.operation in OVERLAY_REVISION_OPERATIONS and request is not None:
                    if item.rerun_of_item_id == request.target_work_item_id:
                        if item.module != "domain_overlay":
                            raise ValueError("overlay revision must replace the target with the domain_overlay module")
                        expected_inputs["source_item_id"] = source.item_id
                        expected_inputs["domain_overlay"] = {
                            **(request.directive_payload or {}),
                            "operation": revision.operation,
                        }
                if source_payload != item_payload:
                    allowed_override = bool(
                        revision.fork_branch_id is not None
                        and directive.input_overrides.get(source.item_id) is not None
                    ) or (
                        revision.fork_branch_id is None
                        and revision.operation in DOMAIN_REPAIR_ACTION_VALUES
                    )
                    if not allowed_override or item.inputs != expected_inputs:
                        raise ValueError("plan revision changes work-item content beyond retry metadata")
                expected_dependencies = [replacement_ids.get(dep, dep) for dep in source.dependencies]
                if item.dependencies != expected_dependencies:
                    raise ValueError("plan revision changes dependencies beyond the affected subgraph overlay")
            known_before |= added_ids
            prior_revision_id = revision.revision_id
        for request in repair_requests:
            source = results.get(request.target_work_item_id)
            if source is None or work_item_result_digest(source) != request.trigger_result_digest:
                raise ValueError("repair request source result digest mismatch")
            domain_repair_source = request.action.value in DOMAIN_REPAIR_ACTION_VALUES
            if domain_repair_source:
                if source.input_digest != request.input_digest:
                    raise ValueError("repair request source input digest mismatch")
                if not request.no_scope_change:
                    raise ValueError("domain repair request must declare no_scope_change")
                if request.candidate_lane_recompute_required != (
                    request.action == RepairAction.SWITCH_DATASET_SAME_CONTEXT
                ):
                    raise ValueError(
                        "repair request candidate lane recompute flag does not match its action"
                    )
                overlay_chain = source.module == "domain_overlay"
                if overlay_chain:
                    if source.status not in {
                        WorkItemStatus.COMPLETED, WorkItemStatus.COMPLETED_WITH_GAPS,
                    }:
                        raise ValueError("repair request overlay source is not terminal")
                elif (source.status != WorkItemStatus.COMPLETED_WITH_GAPS
                        or source.failure_class != FailureClass.SCIENTIFIC_GAP):
                    raise ValueError("repair request source is not a same-context scientific gap")
            elif (source.status != WorkItemStatus.FAILED
                    or source.failure_class != FailureClass.TRANSIENT
                    or source.input_digest != request.input_digest):
                raise ValueError("repair request source is not an identical-input transient failure")
            if not set(request.trigger_assessment_ids).issubset(assessment_ids):
                raise ValueError("repair request references missing trigger assessment")
            trigger_assessments = [
                row for row in assessments if row.assessment_id in request.trigger_assessment_ids
            ]
            expected_methods = (
                {"typed_status_gate"}
                if not domain_repair_source
                else {"typed_status_gate", "typed_dataset_gate", "typed_domain_review"}
            )
            if any(
                row.target_id != request.target_work_item_id
                or row.target_digest != request.trigger_result_digest
                or row.result != AssessmentResult.FAIL
                or not row.blocking
                or row.method not in expected_methods
                or row.actor not in {"independent_review", "fake_independent_review"}
                for row in trigger_assessments
            ):
                raise ValueError("repair request trigger assessment is not a bound blocking failure")
        for resolution in resolutions:
            if resolution.repair_request_id not in request_ids or resolution.revision_id not in revision_by_id:
                raise ValueError("repair resolution references missing request or revision")
            if not set(resolution.verification_assessment_ids).issubset(assessment_ids):
                raise ValueError("repair resolution references missing verification assessment")
            request = request_by_id[resolution.repair_request_id]
            revision = revision_by_id[resolution.revision_id]
            if resolution.before_snapshot_digest != request.trigger_snapshot_digest:
                raise ValueError("repair resolution before snapshot mismatch")
            root = next(
                item for item in revision.added_items
                if item.rerun_of_item_id == request.target_work_item_id
            )
            final_root = chain_final_replacement(revision, request, revisions)
            root_result = results.get(final_root.item_id)
            verification = [
                row for row in assessments
                if row.assessment_id in resolution.verification_assessment_ids
            ]
            verified = (
                root_result is not None
                and any(
                    row.target_id == final_root.item_id
                    and row.target_digest == work_item_result_digest(root_result)
                    and row.result == AssessmentResult.PASS
                    and not row.blocking
                    and row.method == "typed_status_gate"
                    and row.actor in {"independent_review", "fake_independent_review"}
                    for row in verification
                )
            )
            domain_resolution = request.action.value in DOMAIN_REPAIR_ACTION_VALUES
            if resolution.status == RepairResolutionStatus.RESOLVED:
                if any(
                    row.blocking and row.result == AssessmentResult.FAIL
                    for row in active_assessments(assessments, revisions)
                ):
                    raise ValueError("resolved repair leaves a blocking assessment active")
                if not verified or (not domain_resolution and root_result.input_digest != request.input_digest):
                    raise ValueError("resolved repair lacks identical-input independent verification")
                if final_root.item_id == root.item_id:
                    incomplete = [
                        results.get(item.item_id)
                        for item in revision.added_items
                        if results.get(item.item_id) is None
                        or results[item.item_id].status != WorkItemStatus.COMPLETED
                    ]
                else:
                    incomplete = [
                        results.get(item_id)
                        for item_id in active_item_ids(plan, revisions)
                        if results.get(item_id) is None
                        or results[item_id].status != WorkItemStatus.COMPLETED
                    ]
                if incomplete:
                    raise ValueError("resolved repair contains an incomplete recomputed work item")
        if resolutions:
            latest = max(resolutions, key=lambda row: revision_by_id[row.revision_id].revision_number)
            if revision_by_id[latest.revision_id].revision_number == len(revisions):
                current_snapshot = project_snapshot_digest(
                    plan=plan,
                    results=results,
                    assessments=assessments,
                    artifacts=artifacts,
                    revisions=revisions,
                )
                if latest.after_snapshot_digest != current_snapshot:
                    raise ValueError("latest repair resolution after snapshot mismatch")

        for record in artifacts:
            if known_items and record.work_item_id not in known_items:
                raise ValueError(f"artifact references unknown work item: {record.work_item_id}")
            path = self.artifact_path(record)
            if not path.exists() or not path.is_file():
                raise ValueError(f"artifact file is missing: {record.artifact_id}")
            if path.stat().st_size != record.size_bytes:
                raise ValueError(f"artifact size mismatch: {record.artifact_id}")
            if self._sha256(path) != record.sha256:
                raise ValueError(f"artifact digest mismatch: {record.artifact_id}")


__all__ = ["ProjectBusyError", "ResearchProjectStore"]
