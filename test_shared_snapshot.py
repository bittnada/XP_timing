"""Publish/attach a tiny two-DB snapshot without running placement."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from MakeDBAdapter import FLOATS, INTS, STRINGS, SCHEMA
from SharedSnapshot import attach, file_hash, load_physical, publish


def tiny_physical(root):
    n, p, m = 2, 2, 1
    values = {
        'node_x': np.array([1., 3.]), 'node_y': np.array([2., 4.]),
        'node_size_x': np.array([2., 2.]), 'node_size_y': np.array([2., 2.]),
        'pin_offset_x': np.array([0., 0.]), 'pin_offset_y': np.array([1., 1.]),
        'net_weights': np.array([1.]), 'rows': np.array([[0., 0., 20., 10.]]),
        'flat_region_boxes': np.zeros((0, 2)),
        'node_size_x_LEF': np.array([2., 2.]), 'node_size_y_LEF': np.array([2., 2.]),
        'node2orig_node_map': np.array([0, 1], np.int32),
        'flat_net2pin_map': np.array([0, 1], np.int32),
        'flat_net2pin_start_map': np.array([0, 2], np.int32),
        'flat_node2pin_map': np.array([0, 1], np.int32),
        'flat_node2pin_start_map': np.array([0, 1, 2], np.int32),
        'pin2node_map': np.array([0, 1], np.int32),
        'pin2net_map': np.array([0, 0], np.int32),
        'flat_region_boxes_start': np.array([0], np.int32),
        'node2fence_region_map': np.array([0, 0], np.int32),
        'node_names': np.array(['u0', 'u1']), 'pin_names': np.array(['u0 Y', 'u1 A']),
        'pin_direct': np.array(['OUTPUT', 'INPUT']), 'net_names': np.array(['n0']),
        'node_orient': np.array(['N', 'N']),
        'num_physical_nodes': n, 'num_terminals': 0, 'num_terminal_NIs': 0,
        'num_movable_pins': p, 'num_fake_macro': 0, 'num_blockage': 0,
        'num_movable_std_cell': 2, 'num_movable_macro': 0,
        'num_fixed_std_cell': 0, 'num_fixed_macro': 0,
        'xl': 0., 'yl': 0., 'xh': 20., 'yh': 20., 'row_height': 10., 'site_width': 1.,
        'lef_scale': 2000, 'def_scale': 2000, 'total_space_area': 400.,
        'design_name': 'tiny', 'row_orients': [], 'placement_only_nets': [],
        'net_uses': ['SIGNAL'], 'congestion_metadata': {},
        'def_template': 'DESIGN tiny ;\n',
    }
    phys = root / 'physical_db'
    phys.mkdir(parents=True)
    for field in FLOATS + INTS + STRINGS:
        dtype = np.float64 if field in FLOATS else np.int32 if field in INTS else str
        np.save(phys / (field + '.npy'), np.asarray(values[field], dtype=dtype), allow_pickle=False)
    meta = {name: values[name] for name in (
        'num_physical_nodes num_terminals num_terminal_NIs num_movable_pins '
        'num_fake_macro num_blockage num_movable_std_cell num_movable_macro '
        'num_fixed_std_cell num_fixed_macro xl yl xh yh row_height site_width '
        'lef_scale def_scale total_space_area').split()}
    meta.update(schema=SCHEMA, design_name='tiny', row_orients=[],
                placement_only_nets=[], net_uses=['SIGNAL'], congestion_metadata={})
    (phys / 'template.def').write_text(values['def_template'])
    (phys / 'manifest.json').write_text(json.dumps(meta))
    return root


class SharedSnapshotTest(unittest.TestCase):
    def test_publish_attach_views_and_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            timing = tiny_physical(tmp / 'timing')
            placement = tiny_physical(tmp / 'placement')
            files = {}
            for prefix, root in (('timing', timing), ('placement', placement)):
                for path in (root / 'physical_db').iterdir():
                    files['%s/physical_db/%s' % (prefix, path.name)] = path
            files['timing_cache/model.bin'] = tmp / 'model.bin'
            files['timing_cache/model.bin'].write_bytes(b'fake-ot-model')
            files['timing_cache/manifest.json'] = tmp / 'cache.json'
            files['timing_cache/manifest.json'].write_text('{"model":"model.bin"}\n')
            mapping = tmp / 'map'
            mapping.mkdir()
            arr = np.array([0, 1], dtype=np.int64)
            np.save(mapping / 'timing_node_to_placement_node.npy', arr)
            (mapping / 'manifest.json').write_text(json.dumps({
                'schema': 1, 'status': 'complete',
                'arrays': {'timing_node_to_placement_node': {
                    'file': 'timing_node_to_placement_node.npy',
                    'sha256': file_hash(mapping / 'timing_node_to_placement_node.npy'),
                    'length': 2}}}))
            files['mapping/manifest.json'] = mapping / 'manifest.json'
            files['mapping/timing_node_to_placement_node.npy'] = (
                mapping / 'timing_node_to_placement_node.npy')
            out = tmp / 'shm'
            store = publish(files, out, name='dp2db_test_tiny')
            client = attach(out)
            self.assertEqual(client.blob('timing_cache/model.bin'), b'fake-ot-model')
            db = load_physical(client, 'placement')
            np.testing.assert_array_equal(db.node_x, [1., 3.])
            self.assertEqual(list(db.node_names), ['u0', 'u1'])
            db.node_x[0] = 99
            again = load_physical(client, 'placement')
            self.assertEqual(again.node_x[0], 1.)
            conn = client.array('placement/physical_db/pin2node_map.npy', copy=False)
            self.assertIsNotNone(conn.base)
            client.close()
            script = (
                'from SharedSnapshot import attach\n'
                'c = attach(%r)\n'
                'c.close()\n'
            ) % str(out)
            env = os.environ.copy()
            env['PYTHONPATH'] = str(Path(__file__).resolve().parent)
            subprocess.check_call(
                [sys.executable, '-c', script],
                cwd=str(Path(__file__).resolve().parent),
                env=env,
            )
            still = attach(out)
            still.close()
            store.close(unlink=True)
            again_store = publish(files, out, name='dp2db_test_tiny')
            self.addCleanup(lambda: again_store.close(unlink=True))
            again_client = attach(out)
            self.addCleanup(again_client.close)
            self.assertEqual(again_client.blob('timing_cache/model.bin'), b'fake-ot-model')


if __name__ == '__main__':
    unittest.main()
