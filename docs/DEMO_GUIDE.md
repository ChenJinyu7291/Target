# Workbench demo and offline review guide

The workbench no longer ships a stored-replay demo catalog: the repository does
not contain the demo runs that older versions of this guide referenced, and
`/api/demo/*` / `/api/runs/*` are legacy endpoints not used by the workbench.
The recommended presentation path is a live project, with a single-file offline
review page as the network-free fallback.

## Start and verify

Infrastructure values belong in the external deployment profile. Supply the
port, projects, run and cache directories explicitly:

```bash
target-agent doctor
target-agent serve \
  --host 0.0.0.0 \
  --port "$TARGET_AGENT_PORT" \
  --projects-dir "$TARGET_AGENT_PROJECT_DIR" \
  --runs-dir "$TARGET_AGENT_RUN_DIR" \
  --cache-dir "$TARGET_AGENT_CACHE_DIR"
```

Binding `--host 0.0.0.0` exposes the workbench to the network. The server has no
multi-user authentication; in a shared environment keep it bound to localhost or
a trusted tunnel, and follow the bind/security and deployment notes in
[DEPLOYMENT.md](DEPLOYMENT.md).

When the service runs on a compute node, create a local tunnel using the
deployment profile rather than recording the host in Git:

```bash
ssh -N -L 8888:<compute-node>:<service-port> <ssh-profile>
```

Open `http://localhost:8888` and verify:

```bash
curl -fsS http://localhost:8888/healthz
curl -fsS http://localhost:8888/api/capabilities
```

The health response must report the service, Evidence Store, cache and executor
as available. The capability pill shows which optional scientific backends are
installed in this environment.

## Presentation path (live project)

1. **Create or open a project.** Use the workbench “新建项目” panel (or
   `target-agent ask` / `target-agent init` + `project-run`), then open it in
   the workbench. Explain that the product is a research Agent, not a gene-list
   generator, and that the frontend renders only stored backend evidence.
2. **Plan and checkpoints.** Show the typed plan (bound to the executable
   workflow template), the Planner backend, and the checkpointed approval loop
   in the “研究会话” panel (researcher / reviewer / admin roles; viewer is
   read-only).
3. **Evidence and ranking.** Show results, branch/rollback history, events,
   artifacts and the evidence graphs. Keep the boundaries explicit:
   `FACT` / `OBSERVED` / `PREDICTED` / `INFERRED` remain separate; a priority
   score is not a clinical success probability; missing context degrades
   honestly (`completed_with_gaps`) instead of being fabricated.
4. **Offline review fallback.** While the live project runs (or after it
   completes), render the single-file offline review page:

   ```bash
   target-agent share --project-id project-xxx --output project-xxx.html
   target-agent share --input project-xxx.target-project.zip --output project-xxx.html
   ```

   The generated HTML has no backend, network or external resources; it embeds
   a SHA-256 snapshot fingerprint and is scrubbed of secrets. Present it as a
   review snapshot of one project state, never as a live backend session.

## Recovery during a presentation

- If a live run is slow, switch to an existing completed project or the offline
  review page; do not wait on public databases.
- If Step is unavailable, the deterministic workflow remains available and the
  Planner backend is shown.
- If a backend capability is missing, use the capability pill and Reviewer
  findings to explain the gap.
- If the service cannot be reached, open the offline review page; do not
  present it as a live backend session.
