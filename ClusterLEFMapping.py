"""Build a structural cluster boundary mapping from MakeDB exports.

This does not characterize timing, infer clock domains or certify clustering.
Only the explicit cell_names table defines membership IDs (never JSON order).
"""
import collections
import json
from pathlib import Path
import re

from ReducedLiberty import ModelError, read_membership
from ReducedVerilog import identifier


def object_items(path, chunk_size=1024 * 1024):
    """Stream a top-level JSON object; retain at most one large member value.

    MakeDB's cell/net JSON exports can be hundreds of MB. Decode each value
    individually rather than materializing all nested dictionaries at once.
    """
    decoder = json.JSONDecoder()
    with open(path, encoding='utf-8-sig') as stream:
        buffer, pos, eof = '', 0, False

        def refill():
            nonlocal buffer, pos, eof
            buffer = buffer[pos:]; pos = 0
            data = stream.read(chunk_size)
            buffer += data
            eof = not data

        def space():
            nonlocal pos
            while True:
                while pos < len(buffer) and buffer[pos].isspace():
                    pos += 1
                if pos < len(buffer) or eof:
                    return
                refill()

        def char(expected):
            nonlocal pos
            space()
            if pos >= len(buffer) or buffer[pos] != expected:
                raise ModelError('Malformed JSON object in %s: expected %r' % (path, expected))
            pos += 1

        def value():
            nonlocal pos
            space()
            while True:
                try:
                    result, end = decoder.raw_decode(buffer, pos)
                    # A scalar token may span a read boundary. Objects/strings
                    # are already delimited, but retry all tokens at EOF edge.
                    if end == len(buffer) and not eof:
                        refill(); continue
                    pos = end
                    return result
                except json.JSONDecodeError as exc:
                    if eof:
                        raise ModelError('Invalid/truncated JSON in %s: %s' % (path, exc)) from exc
                    refill()

        char('{'); space()
        if pos < len(buffer) and buffer[pos] == '}':
            pos += 1
        else:
            while True:
                key = value()
                if not isinstance(key, str):
                    raise ModelError('JSON object key must be a string: ' + str(path))
                char(':')
                item = value()
                yield key, item
                space()
                if pos < len(buffer) and buffer[pos] == '}':
                    pos += 1
                    break
                char(',')
        space()
        if pos < len(buffer):
            raise ModelError('Trailing JSON data: ' + str(path))


def input_paths(args):
    root = Path(args.saved_db) if args.saved_db else None
    paths = {}
    for attr, filename in (('cells_info', 'cells_info.json'), ('lef_info', 'lef_info.json'),
                           ('netlist_info', 'netlist_info.json'), ('ext_pin_info', 'ext_pin_info.json')):
        explicit = getattr(args, attr)
        path = Path(explicit) if explicit else root / filename if root else None
        if path is None and attr == 'ext_pin_info':
            continue
        if path is None or not path.is_file():
            raise ModelError('Membership input requires --%s (or --saved-db containing %s): %s' %
                             (attr.replace('_', '-'), filename, path))
        paths[attr] = path
    if not args.cell_names or not Path(args.cell_names).is_file():
        raise ModelError('Membership input requires --cell-names: the original cell_id/cell_name table')
    paths['cell_names'] = Path(args.cell_names)
    return paths


def source_masters(paths):
    """Optional provenance: find explicit MACRO declarations in original LEFs."""
    result = {}
    for path in paths or []:
        path = Path(path).resolve()
        with path.open() as stream:
            for line in stream:
                match = re.fullmatch(r'\s*MACRO\s+(\S+)\s*(?:#[^\n]*)?\s*', line)
                if match:
                    master = match[1]
                    if master in result and result[master] != str(path):
                        raise ModelError('Master occurs in multiple --source-lef files: ' + master)
                    result[master] = str(path)
    return result


def build(path, paths, source_lefs=None):
    membership = read_membership(path, paths['cell_names'])
    assignment = {name: cid for cid, members in membership.items() for _, name in members}
    # Minimal master index, not full cell placements/geometry dictionaries.
    masters = {}
    for name, info in object_items(paths['cells_info']):
        if name in masters or not isinstance(info, dict) or not isinstance(info.get('macro_id'), str):
            raise ModelError('Invalid/duplicate cells_info entry: ' + name)
        masters[name] = info['macro_id']
    missing = assignment.keys() - masters.keys()
    if missing:
        raise ModelError('Selected cell names absent from cells_info (check ID namespace): %s' % sorted(missing)[:8])
    lef = {}
    for name, info in object_items(paths['lef_info']):
        if name in lef or not isinstance(info, dict):
            raise ModelError('Invalid/duplicate lef_info master: ' + name)
        lef[name] = info
    for name in assignment:
        if masters[name] not in lef:
            raise ModelError('LEF master missing for %s: %s' % (name, masters[name]))
    ports = dict(object_items(paths['ext_pin_info'])) if 'ext_pin_info' in paths else {}
    sources = source_masters(source_lefs)
    if source_lefs:
        missing = {masters[name] for name in assignment} - sources.keys()
        if missing:
            raise ModelError('Selected masters missing from --source-lef inputs: %s' % sorted(missing)[:8])
    inputs, outputs = collections.defaultdict(dict), collections.defaultdict(dict)
    touched_cells, seen_nets = set(), set()
    # Validate each selected cell pin is connected only once. Compact integer
    # bitmasks avoid a Python tuple/string entry for every pin in large designs.
    pin_numbers = {master: {pin: i for i, pin in enumerate(info.get('pin', {}))}
                   for master, info in lef.items()}
    connected = collections.defaultdict(int)
    scanned, touched = 0, 0
    for net, info in object_items(paths['netlist_info']):
        scanned += 1
        if not isinstance(info, dict) or not isinstance(info.get('cell_list'), list):
            raise ModelError('Invalid netlist_info entry: ' + net)
        endpoints = []
        for value in info['cell_list']:
            if not isinstance(value, str) or len(value.split()) != 2:
                raise ModelError('Expected "cell_name pin_name" endpoint on ' + net)
            endpoints.append(tuple(value.split()))
        if not any(name in assignment for name, _ in endpoints):
            continue
        if net in seen_nets:
            raise ModelError('Duplicate touched net in netlist_info: ' + net)
        seen_nets.add(net); touched += 1
        identifier(net)
        if len(set(endpoints)) != len(endpoints):
            raise ModelError('Duplicate endpoint on net ' + net)
        use = str(info.get('use', 'SIGNAL')).replace('USE ', '').strip().upper()
        if use in ('POWER', 'GROUND'):
            # Supply pins must not become logical cluster boundary pins.
            continue
        drivers, sinks = [], []
        for name, pin in endpoints:
            if name == 'PIN':
                port = ports.get(pin)
                if not port:
                    raise ModelError('Missing external pin metadata for %s; supply --ext-pin-info' % pin)
                direction = {'INPUT': 'OUTPUT', 'OUTPUT': 'INPUT'}.get(port.get('direction'))
            else:
                master = masters.get(name)
                model = lef.get(master, {}).get('pin', {}).get(pin)
                if model is None:
                    raise ModelError('Missing LEF pin metadata: %s/%s (master=%s, net=%s)' % (name, pin, master, net))
                direction = model.get('direction')
                if name in assignment:
                    bit = 1 << pin_numbers[master][pin]
                    if connected[name] & bit:
                        raise ModelError('Selected pin appears on multiple nets: %s/%s' % (name, pin))
                    connected[name] |= bit
                    touched_cells.add(name)
            if direction == 'OUTPUT':
                drivers.append((name, pin))
            elif direction == 'INPUT':
                sinks.append((name, pin))
            else:
                raise ModelError('Unsupported INOUT/unknown direction on %s: %s/%s' % (net, name, pin))
        if len(drivers) != 1:
            raise ModelError('Expected one driver on %s; found %d' % (net, len(drivers)))
        driver = drivers[0]
        owner = assignment.get(driver[0])
        for name, pin in sinks:
            cid = assignment.get(name)
            if cid is not None and cid != owner:
                inputs[cid].setdefault(net, []).append('%s:%s' % (name, pin))
        if owner is not None and any(assignment.get(name) != owner for name, _ in sinks):
            outputs[owner][net] = '%s:%s' % driver
    missing = assignment.keys() - touched_cells
    if missing:
        raise ModelError('Selected cells have no signal connectivity: %s' % sorted(missing)[:8])
    clusters = {}
    for cid, members in sorted(membership.items()):
        if not inputs[cid] or not outputs[cid]:
            raise ModelError('Cluster %d needs at least one boundary input and output; check membership/netlist' % cid)
        ins = [dict(pin='I%d' % i, net=net, original_pins=sorted(pins))
               for i, (net, pins) in enumerate(sorted(inputs[cid].items()))]
        outs = [dict(pin='O%d' % i, net=net, original_pin=pin)
                for i, (net, pin) in enumerate(sorted(outputs[cid].items()))]
        item = dict(cluster_id=cid, liberty_cell='TC_%d' % cid,
            model='physical_connectivity_only', timing_characterized=False,
            members=[dict(cell_id=node, cell_name=name, original_master=masters[name],
                          source_lef=sources.get(masters[name])) for node, name in members],
            inputs=ins, outputs=outs)
        for member in item['members']:
            identifier(member['cell_name'])
        pins = {p['pin']: p['net'] for p in ins + outs}
        clusters[cid] = dict(master=item['liberty_cell'], pins=pins, nets=set(pins.values()), source=item)
    return clusters, dict(scanned_nets=scanned, touched_nets=touched,
        selected_cells=len(assignment), clusters=len(clusters),
        input_paths={key: str(value.resolve()) for key, value in paths.items()},
        source_lefs=[str(Path(p).resolve()) for p in source_lefs or []],
        note='Structural boundaries only; no Liberty characterization, sequential/clock-domain or convexity validation')
