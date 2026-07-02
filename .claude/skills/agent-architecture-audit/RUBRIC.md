# Scalability/Security Rubric — agent-architecture-audit

Fixed rubric for auditing AI-agent systems (conversational agents, executional/pipeline agents). Distilled from https://www.bland.ai/blog/conversational-ai-architecture and cross-checked against real failure modes. Static — do not fetch the source article at runtime.

## 1. Modularity
Deep interfaces, swappable adapters. A module is modular if you can replace its implementation without any caller noticing. Bad: business logic that only works with one specific vendor/provider baked into agent code. Good example pattern: a `send_fn`-style seam where the caller passes in a function and never needs to know which concrete provider it is.

## 2. State & concurrency
Shared state — locks, dedup caches, cooldowns, sessions — must be safe across multiple server instances, not just safe on one process. Red flag: plain in-memory dicts/sets used for locking or deduplication. They work fine on one instance and silently break (duplicate sends, race conditions) the moment you run two.

## 3. Tenant isolation
In a multi-tenant system, every query that touches tenant data must be scoped to that tenant (e.g. `WHERE store_id = ?` or equivalent). Red flag: isolation that only holds because every query was written carefully by hand, with nothing structural (a query wrapper, a DB-level policy, a lint rule) enforcing it.

## 4. Auth & attack surface
Every externally-reachable endpoint — admin, internal, or public — needs a real authentication/authorization check. Red flag: any endpoint reachable from the internet with no auth middleware, even if it "isn't meant to be public."

## 5. Latency discipline
Anything that calls an LLM or another slow external service must be off the synchronous request/response path, and that background work must survive a process crash or restart (a real job queue with persistence and retries), not just an in-process fire-and-forget mechanism that loses work on restart.

## 6. Observability/feedback loop
The system needs structured logs, a way to trace one request end-to-end (a request ID), and some signal (confidence scores, failure counts, retry counts) that surfaces degradation before a customer has to report it.

## 7. Schema evolution
Database schema changes go through versioned, reversible migrations (e.g. Alembic) — never hand-run SQL against a live database.

## 8. Readability & best practices
Code should be easy for a new engineer to read and safe to change. Red flags: unclear naming, duplicated logic instead of a shared function, dead code, missing error handling for things that can actually fail, functions doing too many unrelated things at once.
