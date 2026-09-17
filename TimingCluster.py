#!/usr/bin/env python3
"""Timing-aware standard-cell clustering for DREAMPlace.

The implementation deliberately uses PlaceDB's compact numpy/CSR-like arrays.
It therefore does not build a second Python netlist, which is important for the
million-cell ICCAD designs.  The generated files are an inspectable clustering
contract; ``cluster_map.jsonl`` is also the stable interface intended for a
reduced physical/timing database generator.
"""

from __future__ import print_function

import argparse
import collections
import csv
import json
import logging
import math
import os
import re
import sys
import time

import numpy as np


LOG = logging.getLogger("TimingCluster")


def _text(value):
    return value.decode("utf8") if isinstance(value, (bytes, np.bytes_)) else str(value)


def _path(value, base):
    if not value:
        return ""
    return value if os.path.isabs(value) else os.path.normpath(os.path.join(base, value))


def _local_pin(pin_name):
    name = _text(pin_name)
    return name.rsplit(" ", 1)[-1].rsplit(":", 1)[-1].rsplit("/", 1)[-1]


def parse_liberty_boundaries(filename):
    """Return sequential cell names and their clock pins.

    This is intentionally a small structural Liberty reader.  It only examines
    cell/ff/latch headers and clocked_on expressions; LUT contents are skipped.
    """
    sequential = set()
    clock_pins = collections.defaultdict(set)
    current = None
    depth = 0
    cell_depth = 0
    cell_re = re.compile(r'^\s*cell\s*\(\s*"?([^"\)]+)')
    clock_re = re.compile(r'clocked_on\s*:\s*"([^"]+)"')
    with open(filename, "r", errors="replace") as stream:
        for line in stream:
            match = cell_re.search(line)
            if current is None and match:
                current = match.group(1).strip()
                cell_depth = depth + line.count("{") - line.count("}")
            if current is not None:
                if re.search(r'\b(ff|latch)\s*\(', line):
                    sequential.add(current)
                cm = clock_re.search(line)
                if cm:
                    for token in re.findall(r'[A-Za-z_$][\w$]*', cm.group(1)):
                        if token.lower() not in ("not", "and", "or"):
                            clock_pins[current].add(token)
            depth += line.count("{") - line.count("}")
            if current is not None and depth < cell_depth:
                current = None
    return sequential, dict(clock_pins)


def parse_lef_blocks(filename):
    """Return masters declared as BLOCK/PAD and basic site dimensions."""
    blocks = set()
    current = None
    site = None
    site_width = site_height = None
    macro_re = re.compile(r'^\s*MACRO\s+(\S+)')
    site_re = re.compile(r'^\s*SITE\s+(\S+)')
    size_re = re.compile(r'^\s*SIZE\s+([0-9.eE+-]+)\s+BY\s+([0-9.eE+-]+)')
    with open(filename, "r", errors="replace") as stream:
        for line in stream:
            mm = macro_re.match(line)
            if mm:
                current = mm.group(1)
                continue
            sm = site_re.match(line)
            if sm and current is None:
                site = sm.group(1)
                continue
            if current and re.search(r'\bCLASS\s+(BLOCK|PAD)\b', line):
                blocks.add(current)
            if site and current is None:
                zm = size_re.match(line)
                if zm:
                    site_width, site_height = float(zm.group(1)), float(zm.group(2))
                if re.match(r'^\s*END\s+' + re.escape(site) + r'\s*$', line):
                    site = None
            if current and re.match(r'^\s*END\s+' + re.escape(current) + r'\s*$', line):
                current = None
    return blocks, (site_width, site_height)


def parse_sdc_clocks(filename):
    clocks = {}
    if not filename or not os.path.exists(filename):
        return clocks
    name_re = re.compile(r'-name\s+(?:\{([^}]+)\}|(\S+))')
    port_re = re.compile(r'get_ports\s+(?:\{([^}]+)\}|([^\]\s]+))')
    with open(filename, "r", errors="replace") as stream:
        for line in stream:
            if "create_clock" not in line or "get_ports" not in line:
                continue
            nm, pm = name_re.search(line), port_re.search(line)
            if pm:
                port = (pm.group(1) or pm.group(2)).strip()
                name = ((nm.group(1) or nm.group(2)).strip() if nm else port)
                clocks[port] = name
    return clocks


def _name_lookup(mapping, name):
    value = mapping.get(name)
    if value is None:
        value = mapping.get(name.encode("utf8"))
    return value


def scan_anchor_instances(filename, anchor_masters, node_name2id):
    """Scan structural Verilog once, retaining only sequential/macro instances."""
    anchors = {}
    instance_re = re.compile(r'^\s*([^\s/][^\s]*)\s+([^\s(]+)\s*\(')
    with open(filename, "r", errors="replace") as stream:
        for line in stream:
            match = instance_re.match(line)
            if not match or match.group(1) not in anchor_masters:
                continue
            master, name = match.group(1), match.group(2)
            idx = _name_lookup(node_name2id, name)
            if idx is not None:
                anchors[int(idx)] = master
    return anchors


class ClusterEngine(object):
    def __init__(self, params, placedb):
        self.params = params
        self.db = placedb
        self.nphys = int(placedb.num_physical_nodes)
        self.nmove = int(placedb.num_movable_nodes)
        self.max_cells = int(getattr(params, "timing_cluster_max_cells", 32))
        self.max_boundary = int(getattr(params, "timing_cluster_max_boundary_pins", 24))
        self.max_arcs = int(getattr(params, "timing_cluster_max_timing_arcs", 64))
        self.clusters = []
        self.assignment = np.full(self.nphys, -1, dtype=np.int32)
        self.driver = None
        self.domains = np.zeros(self.nphys, dtype=np.uint64)
        self.anchor_master = {}
        self.sequential_nodes = set()
        self.macro_nodes = set()
        self.clock_cells = set()
        self.launch_domains = np.zeros(self.nphys, dtype=np.uint64)
        # Keep structural candidates intact for domain propagation/topology.
        # Protected cells are barriers, not removed vertices in that graph.
        self.critical_nodes = set()
        self.critical_summary = {"status": "not_extracted"}
        self.cyclic_nodes = set()
        self.cyclic_scc_ids = None
        self.cycle_summary = {"status": "not_checked"}

    def prepare(self, config_dir):
        p = self.params
        lef = _path(getattr(p, "lef_input", ""), config_dir)
        verilog = _path(getattr(p, "verilog_input", ""), config_dir)
        liberty = _path(getattr(p, "late_lib_input", "") or getattr(p, "lib_input", ""), config_dir)
        sdc = _path(getattr(p, "sdc_input", ""), config_dir)
        if not (lef and verilog and liberty):
            raise ValueError("timing clustering requires lef_input, verilog_input, and a Liberty input")

        seq_masters, clock_pins = parse_liberty_boundaries(liberty)
        block_masters, self.site_size = parse_lef_blocks(lef)
        masters = seq_masters | block_masters
        # Keep PlaceDB's existing name maps in place.  Copying them would cost
        # hundreds of MB on the largest benchmarks.
        name2id = self.db.node_name2id_map
        self.anchor_master = scan_anchor_instances(verilog, masters, name2id)
        self.sequential_nodes = {n for n, m in self.anchor_master.items() if m in seq_masters}
        self.macro_nodes = {n for n, m in self.anchor_master.items() if m in block_masters}
        self.clock_pins = clock_pins
        self.clocks = parse_sdc_clocks(sdc)
        if len(self.clocks) > 63:
            raise ValueError("Clock-domain masks support at most 63 clock source ports")

        # Fixed terminals, sequential cells and physical macros are never candidates.
        self.candidate = np.zeros(self.nphys, dtype=np.bool_)
        self.candidate[:self.nmove] = True
        if self.sequential_nodes:
            self.candidate[np.fromiter(self.sequential_nodes, dtype=np.int64)] = False
        if self.macro_nodes:
            self.candidate[np.fromiter(self.macro_nodes, dtype=np.int64)] = False
        # Also retain DREAMPlace's geometric movable-macro classification.
        mmask = getattr(self.db, "movable_macro_mask", None)
        if mmask is not None and len(mmask):
            mids = np.flatnonzero(mmask)
            mids = mids[mids < self.nphys]
            self.candidate[mids] = False
            self.macro_nodes.update(map(int, mids))

        pin_dir = self.db.pin_direct
        outpins = np.flatnonzero(pin_dir == b"OUTPUT")
        self.driver = np.full(int(self.db.num_nets), -1, dtype=np.int32)
        self.driver[self.db.pin2net_map[outpins]] = self.db.pin2node_map[outpins]
        self._derive_clock_domains(name2id)
        LOG.info("eligible combinational cells=%d, sequential=%d, macros=%d, clocks=%d",
                 int(self.candidate.sum()), len(self.sequential_nodes),
                 len(self.macro_nodes), len(self.clocks))

    def _node_output_nets(self, node):
        for pin in self.db.node2pin_map[node]:
            if self.db.pin_direct[pin] == b"OUTPUT":
                yield int(self.db.pin2net_map[pin])

    def _predecessors(self, node):
        seen = set()
        for pin in self.db.node2pin_map[node]:
            if self.db.pin_direct[pin] == b"INPUT":
                pred = int(self.driver[int(self.db.pin2net_map[pin])])
                if 0 <= pred < self.nphys and pred != node and pred not in seen:
                    seen.add(pred)
                    yield pred

    def _successors(self, node):
        seen = set()
        for net in self._node_output_nets(node):
            for pin in self.db.net2pin_map[net]:
                if self.db.pin_direct[pin] != b"INPUT":
                    continue
                dst = int(self.db.pin2node_map[pin])
                if 0 <= dst < self.nphys and dst != node and dst not in seen:
                    seen.add(dst)
                    yield dst

    def _derive_clock_domains(self, name2id):
        """Mark clock-tree cells and derive conservative data-domain bitmasks."""
        if not self.clocks:
            self.domains[self.candidate] = 1
            return
        net_name2id = self.db.net_name2id_map
        net_domain = np.zeros(int(self.db.num_nets), dtype=np.uint64)
        queue = collections.deque()
        for bit, port in enumerate(sorted(self.clocks)):
            if bit >= 63:
                break
            net = _name_lookup(net_name2id, port)
            if net is not None:
                mask = np.uint64(1) << np.uint64(bit)
                net_domain[net] |= mask
                queue.append(net)
        # Propagate only through candidate combinational cells.  Sequential and
        # macro boundaries stop the clock traversal.
        visited_cells = np.zeros(self.nphys, dtype=np.uint64)
        while queue:
            net = queue.popleft()
            mask = net_domain[net]
            for pin in self.db.net2pin_map[net]:
                if self.db.pin_direct[pin] != b"INPUT":
                    continue
                node = int(self.db.pin2node_map[pin])
                if node >= self.nphys or not self.candidate[node]:
                    continue
                new = mask & ~visited_cells[node]
                if not new:
                    continue
                visited_cells[node] |= mask
                self.clock_cells.add(node)
                for outnet in self._node_output_nets(node):
                    old = net_domain[outnet]
                    net_domain[outnet] |= mask
                    if old != net_domain[outnet]:
                        queue.append(outnet)

        # The clock tree is timing-sensitive and usually high fanout.  Keep it
        # outside data-path clusters; this also preserves SDC clock topology.
        if self.clock_cells:
            self.candidate[np.fromiter(self.clock_cells, dtype=np.int64)] = False

        # Identify each sequential element's clock domain from Liberty clock pins.
        seq_domain = {}
        for node in self.sequential_nodes:
            master = self.anchor_master[node]
            clocks = self.clock_pins.get(master, set())
            mask = np.uint64(0)
            for pin in self.db.node2pin_map[node]:
                if _local_pin(self.db.pin_names[pin]) in clocks:
                    mask |= net_domain[int(self.db.pin2net_map[pin])]
            seq_domain[node] = mask or np.uint64(1 << 63)

        order, _ = self._topological_order()
        for node, mask in seq_domain.items():
            for dst in self._successors(node):
                if self.candidate[dst]:
                    self.launch_domains[dst] |= mask
            for pred in self._predecessors(node):
                if self.candidate[pred]:
                    self.domains[pred] |= mask
        for node in order:
            for dst in self._successors(node):
                if self.candidate[dst]:
                    self.launch_domains[dst] |= self.launch_domains[node]
        for node in reversed(order):
            mask = self.domains[node]
            if mask:
                for pred in self._predecessors(node):
                    if self.candidate[pred]:
                        self.domains[pred] |= mask
        # Logic without a sequential endpoint is its own neutral domain.
        self.domains[self.candidate & (self.domains == 0)] = np.uint64(1) << np.uint64(63)

    def _topological_order(self):
        candidates = np.flatnonzero(self.candidate)
        indegree = np.zeros(self.nphys, dtype=np.int32)
        for node in candidates:
            indegree[node] = sum(1 for p in self._predecessors(int(node)) if self.candidate[p])
        # A LIFO ready set is still a valid Kahn topological sort, but tends to
        # stay in the same cone after a successor becomes ready.  This produces
        # useful contiguous convex clusters instead of interleaving all cones.
        queue = list(map(int, candidates[indegree[candidates] == 0]))
        order = []
        while queue:
            node = queue.pop()
            order.append(node)
            for dst in self._successors(node):
                if not self.candidate[dst]:
                    continue
                indegree[dst] -= 1
                if indegree[dst] == 0:
                    queue.append(dst)
        residual = candidates[indegree[candidates] > 0]
        return order, list(map(int, residual))

    def detect_cyclic_cells(self):
        """Find actual cyclic SCC members, not all Kahn-sort residual nodes.

        The cell->net->cell incidence graph is linear in pin count, including
        high-fanout/multiple-driver nets. Sequential vertices are cut. All
        other physical cells remain, even fixed/macro/clock/critical cells;
        candidate/domain/weight filtering must not hide a return path.
        Unknown/INOUT directions are conservative bidirectional incidences.
        This is a cell-level structural check, not pin-specific timing arcs.
        """
        if self.cycle_summary['status'] != 'not_checked':
            return self.cycle_summary
        from scipy import sparse
        from scipy.sparse import csgraph

        begin = time.time()
        nnet = len(self.db.net2pin_map)
        nodes = np.asarray(self.db.pin2node_map, dtype=np.int64)
        directions = np.asarray(self.db.pin_direct)
        if directions.dtype.kind == 'O':
            directions = np.asarray([_text(v) for v in directions])
        out_label, in_label = ('OUTPUT', 'INPUT') if directions.dtype.kind == 'U' else (b'OUTPUT', b'INPUT')
        outputs, inputs = directions == out_label, directions == in_label
        unknown = ~(outputs | inputs)
        if hasattr(self.db, 'pin2net_map'):
            nets = np.asarray(self.db.pin2net_map, dtype=np.int64)
        else:
            # Also supports minimal standalone PlaceDB-like test fixtures.
            nets = np.full(len(nodes), -1, dtype=np.int64)
            for net, pins in enumerate(self.db.net2pin_map):
                nets[np.asarray(pins, dtype=np.int64)] = net
        if len(nodes) != len(directions) or len(nodes) != len(nets) or \
                np.any(nets < 0) or np.any(nets >= nnet):
            raise ValueError('Invalid pin arrays for structural cycle detection')
        sequential = np.zeros(self.nphys, dtype=np.bool_)
        if self.sequential_nodes:
            sequential[np.fromiter(self.sequential_nodes, dtype=np.int64)] = True
        valid = (nodes >= 0) & (nodes < self.nphys)
        valid[valid] &= ~sequential[nodes[valid]]
        outgoing = valid & (outputs | unknown)
        incoming = valid & (inputs | unknown)
        src = np.concatenate((nodes[outgoing], self.nphys + nets[incoming]))
        dst = np.concatenate((self.nphys + nets[outgoing], nodes[incoming]))
        graph = sparse.csr_matrix((np.ones(len(src), dtype=np.bool_), (src, dst)),
                                  shape=(self.nphys + nnet, self.nphys + nnet))
        _, labels = csgraph.connected_components(graph, directed=True, connection='strong')
        sizes = np.bincount(labels)
        # A cell output tied to its own input forms cell->net->cell (size 2),
        # so singleton-cell feedback is detected as well as multi-cell SCCs.
        cycle_ids = np.flatnonzero(sizes[labels[:self.nphys]] > 1)
        self.cyclic_nodes = set(map(int, cycle_ids))
        self.cyclic_scc_ids = np.full(self.nphys, -1, dtype=np.int32)
        canonical = {}
        for node in cycle_ids:
            label = int(labels[node])
            if label not in canonical:
                canonical[label] = len(canonical)
            self.cyclic_scc_ids[node] = canonical[label]
        driver_counts = np.bincount(nets[outputs], minlength=nnet)
        ambiguous = driver_counts != 1
        ambiguous[nets[unknown]] = True
        self.cycle_summary = dict(status='enabled', cyclic_cells=len(cycle_ids),
            cyclic_sccs=len(canonical), protected_candidate_cells=int(self.candidate[cycle_ids].sum()),
            sequential_cut_cells=len(self.sequential_nodes), graph_vertices=graph.shape[0],
            graph_edges=graph.nnz, ambiguous_nets_conservative=int(ambiguous.sum()),
            elapsed_seconds=time.time() - begin,
            files=['cyclic_cells.tsv', 'cyclic_cells_summary.json'],
            model='cell-net incidence SCC; sequential vertices cut; no candidate/domain/weight filtering',
            note='Only SCC cycle members are excluded, not acyclic descendants. '
                 'Original cells/nets remain unchanged. Unknown pin directions are conservative; '
                 'this is not a pin-specific timing-loop proof.')
        LOG.info('structural cycles: %d cells in %d SCCs; %d candidate cells protected (%.2fs)',
                 len(cycle_ids), len(canonical), self.cycle_summary['protected_candidate_cells'],
                 self.cycle_summary['elapsed_seconds'])
        return self.cycle_summary

    def extract_cyclic_cells(self, output_dir):
        metadata = self.detect_cyclic_cells()
        os.makedirs(output_dir, exist_ok=True)
        sizes = collections.Counter(int(self.cyclic_scc_ids[n]) for n in self.cyclic_nodes)
        with open(os.path.join(output_dir, 'cyclic_cells.tsv'), 'w', newline='') as stream:
            writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
            writer.writerow(['cell_id', 'cell_name', 'scc_id', 'scc_cells', 'was_cluster_candidate'])
            for node in sorted(self.cyclic_nodes):
                cid = int(self.cyclic_scc_ids[node])
                writer.writerow([node, _text(self.db.node_names[node]), cid, sizes[cid], int(self.candidate[node])])
        with open(os.path.join(output_dir, 'cyclic_cells_summary.json'), 'w') as stream:
            json.dump(metadata, stream, indent=2); stream.write('\n')
        return metadata

    def _resolve_timing_cell(self, cell, pin_name):
        """Map an exact timing cell, or a proven fixed-shape pin owner.

        PyPlaceDB expands fixed macros into <name>.DREAMPlace.Shape<N> and
        puts their pins on the first shape. Use the actual pin mapping instead
        of guessing Shape0 or accepting arbitrary prefix matches. No extra
        design-wide name dictionary is needed.
        """
        node = _name_lookup(self.db.node_name2id_map, cell)
        if node is not None:
            if not 0 <= int(node) < self.nphys:
                raise ValueError("Critical path cell has invalid PlaceDB node ID: %s" % cell)
            return int(node), "exact_name"
        pin = _name_lookup(getattr(self.db, "timing_pin_name2id_map",
                                   getattr(self.db, "pin_name2id_map", {})), pin_name)
        if pin is not None and 0 <= int(pin) < len(self.db.pin2node_map):
            node = int(self.db.pin2node_map[int(pin)])
            if self.nmove <= node < self.nphys:
                physical_name = _text(self.db.node_names[node])
                prefix = cell + ".DREAMPlace.Shape"
                if physical_name.startswith(prefix) and physical_name[len(prefix):].isdigit():
                    return node, "fixed_shape_pin"
        raise ValueError("Critical path cell missing from PlaceDB or not a verified fixed-shape "
                         "pin owner: %s (pin %s)" % (cell, pin_name))

    def extract_critical_cells(self, timer, output_dir):
        """Save the union of actual cells on the worst initial MAX paths.

        A bounded top-K selection is not an exhaustive critical-cell analysis.
        FF/macro endpoints are saved too, though already excluded as anchors.
        """
        count = int(getattr(self.params, "timing_cluster_critical_paths", 100))
        threshold = getattr(self.params, "timing_cluster_critical_slack_ps", None)
        if threshold is not None:
            threshold = float(threshold)
        if count < 0 or (threshold is not None and not math.isfinite(threshold)):
            raise ValueError("critical path count must be >=0 and slack threshold finite or null")
        scale = float(timer.time_unit()) * 1e12
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("Invalid OpenTimer time unit")
        # One extra path tells us whether the user-selected budget omits paths.
        paths = list(timer.report_timing_paths(count + 1, split=True)) if count else []
        if count and not paths:
            raise ValueError("Initial STA returned no MAX timing paths; check SDC/Liberty. "
                             "Use --critical-paths 0 only to explicitly disable protection.")
        for path in paths:
            if path.get("split") != "MAX" or not math.isfinite(float(path["slack"])):
                raise ValueError("Expected finite MAX/setup timing paths")
        paths.sort(key=lambda p: float(p["slack"]))
        qualifying = [p for p in paths if threshold is None or float(p["slack"]) * scale <= threshold]
        selected = qualifying[:count]
        cells = {}
        records = []
        timing_names = {}
        mapping_methods = {}
        for path_id, path in enumerate(selected):
            slack = float(path["slack"]) * scale
            points, path_cells = [], set()
            if not path["points"]:
                raise ValueError("OpenTimer returned an empty timing path")
            for point in path["points"]:
                cell = point["cell"]
                node = None
                mapping_method = None
                if cell is not None:
                    node, mapping_method = self._resolve_timing_cell(cell, point["pin"])
                    if node in timing_names and timing_names[node] != cell:
                        raise ValueError("Different timing cells map to the same PlaceDB node: %s, %s" %
                                         (timing_names[node], cell))
                    timing_names[node] = cell
                    mapping_methods[node] = mapping_method
                    path_cells.add(node)
                arrival = float(point["arrival"]) * scale
                points.append(dict(pin=point["pin"], cell=cell, cell_id=node,
                                   physical_cell_name=_text(self.db.node_names[node]) if node is not None else None,
                                   mapping_method=mapping_method,
                                   transition=point["transition"],
                                   arrival_ps=arrival if math.isfinite(arrival) else None))
            for node in path_cells:
                item = cells.setdefault(node, {"path_count": 0, "worst_path_slack_ps": slack})
                item["path_count"] += 1
                item["worst_path_slack_ps"] = min(item["worst_path_slack_ps"], slack)
            records.append(dict(path_id=path_id, split="MAX", slack_ps=slack,
                                cell_ids=sorted(path_cells), points=points))
        self.critical_nodes = set(cells)
        eligible_count = sum(bool(self.candidate[n]) for n in cells)
        metadata = dict(status="enabled" if count else "disabled", split="MAX/setup",
                        timing_model="initial STA without placement RC", slack_unit="ps",
                        selection="worst top-K paths, then optional slack <= threshold",
                        requested_paths=count, slack_threshold_ps=threshold,
                        reported_paths=len(paths), selected_paths=len(selected),
                        path_budget_limited=len(qualifying) > count,
                        critical_cells=len(cells), protected_candidate_cells=eligible_count,
                        already_anchor_cells=len(cells) - eligible_count,
                        fixed_shape_mapped_cells=sum(m == "fixed_shape_pin" for m in mapping_methods.values()),
                        files=["critical_cells.tsv", "critical_paths.jsonl"],
                        note="Only selected paths are protected; not an exhaustive list of critical cells. "
                             "Protection prevents merging, not physical movement. Clock-tree cells "
                             "are handled by the existing clock-domain anchor detection.")
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "critical_cells.tsv"), "w", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(["cell_id", "cell_name", "worst_path_slack_ps", "path_count", "was_cluster_candidate",
                             "timing_cell_name", "mapping_method"])
            for node in sorted(cells):
                writer.writerow([node, _text(self.db.node_names[node]),
                                 cells[node]["worst_path_slack_ps"], cells[node]["path_count"],
                                 int(self.candidate[node]), timing_names[node], mapping_methods[node]])
        with open(os.path.join(output_dir, "critical_paths.jsonl"), "w") as stream:
            for record in records:
                stream.write(json.dumps(record, allow_nan=False) + "\n")
        with open(os.path.join(output_dir, "critical_cells_summary.json"), "w") as stream:
            json.dump(metadata, stream, indent=2, allow_nan=False)
        self.critical_summary = metadata
        LOG.info("protected %d cells (%d combinational candidates) on %d initial MAX paths",
                 len(cells), eligible_count, len(selected))
        if metadata["path_budget_limited"]:
            LOG.warning("Critical-path budget reached; additional paths are not protected")
        return metadata

    def build_timing_edgelist(self, timer, output_dir):
        """Stream pin-level candidate edges; timer must have completed initial STA.

        Slack is the sink's worst finite MAX (setup) rise/fall slack, not an
        independently constrained edge slack. Parallel cell pairs stay separate
        by sink pin/net; a community consumer should sum their final weights.
        """
        alpha = float(getattr(self.params, "timing_edge_alpha", 4.0))
        tau = float(getattr(self.params, "timing_edge_tau_ps", 100.0))
        if not math.isfinite(alpha) or alpha < 0 or not math.isfinite(tau) or tau <= 0:
            raise ValueError("timing_edge_alpha must be finite >=0 and timing_edge_tau_ps finite >0")
        scale = float(timer.time_unit()) * 1e12
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("Invalid OpenTimer time unit")
        os.makedirs(output_dir, exist_ok=True)
        self.extract_cyclic_cells(output_dir)
        stats = collections.Counter()
        fields = ["driver_id", "driver", "driver_pin", "sink_id", "sink", "sink_pin",
                  "net_id", "net", "launch_domain_mask", "capture_domain_mask",
                  "fanout", "fanout_weight", "rise_slack_ps", "fall_slack_ps",
                  "timing_slack_ps", "slack_status", "criticality", "timing_weight", "weight"]
        path = os.path.join(output_dir, "timing_edges.csv")
        with open(path, "w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            for net, pins in enumerate(self.db.net2pin_map):
                outputs = [int(p) for p in pins if self.db.pin_direct[p] == b"OUTPUT"]
                if len(outputs) != 1 or any(self.db.pin_direct[p] not in (b"INPUT", b"OUTPUT") for p in pins):
                    stats["ambiguous_nets_skipped"] += 1
                    continue
                dp = outputs[0]
                driver = int(self.db.pin2node_map[dp])
                if not 0 <= driver < self.nphys or not self.candidate[driver]:
                    stats["anchor_driver_nets_skipped"] += 1
                    continue
                if driver in self.cyclic_nodes:
                    stats['cyclic_driver_nets_skipped'] += 1
                    continue
                if driver in self.critical_nodes:
                    stats["critical_driver_nets_skipped"] += 1
                    continue
                sinks = [int(p) for p in pins if self.db.pin_direct[p] == b"INPUT"]
                # Count ALL original sink pins, including FFs and excluded sinks.
                fanout = len(sinks)
                fw = 1.0 / math.sqrt(max(1, fanout))
                for sp in sinks:
                    sink = int(self.db.pin2node_map[sp])
                    if not 0 <= sink < self.nphys or not self.candidate[sink] or sink == driver:
                        stats["anchor_or_self_sinks_skipped"] += 1
                        continue
                    if sink in self.cyclic_nodes:
                        stats['cyclic_sink_edges_skipped'] += 1
                        continue
                    if sink in self.critical_nodes:
                        stats["critical_sink_edges_skipped"] += 1
                        continue
                    capture = int(self.domains[driver])
                    launch = int(self.launch_domains[driver])
                    if capture != int(self.domains[sink]) or launch != int(self.launch_domains[sink]):
                        stats["domain_mismatch_edges_skipped"] += 1
                        continue
                    # Unknown and multi-domain logic are retained as anchors for
                    # this edge view. Zero launch means no traced FF launch (PI cone).
                    unknown = 1 << 63
                    if not self.clocks or capture in (0, unknown) or launch & unknown or capture & (capture - 1) or launch & (launch - 1) or (launch and launch != capture):
                        stats["unknown_or_cross_domain_edges_skipped"] += 1
                        continue
                    pin_name = _text(self.db.pin_names[sp])
                    from MakeDBAdapter import timing_pin_name
                    values = [float(timer.raw_timer.report_slack(timing_pin_name(pin_name), True, tran)) * scale
                              for tran in (False, True)]
                    finite = [v for v in values if math.isfinite(v)]
                    slack = min(finite) if finite else None
                    criticality = math.exp(-max(slack, 0.0) / tau) if slack is not None else 0.0
                    tw = 1.0 + alpha * criticality ** 2
                    status = "valid" if len(finite) == 2 else "partial" if finite else "missing"
                    stats["slack_" + status] += 1
                    writer.writerow([driver, _text(self.db.node_names[driver]), _text(self.db.pin_names[dp]),
                                     sink, _text(self.db.node_names[sink]), pin_name, net, _text(self.db.net_names[net]),
                                     launch, capture, fanout, fw,
                                     *[v if math.isfinite(v) else "" for v in values],
                                     slack if slack is not None else "", status, criticality, tw, fw * tw])
                    stats["edges_written"] += 1
        metadata = dict(stats, alpha=alpha, tau_ps=tau, slack_unit="ps",
                        slack_definition="worst finite sink pin MAX rise/fall slack",
                        fanout_definition="all original INPUT pins on the net",
                        weight_formula="(1 + alpha * exp(-max(slack_ps,0)/tau_ps)^2) / sqrt(fanout)",
                        domain_policy="equal launch/capture masks; single known capture; reject known CDC/multi-domain",
                        launch_zero="no traced FF launch; PI constraints not resolved by domain parser",
                        timing_model="initial STA without placement RC", file="timing_edges.csv",
                        critical_protection=self.critical_summary,
                        cycle_protection=self.cycle_summary)
        with open(os.path.join(output_dir, "timing_edges_summary.json"), "w") as stream:
            json.dump(metadata, stream, indent=2)
        LOG.info("saved %d timing edges to %s", stats["edges_written"], path)
        return metadata

    def _boundary(self, members):
        inside = set(members)
        inputs, outputs = set(), set()
        for node in members:
            for pin in self.db.node2pin_map[node]:
                net = int(self.db.pin2net_map[pin])
                if self.db.pin_direct[pin] == b"INPUT":
                    if int(self.driver[net]) not in inside:
                        inputs.add(net)
                elif self.db.pin_direct[pin] == b"OUTPUT":
                    for other in self.db.net2pin_map[net]:
                        dst = int(self.db.pin2node_map[other])
                        if self.db.pin_direct[other] == b"INPUT" and dst not in inside:
                            outputs.add(net)
                            break
        return inputs, outputs

    def _fits(self, cid, node):
        members = self.clusters[cid]["members"] + [node]
        if len(members) > self.max_cells:
            return False
        inputs, outputs = self._boundary(members)
        return len(inputs) + len(outputs) <= self.max_boundary and \
            len(inputs) * len(outputs) <= self.max_arcs

    def cluster(self):
        begin = time.time()
        self.detect_cyclic_cells()
        order, residual = self._topological_order()
        barrier = False
        for node in order:
            if node in self.critical_nodes or node in self.cyclic_nodes:
                barrier = True
                continue
            # Restrict every cluster to one contiguous interval of a valid
            # topological order.  Together with predecessor-connected growth,
            # this is a sufficient (and cheap) convexity condition: a path
            # cannot leave an interval and later re-enter it.
            chosen = -1
            if self.clusters and not barrier:
                cid = len(self.clusters) - 1
                cluster = self.clusters[cid]
                connected = any(int(self.assignment[p]) == cid for p in self._predecessors(node))
                if connected and cluster["domain"] == int(self.domains[node]) and self._fits(cid, node):
                    chosen = cid
            if chosen < 0:
                chosen = len(self.clusters)
                self.clusters.append({"members": [], "domain": int(self.domains[node])})
            self.clusters[chosen]["members"].append(node)
            self.assignment[node] = chosen
            barrier = False

        # Kahn residuals include actual cycle members AND acyclic descendants
        # blocked by them. Exclude only SCC members; keep other residuals as
        # conservative singletons (no valid DAG growth order was established).
        for node in residual:
            if node in self.critical_nodes or node in self.cyclic_nodes:
                continue
            cid = len(self.clusters)
            self.clusters.append({"members": [node], "domain": int(self.domains[node])})
            self.assignment[node] = cid

        # Store exact boundaries and a conservative complete input-output arc set.
        for cluster in self.clusters:
            inputs, outputs = self._boundary(cluster["members"])
            cluster["inputs"] = sorted(inputs)
            cluster["outputs"] = sorted(outputs)
        LOG.info("formed %d clusters in %.2fs", len(self.clusters), time.time() - begin)
        return self.clusters

    def save(self, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        names = self.db.node_names
        net_names = self.db.net_names
        map_path = os.path.join(output_dir, "cluster_map.jsonl")
        with open(map_path, "w") as stream:
            for cid, cluster in enumerate(self.clusters):
                record = {
                    "cluster_id": cid,
                    "name": "TC_%06d" % cid,
                    "clock_domain_mask": cluster["domain"],
                    "members": [_text(names[n]) for n in cluster["members"]],
                    "input_nets": [_text(net_names[n]) for n in cluster["inputs"]],
                    "output_nets": [_text(net_names[n]) for n in cluster["outputs"]],
                }
                stream.write(json.dumps(record, sort_keys=True) + "\n")

        with open(os.path.join(output_dir, "clusters.csv"), "w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["cluster_id", "cells", "input_pins", "output_pins", "timing_arcs", "clock_domain_mask"])
            for cid, c in enumerate(self.clusters):
                writer.writerow([cid, len(c["members"]), len(c["inputs"]), len(c["outputs"]),
                                 len(c["inputs"]) * len(c["outputs"]), c["domain"]])

        with open(os.path.join(output_dir, "boundary_pins.csv"), "w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["cluster_id", "direction", "abstract_pin", "original_net"])
            for cid, c in enumerate(self.clusters):
                for i, net in enumerate(c["inputs"]):
                    writer.writerow([cid, "INPUT", "I%d" % i, _text(net_names[net])])
                for i, net in enumerate(c["outputs"]):
                    writer.writerow([cid, "OUTPUT", "O%d" % i, _text(net_names[net])])

        dot_limit = int(getattr(self.params, "timing_cluster_dot_limit", 500))
        with open(os.path.join(output_dir, "cluster_graph.dot"), "w") as stream:
            stream.write("digraph timing_clusters {\n")
            shown = min(dot_limit, len(self.clusters))
            for cid in range(shown):
                c = self.clusters[cid]
                stream.write('  c%d [label="C%d\\n%d cells\\n%d in / %d out"];\n' %
                             (cid, cid, len(c["members"]), len(c["inputs"]), len(c["outputs"])))
            edges = set()
            for cid in range(shown):
                for net in self.clusters[cid]["outputs"]:
                    for pin in self.db.net2pin_map[net]:
                        node = int(self.db.pin2node_map[pin])
                        if node < self.nphys:
                            dst = int(self.assignment[node])
                            if 0 <= dst < shown and dst != cid:
                                edges.add((cid, dst))
            for src, dst in sorted(edges):
                stream.write("  c%d -> c%d;\n" % (src, dst))
            stream.write("}\n")

        sizes = np.asarray([len(c["members"]) for c in self.clusters], dtype=np.int64)
        summary = {
            "schema_version": 1,
            "mode": "timing_graph_abstraction",
            "physical_nodes": self.nphys,
            "eligible_combinational_cells": int(self.candidate.sum()) - sum(
                bool(self.candidate[n]) for n in self.critical_nodes | self.cyclic_nodes),
            "critical_protection": self.critical_summary,
            "cycle_protection": self.cycle_summary,
            "sequential_anchors": len(self.sequential_nodes),
            "macro_anchors": len(self.macro_nodes),
            "clock_tree_anchors": len(self.clock_cells),
            "cluster_count": len(self.clusters),
            "mean_cells_per_cluster": float(sizes.mean()) if len(sizes) else 0.0,
            "max_cells_per_cluster": int(sizes.max()) if len(sizes) else 0,
            "constraints": {
                "max_cells": self.max_cells,
                "max_boundary_pins": self.max_boundary,
                "max_timing_arcs": self.max_arcs,
                "sequential_and_macro_boundaries_preserved": True,
                "clock_domain_masks_preserved": True,
                "connected_growth": True,
                "topologically_convex": True,
                "cyclic_cells_excluded": True,
                "noncyclic_topological_residuals_are_singletons": True,
            },
            "files": ["cluster_map.jsonl", "clusters.csv", "boundary_pins.csv", "cluster_graph.dot"],
            "note": "This output is an abstraction contract. It does not yet replace OpenTimer's netlist/Liberty database.",
        }
        with open(os.path.join(output_dir, "summary.json"), "w") as stream:
            json.dump(summary, stream, indent=2, sort_keys=True)
            stream.write("\n")
        return summary


def run(params, placedb, output_dir=None, config_dir=None, timer=None, edges_only=False):
    config_dir = config_dir or os.getcwd()
    output_dir = output_dir or getattr(params, "timing_cluster_output_dir", "")
    if not output_dir:
        output_dir = os.path.join(getattr(params, "result_dir", "results"),
                                  params.design_name(), "timing_clusters")
    output_dir = _path(output_dir, config_dir)
    engine = ClusterEngine(params, placedb)
    engine.prepare(config_dir)
    # Save structural protection before STA; repeated edge/cluster calls reuse
    # the cached SCC result while leaving the original graph intact.
    engine.extract_cyclic_cells(output_dir)
    if timer is None:
        try:
            import Timer
        except ImportError:
            from dreamplace import Timer
        timer = Timer.Timer()
        timer(params, placedb)
    # Flush pending STA tasks even when a caller supplied an existing timer.
    timer.update_timing()
    engine.extract_critical_cells(timer, output_dir)
    edge_summary = engine.build_timing_edgelist(timer, output_dir)
    if edges_only:
        return edge_summary
    engine.cluster()
    summary = engine.save(output_dir)
    LOG.info("timing clustering results saved in %s", output_dir)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Standalone DREAMPlace timing-aware clustering")
    parser.add_argument("config", help="DREAMPlace JSON configuration")
    parser.add_argument("--output", help="result directory")
    parser.add_argument("--max-cells", type=int)
    parser.add_argument("--max-boundary-pins", type=int)
    parser.add_argument("--max-timing-arcs", type=int)
    parser.add_argument("--base-dir", help="base directory for paths inside JSON (default: current directory)")
    parser.add_argument("--edges-only", action="store_true", help="save timing edge table without greedy clustering")
    parser.add_argument("--timing-tau-ps", type=float, default=None)
    parser.add_argument("--timing-alpha", type=float, default=None)
    parser.add_argument("--critical-paths", type=int, default=None,
                        help="protect cells on worst K initial setup paths (default 100; 0 disables)")
    parser.add_argument("--critical-slack-ps", type=float, default=None,
                        help="only protect paths with slack <= this ps value within the path budget")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(levelname)-7s] %(message)s")

    # Installed trees contain generated configure.py.  For a source-tree call,
    # also try the sibling installation directory used by this repository.
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.dirname(here), os.path.join(os.path.dirname(os.path.dirname(here)), "bin")):
        if candidate not in sys.path:
            sys.path.append(candidate)
    try:
        import Params
        import PlaceDB
    except ImportError:
        from dreamplace import Params, PlaceDB

    config = os.path.abspath(args.config)
    config_dir = os.path.abspath(args.base_dir or os.getcwd())
    params = Params.Params()
    params.load(config)
    params.gpu = 0
    if args.critical_paths is not None:
        params.timing_cluster_critical_paths = args.critical_paths
    if args.critical_slack_ps is not None:
        params.timing_cluster_critical_slack_ps = args.critical_slack_ps
    if args.timing_tau_ps is not None:
        params.timing_edge_tau_ps = args.timing_tau_ps
    if args.timing_alpha is not None:
        params.timing_edge_alpha = args.timing_alpha
    if args.max_cells is not None:
        params.timing_cluster_max_cells = args.max_cells
    if args.max_boundary_pins is not None:
        params.timing_cluster_max_boundary_pins = args.max_boundary_pins
    if args.max_timing_arcs is not None:
        params.timing_cluster_max_timing_arcs = args.max_timing_arcs
    old_cwd = os.getcwd()
    try:
        os.chdir(config_dir)
        placedb = PlaceDB.PlaceDB()
        placedb(params)
        summary = run(params, placedb, args.output, config_dir, edges_only=args.edges_only)
    finally:
        os.chdir(old_cwd)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
