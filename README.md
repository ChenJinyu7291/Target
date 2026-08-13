# TargetDiscovery Agent

Target turns a disease question and its biological context into a ranked target shortlist, evidence-linked TargetCards, and falsifiable follow-up experiments. It combines genetics, disease omics, perturbation, mechanism, druggability, and safety evidence while keeping supporting, conflicting, and missing evidence visible. Every result can be traced back to the source, tool run, parameters, and project snapshot that produced it.

The current release includes a durable project workflow for review, recovery, export, and offline sharing. For the detailed product boundary and implementation status, see [Product V3](docs/PRODUCT_V3.md) and [Completed capabilities](COMPLETED.md).

## Quickstart

Python 3.11 is the acceptance runtime.

```bash
python -m pip install -e ".[test,omics-bulk]"
target-agent doctor
```

Copy `.env.example` to an untracked `.env`, or set the model credentials in the process environment. Then create a reviewable project from a research question:

```bash
target-agent ask \
  --question "In lung adenocarcinoma, which druggable targets are supported by public evidence?" \
  --disease "lung adenocarcinoma" \
  --create \
  --output ./luad-project/project.yaml

target-agent project-run --input ./luad-project/project.yaml
```

Start the workbench to review plans, checkpoints, evidence, branches, and reports:

```bash
target-agent up --port 8888
```

Open <http://localhost:8888>. Deployment options for pip, Docker Compose, and Singularity are documented in [DEPLOYMENT.md](docs/DEPLOYMENT.md). The grouped CLI reference is in [CLI_REFERENCE.md](docs/CLI_REFERENCE.md); `target-agent --help` is the authoritative command list.

## How it works

```text
Disease question + tissue/cell/stage context
                    |
                    v
          reviewable plan and checkpoints
                    |
                    v
   genetics | omics | perturbation | literature | trials
                    |
                    v
       Evidence Store + provenance + Reviewer
                    |
                    v
 ranked targets + blockers + TargetCards + experiments
                    |
                    v
       report + portable project package + audit trail
```

The target-discovery runtime uses contract `TaskSpec 2.2.0`. The project control plane uses `ResearchProjectSpec 3.1.0` to preserve the accepted goal, workflow, artifacts, approvals, repairs, and branches across long-running work. See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the component-level view.

## What is implemented

- Disease-aware planning for 18 curated diseases, with tissue, cell type, stage, and phenotype context.
- Dynamic GEO and CELLxGENE discovery, metadata eligibility checks, and controlled bulk or single-cell analysis.
- Controlled human-genetics inputs, including checksum-bound audits of supplied SuSiE credible sets and coloc results.
- Europe PMC, Open Targets, and ClinicalTrials.gov evidence retrieval.
- Six evidence lanes for ranking, explicit blockers, TargetCards, and experiment plans.
- Deterministic scientific review with optional LLM or Reviewer LoRA confirmation.
- Durable projects with checkpoints, pause/resume, bounded repairs, rollback branches, export/import, and offline review pages.
- HTTP, Web, and optional MCP interfaces over the same project service.

Exact acceptance records and benchmark results live in [VALIDATION_REPORT.md](docs/VALIDATION_REPORT.md), rather than on the README front page.

## Design decisions

### Two contracts for two kinds of state

`TaskSpec 2.2.0` describes the scientific question: disease, biological context, phenotype, constraints, and data preferences. `ResearchProjectSpec 3.1.0` describes how that question is executed and reviewed over time. Keeping them separate lets the scientific input remain stable while project checkpoints, retries, and approvals evolve.

### Deterministic gates outrank model review

The LLM and Reviewer LoRA can propose plans or confirm a finding, but they cannot waive provenance, context, schema, or completion rules. This keeps a parse failure or an overconfident model response from changing a scientific gate.

### No nearest-gene shortcut

A GWAS locus is not assigned to the nearest gene. Formal genetics evidence requires controlled inputs and the required study, build, ancestry, harmonization, and checksum links; unresolved loci remain unresolved. Aggregate database associations may provide context but do not satisfy the strict genetics score or the `GO` gate.

### Checkpoints bind decisions to exact evidence

Plans, repairs, exclusions, and release decisions are approved against immutable artifact digests. A changed snapshot requires a new decision, so a previous approval cannot silently carry over after the evidence changes.

### Runtime parity is tested

The legacy state machine and LangGraph runtime share contracts, checkpoints, and observable output tests. Keeping both implementations parity-tested made the runtime migration inspectable and provides a reference path for recovery regressions.

## Limitations

- Public benchmark scores are regression and contract checks. Biological ranking quality still needs an evaluator-controlled blind set and independent expert adjudication.
- The Reviewer adapter has passed the repository's template-consistent held-out checks, but open-world Reviewer quality has not been established.
- Disease omics is observational. MCH/K562 is the only causal gold configuration currently included.
- Raw FASTQ/SRA, arbitrary GEO layouts, spatial analysis, statistical fine-mapping or coloc recomputation, patents, wet-lab execution, and clinical decision support are outside the current scope.
- Missing eligible omics data produces `completed_with_gaps`; the report records the missing evidence instead of filling it with a model-generated result.
- Model weights, large datasets, caches, secrets, and deployment profiles are kept outside Git.

## Validation and repository checks

Run the local repository gates with:

```bash
python scripts/repo_policy_check.py
pytest
python benchmark/runner.py
```

Live benchmark tasks, external APIs, GPU training, and environment-specific backends require the appropriate external execution profile. CI configuration is in [.github/workflows/ci.yml](.github/workflows/ci.yml).

## Documentation

- [Documentation index](docs/README.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Product boundary and roadmap](docs/PRODUCT_V3.md)
- [CLI reference](docs/CLI_REFERENCE.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Demo guide](docs/DEMO_GUIDE.md)
- [Validation report](docs/VALIDATION_REPORT.md)
- [Definition of done](docs/DEFINITION_OF_DONE.md)
- [Decision log](DECISION_LOG.md)
