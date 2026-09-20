#!/bin/sh
set -eu
# Run from bin. Geometry/log analysis by default; --sta is explicitly opt-in.
TC_PHYSICAL=${TC_PHYSICAL:-results/superblue1/timing_flow_v2/PLACEMENT_U0.7}
TC_ORIGINAL_DB=${TC_ORIGINAL_DB:-results/superblue1/timing_edges_protected/save}
TC_COMPARE=${TC_COMPARE:-results/two_db_timing/comparison}
TC_ORIGINAL_LOG=${TC_ORIGINAL_LOG:-log2}
TC_TWO_DB_LOG=${TC_TWO_DB_LOG:-log1}
if [ -f "$TC_TWO_DB_LOG" ]; then
    set -- --placement-log "$TC_TWO_DB_LOG" "$@"
fi
if [ -f "$TC_ORIGINAL_LOG" ]; then
    set -- --original-log "$TC_ORIGINAL_LOG" "$@"
fi
exec python3 dreamplace/CompareTimingPlacement.py \
    --original-db "$TC_ORIGINAL_DB" \
    --placement-db "$TC_PHYSICAL/save" \
    --mapping "$TC_PHYSICAL/ID_MAPPING" \
    --original-def "${TC_ORIGINAL_DEF:-results/superblue1/superblue1.gp.def}" \
    --placement-def "${TC_TWO_DB_DEF:-results/two_db_timing/superblue1/superblue1.gp.def}" \
    --original-config "${TC_ORIGINAL_CONFIG:-superblue1.json}" \
    --placement-config "${TC_TWO_DB_CONFIG:-dreamplace/examples/two_db_superblue1.json}" \
    --output "$TC_COMPARE" "$@"
