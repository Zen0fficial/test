# Native LEAN process-loss recovery

This converter reconstructs an **offline simulated** LEAN execution in a fresh
process from immutable real-market files and prescribed commands. It does not
claim that LEAN serializes all engine memory or that a simulated checkpoint can
reconcile orders sent to a real broker. Providers, the shared Python environment
and the pinned LEAN checkout revision remain unchanged.

## Recovery contract

`lean_recovery.run(response, output, repo)` uses the shared Python coordinator
and existing pinned .NET launcher. It converts the frozen real HK.00700 minute
capture once, then uses the same immutable native data files for interrupted,
resumed and reference processes. Each scenario manifest binds market files,
commands, converter source, native binaries and the shared dependency lock by
SHA256. The native algorithm checks those files before initialization; replay
refuses drift rather than silently using a newer converter or a different input.

The six scenario families exercise:

| Boundary | State that must exist at interruption | Subsequent work |
|---|---|---|
| Partial limit cancellation | 100 shares filled out of 200, positive inventory, working remainder | Native scheduled cancellation |
| Partial limit amendment | 100 shares filled, original ticket still working | Native scheduled price amendment and later cancellation |
| Working stop | Submitted, unfilled stop ticket | Scheduled stop-price amendment and later matching |
| Latched stop-limit | Unfilled working ticket with the fill model's trigger latch already set | Scheduled limit-price amendment and finite fills |
| Working trailing order | Submitted trailing ticket with native stop/offset state | Scheduled stop/offset amendment and matching |
| Triggered trailing partial remainder | Actual native first partial-fill callback, 100 shares held | Latched market remainder on subsequent real quote |

Clock-only custom data advance native scheduling between observed quotes. They
do not replace prices, replenish market liquidity or masquerade as additional
equity ticks. A converter subclass guards the existing conditional fill model
against matching on those clock-only instants. Existing limit/stop fill models
already enforce equivalent observation freshness.

For each case the coordinator:

1. Starts a native .NET backtesting process.
2. Waits for its selected native event callback to flush an immutable checkpoint.
3. Kills that actual process while the callback is blocked, before normal engine
   cleanup or final-result publication.
4. Starts an independent uninterrupted reference process.
5. Starts a fresh replay process which reconstructs the prefix and compares its
   native boundary with the saved checkpoint **before allowing suffix execution**.
6. Requires exact prefix, suffix and final economic state equality across the
   processes, then independently checks fills, fees, cash and inventory against
   the actual source observations and cumulative Decimal fee arithmetic.

The snapshot contains native ticket identities, order type/status/quantity,
filled amount, limit/stop/trailing fields, exposed stop-limit latch, callback
journal, pending and requested scheduled commands, account cash, holdings,
average price, realized profit, cumulative fees and current cached market state.
Native scheduler state, private matching state and the trailing market-remainder
latch are reconstructed by deterministic replay. They are not described as
directly serialized heap objects.

Recovery cost is proportional to the replayed prefix. The implementation is
single-instrument and uses the existing declared HK continuous-session/finite
liquidity policy. It does not add an exchange queue model, live feed-gap repair,
option exercise, multileg reconciliation or external brokerage recovery.

## Usage

```powershell
.venv-joint/Scripts/python.exe -m futu_bridge.joint.lean_recovery `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/new_lean_recovery_verification
```

Use a new output directory. The command compiles only converter-owned classes
in the existing pinned native checkout. Do not replace its launcher DLLs while
the acceptance processes are running. It uses no QuantConnect account, paid
data service, new Python environment or brokerage connection.

The real source capture has SHA256
`7e35cdb971bfcfb1f5c6a83ae6db6ad464dda5d6f0414197411f1d15ef6a9128`.
The five holdout sessions are 2026-11-02 through 2026-11-06. Bid/ask quotes are
the previously documented adverse five-basis-point, tick-rounded transformation
of actual observed minute opens. The 100-share execution capacity is explicit
simulation policy, not a historical exchange queue reconstruction.

## Failure handling and local tests

A changed input, source, binary or shared lock rejects startup. A changed
checkpoint rejects suffix execution and produces only a diagnostic boundary
comparison artifact, never a successful final result. The coordinator bounds
process waits, kills only workers it owns and verifies that the interrupted
worker did not finish normal cleanup before it was killed.

JSON.NET checkpoint parsing explicitly keeps clock strings and decimal numeric
values intact. Its default automatic DateTime/double conversion would otherwise
make structurally equal economic checkpoints compare unequal to the in-memory
native snapshot.

Local executable tests cover the real scenario reports, required boundary
states, duplicate/missing fees, clock-only fills, input drift and a corrupted
checkpoint submitted to a fresh native replay process. Tests and market captures
remain private/local under the repository publishing policy.
