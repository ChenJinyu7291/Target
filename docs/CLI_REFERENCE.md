# CLI reference

This page groups the commands most useful for operating and reviewing Target. Run `target-agent --help` or `target-agent <command> --help` for the complete, version-matched arguments.

## Setup and service

```bash
target-agent doctor
target-agent llm-smoke-test
target-agent up --port 8888
target-agent serve --host 127.0.0.1 --port 8888
```

`up` runs capability checks before starting the Waitress workbench. Use `serve --dev` only for local Flask development.

## Create and run a project

```bash
# Draft a project from natural language; add --create to write the project file.
target-agent ask --question "Evidence for IL-23 blockade in IBD?" --disease IBD

# Or initialize from structured fields.
target-agent init --output ./uc-project --disease "ulcerative colitis" --tissue colon

target-agent project-run --input ./uc-project/project.yaml
target-agent project-status --project-id project-xxx
target-agent project-pause --project-id project-xxx --actor reviewer --rationale "Review evidence"
target-agent project-resume --project-id project-xxx --actor reviewer --rationale "Continue"
target-agent project-cancel --project-id project-xxx --actor reviewer --rationale "Stop project"
```

## Review, repair, and rollback

```bash
target-agent project-approve \
  --project-id project-xxx --target-id PLAN_ID \
  --actor reviewer --rationale "Plan accepted" --resume

target-agent project-repairs --project-id project-xxx
target-agent project-repair-decision \
  --project-id project-xxx --repair-request-id REPAIR_ID \
  --snapshot-digest SNAPSHOT_SHA256 --approve \
  --actor reviewer --rationale "Approve bounded retry" --resume

target-agent project-branches --project-id project-xxx
target-agent project-fork-propose \
  --project-id project-xxx --target-work-item-id WORK_ITEM_ID \
  --mode redo --actor reviewer --rationale "Rerun with corrected input"
target-agent project-fork-decision \
  --project-id project-xxx --branch-id branch-xxx --approve \
  --actor reviewer --rationale "Approve branch"
```

Repair and branch decisions are bound to the project snapshot they review. Use the identifiers and digest printed by the read command; do not substitute a later snapshot.

## Research sessions

```bash
target-agent session create --project-id project-xxx --role reviewer
target-agent session list --project-id project-xxx
target-agent session read --project-id project-xxx --session-id session-xxx
target-agent session post \
  --project-id project-xxx --session-id session-xxx \
  --text "What evidence is still missing?" --ask-agent
target-agent session intervene \
  --project-id project-xxx --session-id session-xxx \
  --action accept_checkpoint --target-id PLAN_ID \
  --actor reviewer --rationale "Approved"
```

Viewer sessions are read-only. Session messages summarize project state but do not create scientific evidence.

## Export and offline review

```bash
target-agent project-export \
  --project-id project-xxx --output project-xxx.target-project.zip
target-agent project-package-inspect --input project-xxx.target-project.zip
target-agent project-import --input project-xxx.target-project.zip
target-agent share --project-id project-xxx --output project-xxx.html
target-agent share --input project-xxx.target-project.zip --output project-xxx.html
```

The HTML share page is read-only, self-contained, scrubbed for secrets, and labeled with its project snapshot fingerprint.

## Catalogs and disease batches

```bash
target-agent diseases
target-agent run-disease --disease uc,ra,ad
target-agent workflows list
target-agent workflows show --id disease_to_target
target-agent skills list
target-agent skills search --lanes genetics
target-agent skills show --id experiment-planning
```

## Persistent analysis kernels

```bash
target-agent kernel start --language python
target-agent kernel status
target-agent kernel exec --kernel-id KERNEL_ID --code "x = 5; __kernel_result__ = x * 7"
target-agent kernel stop --kernel-id KERNEL_ID
target-agent kernel stop-all
```

Kernels are for an operator or a registered tool. The planner does not receive arbitrary code-execution access.

## MCP

```bash
target-agent mcp-serve
target-agent mcp-serve \
  --transport streamable-http --host 127.0.0.1 --port 8000 --path /mcp
```

The MCP server exposes typed project operations over the same durable service used by the Web workbench. Authentication and network exposure are deployment responsibilities; see [DEPLOYMENT.md](DEPLOYMENT.md).
