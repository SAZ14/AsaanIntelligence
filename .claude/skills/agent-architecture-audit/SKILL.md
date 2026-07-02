---
name: agent-architecture-audit
description: Audits an AI-agent codebase (conversational agents, executional/pipeline agents, and other agent types relevant to products like this one) against a fixed 7-dimension scalability/security/privacy/performance rubric derived from a conversational-AI-architecture reference article. Produces a plain-language, forward-looking critique report with concrete suggested design changes. Use when the user wants to audit agent architecture, review agent system design for scale, or check whether an AI-agent system is production-ready.
---

# Agent Architecture Audit

Audits this codebase's AI-agent architecture against the fixed rubric in `RUBRIC.md`, and produces a critique-and-suggest report in plain, forward-looking language — not a generic code review.

## Process

### 1. Gather context

Before spawning any agent, read (in this order, using whatever exists):
- This repo's `.agents/CLAUDE.md`. If it doesn't exist, fall back to root `CLAUDE.md`, then `README.md`. Note in the final report which one was actually used.
- The user's memory index (`MEMORY.md` in the auto-memory directory for this project) and any linked memory files whose description looks relevant to architecture, business context, or prior findings.
- `RUBRIC.md` in this skill's folder.

If no memory files exist at all, note this explicitly — it means the audit ran against code only and could not judge business-context fit (subscription model, tenant boundaries, growth plans).

### 2. Spawn two parallel agents

Use the `Agent` tool with `subagent_type: general-purpose` for both — launch them in the same message so they run in parallel. Do not use `fork` — these need fresh eyes, not this conversation's accumulated framing.

**Agent A — Architecture-depth agent.** Prompt:

> Audit this codebase's AI-agent architecture for scalability and code health. You are scoring it against exactly these 5 rubric dimensions — use these dimension names verbatim as your section headers: "Modularity", "State & concurrency", "Latency discipline", "Schema evolution", "Readability & best practices".
>
> For each dimension: [paste the matching section text from RUBRIC.md here].
>
> Method: for anything that looks like a shallow wrapper or suspicious abstraction, apply the deletion test — if you deleted it, would the complexity vanish (it was a pass-through, not a real problem) or reappear elsewhere (it was earning its keep, and its *shape* is the actual finding)? Walk the codebase read-only (Read, Grep, Glob, Bash for read-only commands only — do not edit anything).
>
> For every dimension, report either a finding or explicitly "No issue found" — never omit a dimension. For each finding give: the plain-language problem, a concrete scenario for why it breaks at scale (not abstract theory), a suggested design change (a direction, not just "add tests"), and the file:line location. Avoid code blocks unless a snippet is genuinely clearer than a sentence. Report back in under 500 words.

**Agent B — Security-privacy agent.** Prompt:

> Audit this codebase's AI-agent architecture for security and privacy. You are scoring it against exactly these 3 rubric dimensions — use these dimension names verbatim as your section headers: "Tenant isolation", "Auth & attack surface", "Observability/feedback loop".
>
> For each dimension: [paste the matching section text from RUBRIC.md here].
>
> Walk the codebase read-only (Read, Grep, Glob, Bash for read-only commands only — do not edit anything) looking specifically for: endpoints without auth checks, queries that could leak data across tenants, and any gaps in logging/tracing that would stop you from diagnosing a production incident.
>
> For every dimension, report either a finding or explicitly "No issue found" — never omit a dimension. For each finding give: the plain-language problem, a concrete scenario for why it breaks at scale (not abstract theory), a suggested design change (a direction, not just "add tests"), and the file:line location. Avoid code blocks unless a snippet is genuinely clearer than a sentence. Report back in under 500 words.

### 3. Synthesize

Once both agents return, merge their findings into a single markdown report:

- One section per rubric dimension, in this fixed order: Modularity, State & concurrency, Tenant isolation, Auth & attack surface, Latency discipline, Observability/feedback loop, Schema evolution, Readability & best practices.
- Within each section, use the agent's finding as-is (light copyedit only for tone consistency) or write "No issue found" if that's what the agent reported.
- If both agents happen to report on the same file/issue (rare, given the dimension split, but possible), dedupe and keep the more specific/actionable version.
- After all 7 sections, add a **"Fix this first"** line: pick the single finding with the largest blast radius (data leak or auth gap > silent data loss > latency/UX complaint > code cleanliness) and say why, in one sentence.

### 4. Report format (per finding)

```markdown
### <Dimension name>

**Problem:** <plain-language statement>
**Why it breaks at scale:** <concrete scenario>
**Suggested design change:** <forward-looking, actionable direction>
**Where:** <file:line>
```

Or, if nothing found: `### <Dimension name>\n\nNo issue found.`

Tone: short, plain, direct sentences. No hedging ("consider potentially maybe"). No unexplained jargon — assume the reader is a founder, not necessarily a working engineer. No thesaurus words, no padded phrasing. Code snippets only when genuinely clearer than prose.

### 5. Save the report

Write to `.agents/audits/<YYYY-MM-DD>-scale-audit.md` (create the `.agents/audits/` directory if it doesn't exist), using today's date. Do **not** commit this file — tell the user it's written and unstaged, and let them decide whether to commit it.
