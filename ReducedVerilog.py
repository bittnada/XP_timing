#!/usr/bin/env python3
"""Replace characterized clusters with their timing-only Liberty instances.

Consumes ReducedLiberty.py's cluster_mapping.jsonl, not a bare membership file:
the exact boundary pin numbering must agree with the characterized Liberty.
This is an STA netlist, not a functionally equivalent synthesis/simulation model.
"""
import argparse
import collections
import json
from pathlib import Path
import re

from ReducedLiberty import IDENT, ModelError, parse_statement, verilog_statements


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(IDENT, value):
        raise ModelError('Unsupported Verilog identifier: %r' % value)
    return value + (' ' if value.startswith('\\') else '')


def declaration(statement):
    match = re.fullmatch(r'(input|output|wire)\s+(?:\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*)?(.*)', statement, re.S)
    if not match:
        return None
    kind, left, right, rest = match.groups()
    names = [n.strip() for n in rest.split(',')]
    for name in names:
        identifier(name)
        if not name.startswith('\\') and '[' in name:
            raise ModelError('Bit-select declarations are unsupported: ' + statement)
        if left is not None and name.startswith('\\'):
            raise ModelError('Packed escaped bus declarations require explicit support')
    return kind, (int(left), int(right)) if left is not None else None, names


def port_nets(decl):
    _, bus, names = decl
    if bus is None:
        return names
    return [name + '[%d]' % i for name in names for i in range(min(bus), max(bus) + 1)]


def load_mapping(path):
    clusters, membership, ids, masters = {}, {}, set(), set()
    with open(path) as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                cid, master = item['cluster_id'], item['liberty_cell']
                if type(cid) is not int or cid < 0 or cid in clusters:
                    raise ModelError('Invalid/duplicate cluster ID')
                identifier(master)
                if master in masters:
                    raise ModelError('Duplicate abstract Liberty cell ' + master)
                masters.add(master)
                pins, nets = {}, set()
                for side in ('inputs', 'outputs'):
                    if not item[side]:
                        raise ModelError('Cluster requires boundary inputs and outputs')
                    for pin in item[side]:
                        name, net = pin['pin'], pin['net']
                        identifier(name); identifier(net)
                        if name in pins or net in nets:
                            raise ModelError('Duplicate boundary pin/net in cluster %d' % cid)
                        pins[name] = net
                        nets.add(net)
                if not item['members']:
                    raise ModelError('Empty cluster')
                for member in item['members']:
                    name, node = member['cell_name'], member['cell_id']
                    identifier(name)
                    if type(node) is not int or node < 0 or node in ids or name in membership:
                        raise ModelError('Duplicate/invalid member ID or name: ' + name)
                    ids.add(node)
                    membership[name] = cid
                clusters[cid] = dict(master=master, pins=pins, nets=nets, source=item)
            except (KeyError, TypeError, ValueError) as exc:
                raise ModelError('Invalid cluster mapping line %d: %s' % (line_number, exc)) from exc
    if not clusters:
        raise ModelError('No characterized clusters in mapping')
    return clusters, membership


def check_cluster_cycles(clusters):
    drivers, adjacency = {}, collections.defaultdict(set)
    for cid, cluster in clusters.items():
        for pin in cluster['source']['outputs']:
            if pin['net'] in drivers:
                raise ModelError('Multiple cluster drivers on net ' + pin['net'])
            drivers[pin['net']] = cid
    degree = {cid: 0 for cid in clusters}
    for cid, cluster in clusters.items():
        for pin in cluster['source']['inputs']:
            src = drivers.get(pin['net'])
            if src is not None and cid not in adjacency[src]:
                adjacency[src].add(cid)
                degree[cid] += 1
    ready = [cid for cid, count in degree.items() if count == 0]
    visited = 0
    while ready:
        cid = ready.pop(); visited += 1
        for sink in adjacency[cid]:
            degree[sink] -= 1
            if not degree[sink]:
                ready.append(sink)
    if visited != len(clusters):
        raise ModelError('Cluster contraction creates a directed cycle; revise the partition')


def reduce_verilog(original, mapping, output, check_manifest=True):
    original, mapping, output = map(lambda p: Path(p).resolve(), (original, mapping, output))
    summary_path = output.with_suffix('.summary.json')
    instances_path = output.with_suffix('.instances.jsonl')
    targets = (output, summary_path, instances_path)
    if len(set(targets)) != 3 or any(path.exists() for path in targets):
        raise ModelError('Reduced Verilog output/sidecar already exists; choose a new path')
    manifest = mapping.parent / 'manifest.json'
    if check_manifest and manifest.exists():
        status = json.loads(manifest.read_text()).get('status')
        if status != 'complete':
            raise ModelError('Refusing incomplete Liberty generation: manifest status %r' % status)
    clusters, membership = load_mapping(mapping)
    check_cluster_cycles(clusters)
    expected_masters = {member['cell_name']: member['original_master']
                        for cluster in clusters.values() for member in cluster['source']['members']
                        if 'original_master' in member}
    abstract_masters = {c['master'] for c in clusters.values()}
    seen, reserved, declared_ports, touched, shared, unmapped = set(), set(), set(), {}, set(), {}
    seen_boundary = {cid: set() for cid in clusters}
    removed, total, module_count = 0, 0, 0
    retained_masters = collections.Counter()
    module_header, formal_ports, declared_port_names = None, set(), set()
    # Pass 1: identifiers, selected-cell nets, and the original interface. No
    # complete instance objects or original source text are retained in memory.
    for statement in verilog_statements(original):
        if re.match(r'module\s', statement):
            module_count += 1
            match = re.fullmatch(r'module\s+(' + IDENT + r')\s*(?:\((.*)\))?', statement, re.S)
            if not match or module_count != 1:
                raise ModelError('Requires exactly one flat, non-ANSI Verilog module')
            module_header = statement
            ports = [p.strip() for p in (match[2] or '').split(',') if p.strip()]
            for name in ports:
                identifier(name)
            if len(set(ports)) != len(ports):
                raise ModelError('Duplicate module port')
            formal_ports = set(ports)
            continue
        decl = declaration(statement)
        if decl:
            if module_header is None:
                raise ModelError('Missing module declaration')
            kind, _, names = decl
            reserved.update(n.lstrip('\\') for n in names)
            if kind in ('input', 'output'):
                declared_ports.update(port_nets(decl))
                for name in names:
                    if name in declared_port_names:
                        raise ModelError('Duplicate port declaration: ' + name)
                    declared_port_names.add(name)
                    if name not in formal_ports:
                        raise ModelError('Declared port missing from module header: ' + name)
            continue
        if module_header is None:
            raise ModelError('Missing module declaration')
        master, name, pins = parse_statement(statement)
        canonical = name[1:] if name.startswith('\\') else name
        if canonical in seen:
            raise ModelError('Duplicate original instance: ' + name)
        if master in abstract_masters:
            raise ModelError('Abstract Liberty cell name collides with an original master: ' + master)
        seen.add(canonical); reserved.add(canonical); total += 1
        if name not in membership:
            retained_masters[master] += 1
            continue
        cid = membership[name]
        if name in expected_masters and master != expected_masters[name]:
            raise ModelError('Mapped member master changed: %s was %s, now %s' %
                             (name, expected_masters[name], master))
        removed += 1
        for net in pins.values():
            if net in touched and touched[net] != cid:
                shared.add(net)
            else:
                touched[net] = cid
            if net in clusters[cid]['nets']:
                seen_boundary[cid].add(net)
            else:
                unmapped.setdefault(net, cid)
    if module_count != 1:
        raise ModelError('Missing module declaration')
    if formal_ports != declared_port_names:
        raise ModelError('Module header ports lack input/output declarations: %s' % sorted(formal_ports - declared_port_names))
    missing = [name for name in membership if (name[1:] if name.startswith('\\') else name) not in seen]
    if missing or removed != len(membership):
        raise ModelError('Mapped members not found exactly in original netlist: %s' % missing[:10])
    for cid in clusters:
        if seen_boundary[cid] != clusters[cid]['nets']:
            raise ModelError('Mapping boundary is not connected to members of cluster %d' % cid)
    external = shared | (declared_ports & touched.keys())
    # Pass 2: find selected nets also attached to any retained instance, even
    # if that instance occurs earlier in the file than the selected cells.
    for statement in verilog_statements(original):
        if re.match(r'module\s', statement) or declaration(statement):
            continue
        _, name, pins = parse_statement(statement)
        if name not in membership:
            external.update(net for net in pins.values() if net in touched)
    omitted = external & unmapped.keys()
    if omitted:
        net = min(omitted)
        raise ModelError('Mapping omits external net %s from cluster %d' % (net, unmapped[net]))
    for cid, cluster in clusters.items():
        extra = cluster['nets'] - external
        if extra:
            raise ModelError('Mapping exposes non-boundary nets in cluster %d: %s' % (cid, sorted(extra)[:5]))
        name = '__tc_cluster_%d' % cid
        suffix = 0
        while name in reserved:
            suffix += 1
            name = '__tc_cluster_%d_%d' % (cid, suffix)
        reserved.add(name)
        cluster['instance'] = name
    internal_nets = touched.keys() - external
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = dict(status='in_progress', original=str(original), mapping=str(mapping),
                   output=str(output), original_instances=total, removed_instances=removed,
                   cluster_instances=len(clusters), retained_instances=total - removed,
                   reduced_instances=total - removed + len(clusters),
                   retained_master_counts=dict(sorted(retained_masters.items())),
                   internal_nets=len(internal_nets), removed_scalar_wire_declarations=0,
                   instance_mapping=str(instances_path), timing_only=True,
                   limitations=['No SDC or SPEF remapping', 'Unused packed bus declarations retained',
                                'Cycle check covers direct cluster-to-cluster edges only'])
    try:
        # Pass 3: preserve retained instances verbatim (apart from comments) and
        # remove only scalar wires proved internal to a replaced cluster.
        with open(output, 'x') as stream:
            stream.write('// Timing-only cluster abstraction; not a functional netlist.\n')
            for statement in verilog_statements(original):
                if re.match(r'module\s', statement):
                    stream.write(statement + ';\n')
                    continue
                decl = declaration(statement)
                if decl:
                    kind, bus, names = decl
                    if kind == 'wire' and bus is None:
                        kept = [name for name in names if name not in internal_nets]
                        summary['removed_scalar_wire_declarations'] += len(names) - len(kept)
                        if kept:
                            stream.write('wire ' + ', '.join(identifier(n) for n in kept) + ';\n')
                    else:
                        stream.write(statement + ';\n')
                    continue
                _, name, _ = parse_statement(statement)
                if name not in membership:
                    stream.write(statement + ';\n')
            for cid, cluster in sorted(clusters.items()):
                connections = ', '.join('.%s(%s)' % (identifier(pin), identifier(net))
                                        for pin, net in cluster['pins'].items())
                stream.write('%s %s (%s);\n' % (identifier(cluster['master']), cluster['instance'], connections))
            stream.write('endmodule\n')
        with open(instances_path, 'x') as stream:
            for cid, cluster in sorted(clusters.items()):
                stream.write(json.dumps(dict(cluster_id=cid, liberty_cell=cluster['master'],
                                             instance=cluster['instance'], connections=cluster['pins'])) + '\n')
        summary['status'] = 'complete'
    except Exception as exc:
        summary['status'], summary['error'] = 'failed_partial_do_not_use', str(exc)
        raise
    finally:
        with open(summary_path, 'x') as stream:
            json.dump(summary, stream, indent=2)
            stream.write('\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verilog', required=True, help='original flat structural Verilog')
    parser.add_argument('--mapping', required=True, help='verified cluster_mapping.jsonl from ReducedLiberty.py')
    parser.add_argument('--output', required=True, help='new reduced .v path; also creates .summary.json and .instances.jsonl')
    args = parser.parse_args()
    try:
        report = reduce_verilog(args.verilog, args.mapping, args.output)
    except (ModelError, OSError, ValueError) as exc:
        parser.exit(1, 'ERROR: %s\n' % exc)
    print('Reduced %d instances to %d; saved %s' %
          (report['original_instances'], report['reduced_instances'], report['output']))


if __name__ == '__main__':
    main()
