# Research brief: Evidence-based lifecycle control for generated tools

**Status:** research hypothesis, not a novelty claim  
**Project:** Nanoscrypt  
**Last reviewed:** 2026-10-07

## Executive summary

Nanoscrypt already has a prototype lifecycle: newly generated tools are candidates, explicit outcome feedback can promote or quarantine them, and operators can retire tools. Its current policy is a useful engineering baseline, but fixed thresholds over human-entered labels do not establish that a tool caused better task outcomes.

The broad idea is not new. Recent work evaluates generated tool libraries, selects persistent task-specific tool sets, and retires reusable skills. The research opportunity is therefore narrower:

> **Can an agent safely govern versions of executable, generated tools across changing task conditions by estimating their causal, risk-adjusted contribution to verified task outcomes—and abstaining from lifecycle changes when the evidence is insufficient?**

This is a hypothesis to test. A contribution is defensible only if a systematic literature review confirms the boundary and experiments show a measurable advantage over existing policies.

## Why this question

Generated tools are persistent executable artifacts. Their value is not captured by whether they compile, exit successfully, or are called often. A tool can run correctly while producing irrelevant output; a useful tool may be called rarely; a version can regress after a dependency or environment change; and a tool can help one task family while harming another.

The existing Nanoscrypt prototype relies on submitted `helpful`, `neutral`, or `harmful` labels and fixed counts. This makes it possible to exercise the lifecycle, but leaves three research gaps:

1. **Attribution:** Did the tool improve the outcome, or was it merely present in a successful run?
2. **Uncertainty:** How should the policy act when evidence is sparse, noisy, or contradictory?
3. **Change over time:** When task distributions, dependencies, or tool versions change, should old evidence still count?

## Closest prior work and the boundary it sets

| Work | What it addresses | Remaining distinction to test |
|---|---|---|
| [EvolveTool-Bench (2026)](https://arxiv.org/abs/2604.00392) | Treats evolving generated tool libraries as artifacts; measures reuse, redundancy, composition, regression stability, and safety. | It is primarily a diagnostic benchmark. We need to compare lifecycle decision policies on shared executable tool versions. |
| [LOTS (2026)](https://arxiv.org/abs/2609.34151) | Builds persistent, task-specific tool spaces by measuring how removing observed tool output changes likelihood of the agent's fixed answer. | It optimizes task-specific selection and explicitly measures model reliance, not correctness. Our question concerns version-level lifecycle decisions under verified outcomes, risk, and changing conditions. |
| [Ratchet (2026)](https://arxiv.org/abs/2605.22148) | Measures skill contribution and retires skills, with analysis of judge error and library drift. | Its governed artifacts are skills; the Nanoscrypt question is lifecycle control for executable generated tool versions, with task-conditioned reliability and regression. This is a distinction, not proof of novelty. |
| [Tool-Genesis (2026)](https://arxiv.org/abs/2603.05578) | Benchmarks task-driven tool creation and validation. | It evaluates creation quality; our target is post-creation reuse and lifecycle decisions over a sequence of tasks. |

These works are close enough that “first lifecycle system for AI tools” is not a credible claim. We must read their full methods, inspect their code/benchmarks, search citations and newer work, then sharpen or abandon this research boundary based on overlap.

## Proposed research question

**RQ:** For generated executable tools reused across a stream of tasks, does a version-aware, uncertainty-aware lifecycle policy improve verified task utility and reduce harmful reuse compared with fixed-threshold feedback, keep-all, task-local pruning, and retire-on-estimated-contribution policies?

The policy may recommend one of five actions for a specific tool version and task context:

- **Keep shared:** make available for later relevant tasks.
- **Improve:** preserve the artifact but request repair or a new version.
- **Isolate:** stop general reuse while retaining it for a narrow known context or evaluation.
- **Retire:** remove it from active discovery while keeping an audit record.
- **Abstain:** gather more evidence or request human review because the decision is uncertain.

“Promote permanently” should not mean irreversible. A shared tool remains subject to re-evaluation when evidence indicates drift or regression.

## Candidate technical idea

Estimate a tool version's **incremental utility** using paired evaluation on the same task instances:

\[
\Delta_v(x) = U(x \mid \text{tool version }v\text{ available}) - U(x \mid \text{tool version }v\text{ withheld})
\]

Here `U` is a task-specific, externally checkable utility that can include task correctness, execution cost, latency, and risk. Tool withholding should be implemented as a controlled replay/ablation where feasible. For compositional workflows, the intervention must account for downstream tools that depend on the withheld output; otherwise attribution is misleading.

Aggregate paired deltas by tool version and task family, with uncertainty intervals and explicit drift/change-point checks. Lifecycle decisions should account for both expected benefit and downside risk. If confidence is inadequate, abstain instead of converting a small sample into a permanent decision.

This is a research direction, not yet a proven method. LOTS already uses leave-one-tool-output interventions for likelihood-based reliance, and Ratchet already studies contribution-based retirement. The experiment must show what is genuinely new: verified outcome utility, executable versioning, context-specific harm, distribution shift, or a principled abstention policy—and ideally a necessary combination rather than a rebranding.

## Hypotheses

- **H1 — Better reuse decisions:** A version-aware evidence policy yields higher verified task utility than keep-all and the current fixed-label threshold policy.
- **H2 — Fewer harmful reuses:** It reduces the rate of downstream failures attributable to reused tools, especially after a version regression or environment change.
- **H3 — Better calibration:** Its abstentions concentrate on genuinely ambiguous or shifted cases; among decisions it does make, confidence predicts decision correctness.
- **H4 — Manageable overhead:** The utility and safety gains are not erased by replay, verification, and lifecycle-management cost.

H1–H4 are hypotheses, not expected results. Negative findings are publishable if the benchmark and analysis are rigorous.

## First experiment: a small, falsifiable pilot

### Workload

Create a sequential benchmark of generated tools and task families with:

1. repeated tasks where reuse should help;
2. near-neighbor tasks where a superficially similar tool may fail;
3. a deliberately regressed new version;
4. dependency/environment drift;
5. sparse or noisy outcome labels;
6. compositional cases where one tool's output feeds another.

Use deterministic tasks with ground-truth checkers wherever possible. Preserve prompts, generated source hashes, dependency/environment manifests, execution traces, verifier outputs, lifecycle decisions, and per-task costs for reproducibility.

### Baselines

1. **Keep-all:** all generated tools stay available.
2. **Regenerate/no persistence:** solve every task without reusing generated tools.
3. **Current Nanoscrypt policy:** fixed promotion/quarantine thresholds over submitted contribution labels.
4. **Task-local pruning:** retain a separate tool subset per recurring task family, following the LOTS-style setting where feasible.
5. **Contribution-based retirement:** a faithful Ratchet-style adaptation for executable tools, after reading its complete method and implementation.
6. **Proposed policy:** paired verified utility estimates, version/task conditioning, uncertainty, and abstention.

Make comparisons fair: same underlying model, task order, execution budget, and tool-generation opportunities. Run multiple seeds and report confidence intervals.

### Primary measures

- verified task success / task utility;
- harmful reuse rate and missed harmful-tool rate;
- useful-tool retirement rate and time-to-detection of regressions;
- promotion precision and recall against held-out utility evidence;
- abstention rate and calibration;
- tool reuse and registry size;
- latency, token, execution, and verifier cost.

Do not collapse these into a single score until the trade-offs are understood. Report verifier coverage and unverified cases separately.

### Pilot decision gate

Before scaling up, answer these questions:

1. Can an automated verifier score the chosen tasks reliably?
2. Does paired replay produce a credible incremental-utility label, including for tool compositions?
3. Does the proposed policy beat the current threshold policy and the closest prior baseline on at least one prespecified primary measure without unacceptable regressions on the others?
4. Does it remain useful under sparse labels and deliberate drift?
5. Is the method sufficiently distinct from LOTS and Ratchet after faithful replication?

If the answer to 2 or 5 is no, revise the problem rather than writing a novelty claim around the current implementation.

## Immediate next steps

1. **Prior-art audit:** read full LOTS, Ratchet, EvolveTool-Bench, and Tool-Genesis papers and source code; build a claim-by-claim comparison, including their references and newer citing work.
2. **Choose one measurable domain:** start with deterministic generated utilities in Nanoscrypt (e.g. structured conversion or numerical computation) where correctness can be checked automatically.
3. **Define the task stream and verifier:** specify the task instances, deliberate regression/drift cases, and exact utility function before running agents.
4. **Implement the evaluation harness:** log paired tool-available / tool-withheld outcomes by immutable tool version; keep this separate from production lifecycle behavior initially.
5. **Run the pilot and revise the claim:** only after results decide whether this becomes a paper, a benchmark, or an engineering feature.

## What we can claim today

We can claim Nanoscrypt has a **prototype lifecycle mechanism** that separates process success from submitted task-contribution feedback and gates reuse of newly generated tools. We cannot yet claim that its decisions are accurate, that it improves outcomes, or that the research idea is unique.

## References

- Kaliyev and Maryanskyy. *EvolveTool-Bench: Evaluating the Quality of LLM-Generated Tool Libraries as Software Artifacts* (2026). https://arxiv.org/abs/2604.00392
- Zhang et al. *Self-Evolving Agents via Likelihood-Guided Tool-Space Optimization* (LOTS, 2026). https://arxiv.org/abs/2609.34151
- Zhang et al. *Ratchet: How Reliable Must an LLM Judge Be to Retire a Skill?* (2026). https://arxiv.org/abs/2605.22148
- *Tool-Genesis: A Task-Driven Tool Creation Benchmark for Self-Evolving Language Agent* (2026). https://arxiv.org/abs/2603.05578
