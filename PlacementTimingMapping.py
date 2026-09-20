#!/usr/bin/env python3
"""Bind original timing-geometry IDs to reduced placement IDs, without STA/GPU.

Inputs are MakeDB physical snapshots and ClusterPlacement name sidecars. Numeric
IDs always mean array indices in those snapshots, NOT OpenTimer internal IDs or
membership cell IDs. Output is non-pickled NPY arrays, TSV audits and checksums.
"""
import argparse
from collections import Counter
import csv
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
from types import SimpleNamespace

import numpy as np

LOG = logging.getLogger(__name__)
SCHEMA = 1
FIELDS = ('node_names', 'pin_names', 'net_names', 'pin2node_map', 'pin2net_map')
ARRAYS = ('timing_node_to_placement_node', 'timing_node_cluster_id',
          'timing_pin_to_placement_pin', 'timing_pin_to_placement_node',
          'timing_net_to_placement_net', 'placement_net_to_timing_net',
          'placement_node_to_timing_nodes', 'placement_node_to_timing_nodes_start',
          'placement_pin_to_timing_pins', 'placement_pin_to_timing_pins_start')


class MappingError(ValueError):
    pass


def text(value):
    return value.decode('utf-8') if isinstance(value, (bytes, np.bytes_)) else str(value)


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(value):
    """Read only identity/connectivity fields; never restore pickle or native STA.

    An already-loaded MakeDB/PlaceDB-compatible object is also accepted. In that
    case its CURRENT ID order is authoritative, including any deliberate sort.
    """
    if isinstance(value, (str, os.PathLike)):
        root = Path(value)
        if not root.is_dir():
            raise MappingError('Saved DB directory does not exist: %s. Prepare it first with '
                               'PreparePlacementDB.py (DEF + binary_write).' % root)
        if (root / 'physical_db').is_dir():
            root = root / 'physical_db'
        native = (root / 'manifest.json').is_file() and (root / 'node_names.npy').is_file()
        values = {}
        if native:
            metadata = json.loads((root / 'manifest.json').read_text())
            if metadata.get('schema') != 1:
                raise MappingError('Unsupported MakeDB physical_db schema: ' + str(root))
            for field in FIELDS:
                values[field] = np.load(root / (field + '.npy'), mmap_mode='r', allow_pickle=False)
        else:
            if not all((root / (field + '.txt')).is_file() for field in FIELDS):
                raise MappingError('No complete physical ID/connectivity arrays found in %s. '
                                   'Prepare the reduced DB with PreparePlacementDB.py first. '
                                   'Need a saved physical_db (or legacy name/owner TXT arrays), '
                                   'not timing_cache/model.bin alone.' % root)
            for field in FIELDS:
                path = root / (field + '.txt')
                if field.endswith('_names'):
                    values[field] = np.asarray(path.read_text().splitlines(), dtype=str)
                else:
                    values[field] = np.loadtxt(path, dtype=np.int64, ndmin=1)
            metadata = {}
        value = SimpleNamespace(**values, placement_only_nets=metadata.get('placement_only_nets', []),
                                source_path=str(root.resolve()))
        expected = metadata.get('num_physical_nodes', len(value.node_names))
        if expected != len(value.node_names):
            raise MappingError('physical_db node count disagrees with node_names')
    for field in FIELDS:
        a = np.asarray(getattr(value, field))
        if a.ndim != 1:
            raise MappingError('Expected 1D DB array: ' + field)
        if field.endswith('_names'):
            if a.dtype.kind not in ('U', 'S', 'O'):
                raise MappingError('Expected string names: ' + field)
            if a.dtype.kind == 'O' and any(not isinstance(v, (str, bytes, np.str_, np.bytes_)) for v in a):
                raise MappingError('Name array contains non-string values: ' + field)
        elif a.dtype.kind not in ('i', 'u'):
            raise MappingError('Expected integer owner IDs: ' + field)
    for kind in ('node', 'net'):
        owner = np.asarray(getattr(value, 'pin2' + kind + '_map'))
        if len(owner) != len(value.pin_names) or np.any(owner < 0) or np.any(owner >= len(getattr(value, kind + '_names'))):
            raise MappingError('Invalid pin2%s_map length/range' % kind)
    if getattr(value, 'num_physical_nodes', len(value.node_names)) != len(value.node_names):
        raise MappingError('Only physical node IDs are supported (no filler names)')
    return value


def fingerprint(db):
    """Representation-independent hash of ordered identities and pin ownership.

    Positions, orientations, weights and sizing are intentionally excluded:
    those change during placement without changing the meaning of an ID map.
    """
    digest = hashlib.sha256(b'DREAMPLACE_ID_MAPPING_V1\0')
    counts = {}
    for field in FIELDS:
        a = np.asarray(getattr(db, field))
        digest.update(field.encode() + b'\0' + struct.pack('<Q', len(a)))
        if field.endswith('_names'):
            counts[field[:-6]] = len(a)
            for value in a:
                encoded = text(value).encode('utf-8')
                digest.update(struct.pack('<Q', len(encoded))); digest.update(encoded)
        else:
            for start in range(0, len(a), 100000):
                digest.update(np.asarray(a[start:start + 100000], dtype='<i8').tobytes())
    return dict(sha256=digest.hexdigest(), counts=counts)


def rows(path, required):
    with open(path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or not set(required) <= set(reader.fieldnames):
            raise MappingError('Missing/duplicate TSV columns in ' + str(path))
        for row in reader:
            if None in row or any(row[k] is None for k in required):
                raise MappingError('Malformed TSV row in ' + str(path))
            yield row


def pin_name(cell, pin):
    # Current MakeDB physical names are exactly "instance pin" or "PIN port".
    if not cell or not pin or any(c.isspace() for c in cell + pin):
        raise MappingError('Invalid MakeDB endpoint: %r / %r' % (cell, pin))
    return cell + ' ' + pin


def owner_name(cell, pin):
    return pin if cell == 'PIN' else cell


def reverse_csr(forward, count):
    present = np.flatnonzero(forward >= 0)
    order = np.argsort(forward[present], kind='stable')
    flat = present[order].astype(np.int64)
    starts = np.empty(count + 1, dtype=np.int64); starts[0] = 0
    np.cumsum(np.bincount(forward[present], minlength=count), out=starts[1:])
    return flat, starts


def validate_arrays(a, timing, placement):
    """Protect callers from -1 indexing and incompatible or corrupt ownership."""
    for kind in ('node', 'pin', 'net'):
        count, target = len(getattr(timing, kind + '_names')), len(getattr(placement, kind + '_names'))
        forward = a['timing_%s_to_placement_%s' % (kind, kind)]
        if len(forward) != count or np.any(forward < (0 if kind == 'node' else -1)) or np.any(forward >= target):
            raise MappingError('Invalid %s mapping length/range' % kind)
    nodes = a['timing_node_to_placement_node']
    cid = a['timing_node_cluster_id']
    if len(cid) != len(nodes) or np.any(cid < -1):
        raise MappingError('Invalid node cluster IDs')
    pnodes = a['timing_pin_to_placement_node']
    if not np.array_equal(pnodes, nodes[timing.pin2node_map]):
        raise MappingError('Inconsistent timing pin coordinate owner')
    pin_forward = a['timing_pin_to_placement_pin']
    net_forward = a['timing_net_to_placement_net']
    net_inverse = a['placement_net_to_timing_net']
    if (len(net_inverse) != len(placement.net_names) or np.any(net_inverse < 0)
            or np.any(net_inverse >= len(timing.net_names))):
        raise MappingError('Invalid placement net inverse')
    if not np.array_equal(net_forward[net_inverse], np.arange(len(net_inverse))):
        raise MappingError('Inconsistent net inverse')
    valid_net = np.flatnonzero(net_forward >= 0)
    if not np.array_equal(net_inverse[net_forward[valid_net]], valid_net):
        raise MappingError('Net mapping is not one-to-one')
    valid_pin = np.flatnonzero(pin_forward >= 0)
    if (not np.array_equal(placement.pin2node_map[pin_forward[valid_pin]], pnodes[valid_pin])
            or not np.array_equal(placement.pin2net_map[pin_forward[valid_pin]],
                                  net_forward[timing.pin2net_map[valid_pin]])):
        raise MappingError('Inconsistent mapped pin node/net ownership')
    if np.any(net_forward[timing.pin2net_map[pin_forward < 0]] != -1):
        raise MappingError('Missing pin on a mapped net')
    for kind in ('node', 'pin'):
        flat, starts = reverse_csr(a['timing_%s_to_placement_%s' % (kind, kind)],
                                  len(getattr(placement, kind + '_names')))
        if (np.any(np.diff(starts) == 0)
                or not np.array_equal(a['placement_%s_to_timing_%ss' % (kind, kind)], flat)
                or not np.array_equal(a['placement_%s_to_timing_%ss_start' % (kind, kind)], starts)):
            raise MappingError('Invalid %s reverse CSR or unrepresented placement item' % kind)


def index_db(sql, side, db):
    for kind in ('node', 'net', 'pin'):
        names = getattr(db, kind + '_names')

        def records():
            for i, name in enumerate(names):
                name = text(name)
                if not name or any(c in name for c in '\t\n\r\0'):
                    raise MappingError('Empty/control-character DB name')
                if kind == 'pin':
                    node, net = int(db.pin2node_map[i]), int(db.pin2net_map[i])
                    owner = text(db.node_names[node])
                    if name != 'PIN ' + owner and not (name.startswith(owner + ' ') and len(name) > len(owner) + 1):
                        raise MappingError('Pin name disagrees with node owner (expected MakeDB physical names): ' + name)
                    yield side, i, name, node, net
                else:
                    yield side, i, name
        sql.executemany('INSERT INTO %s VALUES (%s)' % (kind, ','.join('?' * (5 if kind == 'pin' else 3))), records())


def build(timing_db, placement_db, cluster_data, output, write_tsv=True, temp_dir=None):
    timing, placement = snapshot(timing_db), snapshot(placement_db)
    root, out = Path(cluster_data), Path(output)
    if os.path.lexists(out):
        raise MappingError('Output already exists; choose a new directory: ' + str(out))
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('status') != 'complete' or manifest.get('model') != 'placement_only_per_cluster_net_pin':
        raise MappingError('Need a completed ClusterPlacement output directory')
    inputs = [root / f for f in ('cell_mapping.tsv', 'pin_mapping.tsv', 'net_mapping.tsv', 'manifest.json')]
    hashes = {p.name: file_hash(p) for p in inputs}
    identities = dict(timing=fingerprint(timing), placement=fingerprint(placement))
    n, p, m = (len(getattr(timing, kind + '_names')) for kind in ('node', 'pin', 'net'))
    rm = len(placement.net_names)
    a = dict(timing_node_to_placement_node=np.full(n, -1, dtype=np.int64),
             timing_node_cluster_id=np.full(n, -1, dtype=np.int64),
             timing_pin_to_placement_pin=np.full(p, -1, dtype=np.int64),
             timing_net_to_placement_net=np.full(m, -1, dtype=np.int64),
             placement_net_to_timing_net=np.full(rm, -1, dtype=np.int64))
    counts = Counter()
    # Native MakeDB marks synthetic placement-only nets explicitly. No guessed
    # name prefix is used to waive missing mapping rows.
    synthetic = set(map(text, getattr(timing, 'placement_only_nets', []))) & set(
        map(text, getattr(placement, 'placement_only_nets', [])))
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='placement-timing-index-', dir=temp_dir) as scratch, \
         tempfile.TemporaryDirectory(prefix='.placement-timing-map-', dir=out.parent) as stage:
        stage = Path(stage)
        sql = sqlite3.connect(str(Path(scratch) / 'mapping.sqlite'))
        try:
            sql.executescript('''
                PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-65536;
                CREATE TABLE node(side INTEGER,id INTEGER,name TEXT,PRIMARY KEY(side,id),UNIQUE(side,name));
                CREATE TABLE net(side INTEGER,id INTEGER,name TEXT,PRIMARY KEY(side,id),UNIQUE(side,name));
                CREATE TABLE pin(side INTEGER,id INTEGER,name TEXT,node INTEGER,net INTEGER,
                                 PRIMARY KEY(side,id),UNIQUE(side,name));
                CREATE INDEX pin_net ON pin(side,net);
                CREATE TABLE cm(original TEXT PRIMARY KEY,reduced TEXT,cid INTEGER) WITHOUT ROWID;
                CREATE TABLE nm(original TEXT PRIMARY KEY,reduced TEXT UNIQUE,degree INTEGER) WITHOUT ROWID;
                CREATE TABLE pm(original TEXT PRIMARY KEY,reduced TEXT,net TEXT,original_node TEXT,
                                reduced_node TEXT) WITHOUT ROWID;
            ''')
            LOG.info('Indexing both DBs in their actual ID order (CPU only)')
            index_db(sql, 0, timing); index_db(sql, 1, placement)
            LOG.info('Reading cluster name mappings')
            def cells():
                for r in rows(inputs[0], ('original_cell_name', 'reduced_cell_name', 'cluster_id')):
                    cid = int(r['cluster_id']) if r['cluster_id'] else -1
                    if cid < -1:
                        raise MappingError('Invalid cluster ID')
                    if cid == -1 and r['original_cell_name'] != r['reduced_cell_name']:
                        raise MappingError('Unclustered cell must keep its name')
                    yield r['original_cell_name'], r['reduced_cell_name'], cid
            sql.executemany('INSERT INTO cm VALUES (?,?,?)', cells())
            def nets():
                for r in rows(inputs[2], ('original_net_name', 'reduced_net_name', 'reduced_degree')):
                    if r['original_net_name'] != r['reduced_net_name']:
                        raise MappingError('Net renaming/merging is not supported by this strategy')
                    degree = int(r['reduced_degree'])
                    if degree < 1:
                        raise MappingError('Invalid reduced net degree')
                    yield r['original_net_name'], r['reduced_net_name'], degree
            sql.executemany('INSERT INTO nm VALUES (?,?,?)', nets())
            def pins():
                for i, r in enumerate(rows(inputs[1], ('original_cell_name', 'original_pin_name', 'net_name',
                                                      'reduced_cell_name', 'reduced_pin_name'))):
                    yield (pin_name(r['original_cell_name'], r['original_pin_name']),
                           pin_name(r['reduced_cell_name'], r['reduced_pin_name']), r['net_name'],
                           owner_name(r['original_cell_name'], r['original_pin_name']),
                           owner_name(r['reduced_cell_name'], r['reduced_pin_name']))
                    if i and i % 500000 == 0:
                        LOG.info('Read %d pin mapping rows', i)
            sql.executemany('INSERT INTO pm VALUES (?,?,?,?,?)', pins())
            LOG.info('Resolving node and net ID maps')
            for oid, name, rid, cid in sql.execute('''
                SELECT o.id,o.name,r.id,cm.cid FROM node o LEFT JOIN cm ON cm.original=o.name
                LEFT JOIN node r ON r.side=1 AND r.name=COALESCE(cm.reduced,o.name)
                WHERE o.side=0 ORDER BY o.id'''):
                if rid is None:
                    raise MappingError('Original node has no placement coordinate source: ' + name)
                a['timing_node_to_placement_node'][oid] = rid
                a['timing_node_cluster_id'][oid] = -1 if cid is None else cid
                counts['identity_nodes_not_in_component_table'] += cid is None
            # A declared cluster may not point at more than one placement node.
            by_cluster = {}
            for oid in np.flatnonzero(a['timing_node_cluster_id'] >= 0):
                cid, rid = int(a['timing_node_cluster_id'][oid]), int(a['timing_node_to_placement_node'][oid])
                if by_cluster.setdefault(cid, rid) != rid:
                    raise MappingError('Cluster ID points to multiple placement nodes: %d' % cid)
            counts['clusters'] = len(by_cluster)
            owner_clusters = {}
            for oid, rid in enumerate(a['timing_node_to_placement_node']):
                cid = int(a['timing_node_cluster_id'][oid])
                if rid in owner_clusters and (cid < 0 or owner_clusters[rid] != cid):
                    raise MappingError('Different clusters/unclustered nodes share a placement node')
                owner_clusters[rid] = cid
            for oid, name, rid, degree in sql.execute('''
                SELECT o.id,o.name,r.id,nm.degree FROM net o LEFT JOIN nm ON nm.original=o.name
                LEFT JOIN net r ON r.side=1 AND r.name=COALESCE(nm.reduced,o.name)
                WHERE o.side=0 ORDER BY o.id'''):
                if degree is None and name not in synthetic:
                    raise MappingError('Original net missing from cluster net mapping: ' + name)
                if rid is None:
                    if degree != 1:
                        raise MappingError('Nontrivial net missing from placement DB: ' + name)
                    mapped_nodes = {int(a['timing_node_to_placement_node'][node])
                                    for node, in sql.execute('SELECT node FROM pin WHERE side=0 AND net=?', (oid,))}
                    if len(mapped_nodes) != 1:
                        raise MappingError('Omitted net does not contract to one placement node: ' + name)
                    counts['omitted_single_endpoint_nets'] += 1
                    continue
                if a['placement_net_to_timing_net'][rid] != -1:
                    raise MappingError('Multiple original nets map to one placement net')
                a['timing_net_to_placement_net'][oid] = rid
                a['placement_net_to_timing_net'][rid] = oid
            if np.any(a['placement_net_to_timing_net'] < 0):
                idx = int(np.flatnonzero(a['placement_net_to_timing_net'] < 0)[0])
                raise MappingError('Placement net has no original timing net: ' + text(placement.net_names[idx]))
            LOG.info('Resolving pin maps and checking node/net ownership')
            query = '''SELECT o.id,o.name,o.node,o.net,pm.original,pm.net,pm.original_node,pm.reduced_node,
                              r.id,r.node,r.net,rn.id
                       FROM pin o LEFT JOIN pm ON pm.original=o.name
                       LEFT JOIN pin r ON r.side=1 AND r.name=COALESCE(pm.reduced,o.name)
                       LEFT JOIN node rn ON rn.side=1 AND rn.name=pm.reduced_node
                       WHERE o.side=0 ORDER BY o.id'''
            for oid, name, node, net, declared, netname, oname, rname, rid, rnode, rnet, expected_node in sql.execute(query):
                target_node = int(a['timing_node_to_placement_node'][node])
                target_net = int(a['timing_net_to_placement_net'][net])
                if declared is None:
                    if text(timing.net_names[net]) not in synthetic:
                        raise MappingError('Original pin missing from cluster pin mapping: ' + name)
                elif (netname != text(timing.net_names[net]) or oname != text(timing.node_names[node])
                      or expected_node != target_node):
                    raise MappingError('Pin mapping disagrees with original net/node or cell mapping: ' + name)
                if rid is None:
                    if target_net != -1:
                        raise MappingError('Pin on active net missing from placement DB: ' + name)
                    counts['omitted_pins_on_single_endpoint_nets'] += 1
                    continue
                if rnode != target_node or rnet != target_net:
                    raise MappingError('Mapped pin has wrong placement node/net: ' + name)
                a['timing_pin_to_placement_pin'][oid] = rid
            a['timing_pin_to_placement_node'] = a['timing_node_to_placement_node'][timing.pin2node_map]
            for kind in ('node', 'pin'):
                flat, starts = reverse_csr(a['timing_%s_to_placement_%s' % (kind, kind)],
                                          len(getattr(placement, kind + '_names')))
                if np.any(np.diff(starts) == 0):
                    idx = int(np.flatnonzero(np.diff(starts) == 0)[0])
                    raise MappingError('Placement %s has no original source: %s' % (kind, text(getattr(placement, kind + '_names')[idx])))
                a['placement_%s_to_timing_%ss' % (kind, kind)] = flat
                a['placement_%s_to_timing_%ss_start' % (kind, kind)] = starts
            for table, target in (('cm', 'node'), ('nm', 'net'), ('pm', 'pin')):
                counts['sidecar_%s_rows_absent_from_timing_db' % target] = sql.execute(
                    'SELECT COUNT(*) FROM %s x LEFT JOIN %s o ON o.side=0 AND o.name=x.original WHERE o.id IS NULL' % (table, target)).fetchone()[0]
        except sqlite3.IntegrityError as exc:
            raise MappingError('Duplicate DB name or ambiguous/duplicate name-mapping row: ' + str(exc)) from exc
        finally:
            sql.close()
        # Inputs may be large; hash again to detect editing during generation.
        if any(file_hash(p) != hashes[p.name] for p in inputs):
            raise MappingError('Cluster mapping input changed during generation')
        if identities != dict(timing=fingerprint(timing), placement=fingerprint(placement)):
            raise MappingError('DB identity/ownership changed during generation')
        validate_arrays(a, timing, placement)
        for field in ARRAYS:
            np.save(stage / (field + '.npy'), a[field], allow_pickle=False)
        if write_tsv:
            write_tables(stage, timing, placement, a)
        report = dict(schema=SCHEMA, status='complete', identities=identities, counts=dict(counts),
                      timing_db=getattr(timing, 'source_path', 'in-memory'),
                      placement_db=getattr(placement, 'source_path', 'in-memory'),
                      cluster_data=str(root.resolve()), source_sha256=hashes,
                      sentinel=-1, id_namespace='MakeDB physical array indices, not OpenTimer internal IDs',
                      arrays={field: dict(file=field + '.npy', sha256=file_hash(stage / (field + '.npy')),
                                          length=len(a[field])) for field in ARRAYS},
                      notes=['Positions/weights/geometry are not part of ID fingerprint.',
                             'Original DB may already filter clocks/ports: this tool does not recreate omitted timing topology.',
                             'All original nodes need a placement coordinate source; missing pins are allowed only on verified one-node omitted nets.',
                             'No coordinates, RC, STA, weights, or source DB files are modified.'])
        (stage / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
        out.mkdir()
        for path in stage.iterdir():
            if path.name != 'manifest.json':
                os.replace(path, out / path.name)
        os.replace(stage / 'manifest.json', out / 'manifest.json')
    return report


def write_tables(root, timing, placement, a):
    for kind in ('node', 'pin', 'net'):
        with (root / (kind + '_mapping.tsv')).open('w') as stream:
            w = csv.writer(stream, delimiter='\t', lineterminator='\n')
            extra = ['cluster_id'] if kind == 'node' else ['placement_node_id'] if kind == 'pin' else []
            w.writerow(['timing_%s_id' % kind, 'timing_%s_name' % kind,
                        'placement_%s_id' % kind, 'placement_%s_name' % kind, 'status'] + extra)
            for i, j in enumerate(a['timing_%s_to_placement_%s' % (kind, kind)]):
                row = [i, text(getattr(timing, kind + '_names')[i]), int(j),
                       text(getattr(placement, kind + '_names')[j]) if j >= 0 else '',
                       'mapped' if j >= 0 else 'omitted_single_endpoint_net']
                if kind == 'node': row.append(int(a['timing_node_cluster_id'][i]))
                if kind == 'pin': row.append(int(a['timing_pin_to_placement_node'][i]))
                w.writerow(row)


def load_mapping(path, timing_db, placement_db):
    """Read binary maps only after verifying both current DB ID orders and hashes."""
    root = Path(path)
    report = json.loads((root / 'manifest.json').read_text())
    if report.get('schema') != SCHEMA or report.get('status') != 'complete':
        raise MappingError('Incomplete/unsupported mapping manifest')
    timing, placement = snapshot(timing_db), snapshot(placement_db)
    for role, value in (('timing', timing), ('placement', placement)):
        if fingerprint(value) != report['identities'][role]:
            raise MappingError('%s DB ID order/connectivity differs; regenerate mapping' % role)
    result = {}
    for field in ARRAYS:
        filename = field + '.npy'
        info = report['arrays'][field]
        if info['file'] != filename or file_hash(root / filename) != info['sha256']:
            raise MappingError('Mapping array checksum mismatch: ' + field)
        a = np.load(root / filename, mmap_mode='r', allow_pickle=False)
        if a.ndim != 1 or a.dtype != np.dtype('int64') or len(a) != info['length']:
            raise MappingError('Invalid mapping array shape/dtype: ' + field)
        result[field] = a
    validate_arrays(result, timing, placement)
    return result


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--timing-db', required=True, help='original saved MakeDB root or physical_db directory')
    p.add_argument('--placement-db', required=True, help='reduced saved MakeDB root or physical_db directory')
    p.add_argument('--cluster-data', required=True, help='completed ClusterPlacement output directory')
    p.add_argument('--output', required=True, help='new mapping output directory')
    p.add_argument('--no-tsv', action='store_true', help='save binary arrays and manifest only')
    p.add_argument('--temp-dir', help='scratch directory for SQLite (can use several GB for large designs)')
    return p.parse_args(argv)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    args = parse_args()
    try:
        report = build(args.timing_db, args.placement_db, args.cluster_data, args.output,
                       write_tsv=not args.no_tsv, temp_dir=args.temp_dir)
        print(json.dumps(report['counts'], indent=2))
        print('Saved checked placement/timing ID mappings:', args.output)
    except (MappingError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit('ERROR: ' + str(exc))
