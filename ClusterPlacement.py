#!/usr/bin/env python3
"""Placement-only LEF/DEF contraction; no reduced Liberty or Verilog required.

Every original ordinary net survives. Each (cluster, net) gets one distinct VP
at the cluster center, including nets entirely internal to a cluster. Streaming
DEF passes and a temporary SQLite index bound connectivity memory usage. A
complete replacement LEF is emitted: fixed masters retain their geometry while
all movable geometry receives one design-level area inflation when the requested
target utilization is above the original utilization.
"""
import argparse
from collections import Counter
import csv
from decimal import Decimal, ROUND_CEILING
from functools import lru_cache
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

from ClusterLEFMapping import object_items
from ReducedDEF import component, def_name, dname, discover_lefs, events, option, tokens
from ReducedLEF import lef_name, number
from ReducedLiberty import ModelError, read_membership

LOG = logging.getLogger(__name__)


def positive(value, label):
    try:
        value = Decimal(str(value))
        if not value.is_finite() or value <= 0:
            raise ValueError()
        return value
    except (ValueError, ArithmeticError):
        raise ModelError('%s must be positive and finite' % label)


def integer(value, label):
    value = positive(value, label)
    if value != value.to_integral_value():
        raise ModelError('%s must be an integer in DEF DBU' % label)
    return int(value)


def ceil(value):
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def grid_size(area_um2, aspect_ratio, site_dbu, row_dbu, dbu):
    """Fit target area/aspect to the site/row grid without undersizing."""
    area = positive(area_um2, 'target area')
    aspect = positive(aspect_ratio, 'aspect ratio')
    site, row, scale = (integer(v, n) for v, n in
                        ((site_dbu, 'site_width'), (row_dbu, 'row_height'), (dbu, 'def_scale')))
    target = area * scale * scale
    ideal = (target / aspect).sqrt() / row
    choices = []
    for rows in {max(1, int(ideal)), max(1, ceil(ideal))}:
        height = rows * row
        width = max(1, ceil(target / height / site)) * site
        choices.append((width, height))
    return min(choices, key=lambda wh: (
        abs(Decimal(wh[0]) / wh[1] - aspect), wh[0] * wh[1], max(wh)))


def cluster_size(area_um2, utilization, site_dbu, row_dbu, dbu):
    """Backward-compatible square sizing helper for older callers."""
    u = positive(utilization, 'utilization')
    if u > 1:
        raise ModelError('utilization must be <= 1 (fraction, not percent)')
    return grid_size(positive(area_um2, 'member area') / u, 1, site_dbu, row_dbu, dbu)


def lef_blocks(paths):
    """Return one LEF preamble and unique complete MACRO blocks."""
    preamble, macros, order = None, {}, []
    for path in paths:
        lines = Path(path).read_text().splitlines(keepends=True)
        starts = [i for i, line in enumerate(lines) if re.match(r'^\s*MACRO\s+\S+', line)]
        if preamble is None:
            stop = starts[0] if starts else next((i for i, line in enumerate(lines)
                                                  if re.match(r'^\s*END\s+LIBRARY\b', line)), len(lines))
            preamble = ''.join(lines[:stop])
        for start in starts:
            match = re.match(r'^(\s*MACRO\s+)(\S+)', lines[start])
            name = match.group(2)
            end = next((i for i in range(start + 1, len(lines))
                        if re.match(r'^\s*END\s+' + re.escape(name) + r'\s*$', lines[i])), None)
            if end is None:
                raise ModelError('Unterminated LEF MACRO %s in %s' % (name, path))
            if name in macros:
                raise ModelError('Duplicate LEF master %s in %s and %s' %
                                 (name, macros[name][0], path))
            macros[name] = (path, lines[start:end + 1])
            order.append(name)
    return preamble or '', macros, order


def scaled_macro(lines, old_name, new_name, old_size, new_size):
    """Clone a LEF macro and scale its physical coordinates with its SIZE."""
    old_w, old_h = map(Decimal, old_size)
    new_w, new_h = map(Decimal, new_size)
    sx, sy = new_w / old_w, new_h / old_h
    output = []
    coordinate = re.compile(r'^(\s*)(RECT|POLYGON|PATH)\s+(.+?)(\s*;\s*(?:#.*)?\n?)$')
    for line in lines:
        line = re.sub(r'^(\s*MACRO\s+)' + re.escape(old_name) + r'\b',
                      r'\g<1>' + new_name, line)
        line = re.sub(r'^(\s*END\s+)' + re.escape(old_name) + r'(\s*)$',
                      r'\g<1>' + new_name + r'\g<2>', line)
        line = re.sub(r'^(\s*FOREIGN\s+)' + re.escape(old_name) + r'\b',
                      r'\g<1>' + new_name, line)
        if re.match(r'^\s*SIZE\b', line):
            indent = re.match(r'^(\s*)', line).group(1)
            line = '%sSIZE %s BY %s ;\n' % (indent, number(new_w), number(new_h))
        else:
            match = coordinate.match(line)
            if match:
                try:
                    values = [Decimal(v) for v in match.group(3).split()]
                except ArithmeticError as exc:
                    raise ModelError('Unsupported LEF geometry while scaling %s: %s' %
                                     (old_name, line.strip())) from exc
                if len(values) % 2:
                    raise ModelError('Odd LEF coordinate count while scaling %s' % old_name)
                values = [v * (sx if i % 2 == 0 else sy) for i, v in enumerate(values)]
                line = '%s%s %s%s' % (match.group(1), match.group(2),
                                      ' '.join(number(v) for v in values), match.group(4))
        output.append(line)
    return ''.join(output)


def replace_component_master(record, new_master):
    match = re.match(r'^(\s*-\s+(?:\\\S+|\S+)\s+)(?:\\\S+|\S+)', record)
    if not match:
        raise ModelError('Cannot rewrite DEF component master: ' + record[:80])
    return match.group(1) + def_name(new_master) + record[match.end():]


def endpoint_record(text):
    ts = tokens(text)
    name = dname(ts[1])
    endpoints, i = [], 2
    while i < len(ts) and ts[i] not in ('+', ';'):
        if i + 3 >= len(ts) or ts[i] != '(' or ts[i + 3] != ')':
            raise ModelError('Unsupported NETS endpoint syntax on ' + name)
        inst, pin = dname(ts[i + 1]), dname(ts[i + 2])
        if inst == '*':
            raise ModelError('Wildcard NETS endpoints are not supported: ' + name)
        endpoints.append((inst, pin)); i += 4
    if not endpoints:
        raise ModelError('Empty ordinary net: ' + name)
    return name, endpoints, option(ts, 'USE') or 'SIGNAL'


def tsv(stream, columns):
    writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
    writer.writerow(columns)
    return writer


def generate(args):
    out = Path(args.output)
    if os.path.lexists(out):
        raise ModelError('Output already exists; use a new directory: ' + str(out))
    root, original = Path(args.saved_db), Path(args.def_input)
    die_path = Path(args.die_info) if args.die_info else root / 'die_info.json'
    lef_paths = list(dict.fromkeys(path for item in args.lef_input for path in discover_lefs(item)))
    if not lef_paths:
        raise ModelError('No source LEF files found')
    inputs = ([original, Path(args.clusters), Path(args.cell_names), die_path,
               root / 'lef_info.json'] + lef_paths)
    physical_manifest = root / 'physical_db' / 'manifest.json'
    if physical_manifest.is_file():
        inputs.append(physical_manifest)
    for path in inputs:
        if not path.is_file():
            raise ModelError('Input file does not exist: ' + str(path))
    target_utilization = positive(args.utilization, 'utilization')
    if target_utilization > 1:
        raise ModelError('utilization must be <= 1 (fraction, not percent)')
    layer = lef_name(args.pin_layer)
    pin_width = positive(args.pin_width, 'pin width')
    pin_height = positive(args.pin_width if args.pin_height is None else args.pin_height, 'pin height')
    die = json.loads(die_path.read_text())
    scale = integer(die['def_scale'], 'def_scale')
    site = integer(die['site_width'], 'site_width')
    row = integer(die['row_height'], 'row_height')
    bounds = [float(die[k]) for k in ('xl', 'yl', 'xh', 'yh')]
    if not all(math.isfinite(v) for v in bounds) or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        raise ModelError('Invalid placement bounds in die_info.json')
    membership = read_membership(args.clusters, args.cell_names)
    assignment = {name: cid for cid, members in membership.items() for _, name in members}
    member_ids = {name: node for members in membership.values() for node, name in members}
    catalog = dict(object_items(root / 'lef_info.json'))
    geometry = {master: (positive(info['width'], master + ' width'),
                         positive(info['height'], master + ' height'))
                for master, info in catalog.items()}
    clusters = {}
    for cid in sorted(membership):
        master, inst = 'PC_%d' % cid, '__pc_cluster_%d' % cid
        if master in catalog or inst in assignment:
            raise ModelError('Generated master/instance name collision: ' + master)
        clusters[cid] = dict(master=master, instance=inst, area=Decimal(0),
                             count=0, placed=0, cx=0., cy=0., regions=set(), pins=0)
    generated_names = {c['instance'] for c in clusters.values()}
    headers, ports = {}, {}
    units, def_bounds, row_sites = None, None, set()
    counts = Counter()
    original_movable_area = Decimal(0)
    original_fixed_area = Decimal(0)
    retained_movable_area = Decimal(0)
    retained_movable_masters = set()
    retained_movable_master_counts = Counter()
    fixed_masters = set()
    row_area_dbu2 = Decimal(0)
    lef_preamble, source_macros, source_master_order = lef_blocks(lef_paths)
    missing_lef = set(catalog) - set(source_macros)
    if missing_lef:
        raise ModelError('Saved LEF masters missing from --lef-input: ' + ', '.join(sorted(missing_lef)[:5]))
    out.parent.mkdir(parents=True, exist_ok=True)
    # Publish only after every file and validation succeeds. The target must
    # not exist; a failed conversion cannot look like a completed design.
    with tempfile.TemporaryDirectory(prefix='.cluster-placement-', dir=out.parent) as scratch:
        scratch = Path(scratch)
        result = scratch / 'result'; result.mkdir()
        db = sqlite3.connect(str(scratch / 'index.sqlite'))
        try:
            db.executescript('''
                PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-65536;
                CREATE TABLE components(name TEXT PRIMARY KEY, master TEXT NOT NULL) WITHOUT ROWID;
                CREATE TABLE nets(name TEXT PRIMARY KEY) WITHOUT ROWID;
                CREATE TABLE endpoints(instance TEXT, pin TEXT, PRIMARY KEY(instance,pin)) WITHOUT ROWID;
                CREATE TABLE pins(cid INTEGER, ordinal INTEGER, net TEXT, direction TEXT,
                                  PRIMARY KEY(cid,ordinal)) WITHOUT ROWID;
            ''')
            LOG.info('Scanning original DEF components and floorplan')
            for kind, section, text, count in events(original):
                if kind == 'header':
                    if section in headers:
                        raise ModelError('Duplicate DEF section: ' + section)
                    headers[section] = count
                    if count and section not in ('COMPONENTS', 'PINS', 'NETS', 'REGIONS',
                                                  'BLOCKAGES', 'VIAS', 'NONDEFAULTRULES', 'STYLES'):
                        # Never silently disconnect supplies/groups/scan chains.
                        raise ModelError('Unsupported nonempty %s; provide a placement-only DEF' % section)
                elif kind == 'record' and section == 'COMPONENTS':
                    name, master, state, x, y, region = component(text)
                    if name in generated_names:
                        raise ModelError('Generated instance collides with DEF component: ' + name)
                    if master not in catalog:
                        raise ModelError('Missing saved LEF master: ' + master)
                    db.execute('INSERT INTO components VALUES (?,?)', (name, master))
                    counts['original_components'] += 1
                    w, h = geometry[master]
                    if state not in ('FIXED', 'COVER'):
                        original_movable_area += w * h
                        if name not in assignment:
                            retained_movable_area += w * h
                            retained_movable_masters.add(master)
                            retained_movable_master_counts[master] += 1
                    else:
                        original_fixed_area += w * h
                        fixed_masters.add(master)
                    if name not in assignment:
                        continue
                    if state in ('FIXED', 'COVER') or catalog[master].get('class') != 'CORE':
                        raise ModelError('Cluster member must be movable CORE: ' + name)
                    if region:
                        raise ModelError('Cluster member has a REGION constraint: ' + name)
                    c = clusters[assignment[name]]
                    w, h = geometry[master]; area = w * h
                    c['area'] += area; c['count'] += 1
                    if x is not None:
                        # DEF orientations E/W rotate width and height.
                        ts = tokens(text)
                        i = ts.index(state)
                        orientation = ts[i + 5]
                        if orientation in ('E', 'W', 'FE', 'FW'):
                            w, h = h, w
                        c['cx'] += (x + float(w) * scale / 2) * float(area)
                        c['cy'] += (y + float(h) * scale / 2) * float(area)
                        c['placed'] += 1
                    counts['removed_members'] += 1
                elif kind == 'record' and section == 'PINS':
                    ts = tokens(text); name = dname(ts[1])
                    if name in ports:
                        raise ModelError('Duplicate external pin: ' + name)
                    ports[name] = (dname(option(ts, 'NET') or ''), option(ts, 'DIRECTION'))
                elif kind == 'record' and section == 'BLOCKAGES':
                    ts = tokens(text)
                    if 'COMPONENT' in ts:
                        raise ModelError('Component-associated BLOCKAGES are unsupported')
                elif kind == 'raw' and section is None:
                    ts = tokens(text)
                    if ts[:3] == ['UNITS', 'DISTANCE', 'MICRONS']:
                        units = int(ts[3])
                    elif ts[:1] == ['DIEAREA']:
                        xy = [int(v) for v in ts[1:] if v not in ('(', ')', ';')]
                        if len(xy) != 4:
                            raise ModelError('Only rectangular DIEAREA is currently supported')
                        def_bounds = xy
                    elif ts[:1] == ['ROW']:
                        row_sites.add(ts[2])
                        # The first version requires horizontal, single-row arrays.
                        if ts[5] not in ('N', 'S', 'FN', 'FS') or 'STEP' not in ts or 'BY' not in ts:
                            raise ModelError('Unsupported DEF ROW orientation/array')
                        step = ts.index('STEP')
                        if int(ts[step + 1]) != site or int(ts[ts.index('BY') + 1]) != 1:
                            raise ModelError('DEF ROW step/array disagrees with saved site_width')
                        if 'DO' not in ts:
                            raise ModelError('DEF ROW is missing DO count')
                        row_area_dbu2 += Decimal(int(ts[ts.index('DO') + 1])) * site * row
                        if (int(ts[3]) - bounds[0]) % site or (int(ts[4]) - bounds[1]) % row:
                            raise ModelError('DEF ROW origins disagree with saved site/row grid')
            if units != scale:
                raise ModelError('DEF units disagree with die_info.def_scale')
            if def_bounds is None or not (def_bounds[0] <= bounds[0] < bounds[2] <= def_bounds[2]
                                         and def_bounds[1] <= bounds[1] < bounds[3] <= def_bounds[3]):
                raise ModelError('die_info placement bounds are outside DEF DIEAREA')
            if len(row_sites) != 1:
                raise ModelError('Expected one uniform DEF row SITE type')
            if not all(k in headers for k in ('COMPONENTS', 'NETS')):
                raise ModelError('Original DEF must include COMPONENTS and NETS')

            if physical_manifest.is_file():
                physical_meta = json.loads(physical_manifest.read_text())
                if integer(physical_meta['def_scale'], 'saved physical DB def_scale') != scale:
                    raise ModelError('Saved physical DB units disagree with DEF')
                # MakeDB total_space_area excludes fixed occupancy. Add fixed
                # instance area back because the requested utilization counts
                # fixed cells in the numerator but never inflates them.
                placeable_area = (Decimal(str(physical_meta['total_space_area'])) / scale**2
                                  + original_fixed_area)
                placeable_source = 'saved_total_space_plus_fixed_cells'
            else:
                placeable_area = row_area_dbu2 / scale**2
                placeable_source = 'def_row_area_fallback'
            if placeable_area <= 0 or original_movable_area <= 0:
                raise ModelError('Placeable and movable areas must be positive')
            current_utilization = (original_fixed_area + original_movable_area) / placeable_area
            if target_utilization > current_utilization:
                inflation = ((target_utilization * placeable_area - original_fixed_area)
                             / original_movable_area)
            else:
                inflation = Decimal(1)
            if inflation < 1:
                raise ModelError('Internal error: movable inflation is below one')
            die_aspect = Decimal(str((bounds[2] - bounds[0]) / (bounds[3] - bounds[1])))
            inflated_master = {}
            inflated_geometry = {}
            if inflation > 1:
                for master in sorted(retained_movable_masters):
                    name = 'INF_' + master
                    if name in catalog or name in source_macros or name in inflated_master.values():
                        raise ModelError('Generated inflated master name collision: ' + name)
                    old_w, old_h = geometry[master]
                    width_dbu, height_dbu = grid_size(old_w * old_h * inflation,
                                                      old_w / old_h, site, row, scale)
                    inflated_master[master] = name
                    inflated_geometry[master] = (Decimal(width_dbu) / scale,
                                                 Decimal(height_dbu) / scale)
            for cid, c in clusters.items():
                if c['count'] != len(membership[cid]):
                    raise ModelError('Cluster %d members missing from DEF' % cid)
                c['width_dbu'], c['height_dbu'] = grid_size(
                    c['area'] * inflation, die_aspect, site, row, scale)
                w, h = c['width_dbu'], c['height_dbu']
                if w > bounds[2] - bounds[0] or h > bounds[3] - bounds[1]:
                    raise ModelError('Cluster %d does not fit placement bounding box' % cid)
                if pin_width * scale > w or pin_height * scale > h:
                    raise ModelError('Pin rectangle does not fit cluster %d' % cid)
                c['position'] = None
                if args.placement == 'centroid' and c['placed'] == c['count']:
                    x = c['cx'] / float(c['area']) - w / 2
                    y = c['cy'] / float(c['area']) - h / 2
                    nx = max(0, min(round((x - bounds[0]) / site), math.floor((bounds[2] - bounds[0] - w) / site)))
                    ny = max(0, min(round((y - bounds[1]) / row), math.floor((bounds[3] - bounds[1] - h) / row)))
                    c['position'] = (int(bounds[0] + nx * site), int(bounds[1] + ny * row))
            counts['clusters'] = len(clusters)
            counts['reduced_components'] = counts['original_components'] - counts['removed_members'] + len(clusters)

            @lru_cache(maxsize=65536)
            def master_for(name):
                found = db.execute('SELECT master FROM components WHERE name=?', (name,)).fetchone()
                if not found:
                    raise ModelError('NETS refers to missing component: ' + name)
                return found[0]

            LOG.info('Rewriting DEF nets; preserving net identity and collapsing (cluster, net) endpoints')
            with (result / 'reduced.def').open('w') as dest, \
                 (result / 'cell_mapping.tsv').open('w') as cell_file, \
                 (result / 'pin_mapping.tsv').open('w') as pin_file, \
                 (result / 'net_mapping.tsv').open('w') as net_file:
                cw = tsv(cell_file, ['original_cell_name', 'original_master', 'membership_cell_id',
                                    'cluster_id', 'reduced_cell_name', 'reduced_master'])
                pw = tsv(pin_file, ['original_cell_name', 'original_pin_name', 'net_name',
                                   'reduced_cell_name', 'reduced_pin_name'])
                nw = tsv(net_file, ['original_net_name', 'reduced_net_name', 'original_degree',
                                   'reduced_degree', 'wirelength_nontrivial'])
                for kind, section, text, count in events(original):
                    if section == 'COMPONENTS':
                        if kind == 'header':
                            dest.write('COMPONENTS %d ;\n' % counts['reduced_components'])
                        elif kind == 'record':
                            name, master, state, *_ = component(text)
                            cid = assignment.get(name); c = clusters.get(cid)
                            reduced_master = (c['master'] if c else inflated_master.get(master, master)
                                              if state not in ('FIXED', 'COVER') else master)
                            cw.writerow([name, master, member_ids.get(name, ''), cid if c else '',
                                         c['instance'] if c else name, reduced_master])
                            if c is None:
                                dest.write(replace_component_master(text, reduced_master)
                                           if reduced_master != master else text)
                        elif kind == 'footer':
                            for c in clusters.values():
                                dest.write('- %s %s' % (c['instance'], c['master']))
                                if c['position'] is None:
                                    dest.write(' + UNPLACED ;\n')
                                else:
                                    dest.write(' + PLACED ( %d %d ) N ;\n' % c['position'])
                            dest.write('END COMPONENTS\n')
                        else:
                            dest.write(text)
                    elif section == 'NETS' and kind == 'record':
                        net, endpoints, use = endpoint_record(text)
                        if use not in ('SIGNAL', 'CLOCK'):
                            raise ModelError('Unsupported ordinary net USE %s on %s' % (use, net))
                        db.execute('INSERT INTO nets VALUES (?)', (net,))
                        transformed, touched = [], {}
                        for inst, pin in endpoints:
                            db.execute('INSERT INTO endpoints VALUES (?,?)', (inst, pin))
                            if inst == 'PIN':
                                if pin not in ports or ports[pin][0] != net:
                                    raise ModelError('External pin/net mismatch: ' + pin)
                                direction = {'INPUT': 'OUTPUT', 'OUTPUT': 'INPUT', 'INOUT': 'INOUT'}.get(ports[pin][1])
                            else:
                                master = master_for(inst)
                                info = catalog[master].get('pin', {}).get(pin)
                                if info is None:
                                    raise ModelError('Missing LEF pin: %s/%s' % (inst, pin))
                                direction = info.get('direction')
                            if direction not in ('INPUT', 'OUTPUT', 'INOUT', 'OUTPUT TRISTATE'):
                                raise ModelError('Unsupported pin direction: %s/%s' % (inst, pin))
                            cid = assignment.get(inst)
                            if cid is None:
                                reduced = (inst, pin)
                                transformed.append(reduced)
                            else:
                                c = clusters[cid]
                                if cid not in touched:
                                    ordinal = c['pins']; c['pins'] += 1
                                    touched[cid] = (ordinal, set())
                                    transformed.append((c['instance'], 'VP%d' % ordinal))
                                ordinal, directions = touched[cid]
                                directions.add(direction)
                                reduced = (c['instance'], 'VP%d' % ordinal)
                            pw.writerow([inst, pin, net, *reduced])
                        for cid, (ordinal, directions) in touched.items():
                            # A net driven within the cluster remains an OUTPUT,
                            # even when it also has internal sinks. No STA uses this abstract.
                            direction = ('INOUT' if 'INOUT' in directions else 'OUTPUT'
                                         if directions & {'OUTPUT', 'OUTPUT TRISTATE'} else 'INPUT')
                            db.execute('INSERT INTO pins VALUES (?,?,?,?)', (cid, ordinal, net, direction))
                        dest.write('- %s\n' % def_name(net))
                        for inst, pin in transformed:
                            dest.write('  ( %s %s )\n' % (def_name(inst), def_name(pin)))
                        dest.write('  + USE %s ;\n' % use)
                        nw.writerow([net, net, len(endpoints), len(transformed), int(len(transformed) >= 2)])
                        counts['nets'] += 1
                        counts['original_pins'] += len(endpoints)
                        counts['reduced_pins'] += len(transformed)
                        counts['one_endpoint_nets'] += len(transformed) == 1
                        if counts['nets'] % 100000 == 0:
                            LOG.info('Rewrote %d nets', counts['nets'])
                    else:
                        dest.write(text)
            master_for.cache_clear()
            LOG.info('Writing complete placement LEF and automatically sized geometry tables')
            with (result / 'placement.lef').open('w') as stream, \
                 (result / 'cluster_sizes.tsv').open('w') as sizes, \
                 (result / 'inflated_masters.tsv').open('w') as master_sizes:
                sw = tsv(sizes, ['cluster_id', 'instance', 'master', 'member_count', 'member_area_um2',
                               'width', 'height', 'width_dbu', 'height_dbu', 'site_columns', 'row_count',
                                 'area_inflation', 'actual_member_utilization', 'pin_count'])
                mw = tsv(master_sizes, ['original_master', 'inflated_master', 'instance_count',
                                        'original_width', 'original_height', 'inflated_width',
                                        'inflated_height', 'actual_area_ratio'])
                stream.write(lef_preamble)
                if lef_preamble and not lef_preamble.endswith('\n'):
                    stream.write('\n')
                for master in source_master_order:
                    block = ''.join(source_macros[master][1])
                    stream.write(block)
                    if not block.endswith('\n'):
                        stream.write('\n')
                for master in sorted(inflated_master):
                    new_name = inflated_master[master]
                    old_size = geometry[master]
                    new_size = inflated_geometry[master]
                    stream.write(scaled_macro(source_macros[master][1], master, new_name,
                                              old_size, new_size))
                    mw.writerow([master, new_name, retained_movable_master_counts[master],
                                 number(old_size[0]), number(old_size[1]),
                                 number(new_size[0]), number(new_size[1]),
                                 number(new_size[0] * new_size[1] / (old_size[0] * old_size[1]))])
                for cid, c in clusters.items():
                    w, h = Decimal(c['width_dbu']) / scale, Decimal(c['height_dbu']) / scale
                    stream.write('MACRO %s\n  CLASS CORE ;\n  ORIGIN 0 0 ;\n  SIZE %s BY %s ;\n'
                                 '  SYMMETRY X Y ;\n  SITE %s ;\n' %
                                 (c['master'], number(w), number(h), next(iter(row_sites))))
                    rect = [(w-pin_width)/2, (h-pin_height)/2, (w+pin_width)/2, (h+pin_height)/2]
                    for ordinal, net, direction in db.execute(
                            'SELECT ordinal,net,direction FROM pins WHERE cid=? ORDER BY ordinal', (cid,)):
                        name = 'VP%d' % ordinal
                        stream.write('  PIN %s\n    DIRECTION %s ;\n    USE SIGNAL ;\n    PORT\n'
                                     '      LAYER %s ;\n      RECT %s ;\n    END\n  END %s\n' %
                                     (name, direction, layer, ' '.join(map(number, rect)), name))
                    stream.write('END %s\n\n' % c['master'])
                    sw.writerow([cid, c['instance'], c['master'], c['count'], number(c['area']),
                                 number(w), number(h), c['width_dbu'], c['height_dbu'],
                                 c['width_dbu']//site, c['height_dbu']//row,
                                 number(inflation), number(c['area']/(w*h)), c['pins']])
                stream.write('END LIBRARY\n')
            counts['cluster_pins'] = sum(c['pins'] for c in clusters.values())
            inflated_retained_area = sum(
                Decimal(count) * (inflated_geometry.get(master, geometry[master])[0]
                                  * inflated_geometry.get(master, geometry[master])[1])
                for master, count in retained_movable_master_counts.items())
            reduced_movable_area = inflated_retained_area + sum(
                Decimal(c['width_dbu']) * c['height_dbu'] / scale**2 for c in clusters.values())
            report = dict(status='complete', schema=1, model='placement_only_per_cluster_net_pin',
                          counts=dict(counts), target_utilization=float(target_utilization),
                          current_utilization=float(current_utilization),
                          achieved_utilization=float((original_fixed_area + reduced_movable_area)
                                                     / placeable_area),
                          movable_area_inflation=float(inflation),
                          placeable_area_um2=float(placeable_area),
                          placeable_area_source=placeable_source,
                          original_fixed_area_um2=float(original_fixed_area),
                          original_movable_area_um2=float(original_movable_area),
                          reduced_movable_area_um2=float(reduced_movable_area),
                          def_dbu_per_micron=scale, site_width_dbu=site, row_height_dbu=row,
                          pin_layer=layer, pin_width_um=float(pin_width), pin_height_um=float(pin_height),
                          inputs={str(p.resolve()): dict(bytes=p.stat().st_size, mtime_ns=p.stat().st_mtime_ns)
                                  for p in inputs},
                          warnings=['placement.lef is a complete replacement LEF; do not load the source LEFs alongside it.',
                                    'Fixed cells are counted in utilization but are never inflated.',
                                    'Site/row rounding may make achieved utilization exceed the requested target.',
                                    'Placement-only: central cluster pins overlap; no routing/DRC or timing characterization.',
                                    'Pin rectangle size/layer are user supplied; no technology rule validation.',
                                    'Regular NETS routing and attributes other than USE are discarded.',
                                    'One-endpoint internal nets are retained in DEF; existing MakeDB filters them from placement arrays.',
                                    'Existing MakeDB also filters non-SIGNAL nets (including USE CLOCK); no reader policy is changed.',
                                    'Initial positions are not legalized; blockages/regions are not used for centroid placement.',
                                    'Membership constraints are trusted, not recomputed; no reduced Verilog is generated.',
                                    'Name mappings are not final PlaceDB/TimingDB numeric-ID mappings.'])
            if original_fixed_area + reduced_movable_area > placeable_area:
                report['warnings'].append('Generated total cell area exceeds the placeable area; lower target utilization or revise clusters/floorplan.')
            (result / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
            # Reserve target exclusively. Publish completion marker LAST so
            # an interrupted publication cannot be mistaken for a complete run.
            out.mkdir()
            for path in result.iterdir():
                if path.name != 'manifest.json':
                    os.replace(path, out / path.name)
            os.replace(result / 'manifest.json', out / 'manifest.json')
            return report
        except sqlite3.IntegrityError as exc:
            raise ModelError('Duplicate DEF component/net or original pin appears more than once: ' + str(exc)) from exc
        finally:
            db.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--clusters', required=True, help='cell_id/cluster_id table from LeidenCluster')
    p.add_argument('--cell-names', required=True, help='original membership cell_id/cell_name table')
    p.add_argument('--saved-db', required=True, help='MakeDB export with lef_info.json and die_info.json')
    p.add_argument('--die-info', help='override saved-db/die_info.json (raw DEF DBU)')
    p.add_argument('--def-input', required=True, help='original DEF including NETS')
    p.add_argument('--lef-input', required=True, nargs='+',
                   help='source LEF files/directories used to build one complete replacement placement.lef')
    p.add_argument('--utilization', required=True, type=float,
                   help='target design utilization including fixed cells, 0 < U <= 1')
    p.add_argument('--pin-layer', required=True)
    p.add_argument('--pin-width', type=float, default=0.1, help='placeholder rectangle width in microns (default: 0.1)')
    p.add_argument('--pin-height', type=float, help='rectangle height in microns; defaults to pin-width')
    p.add_argument('--placement', choices=['centroid', 'unplaced'], default='centroid')
    p.add_argument('--output', required=True, help='new output directory; never overwritten')
    return p.parse_args(argv)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    try:
        args = parse_args()
        report = generate(args)
        print(json.dumps(report['counts'], indent=2))
        print('Saved placement LEF/DEF and name mappings:', args.output)
        for warning in report['warnings']:
            print('WARNING:', warning)
    except (ModelError, OSError, ValueError, KeyError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
