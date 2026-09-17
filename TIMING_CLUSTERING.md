# Timing-aware clustering

`TimingCluster.py` creates deterministic, inspectable standard-cell clusters
from the same `PlaceDB` arrays used by DREAMPlace.  It is intended as the
front-end contract for a reduced physical/timing database.

## Standalone run

Run from the directory against which paths in the JSON file are relative.  For
the installed ICCAD examples this is `bin`:

```bash
cd /mnt/hdd1/XP_timing_4.1/bin
python3 dreamplace/TimingCluster.py test/iccad2015.ot/superblue1.json \
  --output results/superblue1/timing_clusters \
  --max-cells 32 --max-boundary-pins 24 --max-timing-arcs 64
```

The output directory contains:

- `summary.json`: counts, limits, and enforced invariants.
- `cluster_map.jsonl`: cluster members and original boundary nets.
- `clusters.csv`: one-row-per-cluster statistics.
- `boundary_pins.csv`: abstract pins mapped to original nets.
- `cluster_graph.dot`: a bounded graph for Graphviz inspection.

For example, render the graph with:

```bash
dot -Tsvg results/superblue1/timing_clusters/cluster_graph.dot \
  -o results/superblue1/timing_clusters/cluster_graph.svg
```

## DREAMPlace hook

Add the following to an ordinary DREAMPlace JSON configuration:

```json
{
  "timing_clustering_flag": 1,
  "timing_cluster_output_dir": "results/superblue1/timing_clusters",
  "timing_cluster_max_cells": 32,
  "timing_cluster_max_boundary_pins": 24,
  "timing_cluster_max_timing_arcs": 64
}
```

The clustering pass runs after `PlaceDB` loading and initial OpenTimer STA.
The integrated flow reuses the initialized timer. Standalone runs initialize
OpenTimer once. Neither flow constructs placement RC for this initial table.

## Original-cycle protection

Before edge export, `detect_cyclic_cells()` identifies strongly connected
components (SCCs) in the physical cell→net→cell graph. Sequential cells cut
feedback paths; all other physical cells participate, including non-candidates,
fixed cells, macros and critical cells. Candidate, domain and weight filters
must not hide a return path. Unknown/INOUT directions are treated
conservatively as bidirectional; this is a structural check, not proof of an
active pin-specific timing loop.

Protection is automatic, including `--edges-only` and direct
`build_timing_edgelist()` calls. It writes:

- `cyclic_cells.tsv`: cell_id, cell_name, scc_id, scc_cells,
  was_cluster_candidate, sorted by cell_id. SCC sizes count physical cells.
- `cyclic_cells_summary.json`: graph model, SCC counts and protected counts.

Edges with a cyclic driver **or** sink are omitted; fanout weights still use
the original sink count. Only actual SCC members are protected, not acyclic
downstream cells left over from topological sorting. Greedy clustering also
excludes cyclic cells; noncyclic topological residuals remain singletons.
The original cells, nets and saved database are not deleted or changed.

`LeidenCluster.py` needs no change. Regenerate `timing_edges.csv`, then rerun
`TimingEdgeAggregate.py` and Leiden using the new files. Its contraction-cycle
check remains necessary: merging an originally acyclic graph can create a new
cycle. Because `--saved-db` still contains original cycles, Leiden may still
require `--original-cycles retain`; filtering weighted edges does not remove
cycles from that full structural graph. Existing output files are not migrated.

## Initial STA edge table

Initial STA now protects cells on the **100 worst MAX/setup paths** by default,
including positive-slack paths. `--critical-paths K` changes the budget;
`--critical-slack-ps S` additionally requires path slack <= S ps within that
budget. `--critical-paths 0` explicitly disables protection. JSON equivalents
are `timing_cluster_critical_paths` and `timing_cluster_critical_slack_ps` (null
by default). This is bounded top-K protection, not exhaustive critical-cell
discovery, not hold analysis, and not placement-RC timing.

Before edge export, `extract_critical_cells(timer, output_dir)` writes:

- `critical_cells.tsv`: sorted cell_id, cell_name, worst_path_slack_ps,
  path_count, was_cluster_candidate. FF/macro endpoints are included if traversed.
- `critical_paths.jsonl`: exact traversed pins/cells, transitions, path slack,
  arrival times in ps. Primary IO points have null cell IDs.
- `critical_cells_summary.json`: policy, counts and `path_budget_limited`.

Fixed macros can be renamed into `<original>.DREAMPlace.Shape<N>` physical nodes.
When the original cell name is absent, `_resolve_timing_cell()` uses the exact
path pin's `pin2node_map` entry and verifies a fixed node with that cell's shape
name. Unrelated owners, movable nodes and unverified names still raise errors.
`critical_cells.tsv` keeps physical `cell_id`/`cell_name` and appends
`timing_cell_name` (original OpenTimer name) and `mapping_method` (`exact_name`
or `fixed_shape_pin`). Path points include `physical_cell_name` and
`mapping_method`; the summary includes `fixed_shape_mapped_cells`. Only the
pin-owning representative shape is recorded; other fixed shapes remain anchors.

`Timer.report_timing_paths()` uses a new C++ binding; rebuild/install `timing_cpp`
along with Python changes. It returns actual traversed cells, **not every sink
of each path net**. Missing timing paths (while enabled), nonfinite path slack,
or an unmapped path cell are errors, not silently unprotected designs.
The normal `run()` flow invokes extraction automatically, also with `--edges-only`.
Direct users of `build_timing_edgelist()` must call extraction first to enable it.

Any edge with a protected driver OR sink is omitted. Original fanout still counts
all original sinks including protected ones. Original domain/topology information
is preserved; greedy clustering also excludes protected cells and treats them as
barriers in the full topological order. Protection prevents merging, not physical
movement. External membership files must likewise omit protected cells before
reduced-Liberty/Verilog generation; those converters do not read this list themselves.
Existing results are not updated automatically; use a fresh output directory and
rerun cell-edge aggregation afterward.

```bash
cd /mnt/hdd1/XP_timing_4.1/bin
python3 dreamplace/TimingCluster.py test/iccad2015.ot/superblue1.json \
  --edges-only --critical-paths 100 --output results/superblue1/timing_edges_protected \
  --timing-tau-ps 100 --timing-alpha 4
```

`ClusterEngine.build_timing_edgelist(timer, output_dir)` streams
`timing_edges.csv` and returns statistics saved as `timing_edges_summary.json`.
Its timer argument must have completed `update_timing()`. The normal clustering
command also produces these files before running the existing greedy algorithm.
This change prepares edges for Leiden; it does not yet execute Leiden or change
the greedy algorithm to use weights.

Each row represents one driver pin -> sink pin connection and includes node
IDs/names, pin names, net ID/name, launch/capture domain masks, original fanout,
fanout weight, rise/fall setup slack, worst finite setup slack, slack status,
criticality, timing weight and combined weight. Slack is in **picoseconds**,
converted using OpenTimer's time unit. It is sink-pin slack, not a difference
between driver and sink slack.

```
fanout_weight = 1 / sqrt(number of original sink INPUT pins)
criticality = exp(-max(slack_ps, 0) / tau_ps)
timing_weight = 1 + alpha * criticality^2
weight = fanout_weight * timing_weight
```

FF sinks count towards fanout even though they are excluded from the edge table.
Missing/nonfinite slacks are blank, with criticality zero and timing weight one.
If only one transition is valid it is used and marked `partial`. Defaults of
100 ps and alpha 4 are tunable experimental settings, not calibrated values.
For the DREAMPlace JSON use `timing_edge_tau_ps` and `timing_edge_alpha`.

Only combinational candidate endpoints with equal launch AND capture masks are
included. Unknown/multiple capture domains, multiple launch domains and known
launch/capture clock crossings are excluded. Launch mask zero denotes no traced
FF source (e.g. a PI cone), not a resolved input-delay clock domain. Multi-driver,
undriven and inout nets are excluded and counted. Duplicate cell pairs through
different pins/nets remain separate; a Leiden consumer should sum their weights.

Clock extraction currently supports simple `create_clock ... [get_ports ...]`
and Liberty `clocked_on` plus structural instance declarations. It is not a full
SDC clock interpreter: generated clocks, PI/PO delay-domain assignment and complex
Tcl expressions need further support. An unrecognized FF clock gets an unknown
mask instead of being assumed to belong to the only declared clock. The current
edge table is conservative and may omit valid edges in unsupported designs.

## Enforced boundaries

Sequential cells, macros, fixed objects, IOs, and propagated clock-tree cells
are anchors and are never absorbed.  Data clusters only grow through a direct
predecessor, have a single clock-domain mask, are contiguous intervals of a
topological order, and obey boundary-pin and conservative input/output arc
limits.  Nodes in combinational cycles remain singleton clusters.

## Current integration boundary

### Cell-pair aggregation

`TimingEdgeAggregate.py INPUT OUTPUT --merge sum` merges repeated directed
`(driver_id, sink_id)` pairs and sorts them numerically by driver ID, then sink
ID. It also writes `OUTPUT_STEM_cells.tsv`, containing `cell_id` and `cell_name`
sorted numerically by ID. The map covers IDs present in the input edges only;
excluded FF/macro/IO and other absent cells are not included. Conflicting names
for the same ID are rejected.

Both outputs use tabs by default. Select `--delimiter comma`, `--delimiter tab`,
`--delimiter '\t'`, `--delimiter semicolon`, or `--delimiter pipe` as needed.
Use `--cell-map PATH` to name the mapping table. Input tab/comma detection is
automatic; `--input-delimiter` explicitly selects another separator. Existing
output files are not overwritten. For example:

```bash
python3 /mnt/hdd1/XP_timing_4.1/dreamplace/TimingEdgeAggregate.py \
  timing_edges.csv cell_edges.tsv --cell-map cell_names.tsv --merge sum
```

### Reduced timing model

This implementation produces the reduced graph contract and invokes it from
DREAMPlace, but deliberately does **not** fabricate characterized Liberty
tables.  Consequently the current `Timer` still reads the original OpenTimer
netlist.  Replacing it with a genuinely reduced OpenTimer database requires a
separate characterization step that emits early/late Liberty arcs plus reduced
Verilog/SDC; using guessed delays here would make timing-driven net weights
misleading.  `cluster_map.jsonl` and `boundary_pins.csv` are the inputs for that
next step.
