#!/bin/sh
# Run from XP_timing_4.1/bin. Existing DB snapshots are read-only.
set -eu
TC_TIMING_DB=${TC_TIMING_DB:-results/superblue1/timing_edges_protected/save}
TC_PHYSICAL=${TC_PHYSICAL:-results/superblue1/timing_flow_v2/PLACEMENT_U0.7}
TC_PLACEMENT_DB=${TC_PLACEMENT_DB:-"$TC_PHYSICAL/save"}
TC_MAPPING=${TC_MAPPING:-"$TC_PHYSICAL/ID_MAPPING"}
if [ ! -f "$TC_PLACEMENT_DB/physical_db/manifest.json" ] && \
   [ ! -f "$TC_PLACEMENT_DB/node_names.npy" ] && \
   [ ! -f "$TC_PLACEMENT_DB/node_names.txt" ]; then
    echo "ERROR: Reduced placement DB has not been prepared: $TC_PLACEMENT_DB" >&2
    echo "Run sh ./run_prepare_placement_db.sh first (with the same TC_PHYSICAL / TC_PLACEMENT_DB overrides)." >&2
    echo "LEF/DEF files alone are not a saved binary DB. No DB is rebuilt automatically." >&2
    exit 1
fi
python3 dreamplace/PlacementTimingMapping.py \
    --timing-db "$TC_TIMING_DB" \
    --placement-db "$TC_PLACEMENT_DB" \
    --cluster-data "$TC_PHYSICAL" \
    --output "$TC_MAPPING" \
    "$@"
