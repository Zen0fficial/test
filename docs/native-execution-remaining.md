# Execution coverage after the November 10 additions

This status supersedes stale pending rows in the earlier native order inventory
without changing its source-bound historical evidence. It distinguishes tested
contracts, remaining implementation work and genuine missing-input limits.
It does **not** declare P5/P6 or all upstream features complete.

## Newly closed scoped gaps

| Component | Current evidence and exact scope |
|---|---|
| Nautilus partial-parent groups and recovery | 15 integral-lot PARTIAL OTO cases; 31 killed-process reconstruction cases including active children, pending commands and timers |
| Nautilus types, triggers and offsets | 50 additional native type/rejection cases, 36 real trade/BBO joint-trigger executions and 18 native offset calculations |
| Nautilus activation and funded sell conversion | `nautilus_activation_final_20261110`: 40 native activation-price trailing cases plus five funded sell market-to-limit cases |
| Native LEAN conditional orders | 60 trailing/touched cases, including percentage trailing and finite post-trigger remainders |
| Native LEAN process recovery | 30 actually interrupted separate-process single-instrument order/account reconstructions; replay from genesis, not heap checkpoints |
| Native LEAN contingent groups | 55 OCO/OUO/complete-parent OTO, amendment/cancel/partial-fill and native rejection cases; see `lean-contingent.md` |
| Native LEAN equity combos | 30 cases for all three native combo types, whole-group completion, amendments/cancellation and native Day/date-end GTD; see `lean-combos-immediate.md` |
| Joint LEAN IOC/FOK | 60 explicitly adapted same-observation intent cases; native underlying tickets remain Market/Limit with GTC |
| Native Nautilus indicators | Every 37 concrete pinned public classes across 48 supported bar/quote routes, immutable Parquet and explicit warmup/reset semantics |

All new execution cases use real captured prices and simulated accounts. Native
engine semantics and declared joint adaptations remain separately identified.
The current selected daily basket supplies genuine same-date multi-equity prices;
it is not a point-in-time historical constituent universe.

## Remaining implementable engineering work

These are not missing-data excuses. Existing bars and the selected real daily
panel are sufficient for meaningful initial acceptance of the following work.

1. **General cross-framework strategy contract.** Current shared contracts expose
   market data, and the lifecycle runners consume prescribed HK-specific task
   dictionaries. The canonical comparison's daily ML simulation and native
   scheduled-command conformance remain distinct. A typed common event/intent
   contract still needs market availability, order/group identities, amendments,
   cancellations, targets, position/cash events, supported parameter negotiation
   and engine-specific semantics. The same causal model outputs should drive
   Qlib, Nautilus and LEAN adapters, with independent account checks and explicit
   unsupported branches. A shared capture or similar scenario name alone does
   not close this integration.
2. **LEAN grouped-order reconstruction.** Existing recovery verifies single-order
   conditional/partial states; it does not yet kill/reconstruct a worker with
   active OCO/OUO/OTO links or multileg groups. Extend immutable command/state
   journals with native group/contingency identities and activation/ratio state,
   then compare economic boundaries and suffixes in separate workers.
3. **Composed combo contingencies.** Pinned LEAN supports relating complete combo
   units through `Triggers`, OCO and OUO. Separate combo and contingent tests do
   not establish that combined path. Native `.Bracket` convenience rejection
   on a combo is not absence of native composition support. Daily two-equity
   observations can test complete-group activation/cancellation without options.
4. **Finite multileg partial ratios.** The new combo adapter deliberately requires
   complete legs. A separate ratio-preserving capacity model and group-level
   accounting oracle are needed for partial multileg execution. Declared
   simulated capacity can support such engineering tests; historical queue
   realism requires additional data. Native OTO complete-parent semantics must
   not silently be replaced by Nautilus PARTIAL behavior.
5. **Broader native composition and configuration.** The existing Nautilus
   Actor/Strategy batch and LEAN Alpha/Insight/portfolio/drawdown cases are scoped
   examples. Native execution algorithms, more portfolio/risk model families,
   reusable typed component composition, and broader native aggregation and
   consolidation variants still need joint entrypoints and behavioral tests.
   Same-currency two-equity combo accounting is now verified; that does not
   certify general multi-currency or derivative portfolio accounting.
6. **Remaining research catalogues.** Complete pinned Nautilus indicator-class
   coverage does not close the LEAN indicator/consolidator catalogue or every
   numeric parameter and input-route combination. Native reset defects and
   source-specific feature meanings must remain visible. Optional analysis/model
   integrations and separately operated distributed services have their own
   acceptance prerequisites; a family-level capability flag is insufficient.
7. **Expanded interaction matrix.** Additional combinations of order types,
   TIFs, triggers, partial fills, amendments, cancellations and latency/recovery
   remain possible. The current case totals are explicit bounded matrices,
   not the Cartesian product of all pinned native parameters.

## Native and real-input boundaries

* Pinned Nautilus auction TIF matching and PRICE_TIER trailing offsets reject
  those requests. Native LEAN OTO releases only complete parents; native OCO
  partial cancellation propagates to the winning remainder. These native facts
  are different from joint integration gaps. A replacement policy would require
  an explicit faithful contract and independent verification.
* The two-equity combo model uses actual daily opens, assumed 09:30 availability,
  zero spread and declared simulation lots. Intraday multileg price alignment,
  historical instrument metadata, true liquidity and spread effects require
  additional genuine observations; they are not inferred from daily OHLCV.
* Auction execution needs dated auction observations and verified HK auction
  calendars/rules. Ordinary continuous-session opens do not supply those.
* Option exercise, option combos and general settlement need dated contracts,
  multipliers, expiry/exercise/settlement inputs, appropriate option prices and
  underlying/currency observations. Equity-only acceptance cannot certify them.
* MARK_PRICE/INDEX_PRICE trigger semantics require the actual product-specific
  fields. Ordinary last equity price is not a verified substitute.
* Snapshot depth is not a lossless exchange event stream or individual-order
  queue. The existing genuine BBO capture has constant prices, and historical
  daily/minute point-in-time availability remains unknown. Real queue-position,
  gap-repair and changing-spread claims need suitable capture evidence.
* Genuine trade-level VWAP remains distinct from the native bar typical-price
  volume proxy. Unverified turnover units must not be guessed to close the gap.
* Live brokerage routing remains intentionally disabled for manual trading.

Future coverage reports should retire a row only after its joint entrypoint,
documented contract, adversarial cases, immutable input/source bindings and
current-source real-data evidence all exist.
