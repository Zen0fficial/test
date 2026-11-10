# Joint framework coding standard

These rules apply to `futu_bridge/joint/`. Existing provider modules retain
their original contracts and are not rewritten to satisfy a new style policy.

1. **Explicit contracts:** type-annotate every function argument/return; document
   every module, class, and function. Public input validation raises exceptions
   rather than assertions that disappear under optimized Python.
2. **Read-only provider scope:** no broker order-submission calls or live
   execution capability. Native offline simulators and local Decimal ledgers
   may record artificial fills; they must have no brokerage routing client.
   All data comes through the fixed provider boundary or a verified capture.
   `paper_nautilus.py` and `lifecycle_nautilus.py` are the explicit Python exceptions allowing native
   `Strategy.submit_order` against `BacktestEngine`. Its imports are checked
   for live/adaptor dependencies. LEAN's `lean_paper.cs` rejects `LiveMode` and
   runs only with `BacktestingTransactionHandler`; live observers are separate.
3. **Numeric and temporal correctness:** retain source decimals and integer UTC
   nanoseconds; distinguish retrospective availability assumptions from observed
   receipt. Specify each approximation's tolerance and unit. No future filling.
4. **Isolation:** keep optional framework imports inside workers. Qlib, Nautilus,
   and MLflow share one pinned Python environment; the provider remains unchanged.
   Invoke child processes with argument lists, never shell strings. Check exit codes and
   use bounded timeouts for bounded jobs. Never auto-install into a data runtime.
5. **Failure semantics:** no bare exception handlers or swallowed backend/data
   failures. Preserve logs and failed status. Fallback is a pre-run capability
   decision, never a way to convert failed verification into success.
6. **Reproducibility:** use new output directories, input hashes, atomic reports,
   deterministic backend policies, declared versions and explicit units. Do not
   overwrite earlier evidence or certify latency from incomparable timings.
7. **Maintainability:** no wildcard imports, trailing whitespace, tabs, or lines
   longer than 119 characters. Prefer small shared functions for correctness
   policies and retain native algorithms behind narrow process adapters.
8. **Verification:** test adversarial boundaries as well as happy paths. Separate
   synthetic contract tests from real-data integration evidence. Never report a
   skipped/unavailable engine or a silent market as a passing live test.

Run the dependency-free structural gate and local tests:

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.quality
.\.venv-joint\Scripts\python.exe -m unittest futu_bridge.tests.test_joint -v
.\.venv-joint\Scripts\python.exe -m unittest discover -s futu_bridge/tests
```

The structural gate recursively parses and compiles every joint framework module,
including the storage package, and checks
annotations/docstrings/style, and rejects several forbidden constructs. It is
**not semantic static type checking**. Ruff/mypy installation was attempted in
an isolated tooling environment, but this machine's network/TLS connection
prevented downloading them. No Ruff or mypy pass is claimed. The source remains
fully annotated, and runtime contract/unit/integration checks provide the
currently executed verification. Add semantic static checking when tooling is
available; do not suppress diagnostics merely to claim a clean run.

The local LEAN build retains upstream package-security warnings already present
in the pinned source. This change does not upgrade those dependencies or claim
to remediate them. See the build logs when reviewing that runtime.
