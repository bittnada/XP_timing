# Two-DB timing-driven placement

`db_option: "two_timing_placement_db"` restores a reduced physical DB for
placement and an original physical DB + OpenTimer binary model for CPU timing.
It does not use reduced Liberty/Verilog and never silently re-parses inputs or
exports/replaces a DB in this mode.

## Run the existing superblue1 design

Run from `/mnt/hdd1/XP_timing_4.1/bin`:

```sh
# Once after replacing timing_cpp: explicitly regenerate ONLY the timing model.
python3 dreamplace/TimingCache.py dreamplace/examples/rebuild_superblue1_timing_cache.json

# Subsequent placement runs read existing snapshots without parsing text inputs.
sh ./run_two_db_timing.sh
```

The first command uses `def + binary_write`, but the timing-only CLI does not
read/export the physical DEF/LEF DB. It recompiles the original LIB/Verilog/SDC
model and publishes a new `timing_cache/manifest.json`. Previously generated
`model-*.bin` files are retained. Physical snapshots and ID_MAPPING are unchanged.
Do not run this export at the same time as a placement reading that cache.
It is **not** automatically invoked by the placement wrapper. No backend hash or
cache compatibility checks are bypassed. Cache restore is read-only.

For a different design, edit copies of the two JSONs. RC values in the example
are copied from the existing superblue1 configuration, not general process rules.

```sh
TC_CONFIG=/path/to/my_two_db.json sh ./run_two_db_timing.sh
```

| Configuration | Meaning |
|---|---|
| `placement_db_path` | Reduced saved DB root, containing `physical_db/` |
| `timing_db_path` | Original saved DB root, containing `physical_db/` and `timing_cache/` |
| `placement_timing_mapping_path` | Checked numeric `ID_MAPPING/` directory |
| `timing_update_start` | First feedback after this many optimizer steps (default 500) |
| `timing_update_interval` | Subsequent feedback spacing (default 15) |
| `wire_resistance_per_micron` | Wire resistance in ohms/micron |
| `wire_capacitance_per_micron` | Wire capacitance in farads/micron |

Both `timing_opt_flag` and `enable_net_weighting` must be 1. The example uses
saved initial positions (`random_center_init_flag=0`) with the configured small
GP noise. Set `gp_noise_ratio=0` for no initial perturbation. `read_posX`,
`read_posY`, `read_orient`, and output flags refer ONLY to reduced placement IDs.
Final DEF/positions are reduced; original cells are not legalized/unclustered.

## Execution and memory ownership

1. Validate cache compatibility, restore the reduced physical DB and initialize
   its placement structures. Restore the original physical DB on CPU only.
2. Verify both DB ID fingerprints, mapping checksums and node/net/pin ownership.
3. Restore the original OpenTimer model. Initial STA does not change weights.
4. Run reduced placement on the requested device; no original cell GPU arrays,
   density operators or filler cells are allocated.
5. After steps 500, 515, 530, ... project current reduced coordinates to the
   original timing view, rebuild original-net RC, run STA, update original
   criticality/weights, and gather weights into the reduced DB/GPU by net ID.
6. Final STA also uses the same bridge (without a final weight update). Existing
   HPWL/overflow/congestion evaluate the reduced placement, while WNS/TNS refer
   to the original graph at projected pin positions.

Early convergence can end GP before step 500: a warning reports that no weight
feedback occurred. Increase the GP budget or lower `timing_update_start` for a
short smoke test. A budget is not a minimum iteration guarantee.

## Coordinates, connectivity and weights

For reduced cluster lower-left `(x,y)` with current dimensions `(W,H)`, every
original member pin has timing position `(x+W/2, y+H/2)`. Original offsets are
not added. Unclustered nodes use current reduced coordinates and pin offsets,
including final orientation changes. Fillers are excluded from ID mappings.
Placement normalization, shift and different DEF DBU units are converted
explicitly; native RC receives microns to avoid raw-DBU FLUTE integer overflow.
The original saved arrays are never modified.

Same-coordinate pins stay distinct electrical terminals: attach every duplicate
to its representative with **zero wire resistance**, preserving pin pointers
and sink capacitances. Internal cell arcs/delays remain in OpenTimer. Nets are
not electrically merged across cluster boundaries. Internal nets omitted from
the placement DB still receive projected geometry in the original timing DB.

For each reduced net `p`, weight is copied, not summed/maximized:

```text
placement.net_weights[p] = original.net_weights[placement_net_to_timing_net[p]]
```

Original criticality and momentum history remain in original ID order. Updates
are in-place so existing wirelength/preconditioner tensors see the new weights.
CPU and CUDA copies are handled explicitly. Cached Nesterov objective/gradients
are refreshed after weights change. Missing or negative map IDs are not used as
array indices. Source DB files remain unchanged; dynamic weights live in memory
for this run (they are not silently exported back to binary snapshots).

## Limits and interpretation

- Full original CPU memory and STA cost remain. Only placement GPU structures
  are reduced. This is an approximate timing-driven placement, not extracted
  signoff timing: co-located internal wires contribute zero RC.
- RC uses the existing FLUTE/Elmore model and `ignore_net_degree` policy.
  Very-high-degree nets are still treated according to that policy; this does
  not constitute complete clock-tree modeling.
- Original physical DB omissions are not reconstructed by mapping. Mapping IDs
  are MakeDB IDs, not OpenTimer's internal IDs. Native connectivity validation
  rejects disagreement rather than guessing a correspondence.
- Routability inflation, macro-stage halos, and in-run clustering are rejected
  in this mode to avoid inconsistent projected geometry. Ordinary legalization
  and final evaluation use the bridge. The example initially disables
  legalization because multi-row supercells need separate legality validation.
- The core `TimingOpt` function still uses FLUTE LUTs relative to the working
  directory; run from installed `bin/` as above.

## Shared-memory master / client

See [SHARED_MEMORY.md](SHARED_MEMORY.md) for the flag-based master/client
commands, what stays shared, and how to restart a dead snapshot.

Publish the immutable two-DB binaries once, then start many clients that only
read their own `read_posX` / `read_posY`.

```sh
python3 dreamplace/SharedMemoryServer.py \
  --timing-db results/superblue1/timing_flow_v2/save \
  --placement-db results/superblue1/timing_flow_v2/PLACEMENT_U0.7/save \
  --mapping results/superblue1/timing_flow_v2/PLACEMENT_U0.7/ID_MAPPING \
  --output results/shared_memory_twodb
```

Keep that process alive. Each client JSON is a normal `two_timing_placement_db`
config plus:

```json
"shared_memory_role": "client",
"shared_memory_dir": "results/shared_memory_twodb"
```

Positions, net weights, GPU tensors and OpenTimer RC/STA stay private.
Connectivity, names, ID maps and the static timing model come from the shared
segment. `"shared_memory_role": "master"` on a two-DB JSON makes `Placer.py`
publish and sleep instead of placing.

## Code and tests

- `TwoDBTiming.py`: restore orchestration, projection, scheduling and weight map.
- `Placer.py`, `BasicPlace.py`, `NonLinearPlace.py`: lifecycle/iteration hooks.
- `ops/timing/src/timing_cpp.cpp`: same-position zero-resistance RC edges.
- `test_two_db_timing.py`: permuted IDs, offsets, units, fillers, weight transfer,
  binary-only end-to-end CPU placement (including deleted text inputs).
- `test_timing_cache.py`: native RC regression, driver-last order, all-coincident
  and partly-coincident pins, translation invariance and repeated RC rebuilds.

```sh
DREAMPLACE_INSTALL=/mnt/hdd1/XP_timing_4.1/bin \
DREAMPLACE_TIMING_CPP=/mnt/hdd1/XP_timing_4.1/bin/dreamplace/ops/timing/timing_cpp.cpython-310-x86_64-linux-gnu.so \
python3 -B -m unittest test_two_db_timing test_timing_cache -q
```
