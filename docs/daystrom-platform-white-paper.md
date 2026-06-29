# The Daystrom Platform: Memory, Personality, Cognition, and Inference for Long-Horizon AI Agents

**Status:** working white paper  
**Audience:** agent-framework maintainers, applied AI engineers, operators, and product stakeholders  
**Scope:** Daystrom Memory Lattice (DML), Daystrom Personality Matrix (DPM), Daystrom Cognition Network (DCN), Daystrom Inference Pipeline (DIP), and Hermes Agent integration

## Executive Summary

The Daystrom Platform is a local-first architecture for making AI agents durable, inspectable, and operationally sane across long-horizon work. It addresses the gap between stateless model calls and real agent operation: memory must persist, personality and user preference must remain bounded, cognition policy must be auditable, and inference preparation must be separated from provider secrets and model execution.

The platform is organized into four bounded layers:

- **DML — Daystrom Memory Lattice:** durable hierarchical semantic memory, retrieval, compression, handoff, and memory hygiene.
- **DPM — Daystrom Personality Matrix:** bounded preference/personality overlays derived from explicit user corrections and scoped relationship/project/thread context.
- **DCN — Daystrom Cognition Network:** deterministic cognition-control policy for deciding what to retrieve, when to write back, when to require verification, and when stronger modes are safe.
- **DIP — Daystrom Inference Pipeline:** prompt/context preparation boundary for frontier or local inference, without owning provider secrets or final model invocation.

Hermes Agent integrates with Daystrom as an operator-facing agent runtime. Hermes supplies the conversation loop, tools, gateway, slash commands, profiles, and user-facing automation. Daystrom supplies the continuity and cognition substrate. The combined goal is simple: users should not spend weeks debugging YAML, path mismatches, memory modes, or silent feature degradation. Agents should know how the system works, perform startup preflight checks, and expose live status through commands such as `/dml-help`.

## Problem Statement

Modern agent frameworks can call tools and models, but durable operation fails in predictable ways:

1. **Context does not survive sessions cleanly.** Raw transcripts are too large, too noisy, and unsafe as continuity state.
2. **Configuration drift is invisible.** A profile can claim DML support while missing the launcher, runtime store, retrieval mode, cognition hook, or iteration-extension contract.
3. **Preference learning is dangerous when unbounded.** Agents can overfit tone, leak preferences across projects, or treat stale personality guidance as stronger than a current instruction.
4. **Cognition policy is often implicit.** Retrieval, writeback, verification, tool selection, and continuation decisions are usually scattered across prompts and code paths.
5. **Inference pipelines mix concerns.** Prompt preparation, context selection, secret management, and model invocation are often bundled together, making security and auditability harder.
6. **Operators carry too much burden.** Users should not have to remember every setting, restart rule, and hidden dependency. The agent should surface what is enabled, what is broken, and what to do next.

The Daystrom Platform is designed to make these failure modes explicit, testable, and recoverable.

## Design Principles

### 1. Local-first durability

Daystrom treats durable agent state as local operational infrastructure. Memory stores, preference graphs, cognition audit artifacts, and handoff packets should be inspectable without depending on a remote SaaS memory provider.

### 2. Secret hygiene by construction

The platform must never require API keys, provider secrets, raw tool logs, or raw transcripts to be stored in memory. Secrets stay in environment files, host secret managers, or provider-specific credential stores. DML stores compact semantic signals and metadata, not credential material.

### 3. Bounded overlays, not hidden control

DPM and DCN can provide guidance, but they do not override current user instructions, hard safety rules, privacy constraints, or operator stoplines. Personality is context, not authority.

### 4. Deterministic gates before stronger autonomy

DCN active-learn and stronger modes require readiness evidence: eval smokes, hygiene artifacts, checkpoints, rollback commands, and explicit promotion boundaries.

### 5. Operator-first observability

Every non-obvious subsystem needs a status surface. Startup preflight should catch partial deployments. Runtime commands should show what is enabled, what is missing, and how to recover.

## Platform Components

## DML: Daystrom Memory Lattice

DML is the durable memory layer. It provides semantic search and hierarchical memory abstraction so an agent can resume meaningful work without replaying entire conversations.

Core responsibilities:

- Ingest durable semantic signals: decisions, preferences, task outcomes, blockers, validated fixes, and next-step state.
- Retrieve compact context for current work.
- Resume from prior continuity state at session start or after compaction.
- Build handoff packets before shutdown, compaction, or long pauses.
- Enforce memory hygiene by excluding raw transcripts, secrets, noisy tool logs, and stale implementation minutiae.
- Support GPU acceleration where available while preserving a CPU/no-GPU fallback path for portability.

DML is not a transcript database. It is the continuity spine. Its value comes from selective, semantic persistence: the smallest durable state that helps the next agent act correctly.

### DML operating pattern

A Daystrom-aware agent generally follows this cycle:

1. **Resume:** load compact continuity relevant to the session or project.
2. **Retrieve:** query memory before context-sensitive decisions.
3. **Act:** use tools and code paths normally.
4. **Ingest:** store durable, semantic outcomes only.
5. **Handoff:** write compact state before compaction, shutdown, or long pauses.

Hermes now also uses DML-related preflight checks to verify that a profile configured for Daystrom actually has the expected provider, paths, retrieval policy, DCN mode, and iteration-extension hooks.

## DPM: Daystrom Personality Matrix

DPM provides bounded personality and preference overlays. It exists because human-agent continuity is not just factual memory; users also develop stable preferences about communication style, initiative, risk tolerance, formatting, and project conventions.

Core responsibilities:

- Represent explicit user preferences and corrections as scoped graph nodes.
- Render compact runtime overlays for the current relationship, project, or thread.
- Keep active-read mode read-only.
- Keep active-write mode auditable, scoped, and bounded.
- Allow user-facing inspection, deletion, suppression, and rollback.

DPM must never become an invisible personality jail. Current-turn user instructions always win. Safety, privacy, and secret hygiene always win. DPM provides helpful continuity only when compatible with the active context.

### DPM modes

- **disabled:** no personality overlay.
- **observe-only:** inspect signals without changing behavior.
- **active-read:** render bounded overlays from existing graph state without writing.
- **active-write:** record explicit preference signals with scoped, auditable writes.

## DCN: Daystrom Cognition Network

DCN is the deterministic cognition-control layer. It does not own memory storage, personality state, or model inference. Instead, it decides how the agent should use those systems safely.

Core responsibilities:

- Select retrieval strategy: none, resume, semantic, or hybrid.
- Recommend context budgets and writeback strictness.
- Gate bounded DPM overlays.
- Decide whether verification is required before finalizing work.
- Provide readiness and promotion gates for stronger modes.
- Record sanitized audit metadata and feedback artifacts.
- Support policy checkpoints, import/export, rollback, and non-promoting seed trials.

### DCN modes

- **disabled:** legacy provider behavior.
- **observe_only:** records structural recommendations while legacy behavior remains authoritative.
- **active_read:** can gate retrieval and overlays, with fallback to legacy behavior on failure.
- **active_learn:** reserved for stronger policy overlays after readiness evidence, hygiene checks, checkpoints, and explicit promotion.

DCN's most important boundary is that learning proposals are not automatically operational policy. Candidate procedural updates must pass schema validation, hygiene review, evals, and promotion gates.

## DIP: Daystrom Inference Pipeline

DIP is the inference preparation boundary. It prepares compact, structured context for a frontier or local model call, but it does not need to own endpoint secrets or final model invocation.

Core responsibilities:

- Build prompt/context packets from DML retrieval, DPM overlays, and DCN policy.
- Support telemetry-only preparation paths for validation without model calls.
- Keep provider secrets outside Daystrom memory and committed config.
- Allow Hermes or another runtime to own final model selection, credential pools, and request execution.

This separation lets Daystrom improve context quality without becoming a credential-bearing inference proxy by default.

## Hermes Integration

Hermes is the agent runtime: it owns the conversation loop, tools, gateway, profiles, slash commands, scheduled work, and user-facing interaction. Daystrom is the continuity and cognition substrate.

The Hermes-Daystrom integration includes:

- `memory.provider: daystrom_dml` for DML-backed memory.
- Always-on retrieval and synchronous turn persistence for Daystrom profiles.
- DML-first compression/handoff behavior.
- Flexible iteration budgets gated through memory cognition.
- Startup DML preflight checks.
- Operator status/help via `/dml-help`.

### Flexible iterations

Long-running work often fails because a fixed turn budget expires while the agent is still making verifiable progress. Hermes now supports flexible iteration budgets:

- Start with a normal `agent.max_turns` budget.
- Near the limit, build a structured `hermes.iteration_extension.v1` state packet.
- Ask memory cognition whether to extend when configured for cognition policy.
- Extend only in bounded chunks.
- Stop at a hard cap to prevent infinite loops.

This gives Daystrom a role in judging whether the agent is productively continuing, stuck in noise, or safely finished.

### Startup preflight

For Daystrom profiles, Hermes preflight checks verify critical configuration and runtime hooks:

- DML provider and config block.
- Retrieval policy, turn sync, and semantic memory enablement.
- DCN mode.
- Required path existence.
- Flexible-iteration configuration.
- Iteration-extension contract availability.
- DML-first compression support.
- Runtime memory-manager/provider hooks.

Preflight is intentionally secret-safe and read-only. Operators can choose warning mode or strict fail-closed mode.

### `/dml-help`

The `/dml-help` command is the first user-facing status surface for Daystrom configuration. It reports:

- Core DML config status.
- Flexible-iteration settings.
- Runtime memory manager/provider visibility.
- Iteration-extension hooks.
- Preflight issues or pass counts.
- Concrete next actions.

This command encodes a product principle: the agent should explain and inspect itself instead of making the user reverse-engineer config drift.

## Data and Control Boundaries

| Layer | Owns | Must not own |
| --- | --- | --- |
| DML | durable semantic memory, retrieval, compression, handoff | secrets, raw transcripts, raw tool logs, unchecked personality authority |
| DPM | scoped preference/personality overlays | current-turn authority, safety override, cross-scope leakage |
| DCN | deterministic cognition policy, audit, readiness gates | memory storage, preference graph mutation outside contracts, model inference |
| DIP | context/prompt preparation | credential storage by default, final model execution by default |
| Hermes | agent loop, tools, gateway UX, profiles, slash commands | silent config burden, unverified claims of runtime behavior |

## Safety and Privacy Model

The Daystrom Platform assumes that memory is powerful and therefore risky. Its safety model is built around these invariants:

- Store compact semantic state, not raw chat logs.
- Exclude secrets, credentials, raw tool outputs, and provider tokens.
- Keep memory and preference writes scoped and auditable.
- Make stronger cognition modes promotion-gated and rollbackable.
- Treat DPM personality guidance as subordinate to current instructions and hard rules.
- Expose status and failure modes to the operator.
- Prefer fail-closed behavior when a profile claims a stronger mode but lacks evidence or hooks.

## Operational Model

A healthy Daystrom-backed Hermes profile should satisfy the following:

1. **Config is explicit:** provider, DML paths, retrieval policy, DCN mode, DPM mode, and iteration extension are readable.
2. **Startup verifies assumptions:** preflight warns or fails when required hooks or paths are missing.
3. **Runtime is observable:** `/dml-help` and logs expose active provider and hook status.
4. **Memory is selective:** durable ingest stores only useful semantic state.
5. **Cognition is bounded:** DCN modes stronger than active-read require evidence and rollback.
6. **Users are not operators by accident:** normal users should not need to inspect YAML to know whether Daystrom is working.

## Roadmap

Near-term priorities:

- Expand `/dml-help` into a richer menu with per-section detail views.
- Add optional self-heal suggestions for common missing config/path/runtime issues.
- Add DPM inspect/suppress commands for preference graph governance.
- Add DCN readiness command surfaces for eval smoke and promotion status.
- Add remote/profile sync checks so AEC/demo machines can verify they match the GitHub branch.
- Add docs-site pages for Daystrom setup, operations, and troubleshooting.

Longer-term priorities:

- Stronger scope intelligence for preference learning.
- Review queues for ambiguous DPM signals.
- DCN active-learn promotion workflows with signed or hashed hygiene artifacts.
- Better visualizations of memory, preference graph, and cognition policy decisions.
- Multi-agent coordination where Daystrom supplies continuity across specialized Hermes profiles without leaking private or project-scoped memory.

## Conclusion

Daystrom is not just a memory plugin. It is a platform architecture for durable, trustworthy, long-horizon agents. DML remembers, DPM personalizes, DCN governs, and DIP prepares inference. Hermes operationalizes those layers through tools, gateway UX, profiles, and now self-diagnostic commands.

The product bar is not merely "the config can be made to work." The bar is that the agent can tell when Daystrom is working, explain what is enabled, catch drift before it wastes the user's time, and preserve continuity without compromising safety or privacy.
