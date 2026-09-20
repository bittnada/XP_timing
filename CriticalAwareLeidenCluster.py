#!/usr/bin/env python3
"""Critical-aware Leiden clustering without changing LeidenCluster.py.

The policy protects timing-sensitive and structurally risky cells, partitions
the remaining candidates into slack bands, and applies a different cell-count
cap to every band.  The existing full-connectivity DAG/boundary repair remains
the final authority for cluster legality.
"""
import argparse
import csv
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import time

import numpy as np
from scipy import sparse
from scipy.spatial import cKDTree

import LeidenCluster as base
from ClusterLEFMapping import object_items
from LeidenGraph import dict_rows, load_design
from ReducedLiberty import ModelError, table_rows


LOG = logging.getLogger('CriticalAwareLeidenCluster')

REASONS = {
    'listed_critical': 1 << 0,
    'negative_slack': 1 << 1,
    'critical_hop': 1 << 2,
    'high_fanout': 1 << 3,
    'control_net': 1 << 4,
    'macro_halo': 1 << 5,
    'scope_boundary': 1 << 6,
    'missing_slack': 1 << 7,
}


def _node_from_row(row, design, prefix=''):
    key = prefix + 'id'
    if key in row and row[key] not in (None, ''):
        return design['id_to_node'].get(int(row[key]))
    return None


def _critical_seeds(path, design, reasons):
    if not path:
        return np.zeros(len(design['eligible']), dtype=bool)
    seeds = np.zeros(len(design['eligible']), dtype=bool)
    unresolved = []
    for row in dict_rows(path):
        node = _node_from_row(row, design, 'cell_')
        row_names = {name for name in (row.get('cell_name'), row.get('timing_cell_name')) if name}
        if node is not None and row_names and design['names'][node] not in row_names:
            node = None
        if node is None:
            # critical_cells also contains FF/macro path endpoints that were
            # intentionally omitted from the timing-edge ID table.  Match the
            # saved physical name exactly, as LeidenGraph.load_design does.
            unresolved.append((row.get('cell_id'), row_names))
            continue
        seeds[node] = True
        reasons[node] |= REASONS['listed_critical']
    if unresolved:
        wanted = set().union(*(names for _, names in unresolved))
        resolved = {name: node for node, name in enumerate(design['names']) if name in wanted}
        for cell_id, names in unresolved:
            matches = {resolved[name] for name in names if name in resolved}
            if len(matches) != 1:
                raise ModelError('Cannot uniquely map critical cell ID/name: ' + str(cell_id))
            node = matches.pop()
            seeds[node] = True
            reasons[node] |= REASONS['listed_critical']
    return seeds


def _read_slacks(path, design, reasons, protect_below):
    """Use worst finite incident sink slack as a conservative cell slack."""
    ncell = len(design['names'])
    slack = np.full(ncell, np.inf, dtype=np.float64)
    seen = np.zeros(ncell, dtype=bool)
    negative = np.zeros(len(design['eligible']), dtype=bool)
    required = {'driver_id', 'sink_id', 'timing_slack_ps'}
    for row in dict_rows(path):
        if not required.issubset(row):
            raise ModelError('--timing-edges must contain driver_id, sink_id and timing_slack_ps')
        value = row.get('timing_slack_ps', '')
        if value in (None, ''):
            continue
        value = float(value)
        if not math.isfinite(value):
            raise ModelError('Nonfinite timing_slack_ps in --timing-edges')
        for field in ('driver_id', 'sink_id'):
            node = design['id_to_node'].get(int(row[field]))
            if node is None:
                raise ModelError('Timing-edge ID is absent from --cell-names: ' + row[field])
            slack[node] = min(slack[node], value)
            seen[node] = True
            if value < protect_below:
                negative[node] = True
                reasons[node] |= REASONS['negative_slack']
    return slack, seen, negative


def _expand_hops(graph, seeds, hops, reasons):
    if hops <= 0 or not seeds.any():
        return seeds.copy()
    reached = seeds.copy()
    frontier = seeds.copy()
    reverse = graph.transpose().tocsr()
    for _ in range(hops):
        # graph rows are drivers and columns are sinks.  These two products
        # visit both fanin and fanout without Python traversal per vertex.
        adjacent = np.asarray(graph @ frontier).ravel().astype(bool)
        adjacent |= np.asarray(reverse @ frontier).ravel().astype(bool)
        frontier = adjacent & ~reached
        if not frontier.any():
            break
        reached |= frontier
        reasons[frontier] |= REASONS['critical_hop']
    return reached


def _protect_high_fanout(design, threshold, reasons):
    result = np.zeros(len(design['eligible']), dtype=bool)
    if threshold <= 0:
        return result
    degree = np.diff(design['graph'].indptr)
    result[:len(design['names'])] = degree[:len(design['names'])] >= threshold
    reasons[result] |= REASONS['high_fanout']
    return result


def _protect_control_nets(args, design, reasons):
    result = np.zeros(len(design['eligible']), dtype=bool)
    if not args.control_net_regex:
        return result, 0
    try:
        pattern = re.compile(args.control_net_regex)
    except re.error as exc:
        raise ModelError('Invalid --control-net-regex: ' + str(exc)) from exc
    names = set()
    matched = 0
    for net, info in object_items(Path(args.saved_db) / 'netlist_info.json'):
        use = str(info.get('use', 'SIGNAL')).replace('USE ', '').strip().upper()
        if use != 'CLOCK' and not pattern.search(net):
            continue
        matched += 1
        for endpoint in info.get('cell_list', []):
            parts = endpoint.split()
            if len(parts) == 2 and parts[0] != 'PIN':
                names.add(parts[0])
    wanted = names
    for node, name in enumerate(design['names']):
        if name in wanted:
            result[node] = True
    reasons[result] |= REASONS['control_net']
    return result, matched


def _protect_scope_boundaries(path, design, reasons):
    result = np.zeros(len(design['eligible']), dtype=bool)
    if not path:
        return result, 0
    scope = np.full(len(design['names']), -1, dtype=np.int64)
    scopes = {}
    for cell_id, value in table_rows(path, ('cell_id', 'scope')):
        node = design['id_to_node'].get(int(cell_id))
        if node is None:
            raise ModelError('Scope-map ID is absent from --cell-names: ' + str(cell_id))
        if scope[node] >= 0:
            raise ModelError('Duplicate cell in --scope-map: ' + str(cell_id))
        if value not in scopes:
            scopes[value] = len(scopes)
        scope[node] = scopes[value]
    coo = design['graph'].tocoo(copy=False)
    ncell = len(design['names'])
    physical = (coo.row < ncell) & (coo.col < ncell)
    row, col = coo.row[physical], coo.col[physical]
    cross = (scope[row] >= 0) & (scope[col] >= 0) & (scope[row] != scope[col])
    result[row[cross]] = True
    result[col[cross]] = True
    reasons[result] |= REASONS['scope_boundary']
    return result, int(cross.sum())


def _protect_macro_halo(args, design, reasons):
    result = np.zeros(len(design['eligible']), dtype=bool)
    if args.macro_halo_um <= 0:
        return result, 0
    root = Path(args.saved_db)
    die = json.loads((root / 'die_info.json').read_text())
    scale = float(die.get('def_scale', 0))
    if not math.isfinite(scale) or scale <= 0:
        raise ModelError('saved die_info.json has invalid def_scale')
    lef = dict(object_items(root / 'lef_info.json'))
    ncell = len(design['names'])
    centers = np.full((ncell, 2), np.nan, dtype=np.float64)
    macros = []
    for node, (name, info) in enumerate(object_items(root / 'cells_info.json')):
        if node >= ncell or name != design['names'][node]:
            raise ModelError('cells_info order changed while constructing macro halo')
        pos = info.get('position')
        model = lef.get(info.get('macro_id'), {})
        if not pos or len(pos) < 2 or 'width' not in model or 'height' not in model:
            continue
        width, height = float(model['width']) * scale, float(model['height']) * scale
        x, y = float(pos[0]), float(pos[1])
        centers[node] = (x + width / 2, y + height / 2)
        cell_class = str(model.get('class', '')).split()[:1]
        if cell_class != ['CORE'] or str(info.get('cell_type', 'CORE')).upper() != 'CORE':
            macros.append((x, y, x + width, y + height))
    valid = np.isfinite(centers).all(axis=1)
    movable = valid & design['eligible'][:ncell]
    nodes = np.flatnonzero(movable)
    if not len(nodes) or not macros:
        return result, len(macros)
    tree = cKDTree(centers[nodes])
    halo = args.macro_halo_um * scale
    for xl, yl, xh, yh in macros:
        center = np.array([(xl + xh) / 2, (yl + yh) / 2])
        radius = math.hypot((xh - xl) / 2 + halo, (yh - yl) / 2 + halo)
        local = np.asarray(tree.query_ball_point(center, radius), dtype=np.int64)
        if not len(local):
            continue
        candidate = nodes[local]
        xy = centers[candidate]
        inside = ((xy[:, 0] >= xl - halo) & (xy[:, 0] <= xh + halo) &
                  (xy[:, 1] >= yl - halo) & (xy[:, 1] <= yh + halo))
        result[candidate[inside]] = True
    reasons[result] |= REASONS['macro_halo']
    return result, len(macros)


def apply_critical_policy(design, args):
    n = len(design['eligible'])
    ncell = len(design['names'])
    originally_eligible = design['eligible'].copy()
    reasons = np.zeros(n, dtype=np.uint16)
    listed = _critical_seeds(args.critical_cells, design, reasons)
    slack, slack_seen, negative = _read_slacks(args.timing_edges, design, reasons,
                                               args.protect_slack_below_ps)
    critical = listed | negative
    hop = _expand_hops(design['graph'], critical, args.critical_hops, reasons)
    fanout = _protect_high_fanout(design, args.protect_fanout, reasons)
    control, control_nets = _protect_control_nets(args, design, reasons)
    scope, cross_scope_edges = _protect_scope_boundaries(args.scope_map, design, reasons)
    macro, macro_count = _protect_macro_halo(args, design, reasons)
    missing = np.zeros(n, dtype=bool)
    if args.protect_missing_slack:
        missing[:ncell] = originally_eligible[:ncell] & ~slack_seen
        reasons[missing] |= REASONS['missing_slack']

    protected = hop | fanout | control | scope | macro | missing
    newly_protected = protected & originally_eligible
    design['eligible'][newly_protected] = False

    band = np.full(n, -1, dtype=np.int8)
    remaining = design['eligible'][:ncell]
    band[:ncell][remaining & (slack < args.slack_t1_ps)] = 0
    band[:ncell][remaining & (slack >= args.slack_t1_ps) & (slack < args.slack_t2_ps)] = 1
    band[:ncell][remaining & (slack >= args.slack_t2_ps)] = 2
    if np.any(remaining & (band[:ncell] < 0)):
        raise ModelError('Eligible cell has no finite slack band; use --protect-missing-slack')

    # A risk band is a hard partition key in addition to launch/capture domain.
    for node in np.flatnonzero(design['eligible'][:ncell]):
        design['domains'][int(node)] = (design['domains'][int(node)], int(band[node]))

    # Remove affinity between different risk bands.  The full directed graph
    # is untouched and is still used by all legality checks.
    weighted = design['weighted'].tocoo(copy=False)
    same = ((band[weighted.row] >= 0) & (band[weighted.row] == band[weighted.col]) &
            design['eligible'][weighted.row] & design['eligible'][weighted.col])
    design['weighted'] = sparse.csr_matrix((weighted.data[same],
        (weighted.row[same], weighted.col[same])), shape=weighted.shape)

    caps = np.array([args.max_cells_critical, args.max_cells_near,
                     args.max_cells_noncritical], dtype=np.int64)
    if args.max_cells:
        caps = np.minimum(caps, args.max_cells)
    reason_counts = {name: int(np.sum((reasons & bit) != 0)) for name, bit in REASONS.items()}
    protected_eligible_reason_counts = {
        name: int(np.sum(((reasons & bit) != 0) & originally_eligible))
        for name, bit in REASONS.items()
    }
    metadata = dict(
        policy='critical-aware-v1', slack_source='minimum finite timing_slack_ps on incident timing edges',
        protect_slack_below_ps=args.protect_slack_below_ps, critical_hops=args.critical_hops,
        protect_fanout=args.protect_fanout, control_net_regex=args.control_net_regex,
        matched_control_nets=control_nets, macro_halo_um=args.macro_halo_um,
        macro_count=macro_count, scope_map=str(Path(args.scope_map).resolve()) if args.scope_map else None,
        cross_scope_edges=cross_scope_edges, protect_missing_slack=args.protect_missing_slack,
        original_eligible_cells=int(originally_eligible.sum()),
        newly_protected_eligible_cells=int(newly_protected.sum()),
        remaining_eligible_cells=int(design['eligible'].sum()), reason_counts=reason_counts,
        protected_eligible_reason_counts=protected_eligible_reason_counts,
        slack_bands=[
            dict(name='critical_nonnegative', lower_ps=args.protect_slack_below_ps,
                 upper_ps=args.slack_t1_ps, max_cells=int(caps[0]), cells=int(np.sum(band == 0))),
            dict(name='near_critical', lower_ps=args.slack_t1_ps, upper_ps=args.slack_t2_ps,
                 max_cells=int(caps[1]), cells=int(np.sum(band == 1))),
            dict(name='noncritical', lower_ps=args.slack_t2_ps, upper_ps=None,
                 max_cells=int(caps[2]), cells=int(np.sum(band == 2))),
        ],
        notes=[
            'Clock/reset protection uses saved net USE CLOCK or --control-net-regex.',
            'Unknown/multiple/cross-domain cells were already excluded by timing-edge eligibility.',
            'Hierarchy protection requires --scope-map because flattened Verilog has no module boundary.',
        ])
    design['metadata']['critical_policy'] = metadata
    design['critical_policy'] = dict(reasons=reasons, slack=slack, band=band,
                                     newly_protected=newly_protected, caps=caps)
    LOG.info('critical policy protected %d eligible cells; %d remain in bands %s',
             newly_protected.sum(), design['eligible'].sum(),
             [int(np.sum(band == i)) for i in range(3)])
    return metadata


def enforce_band_caps(design, labels, rank):
    """Split candidate groups until every group obeys its slack-band cap."""
    labels = base.normalize(labels)
    policy = design['critical_policy']
    eligible, band, caps = design['eligible'], policy['band'], policy['caps']
    split_groups = 0
    while True:
        counts = np.bincount(labels, weights=eligible)
        group_band = np.full(len(counts), -1, dtype=np.int8)
        for b in range(3):
            occupied = np.bincount(labels, weights=eligible & (band == b), minlength=len(counts)) > 0
            if np.any((group_band >= 0) & occupied):
                raise ModelError('Candidate cluster mixes slack bands')
            group_band[occupied] = b
        bad = np.flatnonzero((group_band >= 0) & (counts > caps[np.maximum(group_band, 0)]))
        if not len(bad):
            return labels, split_groups
        next_id = len(counts)
        for nodes in base.selected_groups(labels, bad):
            if not eligible[nodes].all():
                raise ModelError('Adaptive cap group contains an ineligible vertex')
            left, right = base.weighted_topological_split(nodes, rank, design['weighted'])
            labels[left] = next_id; next_id += 1
            labels[right] = next_id; next_id += 1
            split_groups += 1
        labels = base.normalize(labels)


def _write_policy_audit(output, design, metadata):
    policy = design['critical_policy']
    with (output / 'critical_protection.tsv').open('x', newline='') as stream:
        writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
        writer.writerow(('cell_id', 'cell_name', 'slack_ps', 'reasons', 'newly_protected_by_policy'))
        selected = (policy['reasons'][:len(design['names'])] != 0) & (design['ids'] >= 0)
        for node in np.flatnonzero(selected):
            value = policy['slack'][node]
            names = [name for name, bit in REASONS.items() if policy['reasons'][node] & bit]
            writer.writerow((int(design['ids'][node]), design['names'][node],
                             '' if not math.isfinite(value) else value, ','.join(names),
                             int(policy['newly_protected'][node])))
    summary = output / 'summary.json'
    report = json.loads(summary.read_text())
    report['critical_policy'] = metadata
    report['critical_policy']['files'] = ['critical_protection.tsv']
    summary.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    return report


def generate(args):
    begin = time.monotonic()
    output = Path(args.output)
    if os.path.lexists(output):
        raise ModelError('Output directory already exists; choose a new path: ' + str(output))
    if args.max_cells < 0 or min(args.max_cells_critical, args.max_cells_near,
                                 args.max_cells_noncritical) < 1:
        raise ModelError('Cell-count caps must be positive (--max-cells may be 0 for unlimited global cap)')
    if not args.protect_slack_below_ps <= args.slack_t1_ps < args.slack_t2_ps:
        raise ModelError('Require protect-slack-below <= slack-t1 < slack-t2')
    if args.critical_hops < 0 or args.protect_fanout < 0 or args.macro_halo_um < 0:
        raise ModelError('Hop, fanout and macro-halo values must be nonnegative')
    cutoff = args.modularity_cutoff
    if cutoff is not None and (not math.isfinite(cutoff) or not -.5 <= cutoff <= 1.):
        raise ModelError('--modularity-cutoff must be finite and between -0.5 and 1')
    if cutoff is not None and args.initial_clusters:
        raise ModelError('--modularity-cutoff cannot be combined with --initial-clusters')
    for name in ('max_boundary_pins', 'max_timing_arcs'):
        if getattr(args, name) < 1:
            raise ModelError('--' + name.replace('_', '-') + ' must be positive')
    if args.repair_rounds < 0 or args.iterations < 1:
        raise ModelError('repair-rounds must be nonnegative and iterations positive')
    for name in ('resolution', 'utilization', 'aspect_ratio'):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            raise ModelError(name + ' must be positive and finite')
    if args.utilization > 1 or args.max_area <= 0 or math.isnan(args.max_area):
        raise ModelError('utilization must be <=1 and max-area positive')
    if args.critical_cells and args.no_critical_protection:
        raise ModelError('Use --critical-cells OR --no-critical-protection')
    if not args.critical_cells and not args.no_critical_protection:
        path = Path(args.timing_edges).parent / 'critical_cells.tsv'
        if not path.is_file():
            raise ModelError('Supply --critical-cells, or explicitly --no-critical-protection')
        args.critical_cells = str(path)
    if not args.initial_clusters:
        base.leiden_modules()

    design = load_design(args)
    policy = apply_critical_policy(design, args)
    LOG.info('Checking original directed graph before any contraction')
    rank = base.prepare_constraints(design, args.original_cycles)
    LOG.info('Forming critical-aware candidate partition')
    initial, metadata = base.candidates(design, args)
    initial, adaptive_splits = enforce_band_caps(design, initial, rank)
    metadata['adaptive_cap_split_groups'] = adaptive_splits
    counts = np.bincount(initial, weights=design['eligible'])
    metadata['multicell_candidate_groups'] = int(np.sum(counts > 1))
    weights = design['weighted'].tocoo(copy=False)
    active = design['eligible'][weights.row] & design['eligible'][weights.col]
    internal = initial[weights.row] == initial[weights.col]
    total_weight = float(weights.data[active].sum())
    if not math.isfinite(total_weight):
        raise ModelError('Total eligible edge weight overflow')
    metadata['internal_weight_fraction'] = (float(weights.data[active & internal].sum()) / total_weight
                                            if total_weight else 0.)
    final, history, ins, outs = base.repair(design, initial.copy(), rank, args)
    report = base.write_results(output, design, initial, final, history, ins, outs, metadata, args,
                                time.monotonic() - begin)
    try:
        report = _write_policy_audit(output, design, policy)
    except Exception:
        (output / 'FAILED_DO_NOT_USE.txt').write_text(
            'Critical-aware audit export failed; do not use this incomplete output.\n')
        raise
    return report


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--edges', required=True)
    p.add_argument('--timing-edges', required=True)
    p.add_argument('--cell-names', required=True)
    p.add_argument('--saved-db', required=True)
    p.add_argument('--lib-dir', required=True, action='append')
    p.add_argument('--critical-cells')
    p.add_argument('--no-critical-protection', action='store_true')
    p.add_argument('--initial-clusters')
    p.add_argument('--protect-slack-below-ps', type=float, default=0.)
    p.add_argument('--critical-hops', type=int, default=2)
    p.add_argument('--protect-fanout', type=int, default=32,
                   help='protect drivers with at least this many unique graph sinks; 0 disables')
    p.add_argument('--control-net-regex', default=r'(?i)(clk|clock|reset|rst)',
                   help='protect cells incident to matching nets; empty string disables (USE CLOCK always matches when enabled)')
    p.add_argument('--macro-halo-um', type=float, default=10.)
    p.add_argument('--scope-map', help='optional cell_id/scope TSV; both endpoints of cross-scope edges are protected')
    p.add_argument('--protect-missing-slack', action=argparse.BooleanOptionalAction, default=True)
    p.add_argument('--slack-t1-ps', type=float, default=100.)
    p.add_argument('--slack-t2-ps', type=float, default=500.)
    p.add_argument('--max-cells-critical', type=int, default=4)
    p.add_argument('--max-cells-near', type=int, default=16)
    p.add_argument('--max-cells-noncritical', type=int, default=32)
    p.add_argument('--resolution', type=float, default=1.)
    p.add_argument('--modularity-cutoff', type=float, default=None)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--iterations', type=int, default=2)
    p.add_argument('--max-cells', type=int, default=32, help='additional global hard cap; 0 disables')
    p.add_argument('--max-boundary-pins', type=int, default=24)
    p.add_argument('--max-timing-arcs', type=int, default=64)
    p.add_argument('--max-area', type=float, default=float('inf'))
    p.add_argument('--repair-rounds', type=int, default=12)
    p.add_argument('--original-cycles', choices=('error', 'retain'), default='error')
    p.add_argument('--utilization', type=float, default=1.)
    p.add_argument('--aspect-ratio', type=float, default=1.)
    p.add_argument('--output', required=True)
    return p.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    try:
        report = generate(parse_args(argv))
    except (ModelError, OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1
    print('Saved %d critical-aware clusters / %d merged cells; %d eligible cells retained.' %
          (report['clusters'], report['merged_cells'], report['retained_eligible_cells']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
