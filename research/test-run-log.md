# Test and outcome log

**Date:** 2026-10-13 (Asia/Calcutta)  
**Workspace:** Nanoscrypt  
**Python:** 3.14.5 (Windows, `.venv`)  
**Purpose:** Record exactly what was exercised for the lifecycle research draft. These are code tests, not agent-task benchmark results.

## Completed run

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --import-mode=importlib tests/unit/test_tool_lifecycle.py
```

Outcome: **6 passed, 0 failed, 0.05 s**. Two deprecation warnings originate from the Windows selector event-loop policy configured in `tests/conftest.py`.

Covered behavior:

1. Candidate remains candidate with no outcomes.
2. Candidate promotes at the current five-outcome / three-key / 80% helpful threshold.
3. Shared tool with two recent harmful outcomes becomes `needs_improvement`.
4. Shared tool with three recent harmful outcomes becomes `quarantined`.
5. Candidate with three harmful outcomes across two keys becomes `quarantined`.
6. Retired tool remains retired despite a helpful outcome.

The added test file is `tests/unit/test_tool_lifecycle.py`. The tested implementation is `src/nanoscrypt/core/tool_lifecycle.py`.

## Incomplete runs

| Command | Outcome | Diagnostic |
|---|---|---|
| `.venv\Scripts\python.exe -m pytest -q` | Collection failed before tests ran | Import-file mismatch from duplicate module basenames: top-level and `tests/unit/` copies of `test_context.py` and `test_versioning.py`. |
| `.venv\Scripts\python.exe -m pytest -q --import-mode=importlib tests/unit/test_registry.py tests/integration/test_full_lifecycle.py` | Did not complete | Async fixture setup stalled before test output. Interrupted after waiting. |
| `.venv\Scripts\python.exe -m pytest -vv -o faulthandler_timeout=8 --import-mode=importlib tests/unit/test_registry.py` | Did not complete | Faulthandler showed the async test runner blocked during Windows asyncio event-loop creation in `socket._fallback_socketpair` / `accept`. A temporary version-guard experiment switched between selector and default Proactor policies; both paths stalled in this environment. The temporary change was reverted. |

## What these outcomes do and do not establish

The six passing tests verify selected branches in a pure decision function. They do not verify database persistence, version isolation, tool correctness, agent-level task utility, harmful reuse, drift response, or any of the paper's proposed hypotheses. The asynchronous and full-suite test results remain unresolved; they are not counted as passing or failing product assertions.

No benchmark tasks, LLM runs, paired tool-available/tool-withheld comparisons, or statistical analyses were run. Therefore the study has **no experimental evidence yet** for a lifecycle-policy performance claim.

## Follow-up run: version-isolation implementation (2026-10-14)

Focused command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --import-mode=importlib tests/unit/test_tool_lifecycle.py tests/unit/test_registry.py
```

Outcome: **10 passed, 0 failed, 1.10 s** (two deprecation warnings from `tests/conftest.py`). This included the six pure-policy cases, a registry baseline case, helpful and harmful late-v1 feedback after v2 registration, and migration of an unattributed legacy `shared` state. Candidate v2 remained excluded from shared-only lookup.

Full repository command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --import-mode=importlib
```

Outcome: **63 passed, 5 failed, 42.75 s**. Failures:

1. `tests/e2e/test_api.py::test_task_submit`: mock orchestrator lacks `runtime_manager`.
2. `tests/e2e/test_cli.py::test_cli_tools_list`: CLI invocation did not use the seeded registry.
3. `tests/unit/test_code_agent.py::test_code_agent_simple_execution`.
4. `tests/unit/test_code_agent.py::test_code_agent_tool_rpc_call`: both mocks reject the production `shared_only` keyword.
5. `tests/unit/test_runtime.py::test_runtime_execution_timeout`: expected return code `-9`; Windows implementation returned 1 with `timed_out=True`.

The focused run was repeated after a conservative-migration adjustment and passed again. `python -m compileall -q src tests` also completed successfully. These are software regression results only; no LLM tasks, paired utility measurements, or research hypotheses were evaluated. See the manuscript for interpretation and limitations.
