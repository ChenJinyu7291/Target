"""TargetCard and falsifiable experiment-plan construction."""
from __future__ import annotations

from .contracts import ExperimentOutcome, ExperimentPlan, TargetCard, TaskSpec, TerminalStatus
from .ranking import RankedTarget


def experiment_plan(task: TaskSpec, target: RankedTarget) -> ExperimentPlan:
    disease = task.context.disease or "requested disease"
    tissue = task.context.tissue or "disease-relevant tissue"
    cell = task.context.cell_type or "disease-relevant primary cell"
    return ExperimentPlan(
        hypothesis=(f"Changing {target.gene} activity in {cell} will move the prespecified {disease} phenotype "
                    "in the desired direction without a prohibitive viability effect."),
        model_system=f"Primary human {cell} from at least two biologically independent donor batches; confirm in a {tissue} co-culture or organoid.",
        intervention=f"Use two independent perturbation reagents targeting {target.gene}; select activation or inhibition from the direction-specific evidence before execution.",
        direction="compare",
        controls=[
            "non-targeting perturbation control",
            "positive pathway control with an established phenotype effect",
            "mock-treated control",
            "orthogonal second reagent for the same target",
        ],
        primary_endpoints=["prespecified disease-signature score", "prespecified functional phenotype assay"],
        secondary_endpoints=["target engagement", "cell viability", "off-target transcriptional response"],
        replication_and_power=("Use independent biological replicates and preregister an effect size, alpha and power calculation; "
                               "do not substitute cell count for donor-level replication."),
        outcomes=[
            ExperimentOutcome(outcome="positive", observation="Both independent reagents achieve target engagement and improve both primary endpoints without a viability penalty.", conclusion="The target passes the prespecified in-vitro mechanism gate in this model.", next_action="Replicate in an orthogonal tissue model and perform dose-response testing."),
            ExperimentOutcome(outcome="negative", observation="Target engagement is confirmed, but neither primary endpoint changes beyond the preregistered threshold.", conclusion="The tested mechanism is not supported in this context.", next_action="Stop this context-specific route or test only a predeclared alternative cell state."),
            ExperimentOutcome(outcome="contradictory", observation="Reagents disagree, endpoints move in opposite directions, or phenotype improvement accompanies toxicity.", conclusion="The result is uninterpretable or exposes a safety/mechanism conflict.", next_action="Resolve off-target effects, directionality and toxicity before further investment."),
        ],
        highest_information_next_experiment=(f"A direction-resolved, donor-replicated perturbation of {target.gene} in {cell} with joint target-engagement, phenotype and viability readouts."),
        stop_conditions=["no target engagement with two validated reagents", "reproducible toxicity at the minimum effective exposure", "no primary-endpoint effect at adequate power"],
    )


def build_cards(task: TaskSpec, ranked: list[RankedTarget],
                terminal_status: TerminalStatus | None = None) -> list[TargetCard]:
    cards = []
    for rank, target in enumerate(ranked[: task.constraints.max_target_cards], start=1):
        decision = target.decision
        limitations = ["Ranking score is a prioritization score, not a probability of clinical success."]
        if terminal_status is not None and terminal_status != TerminalStatus.COMPLETED:
            if decision == "GO":
                decision = "CONDITIONAL_GO"
            limitations.append(
                f"GAPS: terminal status is {terminal_status.value}; unconditional GO is not allowed."
            )
        limitations.append(
            f"Context score source: {target.context_score_origin} "
            "(deterministic recomputation; tool self-reports are estimates only)."
        )
        cards.append(TargetCard(
            gene_symbol=target.gene, rank=rank, decision=decision,
            scores=target.scores, evidence_ids=target.evidence_ids,
            supporting_evidence_ids=target.supporting_ids, opposing_evidence_ids=target.opposing_ids,
            safety_blockers=target.safety_blockers, evidence_gaps=target.evidence_gaps,
            matched_drugs=target.matched_drugs,
            genetic_evidence_summary=target.genetic_evidence_summary,
            experiment_plan=experiment_plan(task, target),
            limitations=limitations,
        ))
    return cards
