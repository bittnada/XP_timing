"""Membership-ID mapping and saved physical-net boundary extraction."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from ClusterLEFMapping import object_items
from ReducedLEF import generate, parse_args
from ReducedLiberty import ModelError
from ReducedVerilog import load_mapping, reduce_verilog


class MembershipLEFTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.save = self.root / 'save'; self.save.mkdir()
        (self.root / 'clusters.txt').write_text('42\t7\n10\t7\n')
        (self.root / 'names.tsv').write_text('cell_id\tcell_name\n10\tu2\n42\tu1\n50\tkeep\n')
        # Neither dictionary order nor node_names has the membership ID order.
        (self.save / 'node_names.txt').write_text('keep\nu1\nu2\n')
        self.cells = {'u2': {'macro_id': 'INV'}, 'keep': {'macro_id': 'INV'}, 'u1': {'macro_id': 'INV'}}
        self.lef = {'INV': {'pin': {'A': {'direction': 'INPUT'}, 'Y': {'direction': 'OUTPUT'}}}}
        self.ports = {'a': {'direction': 'INPUT'}, 'y': {'direction': 'OUTPUT'}}
        self.nets = {'a': {'cell_list': ['PIN a', 'u1 A', 'keep A']},
                     'n': {'cell_list': ['u1 Y', 'u2 A']},
                     'y': {'cell_list': ['u2 Y', 'PIN y']}}
        self.flush()
        (self.root / 'sizes.tsv').write_text('7\t4\t2\n')
        self.args = parse_args(['--input', str(self.root / 'clusters.txt'),
            '--cell-names', str(self.root / 'names.tsv'), '--saved-db', str(self.save),
            '--sizes', str(self.root / 'sizes.tsv'), '--pin-layer', 'metal2',
            '--output', str(self.root / 'cluster.lef')])

    def flush(self):
        for name, obj in [('cells_info', self.cells), ('lef_info', self.lef),
                          ('netlist_info', self.nets), ('ext_pin_info', self.ports)]:
            (self.save / (name + '.json')).write_text(json.dumps(obj))

    def generate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return generate(self.args)

    def test_exact_ids_names_masters_boundary_and_verilog(self):
        report = self.generate()
        clusters, owners = load_mapping(report['mapping_output'])
        c = clusters[7]['source']
        self.assertEqual([(m['cell_id'], m['cell_name'], m['original_master']) for m in c['members']],
                         [(10, 'u2', 'INV'), (42, 'u1', 'INV')])
        self.assertEqual(c['inputs'], [{'pin': 'I0', 'net': 'a', 'original_pins': ['u1:A']}])
        self.assertEqual(c['outputs'], [{'pin': 'O0', 'net': 'y', 'original_pin': 'u2:Y'}])
        self.assertFalse(c['timing_characterized'])
        self.assertEqual(report['input_format'], 'membership')
        self.assertEqual(report['membership']['scanned_nets'], 3)
        table = Path(report['cell_table_output']).read_text()
        self.assertEqual(table.splitlines()[1:], ['10\tu2\t7\tINV\t', '42\tu1\t7\tINV\t'])
        original = self.root / 'original.v'
        original.write_text('module top(a,y); input a; output y; wire n,z; '
            'INV u1(.A(a),.Y(n)); INV u2(.A(n),.Y(y)); INV keep(.A(a),.Y(z)); endmodule')
        reduce_verilog(original, report['mapping_output'], self.root / 'reduced.v')
        self.assertIn('TC_7 __tc_cluster_7 (.I0(a), .O0(y))', (self.root / 'reduced.v').read_text())

    def test_separate_paths_and_source_lef(self):
        self.args.saved_db = None
        for field in ('cells_info', 'lef_info', 'netlist_info', 'ext_pin_info'):
            setattr(self.args, field, str(self.save / (field + '.json')))
        source = self.root / 'cells.lef'; source.write_text('MACRO INV\nEND INV\n')
        self.args.source_lef = [str(source)]
        report = self.generate()
        self.assertIn(str(source.resolve()), Path(report['cell_table_output']).read_text())
        self.assertFalse(report['technology_validated'])

    def test_intercluster_net_and_deterministic_numbering(self):
        (self.root / 'clusters.txt').write_text('42 8\n10 7\n')
        (self.root / 'sizes.tsv').write_text('8\t2\t2\n7\t2\t2\n')
        report = self.generate()
        clusters, _ = load_mapping(report['mapping_output'])
        self.assertEqual(list(clusters), [7, 8])
        self.assertEqual(clusters[8]['pins'], {'I0': 'a', 'O0': 'n'})
        self.assertEqual(clusters[7]['pins'], {'I0': 'n', 'O0': 'y'})

    def test_missing_metadata_or_names_do_not_write(self):
        for field in ('cell_names', 'cells_info', 'lef_info', 'netlist_info', 'ext_pin_info'):
            saved = getattr(self.args, field)
            setattr(self.args, field, str(self.root / 'missing'))
            with self.subTest(field=field), self.assertRaises(ModelError):
                self.generate()
            self.assertFalse(Path(self.args.output).exists())
            setattr(self.args, field, saved)
        self.cells.pop('u1'); self.flush()
        with self.assertRaisesRegex(ModelError, 'ID namespace'):
            self.generate()

    def test_missing_pin_inout_and_multiple_driver_errors(self):
        for direction in ('INOUT', 'OUTPUT', None):
            self.lef['INV']['pin']['A']['direction'] = direction
            self.flush()
            with self.subTest(direction=direction), self.assertRaises(ModelError):
                self.generate()
            self.assertFalse(Path(self.args.output).exists())
        self.lef['INV']['pin']['A']['direction'] = 'INPUT'
        self.nets['n']['cell_list'].append('keep Y'); self.flush()
        with self.assertRaisesRegex(ModelError, 'one driver'):
            self.generate()

    def test_missing_boundary_duplicate_pin_and_supply_ignored(self):
        self.nets['y']['cell_list'] = ['u2 Y']
        self.flush()
        with self.assertRaisesRegex(ModelError, 'boundary input and output'):
            self.generate()
        self.nets['y']['cell_list'] = ['u2 Y', 'PIN y']
        self.nets['duplicate'] = {'cell_list': ['PIN a', 'u1 A']}; self.flush()
        with self.assertRaisesRegex(ModelError, 'multiple nets'):
            self.generate()
        self.nets.pop('duplicate')
        self.nets['VDD'] = {'cell_list': ['u1 VDD', 'u2 VDD'], 'use': 'USE POWER'}; self.flush()
        report = self.generate()
        self.assertNotIn('VDD', Path(report['mapping_output']).read_text())

    def test_sidecar_collision_preserves_files(self):
        target = Path(self.args.output + '.cells.tsv'); target.write_text('user data')
        with self.assertRaisesRegex(ModelError, 'already exists'):
            self.generate()
        self.assertEqual(target.read_text(), 'user data')
        self.assertFalse(Path(self.args.output).exists())

    def test_json_stream_across_small_chunks_and_invalid_input(self):
        path = self.root / 'stream.json'
        obj = {'a': {'long': 'x' * 200, 'special': '\\"\n{}'}, 'b': [1, 2], 'c': 12345, 'd': None}
        path.write_text(json.dumps(obj, indent=2))
        for chunk in (1, 7, 4096):
            self.assertEqual(dict(object_items(path, chunk)), obj)
        for invalid in ('{"a":', '{"a":1,}', '{} garbage', '[]', '{"a": 1 "b": 2}'):
            path.write_text(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ModelError):
                list(object_items(path, 4))


if __name__ == '__main__':
    unittest.main()
