#!/bin/sh
# Explicit DB preparation only: no optimization, no STA, no automatic rebuild.
set -eu
TC_PHYSICAL=${TC_PHYSICAL:-results/superblue1/timing_flow_v2/PLACEMENT_U0.7}
TC_PLACEMENT_DB=${TC_PLACEMENT_DB:-"$TC_PHYSICAL/save"}
python3 dreamplace/PreparePlacementDB.py \
    --def-input "$TC_PHYSICAL/reduced.def" \
    --lef-input "$TC_PHYSICAL/placement.lef" \
    --output "$TC_PLACEMENT_DB" \
    --threads "${TC_THREADS:-8}" \
    "$@"
