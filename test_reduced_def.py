"""Reduced physical/logical consistency and DEF reader integration."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from ReducedDEF import ModelError, discover_lefs, events, generate, parse_args
import ReducedLEF
from ReducedVerilog import reduce_verilog
from test_makedb_adapter import fixture
from test_reduced_lef import TECH
from test_reduced_verilog import entry


class ReducedDEFTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.p = fixture(self.root)
        self.original = Path(self.p.def_path)
        self.mapping = self.root / 'cluster_mapping.jsonl'
        self.mapping.write_text(json.dumps(entry()) + '\n')
        self.v = self.root / 'original.v'
        self.v.write_text('module top(a,y); input a; output y; wire n; '
                          'INV u1(.A(a),.Y(n)); INV u2(.A(n),.Y(y)); endmodule')
        self.reduced = self.root / 'reduced.v'
        reduce_verilog(self.v, self.mapping, self.reduced)
        (self.root / 'sizes.tsv').write_text('cluster_id\twidth\theight\n7\t2\t1\n')
        (self.root / 'tech.lef').write_text(TECH)
        self.lef = self.root / 'cluster.lef'
        ReducedLEF.generate(ReducedLEF.parse_args([
            '--input', str(self.mapping), '--sizes', str(self.root / 'sizes.tsv'),
            '--tech-lef', str(self.root / 'tech.lef'), '--pin-layer', 'metal2',
            '--output', str(self.lef)]))
        self.argv = ['--input', str(self.original), '--verilog', str(self.reduced),
                     '--mapping', str(self.mapping), '--cluster-lef', str(self.lef),
                     '--output', str(self.root / 'reduced.def')]
        self.args = parse_args(self.argv)

    def change(self, old, new):
        text = self.original.read_text()
        self.assertIn(old, text)
        self.original.write_text(text.replace(old, new))

    def run_generate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return generate(self.args)

    def test_rewrite_and_makedb_round_trip(self):
        import MakeDBAdapter
        before = self.original.read_text()
        report = self.run_generate()
        text = Path(self.args.output).read_text()
        self.assertEqual(self.original.read_text(), before)
        self.assertEqual(report['reduced_components'], 1)
        self.assertEqual(report['reduced_nets'], 2)
        self.assertIn('__tc_cluster_7 TC_7 + PLACED ( 2000 0 ) N', text)
        self.assertNotIn('( u1 ', text)
        self.assertNotIn('( u2 ', text)
        self.assertNotIn('- n\n', text)
        self.assertIn('( __tc_cluster_7 I0 )', text)
        self.assertIn('( __tc_cluster_7 O0 )', text)
        for section in ('PINS',):
            a = [t for k, s, t, _ in events(self.original) if s == section]
            b = [t for k, s, t, _ in events(self.args.output) if s == section]
            self.assertEqual(a, b)
        for line in before.splitlines():
            if line.startswith(('ROW ', 'DIEAREA ', 'UNITS ')):
                self.assertIn(line, text)
        self.p.def_path = self.args.output
        self.p.lef_dir_path += [str(self.root / 'tech.lef'), str(self.lef)]
        self.p.mode = 'default'
        with contextlib.redirect_stdout(io.StringIO()):
            _, db = MakeDBAdapter.read(self.p)
        self.assertEqual(list(db.node_names), ['__tc_cluster_7', 'a', 'y'])
        self.assertEqual(set(db.net_names), {'a', 'y'})
        self.assertEqual(len(db.pin_names), 4)
        for name, x, y in zip(db.pin_names, db.pin_offset_x, db.pin_offset_y):
            if str(name).startswith('__tc_cluster_7 '):
                self.assertEqual((x, y), (1000., 500.))

    def test_missing_nets_is_built_from_verilog(self):
        self.original.write_text(re.sub(r'NETS 3 ;.*?END NETS\n', '', self.original.read_text(), flags=re.S))
        report = self.run_generate()
        self.assertEqual(report['original_nets'], 0)
        self.assertEqual(report['reduced_nets'], 2)
        list(events(self.args.output))

    def test_placement_table_and_unplaced(self):
        positions = self.root / 'pos.tsv'
        positions.write_text('cluster_id\tx\ty\torient\n7\t5000\t500\tFN\n')
        self.args.positions = str(positions)
        report = self.run_generate()
        self.assertEqual(report['clusters'][0]['x'], 5000)
        self.assertIn('PLACED ( 5000 500 ) FN', Path(self.args.output).read_text())
        self.args.output = str(self.root / 'unplaced.def')
        self.args.positions = None; self.args.placement = 'unplaced'
        self.run_generate()
        self.assertIn('TC_7 + UNPLACED', Path(self.args.output).read_text())

    def test_reject_fixed_missing_member_wrong_master(self):
        before = self.original.read_text()
        cases = [(before.replace('+ PLACED', '+ FIXED', 1), 'fixed/cover'),
                 (before.replace('- u1 INV', '- absent INV'), 'missing from DEF')]
        for text, error in cases:
            self.original.write_text(text)
            with self.subTest(error=error), self.assertRaisesRegex(ModelError, error):
                self.run_generate()
            self.assertFalse(Path(self.args.output).exists())
        self.original.write_text(before)
        item = entry()
        item['members'][0]['original_master'] = 'WRONG'
        self.mapping.write_text(json.dumps(item))
        with self.assertRaisesRegex(ModelError, 'master differs'):
            self.run_generate()

    def test_specialnets_and_groups_explicit_drop(self):
        self.change('END DESIGN', 'SPECIALNETS 1 ;\n- VDD ( u1 VDD ) + USE POWER ;\nEND SPECIALNETS\nEND DESIGN')
        with self.assertRaisesRegex(ModelError, 'SPECIALNETS'):
            self.run_generate()
        self.args.drop_specialnets = True
        self.change('END DESIGN', 'GROUPS 1 ;\n- g u* ;\nEND GROUPS\nEND DESIGN')
        with self.assertRaisesRegex(ModelError, 'GROUPS'):
            self.run_generate()
        self.args.drop_groups = True
        report = self.run_generate()
        self.assertEqual(report['dropped_groups'], 1)
        self.assertEqual(report['dropped_specialnets'], 1)
        text = Path(self.args.output).read_text()
        self.assertNotIn('SPECIALNETS', text)
        self.assertNotIn('GROUPS', text)

    def test_retained_physical_only_cell_and_attributes(self):
        self.change('COMPONENTS 2 ;', 'COMPONENTS 3 ;')
        keep = '- filler INV + FIXED ( 6000 0 ) FN + PROPERTY note "keep; me" ;'
        self.change('END COMPONENTS', keep + '\nEND COMPONENTS')
        report = self.run_generate()
        self.assertEqual(report['reduced_components'], 2)
        self.assertIn(keep, Path(self.args.output).read_text())

    def test_routing_discarded_clock_use_preserved(self):
        self.change('( u1 A ) ;', '( u1 A ) + USE CLOCK + ROUTED metal1 ( 0 500 ) ( 2000 500 ) ;')
        self.run_generate()
        text = Path(self.args.output).read_text()
        self.assertIn('+ USE CLOCK', text)
        self.assertNotIn('ROUTED', text)

    def test_protection_count_region_endpoint_and_positions(self):
        before = self.original.read_text()
        cases = [(before.replace('COMPONENTS 2', 'COMPONENTS 3'), 'count mismatch'),
                 (before.replace(') N ;', ') N + REGION r1 ;', 1), 'crosses'),
                 (before.replace('( PIN a )', '( PIN missing )'), 'endpoint differs')]
        for text, message in cases:
            self.original.write_text(text)
            with self.subTest(message=message), self.assertRaisesRegex(ModelError, message):
                self.run_generate()
        self.original.write_text(before)
        positions = self.root / 'positions.tsv'
        positions.write_text('7 9500 0 N\n')
        self.args.positions = str(positions)
        with self.assertRaisesRegex(ModelError, 'outside die'):
            self.run_generate()
        self.args.positions = None
        Path(self.args.output).write_text('existing user data')
        with self.assertRaisesRegex(ModelError, 'Output exists'):
            self.run_generate()
        self.assertEqual(Path(self.args.output).read_text(), 'existing user data')

    def test_cli(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('ReducedDEF.py')), *self.argv],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1 components / 2 nets', result.stdout)

    def test_retained_instance_suffix_and_fanout(self):
        # The generated cluster name has a suffix because a retained logical
        # instance already occupies the preferred cluster name.
        self.change('COMPONENTS 2 ;', 'COMPONENTS 3 ;')
        retained = '- __tc_cluster_7 INV + PLACED ( 7000 0 ) FN ;'
        self.change('END COMPONENTS', retained + '\nEND COMPONENTS')
        self.change('( u1 A )', '( u1 A ) ( __tc_cluster_7 A )')
        self.change('NETS 3 ;', 'NETS 4 ;')
        self.change('END NETS', '- unused ( __tc_cluster_7 Y ) ;\nEND NETS')
        self.reduced.write_text('module top(a,y); input a; output y; wire unused; '
            'INV __tc_cluster_7(.A(a),.Y(unused)); '
            'TC_7 __tc_cluster_7_1(.I0(a),.O0(y)); endmodule')
        report = self.run_generate()
        text = Path(self.args.output).read_text()
        self.assertEqual(report['reduced_components'], 2)
        self.assertEqual(report['reduced_nets'], 3)
        self.assertIn(retained, text)
        self.assertIn('__tc_cluster_7_1 TC_7', text)
        self.assertIn('( __tc_cluster_7 A )', text)
        self.assertIn('( __tc_cluster_7_1 I0 )', text)

    def test_bus_ports_and_escaped_member_names(self):
        self.change('- u1 INV', '- u/1 INV')
        self.change('( u1 ', '( u/1 ')
        self.change('- a + NET a', '- a[0] + NET a[0]')
        self.change('- a ( PIN a )', '- a[0] ( PIN a[0] )')
        item = entry(members=('\\u/1', 'u2'), inputs=('a[0]',))
        self.mapping.write_text(json.dumps(item))
        self.reduced.write_text('module top(a,y); input [0:0] a; output y; '
                                'TC_7 __tc_cluster_7(.I0(a[0]),.O0(y)); endmodule')
        self.run_generate()
        text = Path(self.args.output).read_text()
        self.assertIn('( PIN a[0] )', text)
        self.assertNotIn('u/1', text)

    def lef_tree(self):
        root = self.root / 'libraries'
        (root / 'clusters' / 'nested').mkdir(parents=True)
        (root / 'original').mkdir()
        (root / 'clusters' / 'nested' / 'cluster.LEF').write_text(self.lef.read_text())
        (root / 'original' / 'cells.lef').write_text(Path(self.p.lef_dir_path[0]).read_text())
        (root / 'tech.lef').write_text(TECH)
        (root / 'ignored.lef.summary.json').write_text('not a LEF')
        return root

    def test_recursive_lef_directory_retained_instance_and_pin_validation(self):
        root = self.lef_tree()
        self.args.cluster_lef = str(root)
        self.test_retained_instance_suffix_and_fanout()
        report = json.loads(Path(self.args.output + '.summary.json').read_text())
        self.assertTrue(report['retained_lef_validated'])
        self.assertEqual(len(report['lef_files']), 3)
        self.assertEqual(report['lef_files'], sorted(report['lef_files']))
        self.assertEqual(report['lef_master_sources']['INV'], str(root / 'original' / 'cells.lef'))
        self.assertEqual(report['lef_master_sources']['TC_7'], str(root / 'clusters' / 'nested' / 'cluster.LEF'))

    def test_directory_requires_physical_only_retained_master(self):
        self.args.cluster_lef = str(self.lef_tree())
        self.change('COMPONENTS 2 ;', 'COMPONENTS 3 ;')
        self.change('END COMPONENTS', '- filler MISSING + FIXED ( 6000 0 ) N ;\nEND COMPONENTS')
        with self.assertRaisesRegex(ModelError, 'filler.*MISSING'):
            self.run_generate()
        self.assertFalse(Path(self.args.output).exists())

    def test_directory_requires_retained_logical_pin(self):
        self.args.cluster_lef = str(self.lef_tree())
        self.change('COMPONENTS 2 ;', 'COMPONENTS 3 ;')
        self.change('END COMPONENTS', '- keep INV + PLACED ( 6000 0 ) N ;\nEND COMPONENTS')
        self.reduced.write_text('module top(a,y); input a; output y; wire z; '
            'INV keep(.BAD(a),.Y(z)); TC_7 __tc_cluster_7(.I0(a),.O0(y)); endmodule')
        with self.assertRaisesRegex(ModelError, 'pin missing from LEF: INV/BAD'):
            self.run_generate()
        self.assertFalse(Path(self.args.output).exists())

    def test_directory_duplicate_masters_rejected(self):
        root = self.lef_tree()
        (root / 'original' / 'second.lef').write_text(Path(self.p.lef_dir_path[0]).read_text())
        self.args.cluster_lef = str(root)
        with self.assertRaisesRegex(ModelError, 'Duplicate LEF master INV'):
            self.run_generate()
        self.assertFalse(Path(self.args.output).exists())

    def test_duplicate_within_single_lef_rejected(self):
        self.lef.write_text(self.lef.read_text() + self.lef.read_text())
        with self.assertRaisesRegex(ModelError, 'Duplicate LEF master TC_7'):
            self.run_generate()

    def test_directory_discovery_empty_missing_and_symlink(self):
        empty = self.root / 'empty'; empty.mkdir()
        with self.assertRaisesRegex(ModelError, 'No LEF files'):
            discover_lefs(empty)
        with self.assertRaisesRegex(ModelError, 'does not exist'):
            discover_lefs(empty / 'missing')
        root = self.lef_tree()
        (root / 'alias.lef').symlink_to(root / 'original' / 'cells.lef')
        self.assertEqual(len(discover_lefs(root)), 3)

    def test_lef_dir_cli_alias(self):
        argv = list(self.argv)
        index = argv.index('--cluster-lef')
        argv[index:index + 2] = ['--lef-dir', str(self.lef_tree())]
        args = parse_args(argv)
        self.assertEqual(args.cluster_lef, str(self.root / 'libraries'))
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('ReducedDEF.py')), *argv],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.environ.get('DREAMPLACE_INSTALL'), 'set DREAMPLACE_INSTALL for native parser')
    def test_directory_output_native_reader(self):
        self.args.cluster_lef = str(self.lef_tree())
        self.test_native_def_reader()

    @unittest.skipUnless(os.environ.get('DREAMPLACE_INSTALL'), 'set DREAMPLACE_INSTALL for native parser')
    def test_native_def_reader(self):
        self.run_generate()
        params = dict(lef_input=[*self.p.lef_dir_path, str(self.root / 'tech.lef'), str(self.lef)],
                      def_input=self.args.output, sort_nets_by_degree=0)
        script = ('from types import SimpleNamespace\n'
                  'from dreamplace.ops.place_io.place_io import PlaceIOFunction\n'
                  'import json\n'
                  'p = SimpleNamespace(**json.loads(%r))\n'
                  'db = PlaceIOFunction.pydb(PlaceIOFunction.read(p))\n'
                  'assert len(db.node_names) == 3\n'
                  'assert len(db.pin_names) == 4\n' % json.dumps(params))
        result = subprocess.run([sys.executable, '-c', script], cwd=self.root,
            env=dict(os.environ, PYTHONPATH=os.environ['DREAMPLACE_INSTALL'], OMP_NUM_THREADS='1'),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == '__main__':
    unittest.main()
