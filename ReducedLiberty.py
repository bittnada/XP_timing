#!/usr/bin/env python3
"""Characterize combinational clusters with the local OpenTimer shell.

Inputs: numeric cell_id/cluster_id table, cell_id/cell_name table, flat gate
Verilog, original NLDM Liberty files or directories. Outputs are timing-only abstract
cells, not synthesizable functional replacements. All internal wires are ideal.
"""
import argparse
import collections
from contextlib import ExitStack
import csv
import json
import math
import mmap
import os
from pathlib import Path
import re
import subprocess
import tempfile


class ModelError(ValueError):
    pass


class AbstractionError(ModelError):
    """A cluster cannot be represented/verified by this abstraction model."""


def table_rows(path, columns):
    """Accept headered CSV/TSV or headerless whitespace-separated tables."""
    with open(path, encoding="utf-8-sig", newline="") as stream:
        first = True
        indexes = None
        for line in stream:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            delimiter = "\t" if "\t" in line else "," if "," in line else None
            row = next(csv.reader([line], delimiter=delimiter)) if delimiter else line.split()
            row = [v.strip() for v in row]
            if first:
                first = False
                if set(columns).issubset(row):
                    indexes = [row.index(c) for c in columns]
                    continue
                indexes = list(range(len(columns)))
            if len(row) <= max(indexes):
                raise ModelError("Malformed table row in %s: %s" % (path, line.strip()))
            yield tuple(row[i] for i in indexes)


def read_membership(path, names_path, selected=None):
    assignment = {}
    for node, cluster in table_rows(path, ("cell_id", "cluster_id")):
        node, cluster = int(node), int(cluster)
        if node < 0 or cluster < 0:
            raise ModelError("Cell and cluster IDs must be nonnegative")
        if selected is not None and cluster not in selected:
            continue
        if node in assignment and assignment[node] != cluster:
            raise ModelError("Cell %d belongs to multiple clusters" % node)
        assignment[node] = cluster
    names = {}
    for node, name in table_rows(names_path, ("cell_id", "cell_name")):
        node = int(node)
        if node not in assignment:
            continue
        if node in names and names[node] != name:
            raise ModelError("Conflicting names for cell %d" % node)
        names[node] = name
    missing = assignment.keys() - names.keys()
    if missing:
        raise ModelError("Missing cell names for IDs: %s" % sorted(missing)[:10])
    if not assignment:
        raise ModelError("No selected cluster members")
    if len(set(names.values())) != len(names):
        raise ModelError("Multiple IDs refer to the same cell name")
    clusters = collections.defaultdict(list)
    for node in sorted(assignment):
        clusters[assignment[node]].append((node, names[node]))
    if selected is not None and selected - clusters.keys():
        raise ModelError("Requested cluster IDs are absent: %s" % sorted(selected - clusters.keys()))
    return dict(clusters)


# A small structural Liberty parser. Keep original raw groups for OpenTimer,
# but inspect directions/arcs to avoid accepting unsupported abstractions.
TOKEN = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[{}():;,]|[^\s{}():;,]+')


def unquote(value):
    return value[1:-1] if value.startswith('"') and value.endswith('"') else value


class Group:
    def __init__(self, kind, args, attrs, calls, children, raw):
        self.kind, self.args, self.attrs = kind, args, attrs
        self.calls, self.children, self.raw = calls, children, raw


def parse_liberty(path):
    return parse_liberty_text(Path(path).read_text(), str(path))


def parse_liberty_text(text, source='<memory>'):
    path = source
    text = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S)
    text = re.sub(r'\\\s*\n', '', text)
    tokens = list(TOKEN.finditer(text))
    pos = 0

    def value():
        return tokens[pos].group() if pos < len(tokens) else None

    def group():
        nonlocal pos
        start = tokens[pos].start()
        kind = value(); pos += 1
        if value() != '(':
            raise ModelError("Expected Liberty group in %s" % path)
        pos += 1
        args = []
        while value() != ')':
            if value() is None:
                raise ModelError("Truncated Liberty group")
            if value() != ',':
                args.append(unquote(value()))
            pos += 1
        pos += 1
        if value() != '{':
            raise ModelError("Expected Liberty group body")
        pos += 1
        attrs, calls, children = {}, {}, []
        while value() != '}':
            if value() is None:
                raise ModelError("Truncated Liberty body")
            key = value()
            if pos + 1 >= len(tokens):
                raise ModelError("Truncated Liberty attribute")
            if tokens[pos + 1].group() == ':':
                pos += 2
                parts = []
                while value() != ';':
                    if value() is None:
                        raise ModelError("Truncated Liberty attribute")
                    parts.append(unquote(value())); pos += 1
                attrs[key] = ' '.join(parts); pos += 1
            elif tokens[pos + 1].group() == '(':
                look = pos + 2
                while look < len(tokens) and tokens[look].group() != ')':
                    look += 1
                if look + 1 >= len(tokens):
                    raise ModelError("Truncated Liberty call")
                if tokens[look + 1].group() == '{':
                    children.append(group())
                else:
                    calls[key] = [unquote(t.group()) for t in tokens[pos + 2:look] if t.group() != ',']
                    pos = look + 1
                    if value() != ';':
                        raise ModelError("Unsupported Liberty call")
                    pos += 1
            else:
                raise ModelError("Unsupported Liberty syntax near %s" % key)
        end = tokens[pos].end(); pos += 1
        return Group(kind, args, attrs, calls, children, text[start:end])

    lib = group()
    if lib.kind != 'library' or pos != len(tokens):
        raise ModelError("Expected one Liberty library per input file")
    return lib


# Scan structural tokens without building a token list for a multi-GB library.
# Quoted strings/comments are consumed whole so braces inside them are harmless.
LIB_STRUCTURE = re.compile(rb'/\*.*?\*/|//[^\n]*|"[^"\\]*(?:\\.[^"\\]*)*"|[{};]', re.S)
CELL_DECL = re.compile(rb'cell\s*\(\s*("(?:\\.|[^"\\])*"|[^\s)]+)\s*\)\s*$', re.S)


def index_liberty(path):
    """Return the small library header and byte ranges of cells, not cell bodies."""
    cells, headers = {}, []
    depth, item_start, cell_start, cell_name = 0, 0, None, None
    opened = closed = False
    with open(path, 'rb') as stream:
        if not os.fstat(stream.fileno()).st_size:
            raise ModelError('Empty Liberty: %s' % path)
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            for token in LIB_STRUCTURE.finditer(data):
                value = token.group()
                if value.startswith((b'/*', b'//')) or value.startswith(b'"'):
                    continue
                if value == b'{':
                    if depth == 0:
                        if opened:
                            raise ModelError('Expected one library per file: %s' % path)
                        opened = True
                        headers.append(data[:token.end()])
                        item_start = token.end()
                    elif depth == 1:
                        declaration = re.sub(rb'/\*.*?\*/|//[^\n]*', b'', data[item_start:token.start()], flags=re.S).strip()
                        match = CELL_DECL.fullmatch(declaration)
                        if match:
                            cell_name = unquote(match[1].decode())
                            if cell_name in cells:
                                raise ModelError('Duplicate cell %s inside %s' % (cell_name, path))
                            cell_start = item_start
                    depth += 1
                elif value == b'}':
                    depth -= 1
                    if depth < 0:
                        raise ModelError('Unbalanced Liberty braces: %s' % path)
                    if depth == 1:
                        if cell_start is not None:
                            cells[cell_name] = (cell_start, token.end())
                            cell_start, cell_name = None, None
                        else:
                            headers.append(data[item_start:token.end()])
                        item_start = token.end()
                    elif depth == 0:
                        headers.append(data[item_start:token.end()])
                        item_start = token.end()
                        closed = True
                elif depth == 1:
                    headers.append(data[item_start:token.end()])
                    item_start = token.end()
            if depth or not closed:
                raise ModelError('Unbalanced/truncated Liberty: %s' % path)
            tail = re.sub(rb'/\*.*?\*/|//[^\n]*', b'', data[item_start:], flags=re.S).strip()
            if tail:
                raise ModelError('Trailing Liberty content: %s' % path)
    return dict(path=str(path), header=b'\n'.join(headers).decode(), cells=cells)


def discover_libraries(inputs):
    if isinstance(inputs, (str, Path)):
        inputs = [inputs]
    found = set()
    for value in inputs or []:
        path = Path(value).expanduser().resolve()
        if path.is_dir():
            found.update(p.resolve() for p in path.rglob('*') if p.is_file() and p.suffix.lower() == '.lib')
        elif path.is_file():
            found.add(path)
        else:
            raise ModelError('Library input does not exist: %s' % path)
    if not found:
        raise ModelError('No .lib files found in library inputs')
    return sorted(found)


def parse_overrides(items):
    result = {}
    for item in items or []:
        name, separator, filename = item.partition('=')
        if not separator or not name or not filename:
            raise ModelError('Cell override must be CELL=/path/to/source.lib')
        path = str(Path(filename).expanduser().resolve())
        if name in result and result[name] != path:
            raise ModelError('Conflicting overrides for cell %s' % name)
        result[name] = path
    return result


def load_library_set(paths, needed, internal, overrides, cache):
    """Extract only needed masters; boundary-only models do not define the corner."""
    sources = collections.defaultdict(list)
    for path in paths:
        key = str(path)
        if key not in cache:
            print('Indexing Liberty: %s' % path, flush=True)
            cache[key] = index_liberty(path)
        for name in cache[key]['cells']:
            sources[name].append(key)
    for name, path in overrides.items():
        if path not in sources.get(name, []):
            raise ModelError('Override for %s does not identify a source defining that cell: %s' % (name, path))
    chosen = {}
    for name in sorted(needed):
        candidates = sources.get(name, [])
        if not candidates:
            raise ModelError('Missing library master: %s' % name)
        if len(candidates) > 1 and name not in overrides:
            raise ModelError('Duplicate required cell %s; select with --cell-override CELL=PATH:\n%s' %
                             (name, '\n'.join(candidates)))
        chosen[name] = overrides.get(name, candidates[0])
    active = sorted({chosen[name] for name in internal})
    headers = {path: parse_liberty_text(cache[path]['header'], path) for path in active}
    base = headers[active[0]]
    # A cluster has a single slew definition and corner. Do not silently mix PVT,
    # units or threshold conventions; explicit conversion/multi-voltage support
    # is outside this extractor's scope. External retained blocks may differ.
    checked_attrs = ('nom_process', 'nom_voltage', 'nom_temperature', 'voltage_unit',
                     'input_threshold_pct_rise', 'input_threshold_pct_fall',
                     'output_threshold_pct_rise', 'output_threshold_pct_fall',
                     'slew_lower_threshold_pct_rise', 'slew_lower_threshold_pct_fall',
                     'slew_upper_threshold_pct_rise', 'slew_upper_threshold_pct_fall',
                     'slew_derate_from_library')
    def normalized(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    groups = [g for g in base.children if g.kind == 'operating_conditions']
    template_names = {}
    for source_id, (path, header) in enumerate(headers.items()):
        if units(header) != units(base):
            raise ModelError('Incompatible time/capacitance units: %s and %s' % (active[0], path))
        for attr in checked_attrs:
            if normalized(header.attrs.get(attr)) != normalized(base.attrs.get(attr)):
                raise ModelError('Incompatible %s in library set: %s and %s' % (attr, active[0], path))
        template_names[path] = {}
        for group in header.children:
            if group.kind != 'lu_table_template':
                continue
            original = group.args[0]
            if original in template_names[path]:
                raise ModelError('Duplicate NLDM template %s inside %s' % (original, path))
            renamed = 'TC_src%d_%s' % (source_id, original)
            group.args = [renamed]
            group.raw = serialize_group(group)
            template_names[path][original] = group
            groups.append(group)
    cells = []
    for name, path in chosen.items():
        start, end = cache[path]['cells'][name]
        with open(path, 'rb') as stream:
            stream.seek(start)
            raw = stream.read(end - start).decode()
        cell = parse_liberty_text('library(extract) {\n' + raw + '\n}', path).children[0]
        if name in internal:
            # Materialize source defaults before merging. If a default is absent
            # in one source, another source's default must not leak into its pins.
            defaults = headers[path].attrs
            for pin in cell.children:
                direction = pin.attrs.get('direction')
                if pin.kind != 'pin' or direction not in ('input', 'output', 'inout'):
                    continue
                attributes = {'capacitance': 'default_%s_pin_cap' % direction}
                if direction == 'input':
                    attributes['fanout_load'] = 'default_fanout_load'
                elif direction == 'output':
                    attributes.update(max_fanout='default_max_fanout', max_transition='default_max_transition')
                for attribute, default_key in attributes.items():
                    if attribute not in pin.attrs and default_key in defaults:
                        default = defaults[default_key]
                        pin.attrs[attribute] = default
                        pin.raw = pin.raw.replace('{', '{ %s : %s;' % (attribute, default), 1)
            if 'cell_leakage_power' not in cell.attrs and 'default_cell_leakage_power' in defaults:
                cell.attrs['cell_leakage_power'] = defaults['default_cell_leakage_power']
            nldm_cell(cell, template_names[path])
        cells.append(cell)
    # A timing-only header must not retain references to omitted wire-load,
    # waveform, power or voltage groups from the first source.
    header_keys = set(checked_attrs) | {'delay_model', 'time_unit', 'current_unit',
                                      'pulling_resistance_unit', 'leakage_power_unit',
                                      'default_operating_conditions'}
    merged_attrs = {key: value for key, value in base.attrs.items() if key in header_keys}
    merged = Group('library', ['source'], merged_attrs,
                   {'capacitive_load_unit': base.calls['capacitive_load_unit']}, groups + cells, '')
    metadata = dict(files=[str(p) for p in paths], active_internal_files=active,
                    cell_sources=chosen,
                    extraction='NLDM_only_with_source_namespaced_templates',
                    internal_source_conditions={path: {key: value for key, value in header.attrs.items()
                                                       if key in checked_attrs or key == 'default_operating_conditions'}
                                                for path, header in headers.items()},
                    unused_duplicate_cells={n: p for n, p in sources.items() if len(p) > 1 and n not in needed})
    return merged, metadata


def nldm_cell(cell, template_names):
    """Make a timing-only NLDM source view, preserving arc conditions for checks.

    CCS current/waveform and power groups are deliberately not interpreted.
    Retain sequential/bus markers so build_cluster still rejects those cells.
    """
    cell.children = [g for g in cell.children if g.kind in
                     ('pin', 'bus', 'bundle', 'ff', 'latch', 'ff_bank', 'latch_bank', 'statetable')]
    table_kinds = {'cell_rise', 'cell_fall', 'rise_transition', 'fall_transition',
                   'rise_constraint', 'fall_constraint'}
    for pin in cell.children:
        if pin.kind != 'pin':
            continue
        pin.children = [g for g in pin.children if g.kind == 'timing']
        for key in ('driver_waveform_rise', 'driver_waveform_fall', 'input_voltage',
                    'output_voltage', 'related_ground_pin', 'related_power_pin', 'power_down_function'):
            pin.attrs.pop(key, None)
        for arc in pin.children:
            arc.children = [g for g in arc.children if g.kind in table_kinds]
            for table in arc.children:
                name = table.args[0] if table.args else None
                if name in template_names:
                    template = template_names[name]
                    table.args = list(template.args)
                    # This OpenTimer version does not inherit template indices.
                    # Make them explicit and place axes BEFORE values, as its
                    # parser allocates the LUT when it encounters values().
                    axes = {key: table.calls.get(key, template.calls.get(key))
                            for key in ('index_1', 'index_2')}
                    table.calls = {**{key: val for key, val in axes.items() if val is not None},
                                   **{key: val for key, val in table.calls.items() if key not in axes}}
                elif name != 'scalar':
                    raise ModelError('Missing NLDM template %s in cell %s' % (name, cell.args[0]))
                # Reject bad dimensions before OpenTimer's unchecked table
                # assignment could overrun its allocation or fabricate results.
                sizes = []
                for axis in ('index_1', 'index_2'):
                    points = [float(v) for row in table.calls.get(axis, []) for v in row.split(',')]
                    if any(not math.isfinite(v) for v in points) or any(a >= b for a, b in zip(points, points[1:])):
                        raise ModelError('Invalid NLDM %s in cell %s' % (axis, cell.args[0]))
                    sizes.append(len(points) or 1)
                values = [float(v) for row in table.calls.get('values', []) for v in row.split(',')]
                if len(values) != sizes[0] * sizes[1] or any(not math.isfinite(v) for v in values):
                    raise ModelError('Invalid NLDM table dimensions/values in cell %s' % cell.args[0])
                table.raw = serialize_group(table)
            arc.raw = serialize_group(arc)
        pin.raw = serialize_group(pin)
    cell.raw = serialize_group(cell)


def serialize_group(group):
    """Serialize structural metadata while preserving parsed child group bodies."""
    lines = ['%s (%s) {' % (group.kind, ', '.join('"%s"' % a for a in group.args))]
    lines.extend('%s : "%s";' % item for item in group.attrs.items())
    lines.extend('%s (%s);' % (k, ', '.join('"%s"' % a for a in args)) for k, args in group.calls.items())
    lines.extend(g.raw for g in group.children)
    return '\n'.join(lines + ['}'])


def liberty_header(lib, name, template=False):
    result = ['library ("%s") {' % name]
    for key, val in lib.attrs.items():
        # Preserve the original units, thresholds and nominal corner metadata.
        result.append('  %s : "%s";' % (key, val))
    for key, args in lib.calls.items():
        result.append('  %s (%s);' % (key, ', '.join('"%s"' % a for a in args)))
    result.extend(g.raw for g in lib.children if g.kind != 'cell')
    if template:
        result.append('lu_table_template (TC_slew_load) {\n'
                      ' variable_1 : input_net_transition;\n'
                      ' variable_2 : total_output_net_capacitance;\n'
                      '}')
    return '\n'.join(result) + '\n'


def units(lib):
    time = re.fullmatch(r'([\d.eE+-]+)\s*(s|ms|us|ns|ps|fs)', lib.attrs.get('time_unit', ''))
    cap = lib.calls.get('capacitive_load_unit', [])
    if not time or len(cap) != 2 or cap[1].lower() not in ('f', 'uf', 'nf', 'pf', 'ff'):
        raise ModelError("Explicit supported time/capacitance units are required")
    ts = float(time[1]) * {'s': 1e12, 'ms': 1e9, 'us': 1e6, 'ns': 1e3, 'ps': 1, 'fs': .001}[time[2]]
    cs = float(cap[0]) * {'f': 1e15, 'uf': 1e9, 'nf': 1e6, 'pf': 1e3, 'ff': 1}[cap[1].lower()]
    if ts <= 0 or cs <= 0:
        raise ModelError("Invalid library units")
    return ts, cs


IDENT = r'(?:\\[^\s]+|[A-Za-z_$][\w$]*(?:\[\d+\])?)'
INSTANCE = re.compile(r'^(' + IDENT + r')\s+(' + IDENT + r')\s*\((.*)\)$', re.S)
CONNECTION = re.compile(r'\.(' + IDENT + r')\s*\(\s*(' + IDENT + r')\s*\)')


def verilog_statements(path):
    # Streaming statements: never retain the complete million-cell netlist.
    buffer = []
    comment = False
    with open(path) as stream:
        for line in stream:
            clean = []
            i = 0
            while i < len(line):
                if comment:
                    end = line.find('*/', i)
                    if end < 0:
                        break
                    comment = False; i = end + 2
                elif line.startswith('//', i):
                    break
                elif line.startswith('/*', i):
                    comment = True; i += 2
                else:
                    clean.append(line[i]); i += 1
            for j, part in enumerate(''.join(clean).split(';')):
                if j:
                    yield ''.join(buffer).strip()
                    buffer = []
                buffer.append(part)
            buffer.append('\n')
        tail = ''.join(buffer).strip()
        if tail != 'endmodule':
            raise ModelError("Missing endmodule or unsupported trailing Verilog syntax")


def parse_statement(statement):
    match = INSTANCE.fullmatch(statement)
    if not match:
        raise ModelError("Requires a flat structural netlist with named scalar pin connections: " + statement[:120])
    master, name, body = match.groups()
    pairs = CONNECTION.findall(body)
    residue = CONNECTION.sub('', body).replace(',', '').strip()
    if residue or not pairs or len(dict(pairs)) != len(pairs):
        raise ModelError("Unsupported connection expression in instance " + name)
    return master, name, dict(pairs)


def scan_netlist(path, wanted):
    instances = {}
    modules = 0
    declarations = ('input ', 'output ', 'inout ', 'wire ', 'supply0 ', 'supply1 ')
    # Collect selected instances first, then only owners of nets they touch.
    for statement in verilog_statements(path):
        if statement.startswith('module '):
            modules += 1
            if modules > 1:
                raise ModelError("Only one flat Verilog module is supported")
            continue
        if statement.startswith(declarations):
            if statement.startswith(('supply0 ', 'supply1 ', 'inout ')):
                raise ModelError("Supply/inout declarations require explicit support")
            continue
        master, name, pins = parse_statement(statement)
        if name in wanted:
            if name in instances:
                raise ModelError("Duplicate instance " + name)
            instances[name] = (master, pins)
    if wanted - instances.keys():
        raise ModelError("Instances not found: %s" % sorted(wanted - instances.keys())[:10])
    touched = {net for _, pins in instances.values() for net in pins.values()}
    owners = {net: [] for net in touched}
    for statement in verilog_statements(path):
        if statement.startswith('module ') or statement.startswith('wire '):
            continue
        if statement.startswith(('input ', 'output ')):
            # Expand scalar declarations and buses so top-level boundaries are kept.
            direction, rest = statement.split(None, 1)
            bus = re.match(r'\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*(.*)', rest, re.S)
            names = rest.split(',') if not bus else bus[3].split(',')
            for name in names:
                name = name.strip()
                expanded = [name] if not bus else [name + '[%d]' % i for i in range(min(int(bus[1]), int(bus[2])), max(int(bus[1]), int(bus[2])) + 1)]
                for net in expanded:
                    if net in touched:
                        owners[net].append((None, net, direction, None))
            continue
        master, name, pins = parse_statement(statement)
        for pin, net in pins.items():
            if net in touched:
                owners[net].append((name, pin, None, master))
    return instances, owners


def cell_models(lib):
    result = {}
    for cell in lib.children:
        if cell.kind != 'cell':
            continue
        pins = {p.args[0]: p for p in cell.children if p.kind == 'pin'}
        result[cell.args[0]] = (cell, pins)
    return result


def build_cluster(cid, members, instances, owners, libraries, mixed_polarity='split-arcs'):
    names = {name for _, name in members}
    models = [cell_models(lib) for lib in libraries]
    local = {name: instances[name] for name in names}
    touched = {net for _, pins in local.values() for net in pins.values()}
    inputs, outputs, arcs = {}, {}, []
    drivers = {}
    for net in sorted(touched):
        net_drivers, sinks = [], []
        for name, pin, port_dir, master in owners[net]:
            if name is None:
                direction = 'output' if port_dir == 'input' else 'input'
            else:
                if master not in models[0] or pin not in models[0][master][1]:
                    raise ModelError("No Liberty pin for %s/%s (%s)" % (name, pin, master))
                direction = models[0][master][1][pin].attrs.get('direction')
            if direction == 'output':
                net_drivers.append((name, pin))
            elif direction == 'input':
                sinks.append((name, pin))
            else:
                raise ModelError("Inout/unknown pin direction on " + net)
        if len(net_drivers) != 1:
            raise ModelError("Expected exactly one driver on " + net)
        driver = net_drivers[0]
        drivers[net] = driver
        if driver[0] not in names:
            inputs[net] = [s for s in sinks if s[0] in names]
        elif any(s[0] not in names for s in sinks):
            outputs[net] = driver
    if not inputs or not outputs:
        raise ModelError("Cluster must have at least one boundary input and output")
    for name, (master, connections) in sorted(local.items()):
        signatures = []
        for library_models in models:
            if master not in library_models:
                raise ModelError("Missing library master " + master)
            cell, pins = library_models[master]
            if any(g.kind in ('ff', 'latch', 'ff_bank', 'latch_bank', 'statetable') for g in cell.children):
                raise ModelError("Sequential cell in cluster: " + name)
            if any(g.kind in ('bus', 'bundle') for g in cell.children):
                raise ModelError("Bus/bundle Liberty cells are not supported: " + master)
            signature = []
            for pin_name, net in connections.items():
                pin = pins.get(pin_name)
                if pin is None:
                    raise ModelError("Unknown pin " + pin_name)
                if pin.attrs.get('direction') != 'output':
                    continue
                if 'three_state' in pin.attrs:
                    raise ModelError("Tristate output is not supported")
                seen = set()
                for timing in (g for g in pin.children if g.kind == 'timing'):
                    if any(k in timing.attrs for k in ('when', 'sdf_cond', 'mode')):
                        raise ModelError("Conditional timing arc requires explicit mode handling: " + master)
                    if timing.attrs.get('timing_type', 'combinational') != 'combinational':
                        raise ModelError("Only combinational timing arcs are supported")
                    sense = timing.attrs.get('timing_sense')
                    if sense not in ('positive_unate', 'negative_unate'):
                        raise ModelError("Non-unate/unspecified arcs need transition-specific modeling: " + master)
                    if not {'cell_rise', 'cell_fall', 'rise_transition', 'fall_transition'}.issubset({g.kind for g in timing.children}):
                        raise ModelError("Four NLDM delay/slew tables are required: " + master)
                    for related in timing.attrs.get('related_pin', '').split():
                        if related in seen or related not in connections:
                            raise ModelError("Duplicate or unconnected related_pin: " + master)
                        seen.add(related)
                        signature.append((connections[related], net, sense == 'negative_unate'))
                if not seen:
                    raise ModelError("No combinational arcs for " + name + '/' + pin_name)
            signatures.append(sorted(signature))
        if signatures[0] != signatures[1]:
            raise ModelError("Early/late timing arc topology differs: " + master)
        arcs.extend(signatures[0])
    adjacency = collections.defaultdict(list)
    indegree = {net: 0 for net in touched}
    for src, dst, invert in arcs:
        adjacency[src].append((dst, invert)); indegree[dst] += 1
    ready = sorted(net for net, degree in indegree.items() if degree == 0)
    order = []
    while ready:
        src = ready.pop(); order.append(src)
        for dst, _ in adjacency[src]:
            indegree[dst] -= 1
            if indegree[dst] == 0:
                ready.append(dst)
    if len(order) != len(touched):
        raise ModelError("Combinational cycle within cluster")
    io_arcs = []
    for input_net in inputs:
        reachable = {input_net: {False}}
        for src in order:
            for dst, invert in adjacency[src]:
                reachable.setdefault(dst, set()).update(p ^ invert for p in reachable.get(src, ()))
        for output_net in outputs:
            polarity = reachable.get(output_net, set())
            if len(polarity) > 1 and mixed_polarity == 'reject':
                raise ModelError("Mixed-polarity reconvergence requires non-unate abstraction")
            for invert in sorted(polarity):
                io_arcs.append((input_net, output_net, 'negative_unate' if invert else 'positive_unate'))
    for output_net in outputs:
        visited = set(); stack = [n for n, _ in adjacency[output_net]]
        while stack:
            net = stack.pop()
            if net in visited:
                continue
            visited.add(net)
            if net in outputs:
                raise ModelError("Boundary output feeds another output; independent 2D load tables cannot model this cluster")
            stack.extend(n for n, _ in adjacency[net])
    if not io_arcs:
        raise ModelError("No reachable input/output timing arcs")
    pin_map = {net: 'I%d' % i for i, net in enumerate(sorted(inputs))}
    pin_map.update({net: 'O%d' % i for i, net in enumerate(sorted(outputs))})
    for net in sorted(touched - pin_map.keys()):
        pin_map[net] = 'n%d' % len(pin_map)
    return dict(id=cid, name='TC_%d' % cid, members=members, local=local,
                inputs=inputs, outputs=outputs, arcs=io_arcs, pins=pin_map)


def verilog(cluster, abstract=False):
    ports = [cluster['pins'][n] for n in list(cluster['inputs']) + list(cluster['outputs'])]
    lines = ['module tc (%s);' % ', '.join(ports)]
    lines.extend('input %s;' % cluster['pins'][n] for n in cluster['inputs'])
    lines.extend('output %s;' % cluster['pins'][n] for n in cluster['outputs'])
    if abstract:
        lines.append('%s u (%s);' % (cluster['name'], ', '.join('.%s(%s)' % (p, p) for p in ports)))
    else:
        lines.extend('wire %s;' % p for p in cluster['pins'].values() if p not in ports)
        for i, (name, (master, pins)) in enumerate(sorted(cluster['local'].items())):
            lines.append('%s u%d (%s);' % (master, i, ', '.join('.%s (%s)' % (p, cluster['pins'][n]) for p, n in pins.items())))
    return '\n'.join(lines + ['endmodule', ''])


def shell_values(shell, commands, cwd, timeout):
    result = subprocess.run([str(shell)], input='\n'.join(commands + ['exit', '']),
                            text=True, capture_output=True, cwd=cwd, timeout=timeout)
    if result.returncode or re.search(r'undefined command|failed to parse|syntax error', result.stderr):
        raise ModelError("OpenTimer failed:\n" + result.stderr[-4000:])
    values = []
    for line in result.stdout.splitlines():
        try:
            values.append(float(line.strip()))
        except ValueError:
            pass
    return values


def characterize(cluster, shell, work, slews, loads, corner, timeout, abstract=False,
                 joint=False, joint_offset=0.):
    commands = ['set_num_threads 1', 'read_celllib library.lib',
                'read_verilog %s' % ('abstract.v' if abstract else 'cluster.v'), 'update_timing']
    split = '-early' if corner == 'early' else '-late'
    keys = []
    outputs = [cluster['pins'][n] for n in cluster['outputs']]
    for input_net in cluster['inputs']:
        pi = cluster['pins'][input_net]
        # Clear arrival and slew of ALL inputs: no other PI can dominate this arc.
        for n in cluster['inputs']:
            for rf in ('-rise', '-fall'):
                commands.extend(['set_at -pin %s %s %s' % (cluster['pins'][n], split, rf),
                                 'set_slew -pin %s %s %s' % (cluster['pins'][n], split, rf)])
        for src, dst, sense in cluster['arcs']:
            if src != input_net:
                continue
            po = cluster['pins'][dst]
            for rf in ('rise', 'fall'):
                input_rf = rf if sense == 'positive_unate' else ('fall' if rf == 'rise' else 'rise')
                # Mixed-polarity paths must not compete during characterization:
                # isolate one INPUT transition and observe one OUTPUT transition.
                for transition in ('rise', 'fall'):
                    commands.extend(['set_at -pin %s %s -%s' % (pi, split, transition),
                                     'set_slew -pin %s %s -%s' % (pi, split, transition)])
                if joint:
                    # Unequal input ATs/slews probe interaction between both senses.
                    commands.extend(['set_at -pin %s %s -rise 0' % (pi, split),
                                     'set_at -pin %s %s -fall %.12g' % (pi, split, joint_offset)])
                else:
                    commands.append('set_at -pin %s %s -%s 0' % (pi, split, input_rf))
                for s, slew in enumerate(slews):
                    if joint:
                        commands.extend(['set_slew -pin %s %s -rise %.12g' % (pi, split, slew),
                                         'set_slew -pin %s %s -fall %.12g' % (pi, split, slews[(s + 1) % len(slews)])])
                    else:
                        commands.append('set_slew -pin %s %s -%s %.12g' % (pi, split, input_rf, slew))
                    for l, load in enumerate(loads):
                        for out in outputs:
                            for transition in ('rise', 'fall'):
                                commands.append('set_load -pin %s %s -%s %.12g' % (out, split, transition, load if out == po else 0))
                        commands.append('update_timing')
                        for metric in ('at', 'slew'):
                            commands.append('report_%s -pin %s %s -%s' % (metric, po, split, rf))
                            keys.append((src, dst, sense, metric, rf, s, l))
    values = shell_values(shell, commands, work, timeout)
    if len(values) != len(keys) or any(not math.isfinite(v) for v in values):
        raise ModelError("Missing/nonfinite OpenTimer timing results: expected %d, got %d" % (len(keys), len(values)))
    if any(v < 0 for k, v in zip(keys, values) if k[3] == 'slew'):
        raise ModelError("Negative output slew")
    return dict(zip(keys, values))


def emit_cell(cluster, lib, slews, loads, values):
    models = cell_models(lib)
    lines = ['cell (%s) {' % cluster['name']]
    area = sum(float(models[master][0].attrs.get('area', 0)) for master, _ in cluster['local'].values())
    lines.append(' area : %.12g;' % area)
    for net, sinks in cluster['inputs'].items():
        lines.extend([' pin (%s) {' % cluster['pins'][net], '  direction : input;'])
        for attribute in ('capacitance', 'rise_capacitance', 'fall_capacitance'):
            cap = 0.0
            for name, pin in sinks:
                master = cluster['local'][name][0]
                attrs = models[master][1][pin].attrs
                fallback = attrs.get('capacitance', lib.attrs.get('default_input_pin_cap'))
                val = attrs.get(attribute, fallback)
                if val is None:
                    raise ModelError("Missing input capacitance: %s/%s" % (name, pin))
                cap += float(val)
            lines.append('  %s : %.12g;' % (attribute, cap))
        lines.append(' }')
    for net in cluster['outputs']:
        lines.extend([' pin (%s) {' % cluster['pins'][net], '  direction : output;', '  capacitance : 0.0;'])
        for src, dst, sense in cluster['arcs']:
            if dst != net:
                continue
            lines.extend(['  timing () {', '   related_pin : "%s";' % cluster['pins'][src],
                          '   timing_type : combinational;', '   timing_sense : %s;' % sense])
            for table, metric, rf in (('cell_rise', 'at', 'rise'), ('cell_fall', 'at', 'fall'),
                                      ('rise_transition', 'slew', 'rise'), ('fall_transition', 'slew', 'fall')):
                lines.extend(['   %s (TC_slew_load) {' % table,
                              '    index_1 ("%s");' % ', '.join('%.12g' % x for x in slews),
                              '    index_2 ("%s");' % ', '.join('%.12g' % x for x in loads)])
                rows = ['"%s"' % ', '.join('%.12g' % values[src, dst, sense, metric, rf, s, l] for l in range(len(loads))) for s in range(len(slews))]
                lines.append('    values (%s);' % ',\n            '.join(rows))
                lines.extend(['   }'])
            lines.append('  }')
        lines.append(' }')
    return '\n'.join(lines + ['}', ''])


def grid(value):
    result = [float(v) for v in value.split(',')]
    if len(result) < 2 or any(not math.isfinite(v) or v < 0 for v in result) or any(a >= b for a, b in zip(result, result[1:])):
        raise argparse.ArgumentTypeError("At least two finite nonnegative, increasing grid points required")
    return result


def cluster_sizes(path, membership):
    """Validate dimensions without changing their units or decimal spelling."""
    sizes = {}
    for cid, width, height in table_rows(path, ('cluster_id', 'width', 'height')):
        if not re.fullmatch(r'[0-9]+', cid):
            raise ModelError('Invalid cluster size ID: ' + cid)
        cid = int(cid)
        if cid in sizes:
            raise ModelError('Duplicate cluster size ID: %d' % cid)
        if any(not math.isfinite(float(v)) or float(v) <= 0 for v in (width, height)):
            raise ModelError('Cluster dimensions must be finite and positive: %d' % cid)
        sizes[cid] = (width, height)
    missing = membership.keys() - sizes.keys()
    if missing:
        raise ModelError('Missing cluster sizes: %s' % sorted(missing)[:10])
    return sizes


def generate(args):
    mixed_policy = getattr(args, 'mixed_polarity', 'split-arcs')
    if mixed_policy not in ('split-arcs', 'reject'):
        raise ModelError('Invalid mixed-polarity policy')
    selected = set(map(int, args.cluster_ids.split(','))) if args.cluster_ids else None
    membership = read_membership(args.clusters, args.cell_names, selected)
    failure_policy = getattr(args, 'on_cluster_failure', 'abort')
    if failure_policy not in ('abort', 'retain-original'):
        raise ModelError('Invalid cluster failure policy')
    sizes_path = getattr(args, 'cluster_sizes', None)
    sizes = cluster_sizes(sizes_path, membership) if sizes_path else None
    common = getattr(args, 'lib_dir', None)
    early, late = getattr(args, 'early_lib', None), getattr(args, 'late_lib', None)
    if common and (early or late):
        raise ModelError('Use --lib-dir OR both --early-lib and --late-lib, not both modes')
    if not common and not (early and late):
        raise ModelError('Supply --lib-dir or both --early-lib and --late-lib')
    library_paths = [discover_libraries(common or early), discover_libraries(common or late)]
    shell = Path(args.ot_shell).resolve()
    if not shell.is_file():
        raise ModelError("OpenTimer shell not found: %s" % shell)
    output = Path(args.output).resolve()
    if output.exists():
        raise ModelError("Output directory already exists; use a new path: %s" % output)
    wanted = {name for members in membership.values() for _, name in members}
    print('Reading original netlist for %d clusters / %d cells' % (len(membership), len(wanted)), flush=True)
    instances, owners = scan_netlist(args.verilog, wanted)
    internal = {master for master, _ in instances.values()}
    needed = internal | {master for pins in owners.values() for _, _, _, master in pins if master is not None}
    cache, libraries, library_reports = {}, [], []
    common_overrides = parse_overrides(getattr(args, 'cell_override', None))
    for split, paths in zip(('early', 'late'), library_paths):
        overrides = dict(common_overrides)
        overrides.update(parse_overrides(getattr(args, split + '_cell_override', None)))
        # Reuse the parsed set as well as the index when min/max share sources.
        if libraries and paths == library_paths[0] and overrides == early_overrides:
            lib, metadata = libraries[0], library_reports[0]
        else:
            lib, metadata = load_library_set(paths, needed, internal, overrides, cache)
        if split == 'early':
            early_overrides = overrides
        libraries.append(lib)
        library_reports.append(metadata)
    unit_scales = [units(lib) for lib in libraries]
    output.mkdir(parents=True)
    report = dict(status='in_progress', model='ideal_internal_wires_NLDM',
                  on_cluster_failure=failure_policy, requested_clusters=len(membership),
                  failed_clusters=[], retained_original_cells=0,
                  mixed_polarity_policy=mixed_policy,
                  transition_model='isolated input/output transitions; separate positive/negative timing groups for mixed pairs',
                  slew_grid_ps=args.slews_ps, load_grid_ff=args.loads_ff, clusters=[],
                  limitations=['No internal wire RC', 'Source cells require unconditional unate combinational arcs',
                               'Mixed-polarity structural paths use two unate timing groups, not Boolean sensitization or glitch modeling',
                               'Joint validation uses sampled input slew/arrival combinations, not an exhaustive functional proof',
                               'No boundary output-to-output paths', 'Not a functional/synthesis library',
                               'No automatic SDC exception remapping'],
                  library_mode='shared_min_max' if common else 'separate_early_late',
                  library_sets=dict(zip(('early', 'late'), library_reports)),
                  inputs={k: str(Path(getattr(args, k)).resolve()) for k in ('clusters', 'cell_names', 'verilog')})
    if sizes_path:
        report['inputs']['cluster_sizes'] = str(Path(sizes_path).resolve())
    report['final_membership'] = 'final_clusters.tsv'
    report['final_cluster_sizes'] = 'cluster_sizes.tsv' if sizes is not None else None
    lib_streams = []
    try:
        for corner, lib in zip(('Early', 'Late'), libraries):
            stream = open(output / ('clusters_%s.lib' % corner), 'x')
            lib_streams.append(stream)
            stream.write(liberty_header(lib, 'clusters_' + corner, template=True))
        with ExitStack() as stack:
            mapping = stack.enter_context(open(output / 'cluster_mapping.jsonl', 'x'))
            failed = stack.enter_context(open(output / 'failed_clusters.jsonl', 'x'))
            retained = stack.enter_context(open(output / 'retained_cells.tsv', 'x', newline=''))
            retained_writer = csv.writer(retained, delimiter='\t', lineterminator='\n')
            retained_writer.writerow(('cell_id', 'cluster_id', 'cell_name', 'original_master'))
            final_members = stack.enter_context(open(output / 'final_clusters.tsv', 'x', newline=''))
            member_writer = csv.writer(final_members, delimiter='\t', lineterminator='\n')
            member_writer.writerow(('cell_id', 'cluster_id'))
            if sizes is not None:
                final_sizes = stack.enter_context(open(output / 'cluster_sizes.tsv', 'x', newline=''))
                size_writer = csv.writer(final_sizes, delimiter='\t', lineterminator='\n')
                size_writer.writerow(('cluster_id', 'width', 'height'))
            for cid, members in sorted(membership.items()):
                try:
                    try:
                        cluster = build_cluster(cid, members, instances, owners, libraries, mixed_policy)
                    except ModelError as exc:
                        raise AbstractionError(str(exc)) from exc
                    pair_counts = collections.Counter((src, dst) for src, dst, sense in cluster['arcs'])
                    mixed_pairs = sum(count > 1 for count in pair_counts.values())
                    cell_texts, verification = [], []
                    with tempfile.TemporaryDirectory(prefix='tc_lib_') as temporary:
                        work = Path(temporary)
                        (work / 'cluster.v').write_text(verilog(cluster))
                        (work / 'abstract.v').write_text(verilog(cluster, abstract=True))
                        masters = {master for master, _ in cluster['local'].values()}
                        for corner, lib, (time_ps, cap_ff) in zip(('early', 'late'), libraries, unit_scales):
                            subset = liberty_header(lib, 'source') + '\n'.join(c.raw for c in lib.children if c.kind == 'cell' and c.args[0] in masters) + '\n}\n'
                            (work / 'library.lib').write_text(subset)
                            slews = [s / time_ps for s in args.slews_ps]
                            loads = [c / cap_ff for c in args.loads_ff]
                            values = characterize(cluster, shell, work, slews, loads, corner, args.timeout)
                            cell_text = emit_cell(cluster, lib, slews, loads, values)
                            cell_texts.append(cell_text)
                            # Re-read the emitted Liberty and verify at all grid points.
                            (work / 'library.lib').write_text(liberty_header(lib, 'check', True) + cell_text + '}\n')
                            checked = characterize(cluster, shell, work, slews, loads, corner, args.timeout, abstract=True)
                            error = max(abs(checked[k] - v) * time_ps for k, v in values.items())
                            if any(not math.isclose(checked[k], v, rel_tol=5e-4, abs_tol=1e-3 / time_ps) for k, v in values.items()):
                                raise AbstractionError("Reduced Liberty grid round-trip mismatch (max error %.6g ps)" % error)
                            joint_error = None
                            if mixed_pairs:
                                joint_error = 0.
                                for offset in (-25. / time_ps, 25. / time_ps):
                                    (work / 'library.lib').write_text(subset)
                                    original_joint = characterize(cluster, shell, work, slews, loads, corner, args.timeout,
                                                                  joint=True, joint_offset=offset)
                                    (work / 'library.lib').write_text(liberty_header(lib, 'check', True) + cell_text + '}\n')
                                    reduced_joint = characterize(cluster, shell, work, slews, loads, corner, args.timeout,
                                                                 abstract=True, joint=True, joint_offset=offset)
                                    joint_error = max(joint_error, max(abs(reduced_joint[k] - v) * time_ps for k, v in original_joint.items()))
                                    if any(not math.isclose(reduced_joint[k], v, rel_tol=5e-4, abs_tol=1e-3 / time_ps)
                                           for k, v in original_joint.items()):
                                        raise AbstractionError('Mixed-polarity joint-transition round-trip mismatch (max error %.6g ps); '
                                                         'split this cluster or use a richer timing model' % joint_error)
                            # Midpoint holdout quantifies the interpolation approximation.
                            ms = [(a + b) / 2 for a, b in zip(slews, slews[1:])]
                            ml = [(a + b) / 2 for a, b in zip(loads, loads[1:])]
                            reduced_mid = characterize(cluster, shell, work, ms, ml, corner, args.timeout, abstract=True)
                            (work / 'library.lib').write_text(subset)
                            original_mid = characterize(cluster, shell, work, ms, ml, corner, args.timeout)
                            mid_error = max(abs(reduced_mid[k] - v) * time_ps for k, v in original_mid.items())
                            verification.append(dict(corner=corner, grid_max_error_ps=error, midpoint_max_error_ps=mid_error,
                                                     joint_transition_max_error_ps=joint_error))
                    # Append only fully characterized and verified cells.
                    for stream, cell_text in zip(lib_streams, cell_texts):
                        stream.write(cell_text); stream.flush()
                    entry = dict(cluster_id=cid, liberty_cell=cluster['name'], members=[dict(cell_id=n, cell_name=s, original_master=cluster['local'][s][0]) for n, s in members],
                                 transition_model=report['transition_model'], mixed_polarity_pairs=mixed_pairs,
                                 inputs=[dict(pin=cluster['pins'][net], net=net, original_pins=['%s:%s' % p for p in pins]) for net, pins in cluster['inputs'].items()],
                                 outputs=[dict(pin=cluster['pins'][net], net=net, original_pin='%s:%s' % pin) for net, pin in cluster['outputs'].items()],
                                 arcs=[dict(input=cluster['pins'][i], output=cluster['pins'][o], sense=s) for i, o, s in cluster['arcs']])
                    mapping.write(json.dumps(entry) + '\n'); mapping.flush()
                    member_writer.writerows((node, cid) for node, _ in members)
                    final_members.flush()
                    if sizes is not None:
                        size_writer.writerow((cid, *sizes[cid]))
                        final_sizes.flush()
                    report['clusters'].append(dict(cluster_id=cid, cells=len(members), arcs=len(cluster['arcs']),
                                                   logical_io_pairs=len(pair_counts), mixed_polarity_pairs=mixed_pairs, verification=verification))
                    print('Cluster %d: %d cells, %d arcs; verified early/late' % (cid, len(members), len(cluster['arcs'])), flush=True)
                except AbstractionError as exc:
                    failure = dict(cluster_id=cid, cells=len(members), reason=str(exc),
                                   action='retained_original' if failure_policy == 'retain-original' else 'aborted')
                    failed.write(json.dumps(failure) + '\n'); failed.flush()
                    report['failed_clusters'].append(failure)
                    if failure_policy == 'abort':
                        raise ModelError('Cluster %d: %s' % (cid, exc)) from exc
                    retained_writer.writerows((node, cid, name, instances[name][0]) for node, name in members)
                    retained.flush()
                    report['retained_original_cells'] += len(members)
                    print('WARNING: Cluster %d: retaining %d original cells: %s' % (cid, len(members), exc), flush=True)
                except Exception as exc:
                    raise ModelError('Cluster %d: %s' % (cid, exc)) from exc
        # Only verified clusters appear in the mapping. Unmapped instances and
        # their connections are preserved by ReducedVerilog, including failures.
        if not report['clusters']:
            raise ModelError('No clusters passed verification; use the original design. See failed_clusters.jsonl')
        for stream in lib_streams:
            stream.write('}\n'); stream.close()
        from ReducedVerilog import reduce_verilog
        report['reduced_verilog'] = reduce_verilog(args.verilog, output / 'cluster_mapping.jsonl',
                                                  output / 'reduced.v', check_manifest=False)
        report['status'] = 'complete'
    except Exception as exc:
        report['status'], report['error'] = 'failed_partial_do_not_use', str(exc)
        raise
    finally:
        for stream in lib_streams:
            if not stream.closed:
                stream.write('}\n'); stream.close()
        (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clusters', required=True, help='cell_id cluster_id table (CSV, TSV or whitespace)')
    parser.add_argument('--cell-names', required=True, help='cell_id cell_name table')
    parser.add_argument('--verilog', required=True, help='original flat structural netlist')
    parser.add_argument('--lib-dir', action='append', help='recursively read .lib files; use the same set for min/max; repeatable')
    parser.add_argument('--early-lib', nargs='+', help='early library files/directories (alternative to --lib-dir)')
    parser.add_argument('--late-lib', nargs='+', help='late library files/directories (alternative to --lib-dir)')
    parser.add_argument('--cell-override', action='append', metavar='CELL=PATH', help='explicit source for a duplicate cell; repeatable')
    parser.add_argument('--early-cell-override', action='append', metavar='CELL=PATH')
    parser.add_argument('--late-cell-override', action='append', metavar='CELL=PATH')
    parser.add_argument('--output', required=True, help='new output directory')
    parser.add_argument('--cluster-ids', help='optional comma-separated cluster IDs to characterize')
    parser.add_argument('--on-cluster-failure', choices=('abort', 'retain-original'), default='abort',
                        help='abort (default), or retain original cells when cluster modeling/verification fails; infrastructure errors still abort')
    parser.add_argument('--cluster-sizes', help='optional cluster_id width height table; emit verified-only cluster_sizes.tsv in the same units')
    parser.add_argument('--mixed-polarity', choices=('split-arcs', 'reject'), default='split-arcs',
                        help='mixed reconvergence: separate transition-specific positive/negative timing groups (default), or reject')
    root = Path(__file__).resolve().parent.parent
    shell_options = [root / 'thirdparty/OpenTimer/bin/ot-shell', root / 'bin/ot-shell',
                     root.parent / 'thirdparty/OpenTimer/bin/ot-shell']
    default_shell = next((p for p in shell_options if p.is_file()), shell_options[0])
    parser.add_argument('--ot-shell', default=str(default_shell))
    parser.add_argument('--slews-ps', type=grid, default=grid('10,50,100'))
    parser.add_argument('--loads-ff', type=grid, default=grid('1,5,10'))
    parser.add_argument('--timeout', type=float, default=120, help='timeout seconds per OpenTimer process')
    args = parser.parse_args()
    try:
        report = generate(args)
    except (ModelError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, 'ERROR: %s\n' % exc)
    print('Saved %d clusters to %s' % (len(report['clusters']), args.output))
    if report['retained_original_cells']:
        print('Retained %d original cells from %d failed clusters; see failed_clusters.jsonl and retained_cells.tsv' %
              (report['retained_original_cells'], len(report['failed_clusters'])))


if __name__ == '__main__':
    main()
