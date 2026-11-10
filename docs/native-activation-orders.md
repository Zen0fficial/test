# Native Nautilus activation gates

`futu_bridge.joint.nautilus_activation` exposes trailing-stop-market and
trailing-stop-limit tickets with native activation prices. Both funded buy and
sell paths use `BacktestEngine`; orders remain dormant until the first eligible
bid/ask touch. The native trailing calculation then follows favorable prices.
Cancellation before activation leaves the order unfilled.

The real HK.00700 capture supplies five held-out sessions. Forty cases cover two
sides, two order types and reachable/dormant activation gates. An independent
Decimal oracle checks the first causal activation, fills, pre-activation updates,
fees, holdings and terminal state. Five additional funded sell market-to-limit
cases cover the native reverse side of the existing buy/partial-conversion cases.
The 45-case acceptance report is
`output/joint/nautilus_activation_final_20261110/report.json`.

```powershell
.\.venv-joint\Scripts\python.exe -m futu_bridge.joint.nautilus_activation `
  --response output/joint/qlib_native_rl_data_20261109/provider_response.json `
  --output output/joint/activation_new
```

Prices come from genuine observed minute opens. The five-bps derived spread,
100-share depth, immediate settlement and current IBKR tiered fees are declared
simulation assumptions. This verifies native `BID_ASK` activation and price
offsets; it does not reconstruct exchange queues, mark/index prices or auctions.
The local regression tests re-audit all forty artifacts and reject fee drift,
early fills/updates, changed activation flags and incorrect final cancellation.
