#!/usr/bin/env python3
"""Build a placement-only reduced DEF, preserving the original physical floorplan.

Components are replaced using the characterized mapping and actual reduced
Verilog instance names. NETS is rebuilt (old routing is intentionally discarded).
Large reduced connectivity is staged in a temporary SQLite DB, not a Python
dictionary of every net and endpoint. No original input is modified.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

from ReducedLiberty import IDENT, ModelError, parse_statement, verilog_statements
from ReducedVerilog import declaration, load_mapping, port_nets


SECTIONS = {'COMPONENTS', 'PINS', 'NETS', 'SPECIALNETS', 'GROUPS', 'REGIONS',
            'BLOCKAGES', 'VIAS', 'NONDEFAULTRULES', 'SCANCHAINS', 'PINPROPERTIES',
            'SLOTS', 'FILLS', 'STYLES', 'CONSTRAINTS', 'ASSERTIONS', 'IOTIMINGS'}
TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|#[^\n]*|(?:\\.|[^\s();"#])+|[();]')


def lex(text):
    end = 0
    for match in TOKEN.finditer(text):
        if text[end:match.start()].strip():
            raise ModelError('Unsupported DEF syntax (including multiline strings) near ' + text[end:end + 80])
        end = match.end()
        if not match.group().startswith('#'):
            yield match
    if text[end:].strip():
        raise ModelError('Unsupported DEF syntax near ' + text[end:end + 80])


def tokens(text):
    return [m.group() for m in lex(text)]


def vname(name):
    return name[1:] if name.startswith('\\') else name


def dname(name):
    return re.sub(r'\\(.)', r'\1', name)


def def_name(name):
    # Normal hierarchy and bus names need no extra escaping in DEF. Reject
    # special characters unsupported by the current MakeDB reader as well.
    if not re.fullmatch(r'[A-Za-z0-9_$./:\[\]-]+', name) or name in ('-', '+'):
        raise ModelError('Unsupported DEF identifier: %r' % name)
    return name


def events(path):
    """Stream raw text and semicolon-delimited section records with counts checked."""
    section = None
    parts, active, expected, count = [], False, 0, 0
    with open(path, newline='') as stream:
        for line in stream:
            word = line.strip().split(None, 1)[0] if line.strip() else ''
            if section is None:
                if word in SECTIONS:
                    match = re.fullmatch(r'\s*(\w+)\s+(\d+)\s*;\s*(?:#[^\n]*)?\s*', line)
                    if not match:
                        raise ModelError('Expected a standalone DEF section header: ' + line[:100])
                    section, expected = match[1], int(match[2])
                    parts, active, count = [], False, 0
                    yield 'header', section, line, expected
                else:
                    yield 'raw', None, line, None
                continue
            if re.fullmatch(r'\s*END\s+' + section + r'\s*(?:#[^\n]*)?\s*', line):
                if active:
                    raise ModelError('Unterminated record in ' + section)
                if count != expected:
                    raise ModelError('%s count mismatch: header=%d records=%d' % (section, expected, count))
                if parts:
                    yield 'raw', section, ''.join(parts), None
                yield 'footer', section, line, count
                section = None
                continue
            offset = 0
            matches = list(lex(line))
            for match in matches:
                if not active:
                    if match.group() != '-':
                        raise ModelError('Expected DEF record in %s: %s' % (section, line[:100]))
                    active = True
                if match.group() == ';':
                    parts.append(line[offset:match.end()])
                    count += 1
                    yield 'record', section, ''.join(parts), None
                    parts, active = [], False
                    offset = match.end()
            parts.append(line[offset:])
    if section is not None:
        raise ModelError('Missing END ' + section)


def option(ts, name):
    for i in range(len(ts) - 2):
        if ts[i:i + 2] == ['+', name]:
            return ts[i + 2]
    return None


def component(record):
    ts = tokens(record)
    if len(ts) < 4 or ts[0] != '-':
        raise ModelError('Malformed COMPONENT record')
    name, master = dname(ts[1]), dname(ts[2])
    state, x, y = 'UNPLACED', None, None
    for i in range(3, len(ts) - 1):
        if ts[i] == '+' and ts[i + 1] in ('PLACED', 'FIXED', 'COVER', 'UNPLACED'):
            state = ts[i + 1]
            if state != 'UNPLACED':
                if len(ts) <= i + 6 or ts[i + 2] != '(' or ts[i + 5] != ')':
                    raise ModelError('Unsupported placement syntax for ' + name)
                x, y = int(ts[i + 3]), int(ts[i + 4])
            break
    region = option(ts, 'REGION')
    if region == '(':
        raise ModelError('Inline component REGION boxes are unsupported; use named regions')
    return name, master, state, x, y, dname(region) if region else None


def connection_db(path, clusters, membership, directory):
    db = sqlite3.connect(str(Path(directory) / 'connectivity.sqlite'))
    db.executescript('''
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-32768;
        CREATE TABLE instances(name TEXT PRIMARY KEY, master TEXT, cid INTEGER, found INTEGER DEFAULT 0);
        CREATE TABLE nets(id INTEGER PRIMARY KEY, name TEXT UNIQUE, use TEXT DEFAULT 'SIGNAL');
        CREATE TABLE endpoints(net INTEGER, instance TEXT, pin TEXT, PRIMARY KEY(instance,pin));
        CREATE TABLE ports(name TEXT PRIMARY KEY, direction TEXT, found INTEGER DEFAULT 0);
        CREATE TABLE oldnets(name TEXT PRIMARY KEY);
        CREATE TABLE specialnets(name TEXT PRIMARY KEY);
        CREATE TABLE physical(name TEXT PRIMARY KEY);
    ''')
    by_master = {vname(c['master']): cid for cid, c in clusters.items()}
    cluster_instances, ports_declared, formal = {}, set(), None
    module_name = None

    def connect(net, instance, pin):
        for name in (net, pin):
            def_name(name)
        db.execute('INSERT OR IGNORE INTO nets(name) VALUES (?)', (net,))
        netid = db.execute('SELECT id FROM nets WHERE name=?', (net,)).fetchone()[0]
        db.execute('INSERT INTO endpoints VALUES (?,?,?)', (netid, instance, pin))

    try:
        for statement in verilog_statements(path):
            if re.match(r'module\s', statement):
                match = re.fullmatch(r'module\s+(' + IDENT + r')\s*(?:\((.*)\))?', statement, re.S)
                if formal is not None or not match:
                    raise ModelError('Requires one flat non-ANSI module')
                module_name = def_name(vname(match[1]))
                formal = {vname(n.strip()) for n in (match[2] or '').split(',') if n.strip()}
                continue
            if formal is None:
                raise ModelError('Missing reduced module declaration')
            decl = declaration(statement)
            if decl:
                kind, _, names = decl
                if kind in ('input', 'output'):
                    for name in names:
                        canonical = vname(name)
                        if canonical in ports_declared:
                            raise ModelError('Duplicate port declaration: ' + canonical)
                        ports_declared.add(canonical)
                    for name in port_nets(decl):
                        name = def_name(vname(name))
                        db.execute('INSERT INTO ports(name,direction) VALUES (?,?)', (name, kind.upper()))
                        connect(name, '', name)
                continue
            master, name, pins = parse_statement(statement)
            master, name = def_name(vname(master)), def_name(vname(name))
            if name in membership:
                raise ModelError('Reduced Verilog still contains clustered member: ' + name)
            cid = by_master.get(master)
            if cid is not None:
                actual = {vname(pin): vname(net) for pin, net in pins.items()}
                expected = {vname(pin): vname(net) for pin, net in clusters[cid]['pins'].items()}
                if actual != expected or cid in cluster_instances:
                    raise ModelError('Cluster instance/pins disagree with mapping: ' + name)
                cluster_instances[cid] = name
            db.execute('INSERT INTO instances(name,master,cid) VALUES (?,?,?)', (name, master, cid))
            for pin, net in pins.items():
                connect(vname(net), name, vname(pin))
        if formal is None or formal != ports_declared:
            raise ModelError('Reduced module header and input/output declarations differ')
        if set(cluster_instances) != set(clusters):
            raise ModelError('Reduced Verilog does not contain exactly one instance per mapped cluster')
        db.execute('CREATE INDEX endpoints_net ON endpoints(net)')
        db.commit()
    except Exception:
        db.close()
        raise
    return db, module_name, cluster_instances


def discover_lefs(path):
    """Resolve a single LEF or recursively discover .lef/.LEF files."""
    path = Path(path)
    if path.is_dir():
        files = sorted({p.resolve() for p in path.rglob('*')
                        if p.is_file() and p.suffix.lower() == '.lef'})
        if not files:
            raise ModelError('No LEF files found under directory: ' + str(path))
        return files
    if path.is_file():
        return [path.resolve()]
    raise ModelError('LEF file/directory does not exist: ' + str(path))


def read_lef_catalog(paths):
    """Use MakeDB's reader, but reject duplicate masters instead of overwriting."""
    from ReadLEF import ReadLEFinfo
    reader = ReadLEFinfo([])
    catalog, sources = {}, {}
    declarations = {}
    for path in paths:
        # The underlying reader overwrites repeated MACRO declarations. Catch
        # those even within one file, before any definition can be lost.
        with open(path) as stream:
            for line in stream:
                match = re.fullmatch(r'\s*MACRO\s+(\S+)\s*(?:#[^\n]*)?\s*', line)
                if match:
                    name = match[1]
                    if name in declarations:
                        raise ModelError('Duplicate LEF master %s in %s and %s; select one library variant' %
                                         (name, declarations[name], path))
                    declarations[name] = str(path)
        masters = reader.getMacroInfo(str(path))
        for mid, master in masters.items():
            name = reader.lef_macro_name2index_map[mid]
            if name in catalog:
                raise ModelError('Duplicate LEF master %s in %s and %s' % (name, sources[name], path))
            pins = {reader.lef_pin_name2index_map[p.get_name()]: p.get_direction() for p in master.get_pins()}
            # These are synthesized/exposed by ReadLEF, not ordinary LEF pins.
            pins.pop('VP', None); pins.pop('OBS', None)
            catalog[name] = dict(width=master.get_width(), height=master.get_height(), pins=pins)
            sources[name] = str(path)
    return catalog, sources


def cluster_geometry(catalog, clusters):
    result = {}
    for cid, cluster in clusters.items():
        name = vname(cluster['master'])
        if name not in catalog:
            raise ModelError('Cluster master missing from --cluster-lef: ' + name)
        master = catalog[name]
        w, h = master['width'], master['height']
        if not all(v is not None and math.isfinite(v) and v > 0 for v in (w, h)):
            raise ModelError('Invalid cluster LEF size: ' + name)
        actual = master['pins']
        if set(actual) != set(cluster['pins']):
            raise ModelError('Cluster LEF pin names disagree with mapping: ' + name)
        for side, direction in (('inputs', 'INPUT'), ('outputs', 'OUTPUT')):
            for pin in cluster['source'][side]:
                if actual[pin['pin']] != direction:
                    raise ModelError('Cluster LEF pin direction mismatch: ' + name + '/' + pin['pin'])
        result[cid] = (w, h)
    return result


def read_positions(path, clusters):
    from ReducedLiberty import table_rows
    result = {}
    if not path:
        return result
    for cid, x, y, orient in table_rows(path, ('cluster_id', 'x', 'y', 'orient')):
        cid, x, y = int(cid), int(x), int(y)
        if cid in result or cid not in clusters or orient not in ('N', 'S', 'FN', 'FS', 'E', 'W', 'FE', 'FW'):
            raise ModelError('Invalid/duplicate cluster position: %s' % cid)
        result[cid] = (x, y, orient)
    if set(result) != set(clusters):
        raise ModelError('--positions must provide every mapped cluster (DEF DBU coordinates)')
    return result


def inspect_def(path, db, clusters, membership, module, cluster_instances, drop_specialnets, drop_groups,
                retained_catalog=None):
    state = {cid: dict(count=0, placed=0, x=0, y=0, regions=set()) for cid in clusters}
    headers, design, units, die, end_design = {}, None, None, None, 0
    physical_count = removed = 0
    pg_ports = []
    unsupported = []
    cluster_instance_names = set(cluster_instances.values())
    expected_masters = {vname(m['cell_name']): vname(m['original_master'])
                        for c in clusters.values() for m in c['source']['members'] if 'original_master' in m}
    for kind, section, text, count in events(path):
        if kind == 'header':
            if section in headers:
                raise ModelError('Duplicate DEF section: ' + section)
            headers[section] = count
            if section == 'GROUPS' and count and not drop_groups:
                raise ModelError('Nonempty GROUPS requires explicit --drop-groups for this placement-only converter')
        elif kind == 'record':
            if section == 'COMPONENTS':
                name, master, placement, x, y, region = component(text)
                db.execute('INSERT INTO physical VALUES (?)', (name,))
                physical_count += 1
                if name in cluster_instance_names:
                    raise ModelError('Cluster instance collides with an original DEF component: ' + name)
                if name in membership:
                    cid = membership[name]
                    if placement in ('FIXED', 'COVER'):
                        raise ModelError('Cannot replace a fixed/cover member: ' + name)
                    if name in expected_masters and master != expected_masters[name]:
                        raise ModelError('DEF member master differs from mapping: ' + name)
                    s = state[cid]; s['count'] += 1; s['regions'].add(region)
                    if x is not None:
                        s['placed'] += 1; s['x'] += x; s['y'] += y
                    removed += 1
                else:
                    if retained_catalog is not None and master not in retained_catalog:
                        raise ModelError('Retained DEF component %s has no LEF master %s in the input directory' % (name, master))
                    actual = db.execute('SELECT master,cid FROM instances WHERE name=?', (name,)).fetchone()
                    if actual:
                        if actual[0] != master or actual[1] is not None:
                            raise ModelError('Retained DEF/Verilog master mismatch: ' + name)
                        db.execute('UPDATE instances SET found=1 WHERE name=?', (name,))
            elif section == 'PINS':
                ts = tokens(text); name = dname(ts[1]); net = option(ts, 'NET')
                use = option(ts, 'USE') or 'SIGNAL'
                actual = db.execute('SELECT direction,found FROM ports WHERE name=?', (name,)).fetchone()
                if actual:
                    if actual[1] or not net or dname(net) != name or option(ts, 'DIRECTION') != actual[0]:
                        raise ModelError('DEF PINS/reduced top port mismatch: ' + name)
                    db.execute('UPDATE ports SET found=1 WHERE name=?', (name,))
                elif use in ('POWER', 'GROUND') and net:
                    pg_ports.append((name, dname(net)))
                else:
                    raise ModelError('DEF pin missing from reduced Verilog ports: ' + name)
            elif section == 'NETS':
                ts = tokens(text); name = dname(ts[1]); use = option(ts, 'USE') or 'SIGNAL'
                if use not in ('SIGNAL', 'CLOCK', 'POWER', 'GROUND', 'RESET', 'ANALOG', 'SCAN', 'TIEOFF'):
                    raise ModelError('Unsupported net USE: ' + use)
                db.execute('INSERT INTO oldnets VALUES (?)', (name,))
                if use in ('POWER', 'GROUND'):
                    raise ModelError('POWER/GROUND in regular NETS is unsupported; use a placement-only DEF')
                # Before the first '+' only endpoint pairs are legal. Check
                # retained endpoints instead of silently disconnecting DEF-only
                # signal cells or accepting a differently named logical net.
                for i in range(2, len(ts)):
                    if ts[i] == '+':
                        break
                    if ts[i] == '(' and i + 3 < len(ts) and ts[i + 3] == ')':
                        inst, pin = dname(ts[i + 1]), dname(ts[i + 2])
                        if inst in membership:
                            continue
                        inst = '' if inst == 'PIN' else inst
                        actual = db.execute('SELECT nets.name FROM endpoints JOIN nets '
                                            'ON nets.id=endpoints.net WHERE instance=? AND pin=?',
                                            (inst, pin)).fetchone()
                        if not actual or actual[0] != name:
                            raise ModelError('Retained DEF endpoint differs from reduced Verilog: %s/%s on %s' % (inst or 'PIN', pin, name))
                db.execute('UPDATE nets SET use=? WHERE name=?', (use, name))
            elif section == 'SPECIALNETS':
                ts = tokens(text)
                if not drop_specialnets:
                    db.execute('INSERT INTO specialnets VALUES (?)', (dname(ts[1]),))
                    for i in range(len(ts) - 3):
                        if ts[i] == '(' and ts[i + 3] == ')':
                            inst = dname(ts[i + 1])
                            if inst in membership or inst == '*':
                                raise ModelError('SPECIALNETS references replaced cells/wildcards; '
                                                 'use --drop-specialnets only for a placement-only output')
            elif section == 'GROUPS' and drop_groups:
                pass
            elif section not in ('REGIONS', 'VIAS', 'NONDEFAULTRULES', 'STYLES'):
                # Check after old NETS has been read; tokens that refer to removed
                # cells/nets must not survive in scan/constraint/blockage records.
                unsupported.append((section, [dname(t) for t in tokens(text)]))
        elif kind == 'raw' and section is None:
            ts = tokens(text)
            if ts[:1] == ['DESIGN']:
                if design is not None:
                    raise ModelError('Duplicate DESIGN')
                design = dname(ts[1])
            elif ts[:3] == ['UNITS', 'DISTANCE', 'MICRONS']:
                units = int(ts[3])
            elif ts[:1] == ['DIEAREA']:
                coords = [int(t) for t in ts[1:] if t not in ('(', ')', ';')]
                if len(coords) < 4 or len(coords) % 2:
                    raise ModelError('Invalid DIEAREA')
                die = min(coords[0::2]), min(coords[1::2]), max(coords[0::2]), max(coords[1::2])
            elif ts == ['END', 'DESIGN']:
                end_design += 1
    if design != module or units is None or units <= 0 or die is None or end_design != 1 or 'COMPONENTS' not in headers:
        raise ModelError('DEF DESIGN/UNITS/DIEAREA/COMPONENTS/END DESIGN invalid or module name differs')
    for cid, s in state.items():
        if s['count'] != len(clusters[cid]['source']['members']):
            raise ModelError('Mapped members missing from DEF for cluster %d' % cid)
        if len(s['regions']) > 1:
            raise ModelError('Cluster %d crosses named component REGION constraints' % cid)
    missing = db.execute('SELECT name FROM instances WHERE cid IS NULL AND found=0 LIMIT 8').fetchall()
    if missing:
        raise ModelError('Reduced instances missing from original DEF: %s' % missing)
    missing = db.execute('SELECT name FROM ports WHERE found=0 LIMIT 8').fetchall()
    if missing:
        raise ModelError('Reduced ports missing from original DEF: %s' % missing)
    if retained_catalog is not None:
        # Validate unique master/pin combinations, not each of millions of
        # individual connections in Python. Physical-only cells were checked
        # above even though they are not present in the reduced Verilog.
        for master, pin in db.execute('SELECT DISTINCT instances.master,endpoints.pin FROM instances '
                                      'JOIN endpoints ON instances.name=endpoints.instance WHERE instances.cid IS NULL'):
            if pin not in retained_catalog[master]['pins']:
                raise ModelError('Retained Verilog pin missing from LEF: %s/%s' % (master, pin))
    for name, net in pg_ports:
        if drop_specialnets or not db.execute('SELECT 1 FROM specialnets WHERE name=?', (net,)).fetchone():
            raise ModelError('Power/ground pin %s would lose its net %s; provide a placement-only input' % (name, net))
    for section, ts in unsupported:
        for token in ts:
            if token in membership:
                raise ModelError('%s references removed member %s' % (section, token))
            if db.execute('SELECT 1 FROM oldnets WHERE name=? AND NOT EXISTS '
                          '(SELECT 1 FROM nets WHERE nets.name=oldnets.name)', (token,)).fetchone():
                raise ModelError('%s references removed net %s' % (section, token))
    # Reduced ordinary nets must not collide with independently preserved power nets.
    if db.execute('SELECT 1 FROM nets JOIN specialnets USING(name) LIMIT 1').fetchone():
        raise ModelError('A reduced regular net also appears in preserved SPECIALNETS')
    db.commit()
    return state, headers, units, die, physical_count, removed


def make_clusters(clusters, instances, geometry, state, units, die, positions, placement):
    records, report = [], []
    xl, yl, xh, yh = die
    for cid in sorted(clusters):
        w, h = (v * units for v in geometry[cid])
        s = state[cid]
        orient, x, y, clipped = 'N', None, None, False
        if cid in positions:
            x, y, orient = positions[cid]
        elif placement == 'centroid' and s['placed'] == s['count']:
            x = round(s['x'] / s['count'] - w / 2)
            y = round(s['y'] / s['count'] - h / 2)
        bw, bh = (h, w) if orient in ('W', 'E', 'FW', 'FE') else (w, h)
        if bw > xh - xl or bh > yh - yl:
            raise ModelError('Cluster %d is larger than the die bounding box' % cid)
        if x is not None:
            if cid in positions:
                if x < xl or y < yl or x + bw > xh or y + bh > yh:
                    raise ModelError('Explicit cluster position is outside die bounding box: %d' % cid)
            else:
                nx, ny = max(xl, min(x, math.floor(xh - bw))), max(yl, min(y, math.floor(yh - bh)))
                clipped = (nx, ny) != (x, y); x, y = nx, ny
        region = next(iter(s['regions']))
        record = '- %s %s' % (def_name(instances[cid]), def_name(vname(clusters[cid]['master'])))
        record += (' + PLACED ( %d %d ) %s' % (x, y, orient)) if x is not None else ' + UNPLACED'
        if region:
            record += ' + REGION ' + def_name(region)
        records.append(record + ' ;\n')
        report.append(dict(cluster_id=cid, instance=instances[cid], master=vname(clusters[cid]['master']),
                           members=s['count'], x=x, y=y, orient=orient, region=region, clipped_to_die_bbox=clipped))
    return records, report


def write_nets(stream, db):
    total = db.execute('SELECT COUNT(*) FROM nets').fetchone()[0]
    stream.write('NETS %d ;\n' % total)
    for netid, name, use in db.execute('SELECT id,name,use FROM nets ORDER BY id'):
        stream.write('- %s\n' % def_name(name))
        for instance, pin in db.execute('SELECT instance,pin FROM endpoints WHERE net=? ORDER BY rowid', (netid,)):
            stream.write('  ( %s %s )\n' % (def_name(instance) if instance else 'PIN', def_name(pin)))
        stream.write('  + USE %s ;\n' % use)
    stream.write('END NETS\n')


def generate(args):
    original, verilog, mapping, lef, output = map(Path, (args.input, args.verilog, args.mapping, args.cluster_lef, args.output))
    lef_files = discover_lefs(lef)
    validate_retained = lef.is_dir()
    summary = Path(str(output) + '.summary.json')
    inputs = [original, verilog, mapping, lef, *lef_files, mapping.parent / 'manifest.json']
    if args.positions:
        inputs.append(Path(args.positions))
    for target in (output, summary):
        if os.path.lexists(target) or target.resolve() in {p.resolve() for p in inputs}:
            raise ModelError('Output exists or overlaps input: ' + str(target))
    manifest = mapping.parent / 'manifest.json'
    if manifest.exists() and json.loads(manifest.read_text()).get('status') != 'complete':
        raise ModelError('Refusing incomplete reduced Liberty mapping')
    clusters, members = load_mapping(mapping)
    membership = {vname(name): cid for name, cid in members.items()}
    if len(membership) != len(members):
        raise ModelError('Member names collide after Verilog escape normalization')
    catalog, lef_sources = read_lef_catalog(lef_files)
    geometry = cluster_geometry(catalog, clusters)
    positions = read_positions(args.positions, clusters)
    with tempfile.TemporaryDirectory(prefix='reduced-def-', dir=args.temp_dir) as scratch:
        db, module, instances = connection_db(verilog, clusters, membership, scratch)
        try:
            state, headers, units, die, count, removed = inspect_def(original, db, clusters, membership,
                module, instances, args.drop_specialnets, args.drop_groups,
                catalog if validate_retained else None)
            records, cluster_report = make_clusters(clusters, instances, geometry, state, units, die,
                                                    positions, args.placement)
            final_count = count - removed + len(clusters)
            report = dict(original=str(original.resolve()), reduced_verilog=str(verilog.resolve()),
                mapping=str(mapping.resolve()), cluster_lef=str(lef.resolve()), def_dbu_per_micron=units,
                lef_files=[str(p) for p in lef_files], lef_master_sources=lef_sources,
                retained_lef_validated=validate_retained,
                original_components=count, removed_members=removed, reduced_components=final_count,
                reduced_nets=db.execute('SELECT COUNT(*) FROM nets').fetchone()[0],
                original_nets=headers.get('NETS', 0), placement=args.placement, clusters=cluster_report,
                dropped_specialnets=headers.get('SPECIALNETS', 0) if args.drop_specialnets else 0,
                dropped_groups=headers.get('GROUPS', 0) if args.drop_groups else 0,
                limitations=['Placement-only: all regular-net routing/attributes except USE discarded',
                    'Unconnected DEF-only physical components retained',
                    'Default centroid uses member DEF reference points, not area-weighted cell centers',
                    'Initial placement is not legalized; only die bounding-box limits checked',
                    'No SDC/SPEF remapping, power reconnection, or GROUPS remapping'])
            output.parent.mkdir(parents=True, exist_ok=True)
            # Publish only a complete DEF. Hard-link publication is exclusive and
            # cannot overwrite a file created by another process after validation.
            with tempfile.NamedTemporaryFile(mode='w', dir=output.parent, prefix='.reduced-def-', delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    inserted_nets = False
                    for kind, section, text, _ in events(original):
                        if section == 'COMPONENTS':
                            if kind == 'header':
                                stream.write('COMPONENTS %d ;\n' % final_count)
                            elif kind == 'record':
                                if component(text)[0] not in membership:
                                    stream.write(text)
                            elif kind == 'footer':
                                stream.write('\n' + ''.join(records) + 'END COMPONENTS\n')
                            else:
                                stream.write(text)
                        elif section == 'NETS':
                            if kind == 'header':
                                write_nets(stream, db); inserted_nets = True
                        elif (section == 'SPECIALNETS' and args.drop_specialnets) or (section == 'GROUPS' and args.drop_groups):
                            continue
                        else:
                            if kind == 'raw' and section is None and tokens(text) == ['END', 'DESIGN'] and not inserted_nets:
                                write_nets(stream, db); inserted_nets = True
                            stream.write(text)
                    stream.flush()
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                os.link(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
            with summary.open('x') as stream:
                json.dump(report, stream, indent=2, allow_nan=False); stream.write('\n')
        finally:
            db.close()
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', '--def', dest='input', required=True, help='original DEF')
    parser.add_argument('--verilog', required=True, help='reduced Verilog from ReducedVerilog.py')
    parser.add_argument('--mapping', required=True, help='cluster_mapping.jsonl from ReducedLiberty.py')
    parser.add_argument('--cluster-lef', '--lef-dir', dest='cluster_lef', required=True,
                        help='single cluster LEF (legacy) OR directory of cluster and original LEFs; '
                             'directories are scanned recursively and retained masters/pins are validated')
    parser.add_argument('--output', required=True, help='new reduced DEF (+ FILE.summary.json)')
    parser.add_argument('--placement', choices=('centroid', 'unplaced'), default='centroid')
    parser.add_argument('--positions', help='optional cluster_id x y orient table; x/y are integer DEF DBU')
    parser.add_argument('--drop-specialnets', action='store_true', help='explicitly remove SPECIALNETS for placement-only use')
    parser.add_argument('--drop-groups', action='store_true', help='explicitly remove GROUPS constraints for placement-only use')
    parser.add_argument('--temp-dir', help='temporary SQLite directory (default: system temporary directory)')
    return parser.parse_args(argv)


def main(argv=None):
    try:
        args = parse_args(argv)
        report = generate(args)
    except (ModelError, OSError, ValueError, KeyError, IndexError, sqlite3.Error) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1
    print('Saved %d components / %d nets: %s' % (report['reduced_components'], report['reduced_nets'], args.output))
    print('WARNING: placement-only DEF; regular-net routing was discarded and positions are not legalized.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
