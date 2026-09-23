# Trade Core17 daily runner

This public repository contains orchestration only. The production strategy stays in private `ychenracing/trade`; every run checks out its current `main` and records the actual SHA. It does not place broker orders or represent real holdings.

## Execution and privacy

The formal workflow is `.github/workflows/trade-daily.yml`: Monday–Friday at `09:01 UTC` (`17:01 Asia/Shanghai`), with manual `workflow_dispatch`. Publishing or changing that workflow also performs deployment acceptance against the most recent completed session; this is labelled separately from a current-day scheduled report. The same database reservation applies to every trigger and retry.

`TRADE_READ_TOKEN` is used only to read the private source. Python 3.12 installs that revision's `requirements-lock.txt`. The runner calls the existing production preparation, market validation, snapshot, replay and publication services. It reads Core17 members and order from `quantfusion/config/universe.py`, not a copied list. No strategy implementation, threshold or research pool is changed here.

Reports, input snapshots, native risk state and diagnostic logs are never uploaded to this public repository, Actions artifacts, cache or job summaries. Public output is restricted to fixed status codes, source SHA and execution identity.

Private persistence uses the already connected Supabase project `ykhdfyjbfdvayqmvaxgb`, schema `trade_daily`. Its tables are outside the exposed REST schema, have RLS enabled and grant no access to anonymous or authenticated application users. The `trade-daily` Edge Function verifies GitHub's signed OIDC token, exact audience, repository/owner IDs, main ref and formal workflow identity. It uses platform-injected database credentials internally; no additional long-lived credential is added to GitHub.

## Once per trading day

Preparation is repeatable; the database atomically reserves the target trading date immediately before the native production replay. Only the winning reservation may compute. Started calculations are never automatically restarted, including failed/cancelled runs. A completed run can retry delivery with identical bytes. Publication stores the report, native risk state and exact compressed evidence in one transaction. The runner downloads the saved bundle again and verifies bytes and SHA-256.

Before each new date, the latest validated native risk state and input cache are restored. The native fixed-start simulation remains a simulation: no fabricated account, rolling start or risk reset. Reports compare only with the preceding actual trading day's saved structured result. Missing or incompatible baselines are `不可比较`; absent fields are `未提供`.

## Reading and recovery

Use the connected Supabase account to read `trade_daily.runs` (target date, run identity, status, report JSON/Markdown) and `trade_daily.events` (holiday, pre-close and preparation failures). Full evidence is retained in `runs.bundle` with its SHA-256. An authenticated runner can retrieve it through the same OIDC-protected endpoint. The Supabase Dashboard SQL editor is a private operator access point; there is no public report URL.

A missing calendar year, incomplete data, stale bars or unavailable private store fails closed. A reservation left `RUNNING` after a killed job requires checking that exact Actions run and preserving its state; do not delete it to force a second calculation. A failed private publication is not a successful daily scan. Source/configuration changes are disclosed, not silently called market changes.

Local focused checks: `python -m unittest discover -s tests -v` and `node --test tests/auth.test.mjs`. These check orchestration/security contracts, not strategy economics. The live deployment run separately verifies real data, production execution and private readback. Free-tier service availability and storage capacity remain operational dependencies; no paid upgrade is performed by this runner.
