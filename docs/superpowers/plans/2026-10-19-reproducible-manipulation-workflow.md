# Reproducible manipulation workflow implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Isolated structured strategy planning, immutable replay, and real G1/SONIC execution without experiment-specific defaults.
**Architecture:** Pure contracts and content-addressed evidence store; deterministic planner with constrained decision provider; capability-specific native executor behind frozen experiment preparation.
**Tech Stack:** Python 3.10, NumPy, SciPy, MuJoCo, SONIC; standard-library JSON/HTTP; existing HUMANO wrapper.
**Spec:** ../specs/2026-10-19-reproducible-manipulation-workflow-design.md

## Global constraints

- Only add workflow package, its tests/docs, and timestamped experiment artifacts. Historical executors and other routes remain byte-identical.
- No historical offset/frame/hand/object defaults; robot and task limits have explicit provenance. Missing calibration blocks feedback, not silent fallback.
- Strategy replay never calls API. Changed context requires a new strategy and revalidation. API output cannot execute code or override physical gates.
- No physics without frozen experiment.json, actual model-provider evidence, source/input hashes, declared interfaces, and budget.
- All timestamps Asia/Shanghai; original IDs retained; native success, physical acceptance and visual training acceptance separate.

## Review focus

- Malformed/nonfinite API data and invented evidence references: reject before preparation.
- Package tampering, path traversal and duplicate/concurrent saves: verify content hashes and atomic publication.
- Changed scene with identical strategy graph: new context-bound ID and fresh checks.
- Empty or misaligned phases and measured fingers mistaken for commands: reject before simulation.
- Runtime exceptions and stop events: persist evidence, stop stepping, never retry implicitly.

## Task 1 — contracts and immutable strategies

Files: workflow/contracts.py, store.py, tests/test_workflow_store.py.
Interfaces: validate_strategy(dict); Store(root).save(strategy, artifacts)->id; load(id)->bundle.
- [ ] Write tests for digest stability, context distinction, bad intervals, dependency tampering and unsafe paths.
- [ ] Run tests RED, implement canonical JSON, strict validated strategy records and atomic artifact publication, run GREEN.
- [ ] Save a narrow commit and ledger outcome.

## Task 2 — decision provider and deterministic compiler

Files: workflow/decisions.py, planning.py, tests/test_workflow_planning.py.
Interfaces: ResponsesDecision.propose(evidence,out)->proposal; compile_strategy(request,decision=None)->strategy; candidate ranking is lexicographic.
- [ ] Test replay without network, missing credentials, malformed response, invalid reference, phase and active-hand variation.
- [ ] Run RED; implement explicit provider configuration, saved requests/responses, no implicit models, geometric candidate checks; run GREEN.
- [ ] Save a narrow commit and ledger outcome.

## Task 3 — calibration and native execution

Files: workflow/calibration.py, native.py, execution.py, tests/test_workflow_execution.py.
Interfaces: calibration from measured trace; NativeAdapter binds named model entities; execute(contract)->result.
- [ ] Test feedback provenance/coverage, dynamic phase scheduling and stop behavior with simulated measurements; unsupported capability fails explicitly.
- [ ] Run RED; implement generic stage schedule and named G1 adapter using live SONIC mode1, measured replan, source command alignment and explicit physical gates; run GREEN.
- [ ] Save a narrow commit and ledger outcome.

## Task 4 — preparation and public entrypoints

Files: workflow/service.py, __init__.py, __main__.py, README.md, examples/, tests/test_workflow_service.py.
Interfaces: plan(request), prepare(strategy_id,request), execute(experiment_id), inspect(experiment_id); CLI equivalents JSON in/out.
- [ ] Test missing evidence, changed context, explicit budgets, duplicate execution and report failures.
- [ ] Run RED; implement fresh runtime preflight, frozen contracts and artifact/result publication; run GREEN.
- [ ] Provide portable example construction and replay commands with no /tmp dependence.

## Task 5 — integration and verification

- [ ] Run existing repo and mimicgen tests plus new suite; compare initial file-hash inventory.
- [ ] Prepare and freeze a new native end-to-end trial, run once, reflect before any further attempt; no fake-success stubs.
- [ ] Inspect actual modes, original command alignment, phase measurements and failure gates; render full videos and inspect samples.
- [ ] Verify strategy replay uses saved decisions; package/scene mutation is rejected.
- [ ] Obtain one final code review, fix important findings with regression tests, publish exact coverage and remaining limitations.

## Execution authorization / ledger

User explicitly requested “开始实现”; execute inline without another permission round. Work in a new package in the current checkout: existing untracked source and frozen absolute hashes make a detached worktree an unsuitable implementation base. This preserves the requested route isolation; file-hash inventory verifies no existing implementation file changes.
