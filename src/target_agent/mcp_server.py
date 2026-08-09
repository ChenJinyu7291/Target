"""MCP adapter for the durable Target research product.

The adapter is intentionally thin: MCP is a transport for the same
ResearchProjectService used by local and HTTP clients, not a second Agent or a
second source of scientific state.
"""
from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import Field

from .research_runtime import ResearchProjectRuntime
from .research_service import ResearchProjectService
from .research_session import ResearchSessionService


def create_mcp_server(
    service: ResearchProjectService | None = None,
    *,
    runtime: ResearchProjectRuntime | None = None,
):
    """Create the official-SDK MCP server without importing MCP at package load."""
    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - exercised by CLI packaging smoke
        raise RuntimeError(
            "MCP support is not installed; install the project with the 'mcp' extra"
        ) from exc

    if service is not None and runtime is not None:
        raise ValueError("provide either service or runtime, not both")
    product = service or ResearchProjectService(runtime or ResearchProjectRuntime())
    sessions = ResearchSessionService(product.runtime)
    server = MCPServer("TargetDiscovery")

    @server.tool()
    def target_capabilities() -> dict[str, Any]:
        """Describe Target's real project operations and explicit scientific boundaries."""
        return product.capabilities()

    @server.tool()
    def target_create_disease_project(
        question: str,
        disease: str,
        title: str | None = None,
        project_id: Annotated[str, Field(min_length=1)] | None = None,
        disease_subtype: str | None = None,
        tissue: str | None = None,
        cell_type: str | None = None,
        disease_stage: str | None = None,
        desired_phenotype: str | None = None,
        organism: str = "Homo sapiens",
        autonomy_mode: str = "checkpointed",
    ) -> dict[str, Any]:
        """Create an immutable disease-target project without starting execution.

        Omitted biological context is not inferred. Call target_run_project to
        advance the project until its next checkpoint or terminal state.
        """
        project = product.build_disease_project(
            question=question,
            disease=disease,
            title=title,
            project_id=project_id,
            disease_subtype=disease_subtype,
            tissue=tissue,
            cell_type=cell_type,
            disease_stage=disease_stage,
            desired_phenotype=desired_phenotype,
            organism=organism,
            autonomy_mode=autonomy_mode,
        )
        return product.reserve(project)

    @server.tool()
    def target_run_project(project_id: Annotated[str, Field(min_length=1)]) -> dict[str, Any]:
        """Advance a durable project until a checkpoint or terminal state."""
        return product.run(project_id)

    @server.tool()
    def target_pause_project(
        project_id: Annotated[str, Field(min_length=1)],
        actor: str,
        rationale: str,
    ) -> dict[str, Any]:
        """Pause a project at its next safe work-item boundary.

        The request is recorded in the project ledger; if no execution is
        running it is applied immediately. Resuming continues from the last
        durable boundary.
        """
        return product.pause_project(project_id=project_id, actor=actor, rationale=rationale)

    @server.tool()
    def target_cancel_project(
        project_id: Annotated[str, Field(min_length=1)],
        actor: str,
        rationale: str,
    ) -> dict[str, Any]:
        """Cancel a project durably; no ranking, report or release is produced.

        If an execution is running, cancellation takes effect before the next
        work item starts; otherwise it is applied immediately.
        """
        return product.cancel_project(project_id=project_id, actor=actor, rationale=rationale)

    @server.tool()
    def target_resume_project(
        project_id: Annotated[str, Field(min_length=1)],
        actor: str,
        rationale: str,
    ) -> dict[str, Any]:
        """Resume a paused project and advance until its next stop."""
        return product.resume_project(project_id=project_id, actor=actor, rationale=rationale)

    @server.tool()
    def target_get_project(project_id: Annotated[str, Field(min_length=1)]) -> dict[str, Any]:
        """Read the safe durable projection of a Target research project."""
        return product.snapshot(project_id)

    @server.tool()
    def target_list_projects() -> dict[str, Any]:
        """List available Target projects and their current durable status."""
        return {"projects": product.list_projects()}

    @server.tool()
    def target_get_events(project_id: Annotated[str, Field(min_length=1)], after_sequence: Annotated[int, Field(ge=0)] = 0) -> dict[str, Any]:
        """Replay ordered project events after a previously observed cursor."""
        events = product.events(project_id, after_sequence=after_sequence)
        return {
            "project_id": project_id,
            "events": events,
            "next_cursor": events[-1]["sequence"] if events else after_sequence,
        }

    @server.tool()
    def target_get_domain_activities(
        project_id: Annotated[str, Field(min_length=1)],
        after_sequence: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        """Read source-linked domain stages without copying scientific results."""
        return product.domain_activities(
            project_id,
            after_sequence=after_sequence,
            limit=limit,
            work_item_id=work_item_id,
        )

    @server.tool()
    def target_get_repairs(project_id: Annotated[str, Field(min_length=1)]) -> dict[str, Any]:
        """Read immutable repair requests, execution overlays and verified outcomes."""
        return product.repairs(project_id)

    @server.tool()
    def target_decide_repair(
        project_id: Annotated[str, Field(min_length=1)],
        repair_request_id: str,
        trigger_snapshot_digest: str,
        approve: bool,
        actor: str,
        rationale: str,
        resume: bool = True,
    ) -> dict[str, Any]:
        """Approve or reject one exact repair snapshot; stale digests are refused."""
        return product.decide_repair(
            project_id=project_id,
            repair_request_id=repair_request_id,
            trigger_snapshot_digest=trigger_snapshot_digest,
            approve=approve,
            actor=actor,
            rationale=rationale,
            resume=resume,
        )

    @server.tool()
    def target_propose_fork(
        project_id: Annotated[str, Field(min_length=1)],
        target_work_item_id: str,
        mode: str,
        rationale: str,
        actor: str,
        rollback_to_attempt_id: str | None = None,
        input_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Issue a snapshot-bound user rollback (redo or restore) for one project."""
        return product.propose_fork(
            project_id=project_id,
            target_work_item_id=target_work_item_id,
            mode=mode,
            rationale=rationale,
            actor=actor,
            rollback_to_attempt_id=rollback_to_attempt_id,
            input_overrides=input_overrides,
        )

    @server.tool()
    def target_decide_fork(
        project_id: Annotated[str, Field(min_length=1)],
        branch_id: str,
        approve: bool,
        actor: str,
        rationale: str,
        resume: bool = True,
    ) -> dict[str, Any]:
        """Approve or reject one immutable fork branch snapshot."""
        return product.decide_fork(
            project_id=project_id,
            branch_id=branch_id,
            approve=approve,
            actor=actor,
            rationale=rationale,
            resume=resume,
        )

    @server.tool()
    def target_get_branches(project_id: Annotated[str, Field(min_length=1)]) -> dict[str, Any]:
        """Read the fork branch history and immutable directives for a project."""
        return product.branches(project_id)

    @server.tool()
    def target_accept_checkpoint(
        project_id: Annotated[str, Field(min_length=1)],
        target_id: str,
        actor: str,
        rationale: str,
        resume: bool = True,
    ) -> dict[str, Any]:
        """Accept a frozen plan, supervised work item or exact release gate."""
        return product.accept_checkpoint(
            project_id=project_id,
            target_id=target_id,
            actor=actor,
            rationale=rationale,
            resume=resume,
        )

    @server.tool()
    def target_read_text_artifact(
        project_id: Annotated[str, Field(min_length=1)],
        artifact_id: str,
        max_characters: Annotated[int, Field(ge=1, le=1_000_000)] = 100_000,
    ) -> dict[str, Any]:
        """Read a checksum-verified text artifact under an explicit size bound."""
        return product.read_text_artifact(
            project_id,
            artifact_id,
            max_characters=max_characters,
        )


    @server.tool()
    def target_create_session(project_id: Annotated[str, Field(min_length=1)], title: str | None = None, role: str = "researcher") -> dict[str, Any]:
        """Create a conversation view over one durable project.

        role is researcher|reviewer|admin|viewer; viewer sessions are read-only
        and cannot intervene. The project ledger remains the system of record.
        """
        return sessions.create(project_id, title=title, role=role)

    @server.tool()
    def target_list_sessions(project_id: Annotated[str, Field(min_length=1)]) -> dict[str, Any]:
        """List sessions and their append-only message counts for one project."""
        return sessions.list(project_id)

    @server.tool()
    def target_read_session(project_id: Annotated[str, Field(min_length=1)], session_id: str) -> dict[str, Any]:
        """Read all messages of one session; tampered messages raise an error."""
        return sessions.messages(project_id, session_id)

    @server.tool()
    def target_post_session_message(
        project_id: Annotated[str, Field(min_length=1)],
        session_id: str,
        text: str,
        ask_agent: bool = False,
        actor: str = "researcher",
    ) -> dict[str, Any]:
        """Append a user message; ask_agent returns a deterministic snapshot summary.

        The summary is source_bound=false and never mutates scientific state.
        """
        return sessions.post_message(
            project_id, session_id, text, ask_agent=ask_agent, actor=actor,
        )

    @server.tool()
    def target_session_intervene(
        project_id: Annotated[str, Field(min_length=1)],
        session_id: str,
        action: str,
        rationale: str,
        target_id: str,
        actor: str = "researcher",
        approve: bool | None = None,
        snapshot_digest: str | None = None,
        mode: str | None = None,
        rollback_to_attempt_id: str | None = None,
        input_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Execute one structured control-plane action from inside a session.

        action is one of accept_checkpoint, decide_repair, decide_fork,
        propose_fork. Decisions are persisted to the project ledger; the
        session records the instruction and outcome view only.
        """
        return sessions.intervene(
            project_id=project_id,
            session_id=session_id,
            action=action,
            rationale=rationale,
            actor=actor,
            target_id=target_id,
            approve=approve,
            snapshot_digest=snapshot_digest,
            mode=mode,
            rollback_to_attempt_id=rollback_to_attempt_id,
            input_overrides=input_overrides,
        )
    @server.resource("target://projects/{project_id}")
    def target_project_resource(project_id: Annotated[str, Field(min_length=1)]) -> str:
        """Return one durable project as a JSON resource."""
        return json.dumps(product.snapshot(project_id), ensure_ascii=False, indent=2)

    @server.resource("target://projects/{project_id}/artifacts/{artifact_id}")
    def target_artifact_resource(project_id: Annotated[str, Field(min_length=1)], artifact_id: str) -> str:
        """Return one verified text artifact as a JSON resource."""
        return json.dumps(
            product.read_text_artifact(project_id, artifact_id),
            ensure_ascii=False,
            indent=2,
        )

    return server


def _serve(server: Any, *, transport: str, host: str, port: int, path: str) -> None:
    """Run an MCP server over stdio or the official Streamable HTTP transport."""
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport="streamable-http",
            host=host,
            port=port,
            streamable_http_path=path,
        )


def main(argv: list[str] | None = None) -> None:
    """Run Target as a local MCP server (stdio or streamable HTTP)."""
    import argparse

    parser = argparse.ArgumentParser(prog="target-agent-mcp")
    parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio",
        help="MCP transport; streamable-http exposes the same product operations over HTTP",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for streamable-http")
    parser.add_argument("--port", type=int, default=8000, help="Bind port for streamable-http")
    parser.add_argument("--path", default="/mcp", help="Streamable HTTP endpoint path")
    args = parser.parse_args(argv)
    _serve(
        create_mcp_server(),
        transport=args.transport,
        host=args.host,
        port=args.port,
        path=args.path,
    )


__all__ = ["_serve", "create_mcp_server", "main"]


if __name__ == "__main__":  # pragma: no cover
    main()
