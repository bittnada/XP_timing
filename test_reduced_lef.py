"""Central-pin LEF generation, input validation and MakeDB reader integration."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

from ReducedLEF import ModelError, generate, parse_args, technology_layer


TECH = '''VERSION 5.8 ;
UNITS
 DATABASE MICRONS 1000 ;
END UNITS
LAYER metal2
 TYPE ROUTING ;
 DIRECTION HORIZONTAL ;
 PITCH 0.2 ;
 WIDTH 0.1 ;
 MINWIDTH 0.05 ;
END metal2
LAYER via1
 TYPE CUT ;
END via1
END LIBRARY
'''


def entry(cid=7, cell_id=0):
    return dict(cluster_id=cid, liberty_cell='TC_%d' % cid,
                members=[dict(cell_id=cell_id, cell_name='u%d' % cell_id, original_master='INV')],
                inputs=[dict(pin='I0', net='n%d_in' % cid, original_pins=['u%d:A' % cell_id])],
                outputs=[dict(pin='O0', net='n%d_out' % cid, original_pin='u%d:Y' % cell_id)])


class ReducedLEFTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'cluster_mapping.jsonl').write_text(json.dumps(entry()) + '\n')
        (self.root / 'sizes.tsv').write_text('cluster_id\twidth\theight\n7\t4\t2\n')
        (self.root / 'tech.lef').write_text(TECH)
        self.argv = ['--input', str(self.root / 'cluster_mapping.jsonl'),
            '--sizes', str(self.root / 'sizes.tsv'), '--tech-lef', str(self.root / 'tech.lef'),
            '--pin-layer', 'metal2', '--output', str(self.root / 'cluster.lef')]
        self.args = parse_args(self.argv)

    def test_readlef_center_direction_use_and_size(self):
        from ReadLEF import ReadLEFinfo
        report = generate(self.args)
        reader = ReadLEFinfo([self.args.output])
        masters = reader.get_total_macro_info()
        master = masters[reader.lef_macro_name2index_map['TC_7']]
        self.assertEqual(master.get_cell_type(), 'BLOCK')
        self.assertEqual((master.get_width(), master.get_height()), (4., 2.))
        for name, direction in (('I0', 'INPUT'), ('O0', 'OUTPUT')):
            pin = master.get_pin(reader.lef_pin_name2index_map[name])
            self.assertEqual(pin.get_direction(), direction)
            self.assertEqual(pin.get_use('metal2'), 'SIGNAL')
            rectangle = pin.get_layers()['metal2']['rectangles'].reshape(-1, 4)[0]
            np.testing.assert_allclose(rectangle, [1.95, .95, 2.05, 1.05])
            np.testing.assert_allclose((rectangle[:2] + rectangle[2:]) / 2, [2., 1.])
        self.assertEqual(report['pin_count'], 2)
        self.assertEqual(json.loads(Path(self.args.output + '.summary.json').read_text()), report)

    def test_without_tech_defaults_and_readlef(self):
        from ReadLEF import ReadLEFinfo
        self.args.tech_lef = None
        report = generate(self.args)
        self.assertFalse(report['technology_validated'])
        self.assertIsNone(report['lef_database_microns'])
        self.assertEqual((report['pin_width'], report['pin_height']), (.1, .1))
        self.assertEqual(len(report['warnings']), 2)
        self.assertNotIn('DATABASE MICRONS', Path(self.args.output).read_text())
        reader = ReadLEFinfo([self.args.output])
        master = reader.get_total_macro_info()[reader.lef_macro_name2index_map['TC_7']]
        self.assertEqual((master.get_width(), master.get_height()), (4., 2.))
        pin = master.get_pin(reader.lef_pin_name2index_map['I0'])
        np.testing.assert_allclose(pin.get_layers()['metal2']['rectangles'].reshape(-1, 4)[0],
                                   [1.95, .95, 2.05, 1.05])

    def test_without_tech_explicit_dimensions_and_dbu_input(self):
        self.args.tech_lef = []
        self.args.pin_width = .2; self.args.pin_height = .4
        self.args.size_unit = 'dbu'; self.args.dbu_per_micron = 2000
        Path(self.args.sizes).write_text('7\t8000\t4000\n')
        report = generate(self.args)
        self.assertEqual(len(report['warnings']), 1)
        np.testing.assert_allclose(report['clusters'][0]['pin_rect'], [1.9, .8, 2.1, 1.2])
        self.assertIsNone(report['lef_database_microns'])

    def test_without_tech_cli_and_invalid_dimensions(self):
        argv = list(self.argv)
        i = argv.index('--tech-lef'); del argv[i:i + 2]
        args = parse_args(argv)
        for width in (0., float('nan'), 100.):
            args.pin_width = width
            with self.subTest(width=width), self.assertRaises(ModelError):
                generate(args)
            self.assertFalse(Path(args.output).exists())
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('ReducedLEF.py')), *argv],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No --tech-lef', result.stderr)
        self.assertIn('placeholder', result.stderr)

    def test_delimiters_dbu_conversion_and_sorted_cluster_ids(self):
        entries = [entry(9, 0), entry(2, 1)]
        Path(self.args.input).write_text(''.join(json.dumps(e) + '\n' for e in entries))
        for delimiter, sep in (('tab', '\t'), ('comma', ','), ('space', ' ')):
            self.args.output = str(self.root / (delimiter + '.lef'))
            self.args.delimiter = delimiter
            self.args.size_unit = 'dbu'; self.args.dbu_per_micron = 1000
            Path(self.args.sizes).write_text('# dimensions in original DBU\n' +
                sep.join(('height', 'cluster_id', 'width')) + '\n' +
                sep.join(('2000', '9', '4000')) + '\n' + sep.join(('3000', '2', '6000')) + '\n')
            report = generate(self.args)
            self.assertEqual([r['cluster_id'] for r in report['clusters']], [2, 9])
            self.assertEqual(report['clusters'][0]['pin_center'], [3., 1.5])
            text = Path(self.args.output).read_text()
            self.assertLess(text.index('MACRO TC_2'), text.index('MACRO TC_9'))
            self.assertIn('SIZE 6.0 BY 3.0', text)

    def test_bad_sizes_fail_before_writing(self):
        for row in ('7\t0\t2', '7\t-1\t2', '7\tnan\t2', '7\tinf\t2',
                    '7\t.01\t2', '8\t4\t2', '7\t4\t2\n7\t4\t2', '7\t4',
                    '7.0\t4\t2', '-7\t4\t2'):
            with self.subTest(row=row):
                Path(self.args.sizes).write_text(row + '\n')
                with self.assertRaises(ModelError):
                    generate(self.args)
                self.assertFalse(Path(self.args.output).exists())
                self.assertFalse(Path(self.args.output + '.summary.json').exists())

    def test_layer_validation_and_macro_scope(self):
        for layer in ('missing', 'via1'):
            self.args.pin_layer = layer
            with self.assertRaises(ModelError):
                generate(self.args)
        self.args.pin_layer = 'metal2'
        fake = TECH.replace('END LIBRARY', '''MACRO OTHER
 PIN X
  PORT
   LAYER metal2 ;
   RECT 0 0 1 1 ;
  END
 END X
END OTHER
END LIBRARY''')
        Path(self.args.tech_lef[0]).write_text(fake)
        attrs, dbu, masters = technology_layer(self.args.tech_lef, 'metal2')
        self.assertEqual(attrs['WIDTH'], '0.1')
        self.assertEqual(dbu, 1000)
        self.assertIn('OTHER', masters)
        generate(self.args)

    def test_no_overwrite_or_input_collision(self):
        output = Path(self.args.output)
        output.write_text('user data')
        with self.assertRaises(ModelError):
            generate(self.args)
        self.assertEqual(output.read_text(), 'user data')
        self.args.output = self.args.input
        with self.assertRaises(ModelError):
            generate(self.args)

    def test_bad_mapping_and_incomplete_manifest(self):
        item = entry()
        item['outputs'][0]['pin'] = 'I0'
        Path(self.args.input).write_text(json.dumps(item) + '\n')
        with self.assertRaises(ModelError):
            generate(self.args)
        Path(self.args.input).write_text(json.dumps(entry()) + '\n')
        (self.root / 'manifest.json').write_text('{"status":"in_progress"}')
        with self.assertRaisesRegex(ModelError, 'incomplete'):
            generate(self.args)
        self.assertFalse(Path(self.args.output).exists())

    def test_pin_dimensions_and_missing_layer_width(self):
        Path(self.args.tech_lef[0]).write_text(TECH.replace(' WIDTH 0.1 ;\n', '').replace(' MINWIDTH 0.05 ;\n', ''))
        with self.assertRaisesRegex(ModelError, 'pin width'):
            generate(self.args)
        self.args.pin_width = .2; self.args.pin_height = .4
        report = generate(self.args)
        np.testing.assert_allclose(report['clusters'][0]['pin_rect'], [1.9, .8, 2.1, 1.2])

    def test_makedb_without_tech_lef(self):
        self.args.tech_lef = None
        self.test_makedb_preserves_generated_pin_offsets()

    def test_makedb_preserves_generated_pin_offsets(self):
        from test_makedb_adapter import fixture
        import MakeDBAdapter
        params = fixture(self.root)
        params.mode = 'default'
        generate(self.args)
        design = Path(params.def_path)
        design.write_text(design.read_text().replace(' INV ', ' TC_7 ').replace('u1 A', 'u1 I0')
                          .replace('u1 Y', 'u1 O0').replace('u2 A', 'u2 I0').replace('u2 Y', 'u2 O0'))
        params.lef_dir_path.append(self.args.output)
        with contextlib.redirect_stdout(io.StringIO()):
            _, db = MakeDBAdapter.read(params)
        for pin_name, ox, oy in zip(db.pin_names, db.pin_offset_x, db.pin_offset_y):
            if str(pin_name).startswith('u'):
                self.assertEqual(ox, 2000.)
                self.assertEqual(oy, 1000.)
        self.assertEqual(db.num_movable_macro, 2)

    def test_cli_success_and_failure(self):
        script = str(Path(__file__).with_name('ReducedLEF.py'))
        result = subprocess.run([sys.executable, script, *self.argv], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1 clusters / 2 pins', result.stdout)
        result = subprocess.run([sys.executable, script, *self.argv], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('already exists', result.stderr)

    def test_units_pin_minimum_and_master_collision(self):
        self.args.size_unit = 'dbu'
        with self.assertRaisesRegex(ModelError, 'dbu-per-micron'):
            generate(self.args)
        self.args.size_unit = 'micron'
        for width in (float('nan'), -.1, .01):
            self.args.pin_width = width
            with self.subTest(width=width), self.assertRaises(ModelError):
                generate(self.args)
        self.args.pin_width = None
        Path(self.args.tech_lef[0]).write_text(TECH.replace('END LIBRARY', 'MACRO TC_7\nEND TC_7\nEND LIBRARY'))
        with self.assertRaisesRegex(ModelError, 'already exists in'):
            generate(self.args)
        self.assertFalse(Path(self.args.output).exists())

    def test_conflicting_layer_metadata(self):
        second = self.root / 'second.lef'
        second.write_text(TECH.replace('WIDTH 0.1', 'WIDTH 0.2'))
        self.args.tech_lef.append(str(second))
        with self.assertRaisesRegex(ModelError, 'Conflicting'):
            generate(self.args)

    @unittest.skipUnless(os.environ.get('DREAMPLACE_INSTALL'), 'set DREAMPLACE_INSTALL for native LEF parser')
    def test_native_lef_parser(self):
        from test_makedb_adapter import fixture
        p = fixture(self.root)
        generate(self.args)
        design = Path(p.def_path)
        design.write_text(design.read_text().replace(' INV ', ' TC_7 ').replace('u1 A', 'u1 I0')
                          .replace('u1 Y', 'u1 O0').replace('u2 A', 'u2 I0').replace('u2 Y', 'u2 O0'))
        args = dict(lef_input=[*p.lef_dir_path, *self.args.tech_lef, self.args.output],
                    def_input=p.def_path, sort_nets_by_degree=0)
        script = ('from types import SimpleNamespace\n'
                  'from dreamplace.ops.place_io.place_io import PlaceIOFunction\n'
                  'import json\n'
                  'p = SimpleNamespace(**json.loads(%r))\n'
                  'db = PlaceIOFunction.pydb(PlaceIOFunction.read(p))\n'
                  'assert len(db.node_names) == 4\n'
                  'assert len(db.pin_names) == 6\n' % json.dumps(args))
        env = dict(os.environ, PYTHONPATH=os.environ['DREAMPLACE_INSTALL'], OMP_NUM_THREADS='1')
        result = subprocess.run([sys.executable, '-c', script], cwd=self.root, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == '__main__':
    unittest.main()
