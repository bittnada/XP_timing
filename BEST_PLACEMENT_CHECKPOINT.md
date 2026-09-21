# Timing-quality placement checkpoint

Timing-driven global placement now keeps two aligned checkpoints:

- `density`: the position with the lowest overflow, used only as a fallback;
- `quality`: a feasible position evaluated by fresh STA, filtered unweighted
  HPWL, and overflow at the same coordinates.

Only timing-update iterations can replace the quality checkpoint. At the end
of the final global-placement stage, the quality checkpoint is restored. If no
timing update reached the overflow limit, the density checkpoint is restored.
Final WNS, TNS, HPWL, overflow, and congestion are recomputed after restoration.

Overflow uses a non-cumulative floor policy. The tracker remembers the lowest
overflow observed at any feasible timing candidate, independently of which
quality position is selected. A candidate may regress from that historical
floor by at most

```text
max(best_checkpoint.overflow.absolute_tolerance,
    best_checkpoint.overflow.relative_tolerance * overflow_floor)
```

The allowance is always measured from the historical floor, not from the last
accepted checkpoint, so repeated updates cannot accumulate density regression.
When `lock_at_stop` is enabled and the floor reaches `stop_overflow`,
`stop_overflow` itself becomes the cap. The absolute `hard_limit` applies first.

Overflow remains part of the normalized quality score. There is no additional
degradation-proportional score threshold, because that would penalize the same
overflow regression twice. Every admissible candidate instead uses the common
`minimum_improvement` threshold. Setting both overflow tolerances to zero
restores strict monotonic behavior.

The quality score minimizes normalized penalties with default weights:

```text
0.4 * negative-WNS + 0.3 * negative-TNS
+ 0.2 * filtered-unweighted-HPWL + 0.1 * overflow
```

Normalization references are fixed by the first feasible timing candidate, so
later net-weight updates cannot change the comparison scale. The optimizer's
weighted HPWL is not used for checkpoint selection.

## Filtered unweighted HPWL

The checkpoint HPWL gives every retained net weight 1. Net degree is used as a
filter, not as an additional multiplier. By default it excludes:

- nets with fewer than two pins;
- nets whose degree is greater than or equal to `ignore_net_degree`;
- placement-only synthetic nets;
- surviving DEF `USE CLOCK` nets (MakeDB normally removes non-SIGNAL nets
  before constructing placement arrays);
- names matched by `best_checkpoint.hpwl.exclude_net_regex`, including `MODCSA`,
  `CriticalPathNet`, and common clock-name forms by default.

Explicit DEF `USE CLOCK` metadata is retained in newly exported physical DBs.
Older snapshots fall back to the name regex; re-export the physical DB for
complete USE-based filtering.

## Parameters

The active V4 configuration is:

```json
"best_checkpoint": {
  "enabled": 1,
  "overflow": {
    "hard_limit": 0.2,
    "absolute_tolerance": 0.01,
    "relative_tolerance": 0.10,
    "lock_at_stop": 1
  },
  "hpwl": {
    "max_net_degree": 100,
    "exclude_net_regex": "(?i)^(?:MODCSA|CriticalPathNet)|(?:^|[/_.])(?:clk|clock)(?:$|[/_.0-9])"
  },
  "score": {
    "weights": {
      "wns": 0.4,
      "tns": 0.3,
      "hpwl": 0.2,
      "overflow": 0.1
    },
    "minimum_improvement": 0.001
  }
}
```

`hard_limit <= 0` selects
`max(2 * stop_overflow, stop_overflow + 0.05)`. A maximum degree of zero reuses
`ignore_net_degree`. Set `enabled` to zero to keep the last global-placement
position as before. Nested groups may be partially specified; omitted members
use the defaults in `params.json`. Unknown nested keys and obsolete individual
`best_checkpoint_*` keys are rejected to expose configuration typos or stale
files.

With the defaults, a floor of `0.14` gives a tolerance of `0.014`. A candidate
at `0.15` remains physically admissible and competes using the same 0.1% score
improvement threshold after its overflow score penalty. A candidate above
`0.154` is rejected regardless of timing or HPWL improvement.
