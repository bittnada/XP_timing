"""Read-only two-DB snapshots in POSIX shared memory.

Master publishes physical_db arrays, ID maps and the static timing model once.
Clients attach and copy only mutable geometry (positions, sizes, pin offsets,
weights). Connectivity, names and mapping stay as read-only views into the
shared buffer. Dynamic RC/STA and GPU tensors are never stored here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

import numpy as np
from multiprocessing import resource_tracker, shared_memory

LOG = logging.getLogger(__name__)
SCHEMA = 1
MAGIC = 'DREAMPLACE_TWODB_SHM_V1'
ALIGN = 64
MUTABLE_SUFFIXES = {
    'node_x', 'node_y', 'node_orient', 'node_size_x', 'node_size_y',
    'pin_offset_x', 'pin_offset_y', 'net_weights',
}


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_hash(data):
    return hashlib.sha256(data).hexdigest()


def _align(offset):
    return (offset + ALIGN - 1) // ALIGN * ALIGN


def shm_name_for(directory):
    digest = hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()[:16]
    return ('/dp2db_' + digest)[:30]


def _release_tracker(shm):
    """Clients must not unlink a segment the master created.

    multiprocessing.shared_memory always registers the name. On process
    exit the resource tracker then shm_unlink()s it, which is why a
    crashed client can delete the master's snapshot.
    """
    try:
        resource_tracker.unregister(shm._name, 'shared_memory')
    except Exception:
        pass


class SharedSnapshot:
    def __init__(self, shm, manifest, writable=False):
        self.shm = shm
        self.manifest = manifest
        self.writable = writable
        self.buf = shm.buf

    def close(self, unlink=False):
        self.shm.close()
        if unlink:
            try:
                self.shm.unlink()
            except FileNotFoundError:
                pass

    def _entry(self, key):
        try:
            return self.manifest['entries'][key]
        except KeyError as exc:
            raise KeyError('shared snapshot missing ' + key) from exc

    def blob(self, key):
        info = self._entry(key)
        if info['kind'] != 'blob':
            raise TypeError(key + ' is not a blob')
        start, size = info['offset'], info['size']
        data = bytes(self.buf[start:start + size])
        if bytes_hash(data) != info['sha256']:
            raise ValueError('shared snapshot checksum mismatch: ' + key)
        return data

    def array(self, key, copy=None):
        info = self._entry(key)
        if info['kind'] != 'array':
            raise TypeError(key + ' is not an array')
        view = np.ndarray(info['shape'], dtype=np.dtype(info['dtype']),
                          buffer=self.buf, offset=info['offset'])
        if view.nbytes != info['size']:
            raise ValueError('shared snapshot size mismatch: ' + key)
        if copy is None:
            copy = Path(key).name in MUTABLE_SUFFIXES
        return np.array(view, copy=True) if copy else view

    def has(self, key):
        return key in self.manifest['entries']

    def materialize(self, key, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = self.blob(key)
        fd, tmp = tempfile.mkstemp(prefix='.shm-', dir=str(dest.parent))
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return dest


def collect_two_db_files(timing_db, placement_db, mapping):
    """Return {logical_name: path} for the immutable two-DB snapshot files."""
    from TimingCache import checked_model
    from dreamplace.ops.timing import timing_cpp

    timing_root = Path(timing_db).resolve()
    placement_root = Path(placement_db).resolve()
    mapping_root = Path(mapping).resolve()
    files = {}
    for prefix, root in (('timing', timing_root / 'physical_db'),
                         ('placement', placement_root / 'physical_db')):
        if not (root / 'manifest.json').is_file():
            raise FileNotFoundError('missing ' + str(root / 'manifest.json'))
        for path in sorted(root.iterdir()):
            if path.is_file():
                files['%s/physical_db/%s' % (prefix, path.name)] = path
    model, cache_manifest = checked_model(timing_root / 'timing_cache', timing_cpp)
    files['timing_cache/manifest.json'] = timing_root / 'timing_cache' / 'manifest.json'
    files['timing_cache/model.bin'] = Path(model)
    files['_timing_cache_model_name'] = None  # placeholder, filled below
    if not (mapping_root / 'manifest.json').is_file():
        raise FileNotFoundError('missing mapping manifest: ' + str(mapping_root))
    report = json.loads((mapping_root / 'manifest.json').read_text())
    files['mapping/manifest.json'] = mapping_root / 'manifest.json'
    for field, info in report.get('arrays', {}).items():
        path = mapping_root / info['file']
        if not path.is_file():
            raise FileNotFoundError(path)
        files['mapping/' + info['file']] = path
    files.pop('_timing_cache_model_name', None)
    sources = {
        'timing_db': str(timing_root),
        'placement_db': str(placement_root),
        'mapping': str(mapping_root),
        'timing_model': cache_manifest['model'],
        'file_sha256': {name: file_hash(path) for name, path in files.items()},
    }
    return files, sources


def _reusable_snapshot_dir(out):
    """True if output is empty or a previous master snapshot we can replace."""
    if not out.exists():
        return True
    if not out.is_dir():
        return False
    allowed = {'manifest.json', 'ready', 'runtime'}
    names = {path.name for path in out.iterdir()}
    if not names or names <= allowed:
        return True
    manifest = out / 'manifest.json'
    if not manifest.is_file():
        return False
    try:
        data = json.loads(manifest.read_text())
    except Exception:
        return False
    return data.get('magic') == MAGIC


def publish(files, output, name=None, sources=None):
    """Copy snapshot files into a POSIX shm segment and write a tiny manifest.

    Restarting the master reuses the same shared_memory_dir; only the POSIX
    segment and manifest.json are replaced.
    """
    out = Path(output)
    if not _reusable_snapshot_dir(out):
        raise FileExistsError('Choose a new shared_memory_dir: ' + str(out))
    out.mkdir(parents=True, exist_ok=True)
    name = name or shm_name_for(out)
    entries = {}
    packed = []
    offset = 0
    for logical, path in files.items():
        raw = Path(path).read_bytes()
        if logical.endswith('.npy'):
            array = np.load(path, allow_pickle=False)
            array = np.ascontiguousarray(array)
            offset = _align(offset)
            entries[logical] = dict(kind='array', offset=offset, size=int(array.nbytes),
                                    shape=list(array.shape), dtype=str(array.dtype),
                                    sha256=bytes_hash(array.tobytes()), source_sha256=file_hash(path))
            packed.append(('array', offset, array))
            offset += array.nbytes
        else:
            offset = _align(offset)
            entries[logical] = dict(kind='blob', offset=offset, size=len(raw),
                                    sha256=bytes_hash(raw), source_sha256=file_hash(path))
            packed.append(('blob', offset, raw))
            offset += len(raw)
    try:
        stale = shared_memory.SharedMemory(name=name)
    except FileNotFoundError:
        stale = None
    if stale is not None:
        stale.close()
        stale.unlink()
    shm = shared_memory.SharedMemory(name=name, create=True, size=max(offset, 1))
    try:
        for kind, start, payload in packed:
            if kind == 'array':
                view = np.ndarray(payload.shape, dtype=payload.dtype, buffer=shm.buf, offset=start)
                view[...] = payload
            else:
                shm.buf[start:start + len(payload)] = payload
        manifest = dict(schema=SCHEMA, magic=MAGIC, shm_name=name, bytes=offset,
                        entries=entries, sources=sources or {},
                        note='Read-only snapshot. Positions/weights/STA stay per client.')
        (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    except Exception:
        shm.close()
        shm.unlink()
        raise
    LOG.info('shared snapshot published: %s (%d entries, %.2f GB, name=%s)',
             out, len(entries), offset / 1e9, name)
    return SharedSnapshot(shm, manifest, writable=True)


def attach(directory):
    root = Path(directory)
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('schema') != SCHEMA or manifest.get('magic') != MAGIC:
        raise ValueError('Unsupported shared snapshot manifest: ' + str(root))
    try:
        shm = shared_memory.SharedMemory(name=manifest['shm_name'])
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            'shared snapshot %s is gone; restart the master with '
            '--shared_memory_role master --shared_memory_dir %s'
            % (manifest['shm_name'], root)
        ) from exc
    _release_tracker(shm)
    if shm.size < int(manifest['bytes']):
        shm.close()
        raise ValueError('shared memory segment is smaller than the manifest')
    return SharedSnapshot(shm, manifest)


def load_physical(store, prefix, copy_mutable=True):
    """Rebuild a MakeDB physical namespace from SHM. Mutable fields are copied."""
    from MakeDBAdapter import FLOATS, INTS, STRINGS, SCHEMA as PHYS_SCHEMA, _maps

    metadata = json.loads(store.blob(prefix + '/physical_db/manifest.json').decode())
    if metadata.pop('schema') != PHYS_SCHEMA:
        raise ValueError('Unsupported physical DB schema in shared snapshot')
    for field in FLOATS + INTS + STRINGS:
        key = '%s/physical_db/%s.npy' % (prefix, field)
        copy = copy_mutable and field in MUTABLE_SUFFIXES
        metadata[field] = store.array(key, copy=copy)
    metadata['cell_info'] = {}
    metadata['ext_pin_info'] = {}
    metadata['def_template'] = ''
    if store.has(prefix + '/physical_db/template.def'):
        runtime = Path(tempfile.gettempdir()) / ('dp_shm_' + store.manifest['shm_name'].lstrip('/'))
        metadata['def_template_path'] = str(store.materialize(
            prefix + '/physical_db/template.def', runtime / (prefix + '_template.def')))
    else:
        metadata['def_template_path'] = ''
    metadata.setdefault('net_uses', ['SIGNAL'] * len(metadata['net_names']))
    return _maps(SimpleNamespace(**metadata))
