# Multi-start A-from-C surrogate flow

The goal is to run the reduced design C for many initial conditions, predict
the corresponding full-design A metrics, and run expensive A only for selected
initial conditions. Cluster construction is fixed before this experiment; no A
final coordinates are used as C inputs.

## 1. Generate explicit original initial conditions

Run from `/mnt/hdd1/XP_timing_4.1/bin`:

```bash
SRC=/mnt/hdd1/XP_timing_4.1/dreamplace
ORIGINAL_DB=results/superblue1/timing_edges_protected/save
PHYSICAL=results/superblue1/timing_flow_v2/PLACEMENT_CRITICAL_V4_U0.7

python3 "$SRC/GeneratePlacementSeeds.py" \
  --original-db "$ORIGINAL_DB" \
  --distribution uniform \
  --seeds 1001 1002 1003 1004 1005 1006 1007 1008 \
  --output results/superblue1/multistart_seeds
```

Uniform mode distributes movable lower-left coordinates across the full die.
Explicit checkpoints are required because using the same RNG seed directly in
A and C does not give corresponding initial conditions: their node-array
lengths differ. Unclustered C cells keep the exact A coordinate; clustered C
cells use the area-weighted center of their A members. When reading these
checkpoints, DREAMPlace disables random-center initialization and initial GP
noise.

## 2. Project every original initial condition to C

Example for seed 1001:

```bash
SEED=1001
SEED_DIR=results/superblue1/multistart_seeds/seed_${SEED}

python3 "$SRC/ProjectClusterInitialPlacement.py" \
  --original-db "$ORIGINAL_DB" \
  --placement-db "$PHYSICAL/save" \
  --mapping "$PHYSICAL/ID_MAPPING" \
  --original-posX "$SEED_DIR/original.posX.tsv" \
  --original-posY "$SEED_DIR/original.posY.tsv" \
  --output-prefix "$SEED_DIR/clustered"
```

A uses `original.posX.tsv/original.posY.tsv`. C uses
`clustered.posX.tsv/clustered.posY.tsv`:

Prepare distinct A/C configs, result directories, logs and the matched-STA
command automatically:

```bash
python3 "$SRC/PrepareSurrogateCase.py" \
  --seed "$SEED" \
  --seed-dir "$SEED_DIR" \
  --original-config superblue1.json \
  --placement-config "$SRC/examples/two_db_superblue1_critical_v4_u07.json" \
  --result-root results/superblue1/multistart_runs \
  --output "results/superblue1/multistart_cases/seed_${SEED}"

sh "results/superblue1/multistart_cases/seed_${SEED}/run.sh"
```

The generated `pair_row.tsv` is appended to the combined `pairs.tsv` only
after `run.sh` finishes successfully. The equivalent manual A/C commands are:

```bash
python3 dreamplace/Placer.py ORIGINAL_CONFIG.json \
  --read_posX "$SEED_DIR/original.posX.tsv" \
  --read_posY "$SEED_DIR/original.posY.tsv" 2>&1 | tee "log_A_${SEED}"

TC_CONFIG="$CONFIG" sh ./run_two_db_timing.sh \
  --read_posX "$SEED_DIR/clustered.posX.tsv" \
  --read_posY "$SEED_DIR/clustered.posY.tsv" 2>&1 | tee "log_C_${SEED}"
```

Use a distinct `result_dir` for every run. Do not run multiple cases against a
shared result directory. Calibration A and C runs must use matched RC and
comparable placement settings.

## 3. Build the paired manifest

Create `pairs.tsv` with at least three cases; 10 or more is recommended. The
optional comparison summary should be produced with `--sta`, and supplies the
controlled A WNS/TNS calibration target.

```tsv
case_id	original_log	placement_log	comparison_summary
seed_1001	log_A_1001	log_C_1001	results/pairs/1001/summary.json
seed_1002	log_A_1002	log_C_1002	results/pairs/1002/summary.json
seed_1003	log_A_1003	log_C_1003	results/pairs/1003/summary.json
```

## 4. Fit and validate

```bash
python3 "$SRC/PlacementSurrogate.py" fit \
  --pairs pairs.tsv \
  --top-k 3 \
  --output results/superblue1/a_from_c_model

sed -n '1,200p' results/superblue1/a_from_c_model/report.md
```

The report contains C/A Spearman correlation, leave-one-out prediction error,
and Top-K overlap. Selection should not be trusted from training error alone;
use the leave-one-out columns and later reserve completely held-out seeds.

## 5. Predict A from a new C-only run

```bash
python3 "$SRC/PlacementSurrogate.py" predict \
  --model results/superblue1/a_from_c_model/model.json \
  --placement-log log_C_2001 \
  --case-id seed_2001 \
  --output results/superblue1/prediction_2001.json
```

The predictor reads only C final log metrics. A final placement is used only
for the small calibration/validation subset, never for a new prediction.
