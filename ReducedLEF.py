#!/usr/bin/env python3
"""Placement-only cluster LEF with coincident central boundary pin rectangles.

Uses a characterized cluster_mapping.jsonl, or a cell_id/cluster_id table plus
MakeDB exports to derive structural boundary pins. This is NOT a routable macro
abstract and does not add internal wire RC or characterize timing.
"""
import argparse
import csv
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import sys

from ReducedLiberty import ModelError
from ReducedVerilog import load_mapping


def positive(value, label):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ModelError('%s must be a positive finite number' % label)
    if not math.isfinite(number) or number <= 0:
        raise ModelError('%s must be a positive finite number' % label)
    return number


def lef_name(value):
    # Generated TC_*/I*/O* names are simple. Fail instead of silently renaming
    # escaped identifiers and breaking agreement with Liberty/Verilog.
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', value):
        raise ModelError('Unsupported LEF identifier: %r (use generated TC_*/I*/O* names)' % value)
    return value


def number(value):
    return format(Decimal(str(value)), 'f')


def read_sizes(path, delimiter='tab', unit='micron', dbu_per_micron=None):
    scale = 1.0
    if unit == 'dbu':
        scale = positive(dbu_per_micron, '--dbu-per-micron for DBU sizes')
    elif dbu_per_micron is not None:
        raise ModelError('--dbu-per-micron requires --size-unit dbu')
    separators = {'tab': '\t', 'comma': ',', 'space': None}
    if delimiter not in separators:
        raise ModelError('Unsupported size delimiter: ' + delimiter)
    result, indices = {}, None
    with open(path, encoding='utf-8-sig', newline='') as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            row = (line.split() if separators[delimiter] is None
                   else next(csv.reader([line], delimiter=separators[delimiter])))
            row = [v.strip() for v in row]
            if indices is None:
                columns = ('cluster_id', 'width', 'height')
                if all(c in row for c in columns):
                    if len(row) != len(set(row)):
                        raise ModelError('Duplicate size-table columns')
                    indices = [row.index(c) for c in columns]
                    continue
                indices = [0, 1, 2]
            try:
                cid_text, width, height = [row[i] for i in indices]
                if not re.fullmatch(r'[0-9]+', cid_text):
                    raise ValueError('cluster_id must be a nonnegative integer')
                cid = int(cid_text)
                if cid in result:
                    raise ValueError('duplicate cluster_id %d' % cid)
                result[cid] = (positive(positive(width, 'width') / scale, 'converted width'),
                               positive(positive(height, 'height') / scale, 'converted height'))
            except (ValueError, IndexError) as exc:
                raise ModelError('%s:%d: %s' % (path, line_no, exc)) from exc
    if not result:
        raise ModelError('Empty cluster size table')
    return result


def technology_layer(paths, layer):
    """Read top-level plain LEF blocks; skip macros and quoted properties.

    This is a small technology metadata reader, not a replacement for ReadLEF.
    It does not attempt to interpret LEF58 property strings or routing rules.
    """
    candidates, units, masters = [], set(), set()
    token_re = re.compile(r'"(?:\\.|[^"\\])*"|#[^\n]*|;|[^\s;"#]+')
    named = {'LAYER', 'MACRO', 'SITE', 'VIA', 'VIARULE', 'NONDEFAULTRULE', 'ARRAY'}
    for path in paths:
        tokens = [m.group() for m in token_re.finditer(Path(path).read_text())
                  if not m.group().startswith('#')]
        i = 0
        while i < len(tokens):
            kind = tokens[i]
            if kind == 'END':
                i += 2
                continue
            if kind in named or kind in ('UNITS', 'PROPERTYDEFINITIONS', 'BEGINEXT'):
                if kind == 'BEGINEXT':
                    try:
                        i = tokens.index('ENDEXT', i + 1) + 1
                    except ValueError:
                        raise ModelError('Unterminated BEGINEXT in ' + str(path))
                    continue
                name = tokens[i + 1] if kind in named else kind
                body_start = i + (2 if kind in named else 1)
                j = body_start
                while j + 1 < len(tokens) and tokens[j:j + 2] != ['END', name]:
                    j += 1
                if j + 1 >= len(tokens):
                    raise ModelError('Unterminated %s %s in %s' % (kind, name, path))
                body = tokens[body_start:j]
                if kind == 'MACRO':
                    masters.add(name)
                elif kind == 'UNITS':
                    for k in range(len(body) - 2):
                        if body[k:k + 2] == ['DATABASE', 'MICRONS']:
                            units.add(int(body[k + 2]))
                elif kind == 'LAYER' and name == layer:
                    attrs = {}
                    # Only whole statements can define these attributes;
                    # quoted property values never become parser keywords.
                    start = 0
                    for k, token in enumerate(body):
                        if token == ';':
                            statement = body[start:k]
                            if len(statement) >= 2 and statement[0] in ('TYPE', 'WIDTH', 'MINWIDTH'):
                                attrs[statement[0]] = statement[1]
                            start = k + 1
                    candidates.append(attrs)
                i = j + 2
            else:
                # Top-level VERSION / BUSBITCHARS / MANUFACTURINGGRID etc.
                try:
                    i = tokens.index(';', i) + 1
                except ValueError:
                    raise ModelError('Unsupported/incomplete technology statement in ' + str(path))
    if not candidates:
        raise ModelError('Layer %s is not defined in --tech-lef inputs' % layer)
    if any(c != candidates[0] for c in candidates):
        raise ModelError('Conflicting technology definitions of layer ' + layer)
    attrs = candidates[0]
    if attrs.get('TYPE') != 'ROUTING':
        raise ModelError('Layer %s must have TYPE ROUTING' % layer)
    if len(units) > 1:
        raise ModelError('Technology LEFs disagree on DATABASE MICRONS')
    dbu = next(iter(units), 1000)
    if dbu not in {100, 200, 1000, 2000, 10000, 20000}:
        raise ModelError('Unsupported LEF DATABASE MICRONS value: %s' % dbu)
    return attrs, dbu, masters


def generate(args):
    mapping, sizes_path = Path(args.input), Path(args.sizes)
    input_format = args.input_format
    if input_format == 'auto':
        with mapping.open(encoding='utf-8-sig') as stream:
            first = next((line.strip() for line in stream if line.strip() and not line.lstrip().startswith('#')), '')
        input_format = 'mapping' if first.startswith('{') else 'membership'
    membership_paths = {}
    if input_format == 'membership':
        from ClusterLEFMapping import input_paths
        membership_paths = input_paths(args)
    tech_paths = [Path(p) for p in (args.tech_lef or [])]
    output = Path(args.output)
    summary_path = Path(str(output) + '.summary.json')
    mapping_output = Path(str(output) + '.mapping.jsonl')
    cells_output = Path(str(output) + '.cells.tsv')
    targets = (output, summary_path, mapping_output, cells_output) if input_format == 'membership' else (output, summary_path)
    inputs = [mapping, sizes_path, *tech_paths, mapping.parent / 'manifest.json',
              *membership_paths.values(), *(Path(p) for p in args.source_lef or [])]
    if len({p.resolve() for p in targets}) != len(targets) or any(
            p.resolve() in {s.resolve() for s in inputs} for p in targets):
        raise ModelError('Input/output paths must be distinct')
    if any(os.path.lexists(p) for p in targets):
        raise ModelError('LEF output/summary already exists; choose a new output path')
    manifest = mapping.parent / 'manifest.json'
    if input_format == 'mapping' and manifest.exists() and json.loads(manifest.read_text()).get('status') != 'complete':
        raise ModelError('Refusing incomplete Liberty generation: manifest is not complete')
    membership_report = None
    if input_format == 'membership':
        from ClusterLEFMapping import build
        print('Reading membership IDs, cell names and saved MakeDB connectivity...', flush=True)
        clusters, membership_report = build(mapping, membership_paths, args.source_lef)
    else:
        clusters, _ = load_mapping(mapping)
    sizes = read_sizes(sizes_path, args.delimiter, args.size_unit, args.dbu_per_micron)
    if set(sizes) != set(clusters):
        raise ModelError('Cluster IDs differ: missing sizes=%s; extra sizes=%s' % (
            sorted(set(clusters) - set(sizes))[:10], sorted(set(sizes) - set(clusters))[:10]))
    layer = lef_name(args.pin_layer)
    warnings = []
    if input_format == 'membership':
        warnings.append('Membership-derived mapping is structural only, not timing-characterized. '
                        'Match its boundary pins/nets to reduced Liberty before STA. '
                        'Sequential, clock-domain and convexity constraints were not checked.')
    if tech_paths:
        attrs, dbu, existing_masters = technology_layer(tech_paths, layer)
        default_width = attrs.get('WIDTH', attrs.get('MINWIDTH'))
    else:
        attrs, dbu, existing_masters = {}, None, set()
        default_width = 0.1
        warnings.append('No --tech-lef: layer existence/type, minimum width and master collisions '
                        'with existing LEFs are not checked. No technology or RC rules are invented.')
        if args.pin_width is None:
            warnings.append('Using placeholder pin width 0.1 um; this is not a process rule. '
                            'Override with --pin-width/--pin-height if needed.')
    pw = positive(args.pin_width if args.pin_width is not None else default_width,
                  'pin width (set --pin-width when technology has no WIDTH)')
    ph = positive(args.pin_height if args.pin_height is not None else pw, 'pin height')
    minimum = positive(attrs['MINWIDTH'], 'layer MINWIDTH') if 'MINWIDTH' in attrs else None
    if minimum is not None and min(pw, ph) < minimum:
        raise ModelError('Pin rectangle is smaller than layer MINWIDTH')
    lines = ['# Placement-only abstract clusters. Coincident pins are NOT routing/DRC safe.',
             '# Internal wire RC is not modeled by this LEF.', 'VERSION 5.8 ;',
             'BUSBITCHARS "[]" ;', 'DIVIDERCHAR "/" ;']
    # Geometry is always in microns. Without technology metadata, do not
    # impose an invented DATABASE MICRONS on the downstream reader.
    if dbu is not None:
        lines += ['UNITS', '  DATABASE MICRONS %d ;' % dbu, 'END UNITS']
    lines += ['# WARNING: ' + message for message in warnings] + ['']
    report = dict(model='center_pin_placement_only', coordinate_unit='micron',
                  mapping=str(mapping.resolve()), sizes=str(sizes_path.resolve()),
                  tech_lefs=[str(p.resolve()) for p in tech_paths], pin_layer=layer,
                  pin_width=pw, pin_height=ph, lef_database_microns=dbu,
                  technology_validated=bool(tech_paths), warnings=warnings, clusters=[])
    report['input_format'] = input_format
    if membership_report is not None:
        report['membership'] = membership_report
        report['mapping_output'] = str(mapping_output.resolve())
        report['cell_table_output'] = str(cells_output.resolve())
    for cid in sorted(clusters):
        cluster = clusters[cid]
        master = lef_name(cluster['master'])
        if master in existing_masters:
            raise ModelError('Cluster master already exists in --tech-lef inputs: ' + master)
        width, height = sizes[cid]
        if pw > width or ph > height:
            raise ModelError('Pin rectangle does not fit cluster %d (%s x %s um)' % (cid, width, height))
        rect = [(width - pw) / 2, (height - ph) / 2, (width + pw) / 2, (height + ph) / 2]
        if not (0 <= rect[0] < rect[2] <= width and 0 <= rect[1] < rect[3] <= height):
            raise ModelError('Degenerate pin rectangle for cluster %d; check coordinate magnitudes' % cid)
        lines += ['MACRO ' + master, '  CLASS BLOCK ;', '  ORIGIN 0 0 ;',
                  '  SIZE %s BY %s ;' % (number(width), number(height)), '  SYMMETRY X Y ;']
        for side, direction in (('inputs', 'INPUT'), ('outputs', 'OUTPUT')):
            for pin in cluster['source'][side]:
                name = lef_name(pin['pin'])
                lines += ['  PIN ' + name, '    DIRECTION %s ;' % direction,
                          '    USE SIGNAL ;', '    PORT', '      LAYER %s ;' % layer,
                          '      RECT %s ;' % ' '.join(map(number, rect)),
                          '    END', '  END ' + name]
        lines += ['END ' + master, '']
        report['clusters'].append(dict(cluster_id=cid, lef_macro=master, width=width, height=height,
            pin_center=[width / 2, height / 2], pin_rect=rect,
            member_count=len(cluster['source']['members']),
            inputs=[p['pin'] for p in cluster['source']['inputs']],
            outputs=[p['pin'] for p in cluster['source']['outputs']]))
    lines += ['END LIBRARY', '']
    report['cluster_count'] = len(clusters)
    report['pin_count'] = sum(len(c['pins']) for c in clusters.values())
    report['limitations'] = ['Overlapping central pins: not routable/DRC-clean',
                            'No internal wire RC, power pins, or routing obstructions',
                            'No manufacturing-grid snapping or detailed routing-rule validation',
                            'Cluster sizes are supplied as-is; no automatic utilization scaling',
                            'Original cell LEFs and a matching reduced DEF are still required',
                            'Downstream readers may require separate technology/layer definitions']
    # Validate the entire model before creating either output; never overwrite.
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as stream:
        stream.write('\n'.join(lines))
    with summary_path.open('x') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    if input_format == 'membership':
        with mapping_output.open('x') as stream:
            for cid in sorted(clusters):
                stream.write(json.dumps(clusters[cid]['source'], allow_nan=False) + '\n')
        with cells_output.open('x', newline='') as stream:
            writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
            writer.writerow(('cell_id', 'cell_name', 'cluster_id', 'macro_id', 'source_lef'))
            for node, name, cid, master, source in sorted(
                    (m['cell_id'], m['cell_name'], cid, m['original_master'], m['source_lef'])
                    for cid, cluster in clusters.items() for m in cluster['source']['members']):
                writer.writerow((node, name, cid, master, source or ''))
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', '--mapping', dest='input', required=True,
                        help='cluster_mapping.jsonl OR cell_id/cluster_id membership table')
    parser.add_argument('--input-format', choices=('auto', 'mapping', 'membership'), default='auto')
    parser.add_argument('--cell-names', help='original cell_id/cell_name table; required for membership input')
    parser.add_argument('--saved-db', help='MakeDB save directory containing cells_info/lef_info/netlist_info/ext_pin_info.json')
    parser.add_argument('--cells-info', help='override saved cells_info.json (cell name -> macro_id)')
    parser.add_argument('--lef-info', help='override saved lef_info.json (master -> pin directions)')
    parser.add_argument('--netlist-info', help='override saved netlist_info.json (net -> endpoints)')
    parser.add_argument('--ext-pin-info', help='override saved ext_pin_info.json (top port directions)')
    parser.add_argument('--source-lef', action='append', help='optional original LEF for actual master/file provenance; repeatable, not technology validation')
    parser.add_argument('--sizes', required=True, help='cluster_id width height table; final cluster dimensions')
    parser.add_argument('--delimiter', choices=('tab', 'comma', 'space'), default='tab', help='size table delimiter (default: tab)')
    parser.add_argument('--size-unit', choices=('micron', 'dbu'), default='micron')
    parser.add_argument('--dbu-per-micron', type=float, help='required for --size-unit dbu; source coordinate units per micron')
    parser.add_argument('--tech-lef', action='append', help='optional LEF containing routing-layer definitions; repeatable; omission skips technology validation')
    parser.add_argument('--pin-layer', required=True, help='existing routing layer; not a new invented layer')
    parser.add_argument('--pin-width', type=float, help='pin rectangle width in microns; default: layer WIDTH/MINWIDTH, or 0.1 without --tech-lef')
    parser.add_argument('--pin-height', type=float, help='pin rectangle height in microns; default: pin width')
    parser.add_argument('--output', required=True, help='new output LEF; also writes FILE.summary.json')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        report = generate(args)
    except (ModelError, OSError, ValueError, IndexError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1
    print('Saved %d clusters / %d pins: %s' % (report['cluster_count'], report['pin_count'], args.output))
    if report['input_format'] == 'membership':
        print('Saved boundary mapping: ' + report['mapping_output'])
        print('Saved cell ID/name/master table: ' + report['cell_table_output'])
    print('WARNING: placement-only abstraction; central pins overlap and are not routable.')
    for warning in report['warnings']:
        print('WARNING: ' + warning, file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
