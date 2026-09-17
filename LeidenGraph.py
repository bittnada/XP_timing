"""Full structural constraint graph for Leiden clustering, independent of STA.

IDs in timing tables are joined by cell name, never by MakeDB dictionary order.
Sequential instances have distinct input and output vertices (no D->Q edge).
All other cells, including protected combinational cells, remain in the graph.
"""
from array import array
import csv
import json
import logging
import math
from pathlib import Path
import re

import numpy as np
from scipy import sparse

from ClusterLEFMapping import object_items
from ReducedLiberty import ModelError, discover_libraries, index_liberty, table_rows

LOG = logging.getLogger('LeidenCluster')


def dict_rows(path):
    with open(path, encoding='utf-8-sig', newline='') as stream:
        header = stream.readline()
        stream.seek(0)
        yield from csv.DictReader(stream, delimiter='\t' if '\t' in header else ',')


def liberty_sequential(inputs):
    """Read cell headers/bodies one at a time, without retaining NLDM tables."""
    known, sequential = set(), set()
    paths = discover_libraries(inputs)
    for path in paths:
        index = index_liberty(path)
        with open(path, 'rb') as stream:
            for master, (start, end) in index['cells'].items():
                stream.seek(start)
                body = stream.read(end - start).decode()
                body = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"', '', body, flags=re.S)
                is_seq = bool(re.search(r'\b(?:ff|ff_bank|latch|latch_bank|statetable)\s*\(', body))
                if master in known and (master in sequential) != is_seq:
                    raise ModelError('Conflicting sequential classification across Liberty files: ' + master)
                known.add(master)
                if is_seq:
                    sequential.add(master)
    return known, sequential, [str(p) for p in paths]


def load_design(args):
    root = Path(args.saved_db)
    LOG.info('Loading saved cell/LEF metadata and Liberty sequential boundaries')
    known, sequential, libraries = liberty_sequential(args.lib_dir)
    lef = dict(object_items(root / 'lef_info.json'))
    names, masters, movable = [], [], []
    name_to_node = {}
    for name, info in object_items(root / 'cells_info.json'):
        if name in name_to_node:
            raise ModelError('Duplicate saved cell: ' + name)
        master = info['macro_id']
        name_to_node[name] = len(names)
        names.append(name); masters.append(master)
        movable.append(info.get('placed_state') not in ('FIXED', 'COVER') and
                       str(lef.get(master, {}).get('class', '')).split()[:1] == ['CORE'])
    ncell = len(names)
    ids = np.full(ncell, -1, dtype=np.int64)
    id_to_node = {}; seen_names = set()
    for cell_id, name in table_rows(args.cell_names, ('cell_id', 'cell_name')):
        cell_id = int(cell_id)
        if cell_id < 0 or cell_id in id_to_node or name in seen_names:
            raise ModelError('Duplicate/invalid cell ID or name in --cell-names')
        if name not in name_to_node:
            raise ModelError('cell_names entry absent from saved physical cells: ' + name)
        node = name_to_node[name]
        id_to_node[cell_id] = node; seen_names.add(name); ids[node] = cell_id
    domains = {}; protected = set()
    if args.critical_cells:
        for row in dict_rows(args.critical_cells):
            # Fixed-shape physical names can differ from original timing names.
            for field in ('cell_name', 'timing_cell_name'):
                if row.get(field) in name_to_node:
                    protected.add(name_to_node[row[field]])
    LOG.info('Reading timing-edge eligibility and launch/capture domain masks')
    required = {'driver_id', 'driver', 'sink_id', 'sink', 'launch_domain_mask', 'capture_domain_mask'}
    for row in dict_rows(args.timing_edges):
        if not required.issubset(row):
            raise ModelError('--timing-edges needs driver/sink IDs, names and domain masks')
        launch, capture = int(row['launch_domain_mask']), int(row['capture_domain_mask'])
        unknown = 1 << 63
        if launch < 0 or capture <= 0 or launch >= unknown or capture >= unknown or \
                capture & (capture - 1) or launch & (launch - 1) or (launch and launch != capture):
            raise ModelError('Unresolved/multiple/cross-domain mask in timing edges')
        for prefix in ('driver', 'sink'):
            cell_id = int(row[prefix + '_id'])
            node = id_to_node.get(cell_id)
            if node is None or names[node] != row[prefix]:
                raise ModelError('Timing-edge ID/name mismatch: %s %s' % (cell_id, row[prefix]))
            if node in domains and domains[node] != (launch, capture):
                raise ModelError('Inconsistent domain masks for cell: ' + names[node])
            domains[node] = (launch, capture)
    eligible = np.zeros(ncell, dtype=bool)
    for node in domains:
        if masters[node] not in known:
            raise ModelError('Timing-edge cell master missing from Liberty: ' + masters[node])
        eligible[node] = movable[node] and masters[node] not in sequential and node not in protected
    # Retain all noncandidate cells. Sequential output vertices are extra source
    # nodes, with no path from the instance's input vertex to its output vertex.
    output_node = np.arange(ncell, dtype=np.int64)
    vertex_names = list(names)
    for node, master in enumerate(masters):
        if master in sequential:
            output_node[node] = len(vertex_names)
            vertex_names.append(names[node] + ':<SEQ_OUTPUT>')
    ports = dict(object_items(root / 'ext_pin_info.json'))
    port_nodes = {}
    for name in sorted(ports):
        port_nodes[name] = len(vertex_names); vertex_names.append('PIN ' + name)
    n = len(vertex_names)
    areas = np.zeros(n)
    for node, master in enumerate(masters):
        if master in lef:
            area = float(lef[master]['width']) * float(lef[master]['height'])
            if not math.isfinite(area) or area <= 0:
                raise ModelError('Invalid LEF area for ' + master)
            areas[node] = area
    graph_src, graph_dst = array('q'), array('q')
    net_driver, sink_net, sink_node = array('q'), array('q'), array('q')
    pin_bits = {m: {p: i for i, p in enumerate(info.get('pin', {}))} for m, info in lef.items()}
    connected = [0] * ncell
    seen_ports = set(); seen_nets = set()
    LOG.info('Building complete directed graph; protected cells remain as vertices')
    for net, info in object_items(root / 'netlist_info.json'):
        if net in seen_nets:
            raise ModelError('Duplicate net: ' + net)
        seen_nets.add(net)
        if str(info.get('use', 'SIGNAL')).replace('USE ', '').strip() in ('POWER', 'GROUND'):
            continue
        drivers, sinks = [], []
        for endpoint in info['cell_list']:
            parts = endpoint.split()
            if len(parts) != 2:
                raise ModelError('Unsupported endpoint on ' + net + ': ' + endpoint)
            name, pin = parts
            if name == 'PIN':
                if pin not in ports or pin in seen_ports:
                    raise ModelError('Missing/duplicate external pin: ' + pin)
                seen_ports.add(pin)
                direction = {'INPUT': 'OUTPUT', 'OUTPUT': 'INPUT'}.get(ports[pin].get('direction'))
                node = port_nodes[pin]
            else:
                if name not in name_to_node:
                    raise ModelError('Unknown net endpoint cell: ' + name)
                node = name_to_node[name]; master = masters[node]
                if master not in known:
                    raise ModelError('Connected cell master missing from Liberty (cannot establish sequential boundary): ' + master)
                model = lef.get(master, {}).get('pin', {}).get(pin)
                if model is None:
                    raise ModelError('Missing LEF pin: %s/%s' % (master, pin))
                bit = 1 << pin_bits[master][pin]
                if connected[node] & bit:
                    raise ModelError('Pin connected more than once: ' + endpoint)
                connected[node] |= bit
                direction = model.get('direction')
                if direction == 'OUTPUT':
                    node = int(output_node[node])
            if direction == 'OUTPUT':
                drivers.append(node)
            elif direction == 'INPUT':
                sinks.append(node)
            else:
                raise ModelError('INOUT/unknown direction on net ' + net)
        if len(drivers) != 1:
            raise ModelError('Expected one driver on net %s; found %d' % (net, len(drivers)))
        driver = drivers[0]; netid = len(net_driver); net_driver.append(driver)
        for sink in sorted(set(sinks)):
            graph_src.append(driver); graph_dst.append(sink)
            sink_net.append(netid); sink_node.append(sink)
    src = np.asarray(graph_src, dtype=np.int64); dst = np.asarray(graph_dst, dtype=np.int64)
    graph = sparse.csr_matrix((np.ones(len(src), dtype=bool), (src, dst)), shape=(n, n))
    # Full net information is separate from filtered weighted edges.
    srcs, dsts, weights = array('q'), array('q'), array('d')
    skipped = 0
    for a, b, w in table_rows(args.edges, ('driver_id', 'sink_id', 'weight')):
        a, b, w = int(a), int(b), float(w)
        if a not in id_to_node or b not in id_to_node or not math.isfinite(w) or w < 0:
            raise ModelError('Invalid ID or weight in --edges')
        u, v = id_to_node[a], id_to_node[b]
        if not eligible[u] or not eligible[v] or domains[u] != domains[v]:
            skipped += 1; continue
        if u == v:
            raise ModelError('Self edge in clustering weights')
        srcs.append(u); dsts.append(v); weights.append(w)
    ws, wd = np.asarray(srcs, dtype=np.int64), np.asarray(dsts, dtype=np.int64)
    if len(ws) and not np.asarray(graph[ws, wd]).ravel().all():
        raise ModelError('A weighted cell edge is absent from saved connectivity; use matching input files')
    weighted = sparse.csr_matrix((np.asarray(weights), (ws, wd)), shape=(n, n))
    weighted = weighted + weighted.T
    weighted.eliminate_zeros()
    if not np.isfinite(weighted.data).all():
        raise ModelError('Aggregated weights overflow')
    eligible = np.pad(eligible, (0, n - ncell))
    LOG.info('graph vertices=%d edges=%d eligible=%d protected=%d', n, graph.nnz, eligible.sum(), len(protected))
    return dict(graph=graph, weighted=weighted, eligible=eligible, domains=domains,
                names=names, vertex_names=vertex_names, ids=ids, id_to_node=id_to_node,
                masters=masters, areas=areas, net_driver=np.asarray(net_driver, dtype=np.int64),
                sink_net=np.asarray(sink_net, dtype=np.int64), sink_node=np.asarray(sink_node, dtype=np.int64),
                metadata=dict(libraries=libraries, sequential_masters=sorted(sequential),
                              protected_cells=len(protected), discarded_weight_rows=skipped,
                              domain_source='timing_edges launch/capture masks (not recomputed)',
                              graph_model='conservative cell connectivity; sequential inputs/outputs separated'))
