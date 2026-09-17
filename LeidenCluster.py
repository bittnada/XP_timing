#!/usr/bin/env python3
"""Weighted Leiden candidates followed by local split-only DAG repair.

No STA is rerun. Timing weights/domain metadata are reused, while legality uses
complete saved net connectivity, retaining protected cells and cutting FF D->Q.
Outputs are new files; singleton/unsupported abstractions remain original cells.
"""
import argparse
import collections
import csv
import json
import logging
import math
import os
from pathlib import Path
import sys
import time

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph

from LeidenGraph import load_design
from ReducedLiberty import ModelError, table_rows

LOG = logging.getLogger('LeidenCluster')


def leiden_modules():
    try:
        import igraph
        import leidenalg
        return igraph, leidenalg
    except ImportError:
        here = Path(__file__).resolve().parent
        for path in (here / '_vendor/leiden', here.parent / 'bin/dreamplace/_vendor/leiden'):
            if path.is_dir():
                sys.path.insert(0, str(path))
                import igraph
                import leidenalg
                return igraph, leidenalg
        raise ModelError('Install igraph==0.11.9 and leidenalg==0.10.2 (see LEIDEN_CLUSTER.md)')


def normalize(labels):
    return np.unique(labels, return_inverse=True)[1].astype(np.int64)


def topological_rank(graph, names=None):
    n = graph.shape[0]
    degree = np.asarray(graph.sum(axis=0)).ravel().astype(np.int64)
    ready = collections.deque(np.flatnonzero(degree == 0).tolist())
    rank = np.full(n, -1, dtype=np.int64); step = 0
    while ready:
        node = ready.popleft(); rank[node] = step; step += 1
        for sink in graph.indices[graph.indptr[node]:graph.indptr[node + 1]]:
            degree[sink] -= 1
            if degree[sink] == 0:
                ready.append(int(sink))
    if step != n:
        _, components = csgraph.connected_components(graph, directed=True, connection='strong')
        sizes = np.bincount(components)
        bad = np.flatnonzero((sizes[components] > 1) | graph.diagonal().astype(bool))[:8]
        example = [names[i] if names is not None else int(i) for i in bad]
        raise ModelError('Original constraint graph is cyclic before clustering; check sequential metadata '
                         'or cell-level conservative arcs. Example cycle vertices: %s' % example)
    return rank


def quotient(graph, labels):
    coo = graph.tocoo(copy=False)
    src, dst = labels[coo.row], labels[coo.col]
    cross = src != dst
    count = int(labels.max()) + 1
    return sparse.csr_matrix((np.ones(int(cross.sum()), dtype=bool), (src[cross], dst[cross])),
                             shape=(count, count))


def prepare_constraints(design, policy='error'):
    """Optionally quarantine pre-existing SCCs without modifying the circuit.

    A quarantined SCC is one immutable vertex ONLY in the checking graph. Its
    original cells are never emitted as a reduced cluster, and no net is cut.
    The guarantee is then acyclicity modulo pre-existing SCCs, not an assertion
    that the original circuit is loop-free.
    """
    graph = design['graph']
    _, components = csgraph.connected_components(graph, directed=True, connection='strong')
    sizes = np.bincount(components)
    cyclic = (sizes[components] > 1) | graph.diagonal().astype(bool)
    design['original_cyclic'] = cyclic
    design['metadata']['original_cyclic_vertices'] = int(cyclic.sum())
    design['metadata']['original_cyclic_sccs'] = len(np.unique(components[cyclic]))
    design['metadata']['original_cycle_policy'] = policy
    if not cyclic.any() or policy == 'error':
        return topological_rank(graph, design['vertex_names'])
    design['metadata']['cycle_candidates_retained'] = int(np.sum(design['eligible'] & cyclic))
    design['eligible'][cyclic] = False
    design['fixed_components'] = components.astype(np.int64)
    LOG.warning('Retaining %d original cyclic vertices as original cells; '
                'DAG guarantee is modulo %d pre-existing SCCs (no nets removed)',
                cyclic.sum(), design['metadata']['original_cyclic_sccs'])
    condensed = quotient(graph, components)
    return topological_rank(condensed)[components]


def connected_refinement(graph, labels):
    """Never merge different candidate groups; split disconnected pieces."""
    coo = graph.tocoo(copy=False)
    inside = labels[coo.row] == labels[coo.col]
    sub = sparse.csr_matrix((np.ones(int(inside.sum()), dtype=bool),
                            (coo.row[inside], coo.col[inside])), shape=graph.shape)
    return csgraph.connected_components(sub, directed=True, connection='weak')[1].astype(np.int64)


def boundaries(design, labels):
    count = int(labels.max()) + 1
    if not len(design['sink_node']):
        return np.zeros(count, dtype=np.int64), np.zeros(count, dtype=np.int64)
    driver = labels[design['net_driver']]
    net = design['sink_net']; sink = labels[design['sink_node']]
    different = driver[net] != sink
    # Count each boundary NET once per cluster, not individual original pins.
    pairs = np.unique(np.column_stack((net[different], sink[different])), axis=0)
    ins = np.bincount(pairs[:, 1], minlength=count)
    outside_nets = np.unique(net[different])
    outs = np.bincount(driver[outside_nets], minlength=count)
    return ins, outs


def selected_groups(labels, selected):
    mask = np.zeros(int(labels.max()) + 1, dtype=bool); mask[selected] = True
    nodes = np.flatnonzero(mask[labels])
    nodes = nodes[np.argsort(labels[nodes], kind='stable')]
    ends = np.flatnonzero(np.diff(labels[nodes])) + 1
    return np.split(nodes, ends) if len(nodes) else []


def boundary_output_dependencies(design, labels, mutable):
    """Conservative output-net -> internal cells -> other output-net check.

    Seed ONLY internal sinks of the actual boundary net (not every fanout of
    its driver cell). Propagate inside eligible multi-cell clusters. Since
    these cells are outside original cyclic SCCs, a reached boundary driver
    represents a different downstream output, not a return to the seed net.
    Cell-level propagation assumes every input can affect every output; no
    Liberty pin-arc independence is inferred here. Multiple independent
    outputs, including sibling output pins of one cell, are not rejected.
    """
    n = len(labels)
    bad = np.zeros(len(mutable), dtype=bool)
    downstream = np.zeros(n, dtype=bool)
    driver, net, sink = design['net_driver'], design['sink_net'], design['sink_node']
    if not len(net):
        return bad, downstream
    cross = labels[driver[net]] != labels[sink]
    outside = np.zeros(len(driver), dtype=bool)
    outside[net[cross]] = True
    outputs = np.bincount(labels[driver[outside]], minlength=len(mutable))
    selected = mutable & (outputs > 1)
    seeds = np.unique(sink[outside[net] & ~cross & selected[labels[sink]]])
    if not len(seeds):
        return bad, downstream
    # One compiled sparse traversal for all groups, with inter-group edges
    # removed. This avoids Python DFS per boundary pin on million-cell designs.
    coo = design['graph'].tocoo(copy=False)
    inside = (labels[coo.row] == labels[coo.col]) & selected[labels[coo.row]]
    src = np.concatenate((coo.row[inside], np.full(len(seeds), n)))
    dst = np.concatenate((coo.col[inside], seeds))
    graph = sparse.csr_matrix((np.ones(len(src), dtype=bool), (src, dst)), shape=(n + 1, n + 1))
    reached = csgraph.breadth_first_order(graph, n, directed=True, return_predecessors=False)
    downstream[reached[reached < n]] = True
    reached_outputs = driver[outside & selected[labels[driver]]]
    bad[labels[reached_outputs[downstream[reached_outputs]]]] = True
    return bad, downstream


def weighted_topological_split(nodes, rank, weighted):
    """Balanced topological cut minimizing lost internal weight among cut points.

    This heuristic does not guarantee one-shot repair or global optimality.
    Every split is followed by a full quotient check.
    """
    nodes = nodes[np.argsort(rank[nodes], kind='stable')]
    n = len(nodes)
    positions = {int(node): i for i, node in enumerate(nodes)}
    difference = np.zeros(n + 1)
    for i, node in enumerate(nodes):
        for offset in range(weighted.indptr[node], weighted.indptr[node + 1]):
            j = positions.get(int(weighted.indices[offset]))
            if j is not None and j > i:
                w = weighted.data[offset]
                difference[i + 1] += w; difference[j + 1] -= w
    costs = np.cumsum(difference)
    low, high = max(1, n // 3), min(n - 1, (2 * n + 2) // 3)
    cut = min(range(low, high + 1), key=lambda k: (costs[k], abs(2 * k - n), k))
    return nodes[:cut], nodes[cut:]


def repair(design, labels, rank, args):
    graph, weighted = design['graph'], design['weighted']
    labels = normalize(labels)
    history = []
    # Every pass splits groups; never merges. Singleton fallback terminates on
    # an original DAG even when a useful abstraction cannot be maintained.
    step = 0
    while True:
        before = int(labels.max()) + 1
        labels = connected_refinement(graph, labels)
        counts = np.bincount(labels)
        # Pre-existing cyclic SCCs can be immutable checking vertices in retain
        # mode. They are not physical clusters and are not subject to budgets.
        mutable = np.bincount(labels, weights=design.get('eligible', np.ones(len(labels), dtype=bool))) > 1
        q = quotient(graph, labels)
        _, components = csgraph.connected_components(q, directed=True, connection='strong')
        scc_sizes = np.bincount(components)
        cyclic = scc_sizes[components] > 1
        ins, outs = boundaries(design, labels)
        output_dependency, after_output = boundary_output_dependencies(design, labels, mutable)
        areas = np.bincount(labels, weights=design['areas'])
        limit = (((counts > args.max_cells) if args.max_cells else np.zeros(len(counts), dtype=bool)) | ((ins + outs) > args.max_boundary_pins) |
                 (ins * outs > args.max_timing_arcs) | (areas > args.max_area) |
                 (ins == 0) | (outs == 0))
        bad = np.flatnonzero((cyclic | limit | output_dependency) & mutable)
        if not len(bad):
            if cyclic.any():
                raise ModelError('Cycle remains after singleton fallback; original graph/model mismatch')
            topological_rank(q)
            return labels, history, ins, outs
        next_id = len(counts)
        fallback = step >= args.repair_rounds
        groups = selected_groups(labels, bad)
        for nodes in groups:
            if fallback:
                parts = [np.array([n]) for n in nodes]
            elif output_dependency[labels[nodes[0]]]:
                # Keep the upstream logic together and separate descendants of
                # tapped boundary outputs. Recheck connectivity and all budgets
                # next round; new boundary outputs can reveal more violations.
                parts = [nodes[~after_output[nodes]], nodes[after_output[nodes]]]
                if any(not len(part) for part in parts):
                    parts = weighted_topological_split(nodes, rank, weighted)
            else:
                parts = weighted_topological_split(nodes, rank, weighted)
            for part in parts:
                labels[part] = next_id; next_id += 1
        history.append(dict(round=step + 1, quotient_vertices=q.shape[0], quotient_edges=q.nnz,
            cyclic_sccs=int(np.sum(scc_sizes > 1)), cyclic_groups=int(cyclic.sum()),
            limit_violations=int(np.sum(limit & mutable)), split_groups=len(groups),
            output_dependency_violations=int(np.sum(output_dependency & mutable)),
            connectivity_splits=int(len(counts) - before), singleton_fallback=fallback))
        LOG.info('repair round=%d cyclic_sccs=%d output_dependencies=%d split_groups=%d fallback=%s',
                 step + 1, history[-1]['cyclic_sccs'], history[-1]['output_dependency_violations'], len(groups), fallback)
        labels = normalize(labels)
        if int(labels.max()) + 1 <= before:
            raise ModelError('Repair failed to make progress')
        step += 1


def recursive_modularity_candidates(design, args, ig, la):
    """Reference main.cxx + create_clusters_threads.py semantics.

    Score each induced graph's proposed partition with normalized Q (gamma=1).
    Below cutoff retain the PARENT graph, not its proposed communities. At or
    above cutoff recurse into communities. Domain roots and later legality
    repair are timing-specific additions. Use an explicit stack, not Python
    recursion, and keep physical IDs separate from local graph indices.
    """
    eligible = np.flatnonzero(design['eligible'])
    if not len(eligible):
        raise ModelError('No eligible cells after protection/domain/sequential filtering')
    sub = sparse.triu(design['weighted'][eligible][:, eligible], k=1, format='coo')
    graph = ig.Graph(n=len(eligible), edges=np.column_stack((sub.row, sub.col)).tolist(), directed=False)
    graph.es['weight'] = sub.data.tolist()
    graph.vs['original_node'] = eligible.tolist()
    labels = design.get('fixed_components', np.arange(len(design['eligible']), dtype=np.int64)).copy()
    domains = collections.defaultdict(list)
    for local, node in enumerate(eligible):
        domains[design['domains'][int(node)]].append(local)
    stack = []
    next_tree = 0
    for domain in sorted(domains):
        stack.append((graph.induced_subgraph(domains[domain]), next_tree, -1, 0))
        next_tree += 1
    del graph, sub
    trace = []
    leaves = 0
    last_log = time.monotonic()
    while stack:
        current, tree_id, parent, depth = stack.pop()
        n = current.vcount()
        q = None
        if n <= 1:
            parts, reason = [list(range(n))], 'singleton'
        elif not current.ecount():
            parts, reason = [[i] for i in range(n)], 'edgeless'
        else:
            partition = la.find_partition(current, la.RBConfigurationVertexPartition,
                weights='weight', resolution_parameter=args.resolution, seed=args.seed,
                n_iterations=args.iterations, max_comm_size=0)
            # Reference getModularity uses R=1 even if optimization resolution differs.
            q = float(current.modularity(partition.membership, weights='weight', resolution=1., directed=False))
            if not math.isfinite(q):
                raise ModelError('Nonfinite local modularity')
            parts = list(partition)
            reason = 'below_cutoff' if q < args.modularity_cutoff else 'one_community' if len(parts) <= 1 else 'recurse'
        trace.append((tree_id, parent, depth, n, current.ecount(), q, len(parts), reason))
        if reason == 'recurse':
            if any(not part or len(part) >= n for part in parts):
                raise ModelError('Recursive Leiden partition made no progress')
            for part in reversed(parts):
                stack.append((current.induced_subgraph(part), next_tree, tree_id, depth + 1))
                next_tree += 1
        else:
            nodes = np.asarray(current.vs['original_node'], dtype=np.int64)
            # No edges have no defined modularity. Keep vertices separately;
            # the normal connected_refinement would split them anyway.
            if reason == 'edgeless':
                labels[nodes] = len(labels) + leaves + np.arange(n)
                leaves += n
            else:
                labels[nodes] = len(labels) + leaves
                leaves += 1
        if time.monotonic() - last_log > 20:
            LOG.info('recursive Leiden: evaluated=%d leaves=%d pending=%d depth=%d',
                     len(trace), leaves, len(stack), depth)
            last_log = time.monotonic()
    design['modularity_trace'] = trace
    metadata = dict(mode='recursive_modularity', backend='leidenalg.RBConfigurationVertexPartition',
        igraph_version=ig.__version__, leidenalg_version=getattr(la, 'version', 'unknown'),
        seed=args.seed, resolution=args.resolution, iterations=args.iterations,
        modularity_cutoff=args.modularity_cutoff, modularity_score_resolution=1.0,
        score_scope='normalized weighted modularity on each domain-local induced graph',
        recurse_condition='Q >= cutoff and more than one community',
        stop_policy='retain current graph as one leaf, not its proposed partition',
        max_cells_applied='legality repair only; no size cap during recursive Leiden',
        evaluated_subgraphs=len(trace), domain_roots=len(domains), leaf_groups=leaves,
        maximum_depth=max((r[2] for r in trace), default=0),
        stop_counts=dict(collections.Counter(r[7] for r in trace)),
        trace_file='modularity_tree.tsv',
        reference='leiden_cpu/main.cxx and scripts/create_clusters_threads.py; Python backend, not bit-identical C++')
    return normalize(labels), metadata


def candidates(design, args):
    n = design['graph'].shape[0]
    eligible = np.flatnonzero(design['eligible'])
    labels = design.get('fixed_components', np.arange(n, dtype=np.int64)).copy()
    if args.initial_clusters:
        seen = set(); keys = {}; removed = 0
        design['input_cluster_ids'] = np.full(n, -1, dtype=np.int64)
        for cell_id, cid in table_rows(args.initial_clusters, ('cell_id', 'cluster_id')):
            cell_id, cid = int(cell_id), int(cid)
            if cell_id < 0 or cid < 0 or cell_id in seen or cell_id not in design['id_to_node']:
                raise ModelError('Invalid/duplicate initial membership ID')
            seen.add(cell_id); node = design['id_to_node'][cell_id]
            design['input_cluster_ids'][node] = cid
            if not design['eligible'][node]:
                removed += 1; continue
            key = (cid, design['domains'][node])
            if key not in keys:
                keys[key] = n + len(keys)
            labels[node] = keys[key]
        return normalize(labels), dict(mode='repair_existing', ineligible_members_retained=removed)
    ig, la = leiden_modules()
    if getattr(args, 'modularity_cutoff', None) is not None:
        return recursive_modularity_candidates(design, args, ig, la)
    sub = sparse.triu(design['weighted'][eligible][:, eligible], k=1, format='coo')
    if not len(eligible):
        raise ModelError('No eligible cells after protection/domain/sequential filtering')
    graph = ig.Graph(n=len(eligible), edges=np.column_stack((sub.row, sub.col)).tolist(), directed=False)
    if graph.ecount():
        partition = la.find_partition(graph, la.RBConfigurationVertexPartition, weights=sub.data.tolist(),
             resolution_parameter=args.resolution, seed=args.seed, n_iterations=args.iterations,
             max_comm_size=args.max_cells)
        membership = partition.membership
    else:
        membership = range(len(eligible))
    # Explicitly enforce domain separation even if a backend ever returns
    # disconnected communities or the weighted graph has isolated vertices.
    keys = {}
    for node, cid in zip(eligible, membership):
        key = (cid, design['domains'][int(node)])
        if key not in keys:
            keys[key] = n + len(keys)
        labels[node] = keys[key]
    return normalize(labels), dict(mode='leiden', backend='leidenalg.RBConfigurationVertexPartition',
        igraph_version=ig.__version__, leidenalg_version=getattr(la, 'version', 'unknown'),
        seed=args.seed, resolution=args.resolution, iterations=args.iterations)


def write_results(output, design, initial, labels, history, ins, outs, metadata, args, elapsed):
    counts = np.bincount(labels)
    selected = np.flatnonzero(np.bincount(labels, weights=design['eligible']) > 1)
    groups = selected_groups(labels, selected)
    groups.sort(key=lambda nodes: int(design['ids'][nodes].min()))
    final = np.full(len(design['eligible']), -1, dtype=np.int64)
    records = []
    for cid, nodes in enumerate(groups, 1):
        if not design['eligible'][nodes].all():
            raise ModelError('Noncandidate cell was merged')
        if len({design['domains'][int(node)] for node in nodes}) != 1:
            raise ModelError('Cross-domain final cluster')
        final[nodes] = cid; label = labels[nodes[0]]
        area = float(design['areas'][nodes].sum())
        footprint = area / args.utilization
        width = math.sqrt(footprint * args.aspect_ratio); height = footprint / width
        if not all(math.isfinite(v) and v > 0 for v in (area, footprint, width, height)):
            raise ModelError('Cluster size estimate overflow; check LEF areas/utilization/aspect-ratio')
        records.append((cid, len(nodes), area, width, height, int(ins[label]), int(outs[label]),
                        int(ins[label] * outs[label])))
    report = dict(status='complete', candidate=metadata, constraints=design['metadata'],
        vertices=design['graph'].shape[0], directed_edges=design['graph'].nnz,
        eligible_cells=int(design['eligible'].sum()), merged_cells=int(np.sum(final >= 0)),
        retained_eligible_cells=int(np.sum(design['eligible'] & (final < 0))),
        clusters=len(groups), original_dag=not bool(design['original_cyclic'].any()), final_quotient_dag=True,
        boundary_output_independence=dict(verified=True, remaining_violating_clusters=0,
            model='conservative cell reachability seeded from exact boundary-net internal sinks',
            scope='final multi-cell clusters only; retained original cells/SCCs unchanged',
            note='Not a complete Liberty-characterizability check; non-unate/conditional arcs and reconvergence still require validation'),
        dag_scope='full sequential-cut graph' if not design['original_cyclic'].any() else
                  'quotient with pre-existing SCCs retained as immutable checking vertices; original loops remain',
        repair_history=history, elapsed_seconds=elapsed,
        limits=dict(max_cells=args.max_cells, max_boundary_pins=args.max_boundary_pins,
                    max_timing_arcs=args.max_timing_arcs, max_area=None if math.isinf(args.max_area) else args.max_area),
        size_model=dict(unit='micron', area='sum of original LEF cell areas',
                        utilization=args.utilization, aspect_ratio=args.aspect_ratio),
        inputs={k: str(Path(getattr(args, k)).resolve()) if getattr(args, k) else None for k in
                ('edges', 'timing_edges', 'cell_names', 'saved_db', 'critical_cells', 'initial_clusters')},
        limitations=['DAG guarantee is for conservative cell graph, not pin-specific timing arcs',
                     'Macro combinational paths are conservatively connected; original cycles fail closed',
                     'Domain/clock eligibility comes from supplied timing-edge metadata, not fresh STA',
                     'No internal timing characterization, polarity/reconvergence or SDC exception validation',
                     'Sizes are area/aspect estimates without row/site/grid legalization',
                     'All reduced LEF/Liberty/Verilog/DEF artifacts must be regenerated for this membership'])
    # Every output cell in the input ID table is accounted for, either merged or
    # explicitly retained. Membership contains only real multi-cell clusters.
    output.mkdir(parents=True, exist_ok=False)
    try:
        if 'modularity_trace' in design:
            with (output / 'modularity_tree.tsv').open('x') as stream:
                writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
                writer.writerow(('tree_id', 'parent_id', 'depth', 'cells', 'edges', 'modularity', 'communities', 'decision'))
                writer.writerows(design['modularity_trace'])
        with (output / 'clusters.tsv').open('x') as membership, \
             (output / 'candidate_clusters.tsv').open('x') as candidate, \
             (output / 'cell_assignments.tsv').open('x') as audit:
            membership.write('cell_id\tcluster_id\n'); candidate.write('cell_id\tcluster_id\n')
            writer = csv.writer(audit, delimiter='\t', lineterminator='\n')
            writer.writerow(('cell_id', 'cell_name', 'candidate_cluster', 'cluster_id', 'disposition', 'input_cluster_id'))
            for cell_id, node in sorted(design['id_to_node'].items()):
                cid = int(final[node])
                candidate_id = int(initial[node]) if design['eligible'][node] else -1
                if cid >= 0:
                    membership.write('%d\t%d\n' % (cell_id, cid))
                if candidate_id >= 0:
                    candidate.write('%d\t%d\n' % (cell_id, candidate_id))
                disposition = ('clustered' if cid >= 0 else 'retained_original_cycle' if design['original_cyclic'][node]
                               else 'retained_singleton' if design['eligible'][node] else 'retained_ineligible')
                input_id = int(design['input_cluster_ids'][node]) if 'input_cluster_ids' in design else -1
                writer.writerow((cell_id, design['names'][node], candidate_id, cid, disposition, input_id))
        with (output / 'cluster_sizes.tsv').open('x') as stream:
            stream.write('cluster_id\twidth\theight\n')
            for cid, _, _, width, height, *_ in records:
                stream.write('%d\t%.12g\t%.12g\n' % (cid, width, height))
        with (output / 'cluster_stats.tsv').open('x', newline='') as stream:
            writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
            writer.writerow(('cluster_id', 'cells', 'cell_area_um2', 'width_um', 'height_um',
                             'input_pins', 'output_pins', 'conservative_timing_arcs'))
            writer.writerows(records)
        with (output / 'summary.json').open('x') as stream:
            json.dump(report, stream, indent=2, allow_nan=False); stream.write('\n')
    except Exception:
        # A failed export must never masquerade as a completed partition.
        with (output / 'FAILED_DO_NOT_USE.txt').open('w') as stream:
            stream.write('Export failed; incomplete outputs. Use a fresh output directory.\n')
        raise
    return report


def generate(args):
    begin = time.monotonic()
    output = Path(args.output)
    if os.path.lexists(output):
        raise ModelError('Output directory already exists; choose a new path: ' + str(output))
    if args.max_cells < 0:
        raise ModelError('--max-cells must be nonnegative (0 means unlimited)')
    cutoff = getattr(args, 'modularity_cutoff', None)
    if cutoff is not None:
        if not math.isfinite(cutoff) or not -.5 <= cutoff <= 1.:
            raise ModelError('--modularity-cutoff must be finite and between -0.5 and 1')
        if args.initial_clusters:
            raise ModelError('--modularity-cutoff cannot be combined with --initial-clusters')
    for name in ('max_boundary_pins', 'max_timing_arcs'):
        if getattr(args, name) < 1:
            raise ModelError('--' + name.replace('_', '-') + ' must be positive')
    if args.repair_rounds < 0 or args.iterations < 1:
        raise ModelError('repair-rounds must be nonnegative and iterations positive')
    for name in ('resolution', 'utilization', 'aspect_ratio'):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
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
        leiden_modules()  # Fail early if the backend is unavailable.
    design = load_design(args)
    LOG.info('Checking original directed graph before any contraction')
    rank = prepare_constraints(design, args.original_cycles)
    LOG.info('Forming candidate partition')
    initial, metadata = candidates(design, args)
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
    if args.initial_clusters and metadata['multicell_candidate_groups'] and metadata['internal_weight_fraction'] < .01:
        LOG.warning('Existing membership keeps less than 1%% of eligible edge weight inside clusters; '
                    'check that IDs are original cell IDs, not reindexed community-graph vertex numbers')
    del weights, active, internal
    final, history, ins, outs = repair(design, initial.copy(), rank, args)
    report = write_results(output, design, initial, final, history, ins, outs, metadata, args,
                           time.monotonic() - begin)
    return report


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--edges', required=True, help='driver_id sink_id weight table, e.g. cell_edges.tsv')
    p.add_argument('--timing-edges', required=True, help='original timing_edges.csv for eligibility/domain masks')
    p.add_argument('--cell-names', required=True, help='original ID/name table (not save/node_names.txt)')
    p.add_argument('--saved-db', required=True, help='MakeDB save directory with cells/lef/netlist/ext_pin_info.json')
    p.add_argument('--lib-dir', required=True, action='append', help='original Liberty file or directory; repeatable')
    p.add_argument('--critical-cells', help='protected cell table; default sibling critical_cells.tsv')
    p.add_argument('--no-critical-protection', action='store_true')
    p.add_argument('--initial-clusters', help='optional existing cell_id cluster_id table: repair only, no Leiden run')
    p.add_argument('--resolution', type=float, default=1.)
    p.add_argument('--modularity-cutoff', type=float, default=None,
                   help='recursive induced-graph Q cutoff, e.g. 0.7; Q >= cutoff recurses, Q < cutoff retains parent; omitted: legacy one-shot Leiden')
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--iterations', type=int, default=2)
    p.add_argument('--max-cells', type=int, default=32, help='final cell-count cap; 0 disables; recursive mode applies it only during repair')
    p.add_argument('--max-boundary-pins', type=int, default=24)
    p.add_argument('--max-timing-arcs', type=int, default=64, help='conservative inputs*outputs limit')
    p.add_argument('--max-area', type=float, default=float('inf'), help='sum of member LEF area in um^2')
    p.add_argument('--repair-rounds', type=int, default=12, help='weighted split rounds before singleton fallback')
    p.add_argument('--original-cycles', choices=('error', 'retain'), default='error',
                   help='default error; retain quarantines pre-existing cyclic SCCs as original cells without deleting nets')
    p.add_argument('--utilization', type=float, default=1., help='size estimate: cell area / utilization')
    p.add_argument('--aspect-ratio', type=float, default=1., help='estimated cluster width/height')
    p.add_argument('--output', required=True, help='new output directory; never overwritten')
    return p.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    try:
        report = generate(parse_args(argv))
    except (ModelError, OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr); return 1
    print('Saved %d clusters / %d merged cells; %d eligible cells retained; final quotient DAG verified.' %
          (report['clusters'], report['merged_cells'], report['retained_eligible_cells']))
    if not report['original_dag']:
        print('WARNING: original cyclic SCCs remain unchanged; DAG verification is modulo those pre-existing SCCs.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
