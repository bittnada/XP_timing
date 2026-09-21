"""XP_shared_memory MakeDB bridge. Physical IDs are never renumbered.

The reference parser lives in adjacent modules, not in the source project.
Numeric snapshots use non-pickled NPY + JSON. Legacy bin_sep snapshots are
read-only imports (their object NPY files must come from a trusted source).
"""
import copy
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace

import numpy as np
from PlacementState import ORIENTS, orient_offsets, text

SCHEMA = 1
FLOATS = '''node_x node_y node_size_x node_size_y pin_offset_x pin_offset_y
net_weights rows flat_region_boxes node_size_x_LEF node_size_y_LEF'''.split()
INTS = '''node2orig_node_map flat_net2pin_map flat_net2pin_start_map
flat_node2pin_map flat_node2pin_start_map pin2node_map pin2net_map
flat_region_boxes_start node2fence_region_map'''.split()
STRINGS = 'node_names pin_names pin_direct net_names node_orient'.split()
SCALARS = '''num_physical_nodes num_terminals num_terminal_NIs num_movable_pins
num_fake_macro num_blockage num_movable_std_cell num_movable_macro
num_fixed_std_cell num_fixed_macro xl yl xh yh row_height site_width
lef_scale def_scale total_space_area'''.split()


def enabled(params):
    return (getattr(params, 'db_option', '') in ('def', 'binary', 'binary_wo_pos')
            or bool(getattr(params, 'def_path', '') or getattr(params, 'def_input', '')))


def export_requested(params):
    return (getattr(params, 'db_option', '') == 'def'
            and 'binary_write' in re.split(r'[/,\s]+', getattr(params, 'mode', '')))


def _json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(type(value).__name__)


def _maps(db):
    for field in FLOATS + INTS + STRINGS:
        dtype = np.float64 if field in FLOATS else np.int32 if field in INTS else str
        setattr(db, field, np.asarray(getattr(db, field), dtype=dtype))
    db.num_nodes = db.num_physical_nodes
    for kind in ('node', 'pin', 'net'):
        names = list(getattr(db, kind + '_names'))
        if len(set(names)) != len(names):
            raise ValueError('MakeDB duplicate ' + kind + ' names')
        setattr(db, kind + '_name2id_map', dict(zip(names, range(len(names)))))
    for kind in ('node', 'net'):
        flat = np.asarray(getattr(db, 'flat_' + kind + '2pin_map'), dtype=np.int32)
        starts = np.asarray(getattr(db, 'flat_' + kind + '2pin_start_map'), dtype=np.int32)
        count = len(getattr(db, kind + '_names'))
        if len(starts) != count + 1 or starts[0] != 0 or starts[-1] != len(flat) or np.any(np.diff(starts) < 0):
            raise ValueError('MakeDB invalid ' + kind + ' CSR map')
        setattr(db, kind + '2pin_map', [flat[a:b] for a, b in zip(starts[:-1], starts[1:])])
    n, p, m = db.num_physical_nodes, len(db.pin_names), len(db.net_names)
    if db.row_height <= 0 or db.site_width <= 0 or db.xh <= db.xl or db.yh <= db.yl:
        raise ValueError('MakeDB invalid die/row geometry; check DEF ROW definitions')
    if len(db.node2fence_region_map) == n - db.num_terminal_NIs:
        db.node2fence_region_map = np.concatenate((db.node2fence_region_map,
            np.full(db.num_terminal_NIs, np.iinfo(np.int32).max, dtype=np.int32)))
    for field in ('node_x', 'node_y', 'node_orient', 'node_size_x', 'node_size_y', 'node2orig_node_map', 'node2fence_region_map'):
        if len(getattr(db, field)) != n:
            raise ValueError('MakeDB length mismatch: ' + field)
    for field in ('pin2node_map', 'pin2net_map', 'pin_direct', 'pin_offset_x', 'pin_offset_y'):
        if len(getattr(db, field)) != p:
            raise ValueError('MakeDB length mismatch: ' + field)
    for kind, count in (('node', n), ('net', m)):
        owners = np.asarray(getattr(db, 'pin2' + kind + '_map'))
        if np.any(owners < 0) or np.any(owners >= count):
            raise ValueError('MakeDB invalid pin2' + kind + '_map')
        flat = np.asarray(getattr(db, 'flat_' + kind + '2pin_map'))
        if len(flat) != p or not np.array_equal(np.sort(flat), np.arange(p)):
            raise ValueError('MakeDB CSR must contain each pin exactly once')
        starts = np.asarray(getattr(db, 'flat_' + kind + '2pin_start_map'))
        if not np.array_equal(owners[flat], np.repeat(np.arange(count), np.diff(starts))):
            raise ValueError('MakeDB inconsistent pin ownership')
    if len(db.net_weights) != m:
        raise ValueError('MakeDB net_weights length mismatch')
    # Dynamic timing state is recomputed; never resume stale criticality/RC.
    db.net_weight_deltas = np.zeros(m)
    db.net_criticality = np.zeros(m)
    db.net_criticality_deltas = np.zeros(m)
    db.regions = [np.asarray(db.flat_region_boxes)[a:b].reshape(-1, 4)
                  for a, b in zip(db.flat_region_boxes_start[:-1], db.flat_region_boxes_start[1:])]
    db.routing_grid_xl, db.routing_grid_yl = db.xl, db.yl
    db.routing_grid_xh, db.routing_grid_yh = db.xh, db.yh
    db.num_routing_grids_x = db.num_routing_grids_y = 0
    return db


def _read_def(params):
    from MakeDB import readDB

    class Reader(readDB):
        # Keep the original ID/connectivity routines. PlacementState owns all
        # explicit position files, including read/binary_write restarts.
        def getNodePos(self):
            for name in self.node_names:
                if name in self.cell_info:
                    pos = self.cell_info[name].get('position') or [0, 0]
                    x, y = map(float, pos[:2])
                else:
                    pin = self.extPinInfo[name]['layer0']
                    x = float(pin['position'][0]) + float(pin['width']) / 2
                    y = float(pin['position'][1]) + float(pin['height']) / 2
                self.node_x.append(x)
                self.node_y.append(y)

        def getNodeOrient(self):
            self.node_orient = [self.cell_info.get(name, {}).get('orientation') or 'N'
                                for name in self.node_names]

        def convertOrient(self):
            # Offsets remain canonical until PlacementState has processed
            # optional orientation overrides. This avoids double transforms.
            pass

    p = copy.copy(params)
    if export_requested(params):
        if not getattr(params, 'save_path', ''):
            raise ValueError('save_path is required for def + binary_write')
        if (Path(params.save_path) / 'physical_db/manifest.json').exists():
            raise FileExistsError('Physical DB already exists; choose a new save_path')
    p.db_option = 'def'
    p.def_path = getattr(params, 'def_path', '') or getattr(params, 'def_input', '')
    p.lef_dir_path = getattr(params, 'lef_dir_path', None) or getattr(params, 'lef_input', None)
    if not p.def_path or not p.lef_dir_path:
        raise ValueError('MakeDB requires def_path/def_input and lef_dir_path/lef_input')
    p.mode = 'default'
    p.macro_move = str(getattr(params, 'macro_move', 'True'))
    # Do not construct/write redundant metadata during an ordinary DEF read.
    # Explicit exports keep the legacy JSON/TXT sidecars for interoperability.
    p._makedb_write_metadata = export_requested(params)
    with tempfile.TemporaryDirectory(prefix='dreamplace-makedb-') as scratch:
        p.save_path = params.save_path if export_requested(params) else scratch
        Path(p.save_path).mkdir(parents=True, exist_ok=True)
        reader = Reader(p)
        reader()
        values = {name: getattr(reader, name) for name in FLOATS + INTS + STRINGS + SCALARS}
        values['def_template'] = Path(p.def_path).read_text()
        values['cell_info'] = reader.cell_info
        values['congestion_metadata'] = {
            'blockageInfo': getattr(reader, 'blockageInfo', {}),
            'plot_fixed_macros': getattr(reader.lef_def_analysis, 'plot_fixed_macros', []).tolist()
                if isinstance(getattr(reader.lef_def_analysis, 'plot_fixed_macros', []), np.ndarray)
                else getattr(reader.lef_def_analysis, 'plot_fixed_macros', []),
        }
        values['ext_pin_info'] = reader.extPinInfo
        values['design_name'] = re.search(r'\bDESIGN\s+(\S+)\s*;', values['def_template']).group(1)
        values['row_orients'] = _row_orients(values['def_template'])
        values['placement_only_nets'] = [n for n in reader.net_names if n not in reader.original_net_names]
        values['net_uses'] = [str(reader.net_info.get(n, {}).get('use', 'SIGNAL'))
                              .replace('USE ', '').strip().upper() for n in reader.net_names]
        return _maps(SimpleNamespace(**values))


def _row_orients(template):
    return [(float(m[0]), m[1]) for m in re.findall(
        r'\bROW\s+\S+\s+\S+\s+[-+\d.]+\s+([-+\d.]+)\s+(\w+)', template)]


def save(db, params):
    if not export_requested(params):
        raise ValueError('Physical export requires db_option=def, mode=binary_write')
    root = Path(params.save_path) / 'physical_db'
    root.mkdir(parents=True, exist_ok=True)
    # Publish the manifest last. Refuse to overwrite an existing snapshot.
    if (root / 'manifest.json').exists():
        raise FileExistsError('Physical DB already exists; choose a new save_path: ' + str(root))
    for field in FLOATS + INTS + STRINGS:
        dtype = np.float64 if field in FLOATS else np.int32 if field in INTS else str
        np.save(root / (field + '.npy'), np.asarray(getattr(db, field), dtype=dtype), allow_pickle=False)
    metadata = {name: getattr(db, name) for name in SCALARS}
    metadata.update(schema=SCHEMA, design_name=db.design_name, row_orients=db.row_orients,
                    placement_only_nets=db.placement_only_nets,
                    net_uses=getattr(db, 'net_uses', ['SIGNAL'] * len(db.net_names)))
    metadata['congestion_metadata'] = getattr(db, 'congestion_metadata', {})
    (root / 'template.def').write_text(db.def_template)
    (root / 'manifest.json').write_text(json.dumps(metadata, default=_json_default))
    logging.info('physical DB saved: %s', root)


def _load(root):
    metadata = json.loads((root / 'manifest.json').read_text())
    if metadata.pop('schema') != SCHEMA:
        raise ValueError('Unsupported physical DB schema; export again')
    for field in FLOATS + INTS + STRINGS:
        metadata[field] = np.load(root / (field + '.npy'), allow_pickle=False)
    # No million-cell JSON or DEF text needs to be parsed on binary restore.
    metadata['cell_info'] = {}; metadata['ext_pin_info'] = {}
    metadata['def_template'] = ''
    metadata['def_template_path'] = str(root / 'template.def')
    metadata.setdefault('net_uses', ['SIGNAL'] * len(metadata['net_names']))
    return _maps(SimpleNamespace(**metadata))


def _legacy(root):
    """Read bin_sep's existing mixed TXT/NPY layout without invoking readers."""
    def lines(name, dtype):
        return np.asarray((root / (name + '.txt')).read_text().splitlines(), dtype=dtype)
    counts = np.load(root / 'num_node_info.npy', allow_pickle=False)
    if len(counts) < 12:
        raise ValueError('Legacy num_node_info.npy requires 12 entries')
    db = SimpleNamespace()
    for name, idx in dict(num_physical_nodes=0, num_terminals=1, num_terminal_NIs=2,
                         num_movable_pins=3, num_fake_macro=5, num_blockage=6,
                         num_movable_std_cell=8, num_movable_macro=9,
                         num_fixed_std_cell=10, num_fixed_macro=11).items():
        setattr(db, name, int(counts[idx]))
    for field in FLOATS + INTS + STRINGS:
        if field in ('node_x', 'node_y', 'node_orient'):
            continue
        if field in ('rows', 'flat_region_boxes', 'node2fence_region_map'):
            value = np.load(root / (field + '.npy'), allow_pickle=True)
        else:
            value = lines(field, str if field in STRINGS else np.int32 if field in INTS else float)
        setattr(db, field, value)
    for field, value in zip('xl yl xh yh row_height site_width lef_scale def_scale'.split(),
                            np.load(root / 'die_info.npy', allow_pickle=True)):
        setattr(db, field, float(value))
    db.total_space_area = float(np.load(root / 'area_info.npy', allow_pickle=True)[2])
    db.cell_info = json.loads((root / 'cells_info.json').read_text())
    blockage_path = root / 'blockage_info.json'
    if blockage_path.is_file():
        db.congestion_metadata = {'blockageInfo': json.loads(blockage_path.read_text()),
                                  'plot_fixed_macros': []}
        conversion = root / 'fixed_macro_to_port_conversion.json'
        if conversion.is_file():
            db.congestion_metadata['plot_fixed_macros'] = [
                [row['x'], row['y'], row['width'], row['height']]
                for row in json.loads(conversion.read_text()).get('plot_fixed_macros', [])]
    db.ext_pin_info = json.loads((root / 'ext_pin_info.json').read_text())
    db.node_x, db.node_y, db.node_orient = [], [], []
    for name in db.node_names:
        if name in db.cell_info:
            info = db.cell_info[name]
            pos = info.get('position') or [0, 0]
            orient = info.get('orientation') or 'N'
        else:
            info = db.ext_pin_info[name]['layer0']
            pos = [float(info['position'][0]) + float(info['width']) / 2,
                   float(info['position'][1]) + float(info['height']) / 2]
            orient = 'N'
        db.node_x.append(float(pos[0])); db.node_y.append(float(pos[1]))
        db.node_orient.append(orient)
    # pinInfo_dict is captured BEFORE the reference convertOrient(). Its
    # canonical offsets avoid reapplying that transform on legacy snapshots.
    info_path = root / 'pinInfo_dict.json'
    if not info_path.exists():
        raise ValueError('Legacy import needs pinInfo_dict.json (canonical pin offsets)')
    pins = json.loads(info_path.read_text())
    db.pin_offset_x = [pins[str(i)]['offset_x'] for i in range(len(db.pin_names))]
    db.pin_offset_y = [pins[str(i)]['offset_y'] for i in range(len(db.pin_names))]
    db.node_size_x, db.node_size_y = db.node_size_x_LEF.copy(), db.node_size_y_LEF.copy()
    db.row_orients = []
    db.design_name = root.name
    db.def_template = ''
    db.placement_only_nets = []
    netlist_path = root / 'netlist_info.json'
    netlist = json.loads(netlist_path.read_text()) if netlist_path.is_file() else {}
    db.net_uses = [str(netlist.get(text(n), {}).get('use', 'SIGNAL'))
                   .replace('USE ', '').strip().upper() for n in db.net_names]
    for i, name in enumerate(db.net_names):
        pins = db.flat_net2pin_map[db.flat_net2pin_start_map[i]:db.flat_net2pin_start_map[i + 1]]
        names = [str(db.pin_names[int(p)]).rsplit(' ', 1) for p in pins]
        if names and all(len(v) == 2 and (v[1] == 'VP' or
                db.cell_info.get(v[0], {}).get('macro_id') == 'virtual_type_0') for v in names):
            db.placement_only_nets.append(str(name))
    for kind in ('node', 'pin', 'net'):
        field = 'pin_name2id_map_list' if kind == 'pin' else kind + '_name2id_list'
        if not np.array_equal(lines(field, int), np.arange(len(getattr(db, kind + '_names')))):
            raise ValueError('Legacy name/ID table is not in canonical ID order: ' + field)
    logging.warning('Reading trusted legacy bin_sep object NPY files: %s', root)
    return _maps(db)


def read(params):
    option = getattr(params, 'db_option', '') or 'def'
    if option not in ('def', 'binary', 'binary_wo_pos'):
        raise ValueError('MakeDB adapter supports def, binary and binary_wo_pos, not ' + option)
    if option == 'def':
        db = _read_def(params)
    else:
        if not getattr(params, 'save_path', ''):
            raise ValueError('save_path is required to restore MakeDB')
        root = Path(params.save_path)
        if (root / 'physical_db').exists():
            db = _load(root / 'physical_db')
        else:
            db = _legacy(root)
            if option == 'binary' and not all(getattr(params, 'read_' + f, '') for f in ('posX', 'posY', 'orient')):
                raise ValueError('Legacy binary needs read_posX, read_posY and read_orient; '
                                 'use binary_wo_pos for geometry-only restoration')
        if option == 'binary_wo_pos':
            end = db.num_physical_nodes - db.num_terminals - db.num_terminal_NIs
            db.node_x = np.asarray(db.node_x).copy(); db.node_x[:end] = 0
            db.node_y = np.asarray(db.node_y).copy(); db.node_y[:end] = 0
        logging.info('physical DB restored: %s (%s)', root, option)
    params._physical_design_name = db.design_name
    template = getattr(params, 'def_template_input', '')
    if template:
        if not Path(template).is_file():
            raise FileNotFoundError(template)
        db.def_template_path = template
    params._physical_has_def_template = bool(db.def_template or getattr(db, 'def_template_path', ''))
    if not params._physical_has_def_template:
        logging.warning('Legacy DB has no original DEF template: default output is .pl. '
                        'Set def_template_input to preserve all original DEF sections in DEF output.')
    if getattr(params, 'read_node_names', ''):
        names = Path(params.read_node_names).read_text().splitlines()
        if names[:db.num_physical_nodes] != list(db.node_names):
            raise ValueError('read_node_names differs from MakeDB node IDs')
    return RawDB(db), db


def finish_initial(db, params):
    """Apply each DEF orientation once, then export unscaled canonical arrays."""
    raw = db.rawdb
    source = raw.data
    source.node_x, source.node_y = db.node_x.copy(), db.node_y.copy()
    source.node_orient = [text(o) for o in db.node_orient]
    if export_requested(params):
        save(source, params)
    # PlacementState transforms movable offsets when a file is supplied;
    # remaining nodes still have canonical MakeDB offsets.
    start = db.num_movable_nodes if getattr(params, 'read_orient', '') else 0
    for node in range(start, db.num_physical_nodes):
        orient = text(db.node_orient[node])
        if orient == 'UNKNOWN':
            orient = 'N'
        pins = db.node2pin_map[node]
        x, y, w, h = orient_offsets(db.pin_offset_x[pins], db.pin_offset_y[pins],
                                     db.node_size_x[node], db.node_size_y[node], orient)
        db.pin_offset_x[pins], db.pin_offset_y[pins] = x, y
        db.node_size_x[node], db.node_size_y[node] = w, h
        db.node_orient[node] = orient.encode()
    raw.orients = [text(o) for o in db.node_orient]
    # The parser's large nested cell/pin geometry dictionaries are not needed
    # during placement or RC updates. The exported sidecars retain them.
    source.cell_info = {}
    source.ext_pin_info = {}
    # Physical names/IDs remain exactly as MakeDB exported them. Only this
    # timing view uses OpenTimer's instance:pin / top-level-port spelling.
    db.timing_pin_names = np.asarray([timing_pin_name(p) for p in db.pin_names], dtype='S')
    db.timing_pin_name2id_map = {text(p): i for i, p in enumerate(db.timing_pin_names)}
    excluded = set(source.placement_only_nets)
    db.timing_net_names = np.asarray(['' if text(n) in excluded else text(n) for n in db.net_names], dtype='S')
    db.timing_net_name2id_map = {text(n): i for i, n in enumerate(db.net_names) if text(n) not in excluded}


def timing_pin_name(name):
    name = text(name)
    if ' ' not in name:
        return name
    cell, pin = name.rsplit(' ', 1)
    return pin if cell == 'PIN' else cell + ':' + pin


class RawDB:
    """Small raw-DB interface used by placement checkpoints and RC units."""
    is_makedb = True

    def __init__(self, data):
        self.data = data
        self.orients = [text(o) for o in data.node_orient]

    def lefUnit(self): return int(self.data.lef_scale)
    def defUnit(self): return int(self.data.def_scale)
    def setNodeOrient(self, node, orient): self.orients[node] = ORIENTS[int(orient)]
    def nodeName(self, node): return self.data.node_names[node]
    def node(self, node):
        return SimpleNamespace(orient=lambda: ORIENTS.index(self.orients[node]),
                               xl=lambda: self.data.node_x[node], yl=lambda: self.data.node_y[node])
    def fixedNodeIndices(self):
        return range(self.data.num_physical_nodes - self.data.num_terminals - self.data.num_terminal_NIs,
                     self.data.num_physical_nodes - self.data.num_terminal_NIs)

    def apply(self, params, x, y):
        end = self.data.num_physical_nodes - self.data.num_terminals - self.data.num_terminal_NIs
        self.data.node_x = np.asarray(x[:self.data.num_physical_nodes]).copy()
        self.data.node_y = np.asarray(y[:self.data.num_physical_nodes]).copy()
        if params.legalize_flag or params.detailed_place_flag:
            row_orients = dict(self.data.row_orients)
            for i in range(end):
                if self.data.node_size_y_LEF[i] == self.data.row_height:
                    for row_y in (float(y[i]), round(float(y[i]))):
                        if row_y in row_orients:
                            self.orients[i] = row_orients[row_y]
                            break

    def write_def(self, filename, x, y):
        template = self.data.def_template
        if not template and getattr(self.data, 'def_template_path', ''):
            template = Path(self.data.def_template_path).read_text()
        if not template:
            raise ValueError('Legacy DB lacks a DEF template. Export once with def + binary_write '
                             'to enable full DEF output; coordinate tables remain available.')
        ids = self.data.node_name2id_map
        end = self.data.num_physical_nodes - self.data.num_terminals - self.data.num_terminal_NIs
        visited = set()
        def component(match):
            record = match.group(0)
            name = re.match(r'-\s+(\S+)', record).group(1)
            node = ids.get(name)
            if node is None or node >= end:
                return record
            visited.add(name)
            placement = '+ PLACED ( %d %d ) %s' % (round(float(x[node])), round(float(y[node])), self.orients[node])
            pattern = r'\+\s*(?:(?:PLACED|FIXED|COVER)\s*\([^)]*\)\s*\w+|UNPLACED)'
            if re.search(pattern, record):
                return re.sub(pattern, lambda _: placement, record, count=1)
            return record.rstrip().removesuffix(';') + ' ' + placement + ' ;'
        def section(match):
            return re.sub(r'-\s+\S+\s+[^;]*;', component, match.group(0))
        result, count = re.subn(r'(?ms)^\s*COMPONENTS\s+\d+\s*;.*?^\s*END COMPONENTS', section, template)
        if count != 1:
            raise ValueError('DEF template must contain exactly one COMPONENTS section')
        missing = [text(n) for n in self.data.node_names[:end] if text(n) not in visited
                   and self.data.cell_info.get(text(n), {}).get('macro_id') != 'virtual_type_0']
        # Newly exported snapshots can identify virtual nodes through their
        # placement-only nets even without loading cells_info.json.
        virtual_nodes = set()
        for name in self.data.placement_only_nets:
            if name.startswith('virtual_region'):
                net = self.data.net_name2id_map[name]
                virtual_nodes.update(text(self.data.node_names[int(self.data.pin2node_map[p])])
                                     for p in self.data.net2pin_map[net])
        missing = [n for n in missing if n not in virtual_nodes]
        if missing:
            raise ValueError('DEF template missing movable cells: ' + ', '.join(missing[:8]))
        Path(filename).write_text(result)
