# Reduced combinational Liberty generation

`ReducedLiberty.py` characterizes **fixed cluster memberships**, using the
existing `ot-shell` executable. It does not cluster the design, use slack as
delay, or change the placement netlist. It produces early and late timing-only
Liberty cells using original cell delay/slew models and pin loads, with **zero
internal wire RC**, and a matching reduced Verilog netlist for STA.

## Input

Membership (`--clusters`), with optional header:

```text
cell_id cluster_id
0 2
1149067 2
```

Name mapping (`--cell-names`), with optional header:

```text
cell_id,cell_name
0,FE_OFC114571_n176682
1149067,FE_OFC114572_n176682
```

CSV, TSV and whitespace-separated tables are accepted. IDs must be nonnegative
integers. Membership must be disjoint; missing/conflicting names fail explicitly.
The name table only needs to cover selected cluster members. All names must
resolve in the **same original netlist** used to assign PlaceDB IDs.

Also supply original flat structural Verilog and the original NLDM libraries
(files or a directory tree). The entire Verilog is scanned to retain external boundary connections,
including FF and macro connections not present in `timing_edges.csv`.

## Run

For a directory containing the libraries for **one intended analysis scenario**:

```bash
python3 /mnt/hdd1/XP_timing_4.1/dreamplace/ReducedLiberty.py \
  --clusters /absolute/path/to/clusters.txt \
  --cell-names /absolute/path/to/cell_names.csv \
  --verilog /absolute/path/to/design.v \
  --lib-dir /mnt/hdd1/PNR/unosilicon/ddi/LIB/LX_lib_20260915 \
  --output /absolute/path/to/new_reduced_liberty
```

`--lib-dir` searches subdirectories recursively, accepts `.lib` and `.LIB`, and
can be repeated for multiple roots. Paths are sorted and deduplicated. Compressed
`.lib.gz` files are not supported. In this mode the same source library set is
used for min/early and max/late analysis. Outputs remain `clusters_Early.lib` and
`clusters_Late.lib`: these are **min/max propagation models for the supplied
scenario**, not newly synthesized fast/slow process corners.

For separate early/late library sets, the existing options still work. Each now
accepts one or more files **or directories**:

```bash
python3 dreamplace/ReducedLiberty.py \
  --clusters clusters.txt --cell-names cell_names.csv --verilog design.v \
  --early-lib /libs/early_std /libs/early_extra.lib \
  --late-lib /libs/late_std /libs/late_extra.lib \
  --output results/reduced_liberty
```

Do not combine `--lib-dir` with `--early-lib`/`--late-lib`. A directory is not an
automatic corner selector: do not point it at an unfiltered collection of
different PVT corners. Incompatible units, nominal conditions or slew/threshold
conventions among **used internal cell libraries** are rejected. Matching
metadata alone cannot prove that sources belong to the same intended corner.
Retained external blocks can use different voltage conditions; their models are
read for boundary pin directions, not included in cluster characterization.

### Duplicate cells and large libraries

The tool first scans the selected netlist, then indexes library cell byte ranges
without tokenizing all cell bodies. It parses only cluster masters and masters
connected to their boundary nets. The source directory is still scanned in full
once per run; the index is reused between early/late within that run, not cached
persistently. Memory use depends on extracted cells and library-level metadata,
not a Python token array of the entire multi-GB directory. Source files must not
change during a run.

A duplicate **required** master causes an error listing its source files. Select
explicitly with repeatable `--cell-override CELL=/absolute/path/to/file.lib`.
For example, if `SDL_TOP` is needed in the LX library directory:

```text
--cell-override SDL_TOP=/mnt/hdd1/PNR/unosilicon/ddi/LIB/LX_lib_20260915/SDL_TOP/SDL_TOP_WCG1.lib
```

This selects just that cell, not the entire file over other libraries. Other
cells from `LX89507_ANALOG_SYN.lib` remain available. Select the appropriate
model for your netlist; this example does not verify that block's pin interface.
`--early-cell-override` and `--late-cell-override` override the common selection
for separate sets. An override naming a file outside its set or a file not
defining the cell is rejected. Unused duplicate masters are listed in the
manifest without blocking the run. Duplicate definitions within a single file
are always rejected.

NLDM templates are renamed with a source-specific prefix, so same-named
templates in different files cannot overwrite each other. Duplicate NLDM
template names within one source are rejected. Inherited template axes are made
explicit in each table for compatibility with this OpenTimer version; axis
ordering and table dimensions are checked before invoking its parser. Source-specific pin defaults
are materialized before merging. Characterization extracts a timing-only NLDM
view: CCS current/waveform and power groups are omitted, not combined or
interpreted. This handles CCS files that also supply supported NLDM delay/slew
tables, but does **not** add CCS waveform, conditional-arc, source non-unate, or
multi-voltage cluster characterization support. Mixed-polarity reconvergence of
otherwise supported unate source arcs is handled as described below.

### Existing superblue1 invocation

From the installation's `bin` directory:

```bash
python3 dreamplace/ReducedLiberty.py \
  --clusters /absolute/path/to/clusters.txt \
  --cell-names results/superblue1/timing_edges/cell_names.tsv \
  --verilog benchmarks/iccad2015.ot/superblue1/superblue1.v \
  --early-lib benchmarks/iccad2015.ot/superblue1/superblue1_Early.lib \
  --late-lib benchmarks/iccad2015.ot/superblue1/superblue1_Late.lib \
  --slews-ps 10,50,100 \
  --loads-ff 1,5,10 \
  --output results/superblue1/reduced_liberty
```

Output must be a **new** directory. `--cluster-ids 0,1,2` selects a pilot subset.
`--ot-shell /path/to/ot-shell` overrides local executable discovery. No torch or
Python packages other than the standard library are needed. File paths are
handled by Python; the shell runs in a temporary directory with simple relative
filenames. `--timeout` limits each OpenTimer subprocess (default 120 seconds).

An example membership for real superblue1 cells is provided at
`examples/reduced_liberty/superblue1_clusters.txt`; it is only a test partition.

### Keep original cells when a cluster fails

The default `--on-cluster-failure abort` still stops at the first failure.
For a full flow, add:

```text
--on-cluster-failure retain-original
--cluster-sizes results/superblue1/timing_flow_v2/leiden/cluster_sizes.tsv
```

In `retain-original` mode, unsupported cluster structures and failed grid/joint
round-trip checks retain **all original cells and connections of that cluster**.
No failed cell is added to either generated Liberty or to the boundary mapping;
processing continues with the next cluster. This does not relax timing tolerances
or automatically split/retry a cluster. A successful run can therefore contain
both verified abstract cells and retained original cells. Load the original
Liberty and LEF files as well as the generated ones when using this design.

Global input/library errors, OpenTimer failures/timeouts, filesystem errors and
final Verilog conversion failures still abort. If no cluster passes, the tool
stops with an explicit message to use the original design, rather than publishing
an empty abstraction as a completed reduced flow. Logs remain available.

`--cluster-sizes` accepts a `cluster_id width height` table (CSV, TSV or whitespace).
All selected cluster IDs must have finite positive dimensions. Extra IDs are
allowed for pilot selections. The filtered output preserves input dimensions
and **units**; it does not infer sizes from Liberty area or convert DBU to microns.
For downstream ReducedLEF, use **both** `cluster_mapping.jsonl` and
`cluster_sizes.tsv` from this Liberty output directory, not the original Leiden
size table. Keep the same `--size-unit` (and DBU conversion, if applicable).

## Output

- `clusters_Early.lib`, `clusters_Late.lib`: one `TC_<cluster_id>` cell per
  verified cluster, pin capacitances and input/output timing tables.
- `cluster_mapping.jsonl`: numeric membership, original names/nets/pins,
  original member masters, abstract boundary pins (`I0`, `O0`, ...), reachable
  timing arcs and senses.
- `reduced.v`: original top-level interface and retained instances, with each
  characterized cluster replaced by one `TC_<cluster_id>` instance.
- `reduced.instances.jsonl`: cluster ID, actual reduced instance name, Liberty
  cell name and boundary pin-to-original-net connections.
- `reduced.summary.json`: before/after instance counts, retained original
  master counts and structural conversion status.
- `final_clusters.tsv`: `cell_id cluster_id` membership for verified clusters
  only; original IDs are preserved, not renumbered.
- `cluster_sizes.tsv`: verified-only dimensions, written when `--cluster-sizes`
  is supplied. Its ID set matches the final boundary mapping.
- `failed_clusters.jsonl`: cluster ID, cell count, reason and action for each
  cluster abstraction failure (empty when none fail).
- `retained_cells.tsv`: `cell_id cluster_id cell_name original_master` for cells
  retained due to abstraction failure, not all unclustered cells in the design.
- `manifest.json`: model assumptions, input paths, sampling grid, per-cluster
  verification errors and completion status. `library_sets` records all resolved
  source files, selected cell-to-file mappings, active internal source conditions
  and unused duplicate cells; `library_mode` records shared or separate sets.
  Only `status: complete` is a
  successful run. It also records `on_cluster_failure`, `failed_clusters`, and
  `retained_original_cells`. An unrecovered failure leaves already emitted cells
  as partial artifacts, explicitly marked `failed_partial_do_not_use`.

Table axes are input transition then total external output capacitance. CLI
axes use ps/fF and are converted to each source Liberty's native units; output
Liberty preserves those units and thresholds. A cluster's input capacitance is
the sum of original internally connected sink pin capacitances. Internal loads
remain in the characterization netlist; swept output load is **external only**.

For each boundary input, all other input arrivals/slews are cleared. Only one
transition (rise OR fall) of the chosen input is activated at arrival zero;
the corresponding output transition is sampled over the slew/load grid,
separately for early and late. This keeps both different boundary inputs and
opposite-polarity paths from competing during table extraction. Actual original
Liberty arcs determine reachability and inversion sense. OpenTimer performs
delay and slew propagation; no hand-estimated delay is substituted.

### Mixed-polarity reconvergence

Default `--mixed-polarity split-arcs` permits both positive and negative paths
between a boundary input/output pair. It emits **two unconditional timing
groups with the same related_pin**, one positive_unate and one negative_unate,
each with its own four NLDM tables. Thus rise→rise, rise→fall, fall→rise and
fall→fall have separate characterized values. Merely emitting one non_unate
group from simultaneously active inputs would lose this distinction and is
not what this implementation does. The local OpenTimer supports these parallel
groups; portability to other timing tools must be verified separately.

`--mixed-polarity reject` retains the previous fail-closed rejection. Source
Liberty arcs explicitly marked non_unate (or missing a sense) remain unsupported;
this feature handles composition of unconditional unate source arcs only.
Membership, master names, boundary pin names and nets do not change, so an
otherwise matching LEF/Verilog need not be regenerated just for this feature.

For mixed clusters, in addition to isolated-transition grid round trips, the
tool tests original and abstract netlists with both input transitions active:
rise arrival 0, fall arrival ±25 ps, and unequal input slews drawn cyclically
from the supplied grid, at all supplied loads. A mismatch fails the cluster;
by default it leaves the manifest `failed_partial_do_not_use`. With explicit
`retain-original`, the cluster's original cells remain instead. Neither mode
publishes that cell as verified. This is sampled graph-based STA equivalence, not an exhaustive
proof for arbitrary input conditions. Correlated internal slew/arrival behavior
can still require splitting or a richer model. No Boolean sensitization, false
path inference, hazards/glitches, functional equation or logic optimization is
performed. Structurally opposite paths may be logically mutually exclusive.

The manifest records `mixed_polarity_policy`, `transition_model`, per-cluster
`logical_io_pairs` and `mixed_polarity_pairs`, and
`joint_transition_max_error_ps` per corner (null for unate-only clusters).
The mapping lists both senses separately. Therefore `arcs` counts timing groups
and can exceed the number of reachable input/output pairs: Leiden's I×O budget
is a pair budget, not an upper bound on the number of polarity-specific groups.

Every generated cell is read back by OpenTimer and compared against its original
cluster at all grid points (tolerance: relative 5e-4 or absolute 0.001ps).
Additional midpoint samples report interpolation error, including output slew,
in `manifest.json`. Midpoint error is **reported, not automatically bounded**;
inspect it and refine the grid for your accuracy target. The 3x3 default is a
pilot setting, not a signoff characterization grid. Source LUT extrapolation
follows OpenTimer's implementation; choose grids suitable for the source models.

## Supported scope

This implementation supports flat, named scalar pin Verilog connections
and unconditional unate source combinational NLDM arcs, including their
mixed-polarity compositions under the checks above. Combinational loops, sequential
members, tristates, conditional/duplicate source arcs, source non-unate arcs,
undriven/multi-driver nets, unconnected related pins and Liberty
bus/bundle cells are rejected. Verilog assigns, behavioral logic, constants,
positional instances and hierarchical modules need further support and fail
instead of being silently misinterpreted.

A boundary output that feeds another boundary output through internal logic is
rejected: load on one output affects another output's delay, which independent
2D input-slew/output-load tables cannot represent. Splitting such a cluster or
implementing a richer approximation is required. Other outputs are unloaded
during characterization.

Models contain timing arcs but no reconstructed Boolean `function`. They are
for placement/STA experiments, not logic synthesis or functional simulation.
Original internal SDC exceptions/modes are not remapped. Apply only to clusters
whose internal paths need no such exceptions; keep exceptions at retained
boundaries or extend the model extraction first. Clock-domain and convexity
partition validation remains the clustering stage's responsibility.

The tool starts small OpenTimer processes for each cluster/corner and verification
pass, reading only used source cells. This prioritizes validation and isolation;
whole-design characterization of hundreds of thousands of clusters will need
benchmarking, model caching and/or a persistent C++ batch worker. Begin with
`--cluster-ids` and inspect errors before scaling up.

## Reduced Verilog and OpenTimer

After all selected clusters are verified or explicitly retained as original cells,
`ReducedLiberty.py` automatically creates `reduced.v`. A failure to convert the netlist also marks
the overall manifest as failed; do not use partial outputs. `--cluster-ids`
replaces only that selected subset; all other original instances remain.

For **previously generated** libraries, no recharacterization is necessary:

```bash
python3 dreamplace/ReducedVerilog.py \
  --verilog /absolute/path/to/original.v \
  --mapping /absolute/path/to/reduced_liberty/cluster_mapping.jsonl \
  --output /absolute/path/to/reduced_liberty/reduced.v
```

The converter also creates `.instances.jsonl` and `.summary.json` sidecars next
to the new `.v`. Existing outputs are never overwritten. If the mapping's
directory has a `manifest.json`, it must have `status: complete`. Use the mapping
from the **matching reduced Liberty generation**, not just a cell/cluster table:
the input/output pin numbering must match the timing tables. Old mapping files
without `original_master` remain supported; new mappings additionally detect
changed master types of selected instances. Do not use a mapping after changing
the original netlist or libraries.

For example, two inverters `u1: a -> n` and `u2: n -> y` in cluster 7 become:

```verilog
TC_7 __tc_cluster_7 (.I0(a), .O0(y));
```

FFs, memories, macros, IO cells and any unselected combinational cells retain
their original instances and connections. Shared inputs, fanout and connections
between clusters keep their original net names. Purely internal scalar wires
are removed; packed bus declarations are retained intact even if some bits
become unused. Generated instance names get deterministic numeric suffixes on
collision; use `reduced.instances.jsonl` rather than assuming an instance name.

The converter streams the original netlist in three passes. It checks that all
mapped members exist, every mapped boundary net touches its cluster, and no
connection to another cluster, retained instance or top-level port is dropped.
It rejects direct cluster-to-cluster cycles. This is not a full timing-graph
convexity check through retained cells; partition legality remains the clustering
stage's responsibility. Supported syntax remains a single flat **non-ANSI**
module with named scalar pin connections. Packed top-level buses/bit-select
connections and scalar escaped identifiers are supported; assignments, constants,
inout/supply declarations and hierarchical/behavioral logic require further work.

OpenTimer must read **both** the reduced Liberty and the original models of
retained instances. Example commands in the OpenTimer shell (replace original
library and SDC paths with your actual, consistently selected inputs):

```text
read_celllib -early original_early.lib
read_celllib -late original_late.lib
read_celllib -early clusters_Early.lib
read_celllib -late clusters_Late.lib
read_verilog reduced.v
read_sdc reduced.sdc
update_timing
```

For a shared-corner original library, `read_celllib original.lib` loads it for
both min/max. Repeat original-library reads as needed, resolving duplicate cells
explicitly; the converter does not generate or merge retained-cell libraries.
`reduced.summary.json` lists which original masters are still needed. If no
original instances remain, only the reduced libraries are needed.

**No SDC or SPEF is rewritten automatically.** Constraints on preserved ports/FF
pins may remain valid, but constraints referencing removed internal instances
must be reviewed/remapped. Do not attach an original SPEF blindly: removed pins
and internal nets no longer exist, and cluster pins aggregate several original
pins. Boundary-net RC must be reconstructed for the reduced view. Until then,
use ideal interconnect for a controlled initial STA experiment. Reading a netlist
alone is not sufficient to obtain meaningful slack without timing constraints.

Reduced physical LEF/DEF and DREAMPlace placement/timer integration still require
separate changes. Generating `reduced.v` enables a reduced standalone STA graph;
it does not switch the existing placement loop to that graph automatically.

## Tests

```bash
python3 -m unittest test_reduced_liberty test_reduced_verilog -v
```

Tests cover analytic two-inverter delay/slew, ns/ps and pF/fF conversion,
pin capacitance, independent input characterization, generated-library reload,
midpoint interpolation, and missing ID/name mappings. Directory tests cover
recursive discovery, uppercase extensions, path deduplication, shared/separate
sets, cross-file clusters, duplicate selection, incompatible units/thresholds/
nominal conditions, template namespacing, CCS omission, source-specific defaults, boundary-only libraries,
inherited table axes, malformed LUT dimensions, structural indexing, and unused
bodies that must not be parsed.

Reduced-Verilog tests cover retained FF/macro connections, fanout/shared inputs,
intercluster nets, instance-name collisions, buses/escaped names, missing members
and boundary connections, partial manifests, overwrites, and direct cycles.
A full five-inverter original design is compared against a two-cluster-plus-one-
retained-inverter reduced netlist in OpenTimer, checking early/late arrival times
and slack at the output (linear synthetic LUT fixture, ideal wires).
