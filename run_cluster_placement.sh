#!/bin/sh
# Run from XP_timing_4.1/bin. Only generate placement LEF/DEF, not timing models.
set -eu
: "${TC_UTILIZATION:?Set TC_UTILIZATION to a fraction, e.g. 0.7}"
TC_RUN=${TC_RUN:-results/superblue1/timing_flow_v2}
TC_BENCH=${TC_BENCH:-benchmarks/iccad2015.ot/superblue1}
TC_SAVE=${TC_SAVE:-results/superblue1/timing_edges_protected/save}
TC_PHYSICAL=${TC_PHYSICAL:-"$TC_RUN/PLACEMENT_U${TC_UTILIZATION}"}
python3 dreamplace/ClusterPlacement.py \
    --clusters "$TC_RUN/leiden/clusters.tsv" \
    --cell-names "$TC_RUN/cell_names.tsv" \
    --saved-db "$TC_SAVE" \
    --def-input "$TC_BENCH/superblue1_withnets.def" \
    --utilization "$TC_UTILIZATION" \
    --pin-layer "${TC_PIN_LAYER:-metal2}" \
    --output "$TC_PHYSICAL