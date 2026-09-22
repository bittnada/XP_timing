#!/bin/sh
set -eu

cd /mnt/hdd1/XP_timing_4.1/bin

TC_RUN=results/superblue1/timing_flow_v2
TC_BENCH=benchmarks/iccad2015.ot/superblue1
TC_SAVE=results/superblue1/timing_edges_protected/save
# Use one Liberty directory for generation and every downstream consumer.
# Existing outputs are never overwritten; override TC_LIB for a fresh run.
TC_LIB=${TC_LIB:-"$TC_RUN/LIB_V2"}

# python3 dreamplace/TimingCluster.py superblue1.json \
#   --edges-only \
#   --critical-paths 100 \
#   --timing-tau-ps 100 \
#   --timing-alpha 4 \
#   --output "$TC_RUN"

#   python3 dreamplace/TimingEdgeAggregate.py \
#   "$TC_RUN/timing_edges.csv" \
#   "$TC_RUN/cell_edges.tsv" \
#   --merge sum \
#   --delimiter tab \
#   --cell-map "$TC_RUN/cell_names.tsv"

#   python3 dreamplace/LeidenCluster.py \
#   --edges "$TC_RUN/cell_edges.tsv" \
#   --timing-edges "$TC_RUN/timing_edges.csv" \
#   --cell-names "$TC_RUN/cell_names.tsv" \
#   --saved-db "$TC_SAVE" \
#   --lib-dir "$TC_BENCH/superblue1_Late.lib" \
#   --critical-cells "$TC_RUN/critical_cells.tsv" \
#   --original-cycles retain \
#   --modularity-cutoff 0.8 \
#   --resolution 1 \
#   --max-cells 128 \
#   --max-boundary-pins 64 \
#   --max-timing-arcs 256 \
#   --output "$TC_RUN/leiden"

#   python3 dreamplace/ReducedLiberty.py \
#   --clusters "$TC_RUN/leiden/clusters.tsv" \
#   --cell-names "$TC_RUN/cell_names.tsv" \
#   --verilog "$TC_BENCH/superblue1.v" \
#   --early-lib "$TC_BENCH/superblue1_Early.lib" \
#   --late-lib "$TC_BENCH/superblue1_Late.lib" \
#   --cluster-ids 1,2,3 \
#   --slews-ps 10,50,100 \
#   --loads-ff 1,5,10 \
#   --output "$TC_RUN/LIB_pilot"

  python3 dreamplace/ReducedLiberty.py \
  --clusters "$TC_RUN/leiden/clusters.tsv" \
  --cell-names "$TC_RUN/cell_names.tsv" \
  --verilog "$TC_BENCH/superblue1.v" \
  --early-lib "$TC_BENCH/superblue1_Early.lib" \
  --late-lib "$TC_BENCH/superblue1_Late.lib" \
  --slews-ps 10,50,100 \
  --loads-ff 1,5,10 \
  --mixed-polarity split-arcs \
  --on-cluster-failure retain-original \
  --cluster-sizes "$TC_RUN/leiden/cluster_sizes.tsv" \
  --output "$TC_LIB"
  
  python3 dreamplace/ReducedLEF.py \
  --input "$TC_LIB/cluster_mapping.jsonl" \
  --sizes "$TC_LIB/cluster_sizes.tsv" \
  --size-unit micron \
  --pin-layer metal2 \
  --output "$TC_RUN/LEF/cluster_cells.lef"

  cp -n "$TC_BENCH/superblue1.lef" \
  "$TC_RUN/LEF/original_superblue1.lef"

  python3 dreamplace/ReducedDEF.py \
  --input "$TC_BENCH/superblue1_withnets.def" \
  --verilog "$TC_LIB/reduced.v" \
  --mapping "$TC_LIB/cluster_mapping.jsonl" \
  --lef-dir "$TC_RUN/LEF" \
  --output "$TC_RUN/DEF/reduced.def"
