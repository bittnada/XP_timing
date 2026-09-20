"""Original/reduced ID maps: permutations, filtered nets and stale-cache rejection."""
import contextlib
import copy
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

import PlacementTimingMapping as mapping


def view(nodes, nets, pin_rows):
    return SimpleNamespace(node_names=np.array(nodes), net_names=np.array(nets),
                           pin_names=np.array([r[0] for r in pin_rows]),
                           pin2node_map=np.array([nodes.index(r[1]) for r in pin_rows], dtype=np.int32),
                           pin2net_map=np.array([nets.index(r[2]) for r in pin_rows], dtype=np.int32))


class MappingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sidecars = self.root / 'cluster'; self.sidecars.mkdir()
        self.output = self.root / 'mapping'
        (self.sidecars / 'manifest.json').write_text(json.dumps(dict(
            status='complete', model='placement_only_per_cluster_net_pin')))
        (self.sidecars / 'cell_mapping.tsv').write_text(
            'original_cell_name\treduced_cell_name\tcluster_id\n'
            'u1\tC7\t7\nu2\tC7\t7\n')
        (self.sidecars / 'net_mapping.tsv').write_text(
            'original_net_name\treduced_net_name\treduced_degree\n'
            'a\ta\t2\nn\tn\t1\ny\ty\t2\n')
        (self.sidecars / 'pin_mapping.tsv').write_text(
            'original_cell_name\toriginal_pin_name\tnet_name\treduced_cell_name\treduced_pin_name\n'
            'PIN\ta\ta\tPIN\ta\nu1\tA\ta\tC7\tVP0\n'
            'u1\tY\tn\tC7\tVP1\nu2\tA\tn\tC7\tVP1\n'
            'u2\tY\ty\tC7\tVP2\nPIN\ty\ty\tPIN\ty\n')
        self.original = view(['y', 'u2', 'a', 'u1'], ['y', 'n', 'a'], [
            ('u2 Y', 'u2', 'y'), ('u1 A', 'u1', 'a'), ('PIN y', 'y', 'y'),
            ('u2 A', 'u2', 'n'), ('PIN a', 'a', 'a'), ('u1 Y', 'u1', 'n')])
        self.reduced = view(['y', 'C7', 'a'], ['y', 'a'], [
            ('PIN y', 'y', 'y'), ('C7 VP0', 'C7', 'a'),
            ('C7 VP2', 'C7', 'y'), ('PIN a', 'a', 'a')])

    def build(self, **kw):
        return mapping.build(self.original, self.reduced, self.sidecars, self.output, **kw)

    def save_snapshot(self, path, db):
        path.mkdir(parents=True)
        for field in mapping.FIELDS:
            np.save(path / (field + '.npy'), getattr(db, field), allow_pickle=False)
        (path / 'manifest.json').write_text(json.dumps(dict(schema=1, num_physical_nodes=len(db.node_names))))
        return path

    def test_independent_id_orders_missing_internal_net_and_roundtrip(self):
        report = self.build()
        self.assertEqual(report['counts']['omitted_single_endpoint_nets'], 1)
        self.assertEqual(report['counts']['omitted_pins_on_single_endpoint_nets'], 2)
        arrays = mapping.load_mapping(self.output, self.original, self.reduced)
        expected = dict(timing_node_to_placement_node=[0, 1, 2, 1],
                        timing_node_cluster_id=[-1, 7, -1, 7],
                        timing_pin_to_placement_pin=[2, 1, 0, -1, 3, -1],
                        timing_pin_to_placement_node=[1, 1, 0, 1, 2, 1],
                        timing_net_to_placement_net=[0, -1, 1],
                        placement_net_to_timing_net=[0, 2],
                        placement_node_to_timing_nodes=[0, 1, 3, 2],
                        placement_node_to_timing_nodes_start=[0, 1, 3, 4],
                        placement_pin_to_timing_pins=[2, 1, 0, 4],
                        placement_pin_to_timing_pins_start=[0, 1, 2, 3, 4])
        for field, value in expected.items():
            np.testing.assert_array_equal(arrays[field], value, err_msg=field)
        with (self.output / 'pin_mapping.tsv').open() as stream:
            table = list(csv.DictReader(stream, delimiter='\t'))
        self.assertEqual(table[3]['placement_pin_id'], '-1')
        self.assertEqual(table[3]['placement_node_id'], '1')
        self.assertIn('omitted', table[3]['status'])

    def test_many_original_pins_to_one_retained_virtual_pin(self):
        self.reduced = view(['y', 'C7', 'a'], ['y', 'a', 'n'], [
            ('PIN y', 'y', 'y'), ('C7 VP0', 'C7', 'a'),
            ('C7 VP2', 'C7', 'y'), ('PIN a', 'a', 'a'), ('C7 VP1', 'C7', 'n')])
        self.build()
        a = mapping.load_mapping(self.output, self.original, self.reduced)
        start, end = a['placement_pin_to_timing_pins_start'][4:6]
        np.testing.assert_array_equal(a['placement_pin_to_timing_pins'][start:end], [3, 5])

    def test_bytes_names_and_position_changes_do_not_invalidate(self):
        self.build()
        other = copy.deepcopy(self.original)
        for field in ('node_names', 'pin_names', 'net_names'):
            setattr(other, field, getattr(other, field).astype('S'))
        other.node_x = np.arange(4) + 100
        other.net_weights = np.arange(3) + 9
        mapping.load_mapping(self.output, other, self.reduced)

    def test_reordered_ids_or_changed_connectivity_rejected(self):
        self.build()
        for field in ('node_names', 'pin_names', 'net_names', 'pin2node_map', 'pin2net_map'):
            other = copy.deepcopy(self.original)
            value = getattr(other, field).copy()
            value[[0, 1]] = value[[1, 0]]
            setattr(other, field, value)
            with self.subTest(field=field), self.assertRaisesRegex(mapping.MappingError, 'differs'):
                mapping.load_mapping(self.output, other, self.reduced)

    def test_array_corruption_and_no_overwrite(self):
        self.build()
        with self.assertRaisesRegex(mapping.MappingError, 'already exists'):
            self.build()
        path = self.output / 'timing_node_to_placement_node.npy'
        data = np.load(path); data[0] = 2; np.save(path, data)
        with self.assertRaisesRegex(mapping.MappingError, 'checksum'):
            mapping.load_mapping(self.output, self.original, self.reduced)

    def test_negative_index_even_with_updated_checksum_rejected(self):
        self.build()
        field = 'timing_node_to_placement_node'
        path = self.output / (field + '.npy')
        data = np.load(path); data[0] = -1; np.save(path, data)
        manifest_path = self.output / 'manifest.json'
        report = json.loads(manifest_path.read_text())
        report['arrays'][field]['sha256'] = mapping.file_hash(path)
        manifest_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(mapping.MappingError, 'length/range'):
            mapping.load_mapping(self.output, self.original, self.reduced)

    def test_model_only_directory_rejected(self):
        with self.assertRaisesRegex(mapping.MappingError, 'model.bin alone'):
            mapping.snapshot(self.root)

    def test_cross_cluster_collision_rejected(self):
        path = self.sidecars / 'cell_mapping.tsv'
        path.write_text(path.read_text().replace('u2\tC7\t7', 'u2\tC7\t8'))
        with self.assertRaisesRegex(mapping.MappingError, 'Different clusters'):
            self.build()

    def test_missing_node_and_unexplained_original_net_rejected(self):
        original = self.original.node_names.copy()
        self.original.node_names[3] = 'u3'
        self.original.pin_names[1] = 'u3 A'; self.original.pin_names[5] = 'u3 Y'
        with self.assertRaisesRegex(mapping.MappingError, 'coordinate source'):
            self.build()
        self.original.node_names = original
        self.original.pin_names[1] = 'u1 A'; self.original.pin_names[5] = 'u1 Y'
        path = self.sidecars / 'net_mapping.tsv'
        path.write_text(path.read_text().replace('a\ta\t2\n', ''))
        with self.assertRaisesRegex(mapping.MappingError, 'Original net missing'):
            self.build()

    def test_explicit_synthetic_identity_mapping(self):
        # Existing MakeDB virtual connections need not appear in source DEF TSVs.
        pin_path = self.sidecars / 'pin_mapping.tsv'
        pin_path.write_text(pin_path.read_text().replace('u1\tA\ta\tC7\tVP0\n', '').replace('PIN\ta\ta\tPIN\ta\n', ''))
        net_path = self.sidecars / 'net_mapping.tsv'
        net_path.write_text(net_path.read_text().replace('a\ta\t2\n', ''))
        # Both DBs must agree on names and ownership for the unlisted net.
        self.original.node_names[3] = 'C7'
        self.original.pin_names = self.original.pin_names.astype('U32')
        self.original.pin_names[1] = 'C7 VP0'; self.original.pin_names[5] = 'C7 VP1'
        cell_path = self.sidecars / 'cell_mapping.tsv'
        cell_path.write_text(cell_path.read_text().replace('u1\tC7', 'C7\tC7'))
        pin_path.write_text(pin_path.read_text().replace('u1\tY', 'C7\tVP1'))
        self.original.placement_only_nets = ['a']
        self.reduced.placement_only_nets = ['a']
        self.build()
        mapping.load_mapping(self.output, self.original, self.reduced)

    def test_bad_pin_mapping_or_absent_boundary_fails_without_output(self):
        path = self.sidecars / 'pin_mapping.tsv'; before = path.read_text()
        for text, message in [
            (before.replace('u1\tA\ta\tC7\tVP0', 'u1\tA\ty\tC7\tVP0'), 'disagrees'),
            (before.replace('u1\tA\ta\tC7\tVP0', 'u1\tA\ta\tC7\tVP2'), 'wrong placement'),
            (before.replace('u1\tA\ta\tC7\tVP0', 'u1\tA\ta\tC7\tmissing'), 'active net missing'),
            (before.replace('u1\tA\ta\tC7\tVP0\n', ''), 'missing from cluster'),
            (before + 'u1\tA\ta\tC7\tVP0\n', 'Duplicate'),
        ]:
            path.write_text(text)
            with self.subTest(message=message), self.assertRaisesRegex(mapping.MappingError, message):
                self.build()
            self.assertFalse(self.output.exists())

    def test_incomplete_sidecar_and_net_merging_rejected(self):
        path = self.sidecars / 'net_mapping.tsv'; before = path.read_text()
        path.write_text(before.replace('n\tn\t1', 'n\ty\t1'))
        with self.assertRaisesRegex(mapping.MappingError, 'merging'):
            self.build()
        path.write_text(before.replace('n\tn\t1', 'n\tn\t2'))
        with self.assertRaisesRegex(mapping.MappingError, 'Nontrivial net missing'):
            self.build()
        path.write_text(before)
        (self.sidecars / 'manifest.json').write_text('{"status":"incomplete"}')
        with self.assertRaisesRegex(mapping.MappingError, 'completed'):
            self.build()

    def test_native_snapshot_cli_and_no_tsv(self):
        original = self.save_snapshot(self.root / 'original/physical_db', self.original)
        reduced = self.save_snapshot(self.root / 'reduced/physical_db', self.reduced)
        result = subprocess.run([sys.executable, '-B', str(Path(mapping.__file__)),
                                 '--timing-db', str(original.parent), '--placement-db', str(reduced),
                                 '--cluster-data', str(self.sidecars), '--output', str(self.output), '--no-tsv'],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.output / 'node_mapping.tsv').exists())
        mapping.load_mapping(self.output, original.parent, reduced)

    def test_legacy_text_identity_only_without_pickle(self):
        path = self.root / 'legacy'; path.mkdir()
        for field in mapping.FIELDS:
            a = getattr(self.original, field)
            (path / (field + '.txt')).write_text('\n'.join(map(str, a)) + '\n')
        restored = mapping.snapshot(path)
        self.assertEqual(mapping.fingerprint(restored), mapping.fingerprint(self.original))

    def test_duplicate_names_and_bad_pin_owner(self):
        self.original.node_names[0] = 'u1'
        with self.assertRaisesRegex(mapping.MappingError, 'Duplicate'):
            self.build()
        self.original.node_names[0] = 'y'
        self.original.pin2node_map[0] = 0
        with self.assertRaisesRegex(mapping.MappingError, 'disagrees with node owner'):
            self.build()

    def test_real_makedb_snapshots_and_cluster_converter(self):
        from ClusterPlacement import generate, parse_args
        from test_makedb_adapter import fixture
        import MakeDBAdapter
        params = fixture(self.root)
        with contextlib.redirect_stdout(io.StringIO()):
            _, original = MakeDBAdapter.read(params)
            MakeDBAdapter.save(original, params)
        original_path = params.save_path
        (self.root / 'membership.tsv').write_text('100 7\n200 7\n')
        (self.root / 'names.tsv').write_text('100 u1\n200 u2\n')
        output = self.root / 'generated'
        generate(parse_args(['--clusters', str(self.root / 'membership.tsv'), '--cell-names', str(self.root / 'names.tsv'),
                             '--saved-db', original_path, '--def-input', params.def_path,
                             '--lef-input', str(self.root / 'tiny.lef'), '--utilization', '.5',
                             '--pin-layer', 'metal1', '--output', str(output)]))
        params.save_path = str(self.root / 'reduced_saved')
        params.def_path = str(output / 'reduced.def')
        params.lef_dir_path = [str(output / 'placement.lef')]
        with contextlib.redirect_stdout(io.StringIO()):
            _, reduced = MakeDBAdapter.read(params)
            MakeDBAdapter.save(reduced, params)
        mapping.build(original_path, params.save_path, output, self.output)
        arrays = mapping.load_mapping(self.output, original, reduced)
        np.testing.assert_array_equal(arrays['timing_net_to_placement_net'], [0, -1, 1])
        # Existing physical snapshots have not been rewritten by the mapper.
        mapping.load_mapping(self.output, original_path, params.save_path)


if __name__ == '__main__':
    unittest.main()
