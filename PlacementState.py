"""Cell-ID placement checkpoints in original (unscaled) database units.

Each table contains ``cell_id value``. IDs are physical PlaceDB IDs, not raw
macro IDs, cluster labels, or filler IDs. Fixed objects may be verified but
cannot be moved by a checkpoint.
"""
import argparse
import csv
import hashlib
import logging
import math
import os

import numpy as np

FIELDS = ('posX', 'posY', 'orient')
ORIENTS = ('N', 'S', 'W', 'E', 'FN', 'FS', 'FW', 'FE', 'UNKNOWN')


def text(value):
    return value.decode('utf8') if isinstance(value, (bytes, np.bytes_)) else str(value)


def add_arguments(parser):
    for action in ('read', 'write'):
        for field in FIELDS:
            flags = ['--' + action + '_' + field]
            if action == 'read' and field == 'posX':
                flags.append('--read_psoX')
            if action == 'write' and field == 'orient':
                flags.append('--wrtie_orient')
            parser.add_argument(*flags, dest=action + '_' + field, metavar='FILE',
                                help='%s cell_id/value %s table (unscaled database units)' % (action, field))
    return parser


def parse_cli(argv=None):
    parser = add_arguments(argparse.ArgumentParser(description='DREAMPlace placement'))
    parser.add_argument('config', help='DREAMPlace JSON configuration')
    return parser.parse_args(argv)


def output_paths(params):
    paths = {field: os.fspath(getattr(params, 'write_' + field, '')) for field in FIELDS
             if getattr(params, 'write_' + field, '')}
    if paths:
        paths['cell_map'] = next(iter(paths.values())) + '.cells.tsv'
    return paths


def validate_paths(params):
    """Fail before placement; never overwrite an input or an existing output."""
    reads = [os.path.realpath(getattr(params, 'read_' + f)) for f in FIELDS
             if getattr(params, 'read_' + f, '')]
    for path in reads:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
    outputs = output_paths(params)
    destinations = [os.path.realpath(p) for p in outputs.values()]
    if len(set(destinations)) != len(destinations) or set(reads) & set(destinations):
        raise ValueError('Placement input/output paths must be distinct')
    for path in outputs.values():
        if os.path.lexists(path):
            raise FileExistsError('Placement output already exists: ' + path)


def id_digest(db):
    digest = hashlib.sha256()
    for name in db.node_names[:db.num_physical_nodes]:
        value = text(name).encode('utf8')
        digest.update(len(value).to_bytes(8, 'little'))
        digest.update(value)
    return digest.hexdigest()


def read_table(path, field, db, digest):
    n = db.num_physical_nodes
    values = np.full(n, '', dtype='U7') if field == 'orient' else np.full(n, np.nan, dtype=np.float64)
    seen = np.zeros(n, dtype=bool)
    checked_digest = False
    header_seen = False
    legacy_row = 0
    table_style = None
    with open(path, encoding='utf-8-sig') as stream:
        for line_number, line in enumerate(stream, 1):
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                if line.startswith('# cell_names_sha256='):
                    if line.split('=', 1)[1] != digest:
                        raise ValueError('%s: cell ID/name mapping differs from this design' % path)
                    checked_digest = True
                continue
            fields = line.split()
            if fields == ['cell_id', field] and not header_seen and not seen.any():
                header_seen = True
                continue
            try:
                style = len(fields)
                if table_style is not None and style != table_style:
                    raise ValueError('mixed one-column and cell_id/value records')
                table_style = style
                if style == 1 and getattr(db.rawdb, 'is_makedb', False):
                    fields = [str(legacy_row), fields[0]]
                    legacy_row += 1
                if len(fields) != 2:
                    raise ValueError('expected cell_id value')
                node = int(fields[0])
                if not 0 <= node < n:
                    raise ValueError('ID outside physical node range (fillers not supported)')
                if seen[node]:
                    raise ValueError('duplicate cell ID')
                if field == 'orient':
                    value = fields[1].upper()
                    if value not in ORIENTS:
                        raise ValueError('unsupported orientation')
                    if node < db.num_movable_nodes and value == 'UNKNOWN':
                        raise ValueError('movable orientation must be explicit')
                else:
                    value = float(fields[1])
                    if not math.isfinite(value) or abs(value) > np.finfo(db.dtype).max:
                        raise ValueError('coordinate must be finite and representable')
                values[node] = value
                seen[node] = True
            except ValueError as error:
                raise ValueError('%s:%d: %s' % (path, line_number, error)) from error
    if not seen[:db.num_movable_nodes].all():
        missing = np.flatnonzero(~seen[:db.num_movable_nodes])[:8]
        raise ValueError('%s: missing movable cell IDs %s' % (path, missing.tolist()))
    if not checked_digest:
        logging.warning('%s has no ID/name checksum; ensure IDs belong to this exact physical design', path)
    return values, seen


def orient_offsets(x, y, width, height, orient):
    """Transform canonical N pin offsets to DEF orientation about the bbox."""
    if orient == 'N':
        return x, y, width, height
    if orient == 'S':
        return width - x, height - y, width, height
    if orient == 'FN':
        return width - x, y, width, height
    if orient == 'FS':
        return x, height - y, width, height
    if orient == 'W':
        return height - y, x, height, width
    if orient == 'E':
        return y, width - x, height, width
    if orient == 'FW':
        return y, x, height, width
    if orient == 'FE':
        return height - y, width - x, height, width
    raise ValueError('unsupported movable orientation: ' + orient)


def read_initial(params, db):
    """Called after PlaceDB.read, BEFORE scaling/density initialization.

    PyPlaceDB converts pin offsets and sizes to canonical N. Transform those
    arrays, not just an orientation label, when an orientation file is supplied.
    """
    requested = {f: getattr(params, 'read_' + f, '') for f in FIELDS if getattr(params, 'read_' + f, '')}
    if not requested:
        return
    digest = id_digest(db)
    tables = {f: read_table(path, f, db, digest) for f, path in requested.items()}
    # Validate all inputs before any mutation. Fixed macros/shapes/IO stay fixed.
    for field, (values, seen) in tables.items():
        fixed = np.flatnonzero(seen & (np.arange(db.num_physical_nodes) >= db.num_movable_nodes))
        current = db.node_orient if field == 'orient' else db.node_x if field == 'posX' else db.node_y
        for node in fixed:
            equal = values[node] == text(current[node]) if field == 'orient' else np.isclose(
                values[node], current[node], rtol=1e-7, atol=1e-6)
            if not equal:
                raise ValueError('%s attempts to change fixed/IO cell ID %d' % (field, node))
    if 'orient' in tables:
        orientations = tables['orient'][0][:db.num_movable_nodes]
        if (getattr(params, 'legalize_flag', False) or getattr(params, 'detailed_place_flag', False)) and any(
                o in ('E', 'W', 'FE', 'FW') for o in orientations):
            raise ValueError('90-degree movable orientations require legalize_flag=0 and detailed_place_flag=0; '
                             'the row-based legalizers do not support arbitrary rotated cells')
        from dreamplace.ops.place_io.place_io import OrientEnum
        db.node_orient = np.asarray(db.node_orient, dtype='S7')
        for node, orient in enumerate(orientations):
            pins = db.node2pin_map[node]
            x, y, width, height = orient_offsets(db.pin_offset_x[pins], db.pin_offset_y[pins],
                                                db.node_size_x[node], db.node_size_y[node], orient)
            db.pin_offset_x[pins], db.pin_offset_y[pins] = x, y
            db.node_size_x[node], db.node_size_y[node] = width, height
            db.node_orient[node] = orient.encode('ascii')
            db.rawdb.setNodeOrient(int(db.node2orig_node_map[node]), getattr(OrientEnum, orient))
    for field, array in (('posX', db.node_x), ('posY', db.node_y)):
        if field in tables:
            array[:db.num_movable_nodes] = tables[field][0][:db.num_movable_nodes]
    if 'posX' in tables or 'posY' in tables:
        params.random_center_init_flag = 0
        params.gp_noise_ratio = 0.0
        logging.info('Placement restart: disabled random-center initialization and initial GP noise')
    logging.info('Loaded placement state: %s (unscaled database units)', ', '.join(requested))


def final_orientations(db):
    """Raw DB owns post-legalization row flips; Python labels can be stale."""
    return [ORIENTS[int(db.rawdb.node(int(db.node2orig_node_map[i])).orient())]
            for i in range(db.num_physical_nodes)]


def write_final(params, db):
    """Export internal DREAMPlace final state, before any external DP engine."""
    paths = output_paths(params)
    if not paths:
        return
    validate_paths(params)
    digest = id_digest(db)
    x, y = db.unscale_pl(params.shift_factor, params.scale_factor)
    for field, coords in (('posX', x), ('posY', y)):
        if field in paths and not np.isfinite(coords[:db.num_physical_nodes]).all():
            raise ValueError('Cannot export nonfinite final ' + field)
    values = {'posX': x, 'posY': y}
    if 'orient' in paths:
        values['orient'] = final_orientations(db)
    for field, path in paths.items():
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'x', newline='', encoding='utf8') as stream:
            stream.write('# cell_names_sha256=' + digest + '\n')
            writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
            writer.writerow(['cell_id', 'cell_name' if field == 'cell_map' else field])
            for node in range(db.num_physical_nodes):
                value = text(db.node_names[node]) if field == 'cell_map' else values[field][node]
                if field in ('posX', 'posY'):
                    value = format(float(value), '.17g')
                writer.writerow([node, value])
            if field == 'orient':
                for name, (value, unit) in getattr(db, 'final_placement_metrics', {}).items():
                    value = 'NA' if value is None else format(float(value), '.17g')
                    stream.write('# metric\t%s\t%s\t%s\n' % (name, value, unit))
        logging.info('Saved final DREAMPlace %s: %s', field, path)
