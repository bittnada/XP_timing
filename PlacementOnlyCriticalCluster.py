#!/usr/bin/env python3
"""Aggressive critical-aware clustering exclusively for two-DB placement.

Unlike LeidenCluster, this placement-only variant does not enforce quotient
DAG, ReducedLiberty output independence, boundary-pin, or timing-arc budgets.
It preserves critical protection, timing-domain/slack-band separation, weak
connectivity, and per-band cell-count caps.  Its output must never be used to
build reduced Liberty or reduced timing Verilog models.
"""
import json
import logging
import math
import os
from pathlib import Path
import sys
import time

import numpy as np

import LeidenCluster as base
import CriticalAwareLeidenCluster as critical
from LeidenGraph import load_design
from ReducedLiberty import ModelError


LOG = logging.getLogger('PlacementOnlyCriticalCluster')


def placement_only_partition(design, labels):
    """Keep only weakly connected pieces; do not apply timing-model repair."""
    before = int(labels.max()) + 1
    labels = base.connected_refinement(design['graph'], base.normalize(labels))
    after = int(labels.max()) + 1
    ins, outs = base.boundaries(design, labels)
    return labels, ins, outs, max(0, after - before)


def _validate(args):
    output = Path(args.output)
    if os.path.lexists(output):
        raise ModelError('Output directory already exists; choose a new path: ' + str(output))
    if args.modularity_cutoff is not None:
        raise ModelError('Placement-only mode uses one-shot Leiden; omit --modularity-cutoff')
    if args.initial_clusters:
        raise ModelError('Placement-only mode does not accept --initial-clusters')
    if args.max_cells < 1 or min(args.max_cells_critical, args.max_cells_near,
                                 args.max_cells_noncritical) < 1:
        raise ModelError('Placement-only cell-count caps must be positive')
    if not args.protect_slack_below_ps <= args.slack_t1_ps < args.slack_t2_ps:
        raise ModelError('Require protect-slack-below <= slack-t1 < slack-t2')
    if args.critical_hops < 0 or args.protect_fanout < 0 or args.macro_halo_um < 0:
        raise ModelError('Hop, fanout and macro-halo values must be nonnegative')
    if args.iterations < 1 or not math.isfinite(args.resolution) or args.resolution <= 0:
        raise ModelError('iterations and resolution must be positive')
    if args.critical_cells and args.no_critical_protection:
        raise ModelError('Use --critical-cells OR --no-critical-protection')
    if not args.critical_cells and not args.no_critical_protection:
        path = Path(args.timing_edges).parent / 'critical_cells.tsv'
        if not path.is_file():
            raise ModelError('Supply --critical-cells, or explicitly --no-critical-protection')
        args.critical_cells = str(path)
    return output


def generate(args):
    started = time.monotonic()
    output = _validate(args)
    base.leiden_modules()
    design = load_design(args)
    policy = critical.apply_critical_policy(design, args)

    # Rank is used only for deterministic adaptive-cap splitting. Retain mode
    # quarantines pre-existing SCC cells; no quotient-DAG claim is made here.
    rank = base.prepare_constraints(design, args.original_cycles)
    LOG.info('Forming one-shot Leiden communities for placement only')
    initial, metadata = base.candidates(design, args)
    initial, adaptive_splits = critical.enforce_band_caps(design, initial, rank)
    metadata.update(mode='placement_only_leiden', adaptive_cap_split_groups=adaptive_splits,
                    omitted_checks=['quotient_dag', 'boundary_output_independence',
                                    'boundary_pin_budget', 'timing_arc_budget', 'area_budget'])
    labels, ins, outs, connectivity_splits = placement_only_partition(design, initial)
    metadata['connectivity_splits'] = connectivity_splits
    counts = np.bincount(labels, weights=design['eligible'])
    metadata['multicell_candidate_groups'] = int(np.sum(counts > 1))
    weights = design['weighted'].tocoo(copy=False)
    active = design['eligible'][weights.row] & design['eligible'][weights.col]
    internal = labels[weights.row] == labels[weights.col]
    total = float(weights.data[active].sum())
    metadata['internal_weight_fraction'] = (float(weights.data[active & internal].sum()) / total
                                            if total else 0.)

    report = base.write_results(output, design, labels, labels, [], ins, outs, metadata, args,
                                time.monotonic() - started)
    # Correct base report fields whose guarantees intentionally do not apply.
    report['mode'] = 'two_db_placement_only'
    report['final_quotient_dag'] = None
    report['dag_scope'] = 'not checked; original timing DB remains uncontracted'
    report['boundary_output_independence'] = dict(
        verified=False, remaining_violating_clusters=None,
        scope='not checked in placement-only mode',
        note='Never use this membership for reduced Liberty or reduced timing Verilog.')
    report['limits']['max_boundary_pins'] = None
    report['limits']['max_timing_arcs'] = None
    report['limits']['max_area'] = None
    report['limitations'].extend([
        'PLACEMENT ONLY: quotient cycles are permitted because timing uses the uncontracted original DB',
        'PLACEMENT ONLY: ReducedLiberty output independence and boundary budgets are not enforced',
        'Do not generate or use reduced Liberty/timing Verilog from this membership'])
    (output / 'summary.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    report = critical._write_policy_audit(output, design, policy)
    (output / 'PLACEMENT_ONLY_DO_NOT_BUILD_REDUCED_TIMING.txt').write_text(
        'This membership is valid only as a two-DB placement abstraction.\n'
        'Timing must use the uncontracted original timing DB.\n'
        'Do not build reduced Liberty or reduced timing Verilog from these clusters.\n')
    return report


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    try:
        args = critical.parse_args(argv)
        report = generate(args)
    except (ModelError, OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1
    print('Saved %d placement-only clusters / %d merged cells; %d eligible cells retained.' %
          (report['clusters'], report['merged_cells'], report['retained_eligible_cells']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
