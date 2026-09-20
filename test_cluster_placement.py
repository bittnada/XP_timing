"""Placement contraction without any reduced Liberty or Verilog."""
import contextlib
import csv
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest

from ClusterPlacement import cluster_size, generate, grid_size, parse_args, ModelError
from ReducedDEF import events
from test_makedb_adapter import fixture


class ClusterPlacementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.params = fixture(self.root)
        self.original = Path(self.params.def_path)
        self.saved = self.root / 'saved'; self.saved.mkdir()
        self.die = dict(xl=0, yl=0, xh=10000, yh=2000, site_width=1000,
                        row_height=1000, def_scale=1000)
        self.catalog = {'INV': dict(width=1, height=1, **{'class': 'CORE'},
                                   pin={'A': {'direction': 'INPUT'}, 'Y': {'direction': 'OUTPUT'}})}
        self.flush()
        (self.root / 'clusters.tsv').write_text('cell_id\tcluster_id\n100\t7\n200\t7\n')
        (self.root / 'names.tsv').write_text('cell_id\tcell_name\n100\tu1\n200\tu2\n')
        self.args = parse_args(['--clusters', str(self.root / 'clusters.tsv'),
                               '--cell-names', str(self.root / 'names.tsv'),
                               '--saved-db', str(self.saved), '--def-input', str(self.original),
                               '--lef-input', str(self.root / 'tiny.lef'),
                               '--utilization', '.5', '--pin-layer', 'metal1',
                               '--output', str(self.root / 'result')])
        self.output = Path(self.args.output)

    def flush(self):
        (self.saved / 'die_info.json').write_text(json.dumps(self.die))
        (self.saved / 'lef_info.json').write_text(json.dumps(self.catalog))

    def table(self, name):
        with (self.output / name).open() as stream:
            return list(csv.DictReader(stream, delimiter='\t'))

    def test_sizes_exact_units_and_utilization(self):
        for area in ('0.001', '1', '2.1', '34.00001', '900000'):
            for utilization in ('.2', '.7', '1'):
                w, h = cluster_size(area, utilization, 380, 3420, 2000)
                self.assertEqual(w % 380, 0)
                self.assertEqual(h % 3420, 0)
                actual = Decimal(area) * 2000**2 / (w*h)
                self.assertLessEqual(actual, Decimal(utilization))
        self.assertEqual(cluster_size(2, .5, 1000, 1000, 1000), (2000, 2000))
        self.assertEqual(cluster_size('1.2996', 1, 380, 3420, 2000), (1520, 3420))
        for u in (0, -1, 1.1, float('nan'), float('inf')):
            with self.assertRaises(ModelError):
                cluster_size(2, u, 1000, 1000, 1000)
        self.assertEqual(grid_size(10, 5, 1000, 1000, 1000), (5000, 2000))

    def test_contract_preserve_internal_net_and_makedb_roundtrip(self):
        before = self.original.read_bytes()
        report = generate(self.args)
        counts = report['counts']
        self.assertEqual(counts['original_components'], 2)
        self.assertEqual(counts['reduced_components'], 1)
        self.assertEqual(counts['original_pins'], 6)
        self.assertEqual(counts['reduced_pins'], 5)
        self.assertEqual(counts['cluster_pins'], 3)
        self.assertEqual(counts['nets'], 3)
        self.assertEqual(counts['one_endpoint_nets'], 1)
        self.assertEqual(report['original_movable_area_um2'], 2)
        self.assertEqual(report['current_utilization'], .1)
        self.assertEqual(report['movable_area_inflation'], 5)
        self.assertEqual(report['reduced_movable_area_um2'], 10)
        self.assertEqual(report['achieved_utilization'], .5)
        self.assertEqual(self.original.read_bytes(), before)
        text = (self.output / 'reduced.def').read_text()
        self.assertIn('__pc_cluster_7 PC_7 + PLACED ( 1000 0 ) N', text)
        self.assertIn('- n\n  ( __pc_cluster_7 VP1 )', text)
        for section in ('PINS',):
            self.assertEqual([t for _, s, t, _ in events(self.original) if s == section],
                             [t for _, s, t, _ in events(self.output / 'reduced.def') if s == section])
        sizes = self.table('cluster_sizes.tsv')[0]
        self.assertEqual((sizes['width'], sizes['height']), ('5', '2'))
        self.assertEqual(Decimal(sizes['area_inflation']), Decimal(5))
        pin_map = self.table('pin_mapping.tsv')
        internal = [p for p in pin_map if p['net_name'] == 'n']
        self.assertEqual({p['reduced_pin_name'] for p in internal}, {'VP1'})
        self.assertEqual(len(internal), 2)
        from ReadDEF import ReadDEFinfo
        import MakeDBAdapter
        self.params.mode = 'default'
        self.params.def_path = str(self.output / 'reduced.def')
        self.params.lef_dir_path = [str(self.output / 'placement.lef')]
        with contextlib.redirect_stdout(io.StringIO()):
            reader = ReadDEFinfo(self.params)
            reader.read()
            _, db = MakeDBAdapter.read(self.params)
        self.assertEqual(len(reader.netInfo), 3)
        # Existing Analysis removes degree-one nets from placement arrays;
        # the generated DEF and name mapping still retain every original net.
        self.assertEqual(set(db.net_names), {'a', 'y'})
        self.assertEqual(len(db.pin_names), 4)
        self.assertEqual(len(set(db.pin_names)), 4)
        for name, x, y in zip(db.pin_names, db.pin_offset_x, db.pin_offset_y):
            if str(name).startswith('__pc_cluster_7 '):
                self.assertEqual((x, y), (2500, 1000))

    def test_unmerged_components_and_connections_preserved(self):
        (self.root / 'clusters.tsv').write_text('100 7\n')
        self.args.utilization = 1
        report = generate(self.args)
        self.assertEqual(report['counts']['reduced_components'], 2)
        text = (self.output / 'reduced.def').read_text()
        self.assertIn('- u2 INF_INV + PLACED ( 4000 0 ) N ;', text)
        self.assertIn('( u2 A )', text)
        row = [r for r in self.table('cell_mapping.tsv') if r['original_cell_name'] == 'u2'][0]
        self.assertEqual(row['reduced_cell_name'], 'u2')
        self.assertEqual(row['reduced_master'], 'INF_INV')
        lef = (self.output / 'placement.lef').read_text()
        self.assertIn('MACRO INV', lef)
        self.assertIn('MACRO INF_INV', lef)

    def test_fixed_area_counts_but_fixed_cell_is_not_inflated(self):
        self.original.write_text(self.original.read_text().replace(
            'u2 INV + PLACED', 'u2 INV + FIXED'))
        (self.root / 'clusters.tsv').write_text('100 7\n')
        report = generate(self.args)
        self.assertEqual(report['original_fixed_area_um2'], 1)
        self.assertEqual(report['original_movable_area_um2'], 1)
        self.assertEqual(report['current_utilization'], .1)
        self.assertEqual(report['movable_area_inflation'], 9)
        text = (self.output / 'reduced.def').read_text()
        self.assertIn('- u2 INV + FIXED', text)
        self.assertNotIn('- u2 INF_INV', text)

    def test_same_cluster_pair_different_nets_not_merged(self):
        # Two independent nets both connect the same pair of clusters.
        (self.root / 'clusters.tsv').write_text('100 7\n200 8\n')
        text = self.original.read_text()
        text = text.replace('- a ( PIN a ) ( u1 A ) ;', '- a ( PIN a ) ;')
        text = text.replace('- y ( u2 Y ) ( PIN y ) ;', '- y ( PIN y ) ;\n- n2 ( u2 Y ) ( u1 A ) ;')
        self.original.write_text(text.replace('NETS 3 ;', 'NETS 4 ;'))
        report = generate(self.args)
        self.assertEqual(report['counts']['nets'], 4)
        for cid in (7, 8):
            rows = [r for r in self.table('pin_mapping.tsv')
                    if r['reduced_cell_name'] == '__pc_cluster_%d' % cid]
            self.assertEqual(len({r['reduced_pin_name'] for r in rows}), 2)

    def test_invalid_inputs_leave_no_output(self):
        original = self.original.read_text()
        cases = [
            (original.replace('u1 INV + PLACED', 'u1 INV + FIXED'), 'movable CORE'),
            (original.replace('u1 INV', 'missing INV'), 'missing from DEF'),
            (original.replace('( u1 Y )', '( u1 A )'), 'original pin'),
            (original.replace('( u1 Y )', '( u1 missing )'), 'Missing LEF pin'),
            (original.replace('MICRONS 1000', 'MICRONS 2000'), 'units disagree'),
            (original.replace('STEP 1000', 'STEP 500'), 'step/array'),
            (original.replace('END DESIGN', 'SPECIALNETS 1 ;\n- VDD ( u1 A ) ;\nEND SPECIALNETS\nEND DESIGN'), 'SPECIALNETS'),
            (original.replace('u1 INV + PLACED', '__pc_cluster_7 INV + PLACED'), 'collides'),
        ]
        for text, message in cases:
            self.original.write_text(text)
            with self.subTest(message=message), self.assertRaisesRegex(ModelError, message):
                generate(self.args)
            self.assertFalse(self.output.exists())

    def test_no_overwrite(self):
        self.output.mkdir()
        marker = self.output / 'data'; marker.write_text('existing user data')
        with self.assertRaisesRegex(ModelError, 'already exists'):
            generate(self.args)
        self.assertEqual(marker.read_text(), 'existing user data')

    def test_unplaced_and_pin_geometry_validation(self):
        self.args.pin_height = 0
        with self.assertRaisesRegex(ModelError, 'positive'):
            generate(self.args)
        self.args.pin_height = None
        self.args.placement = 'unplaced'
        generate(self.args)
        self.assertIn('__pc_cluster_7 PC_7 + UNPLACED', (self.output / 'reduced.def').read_text())

    def test_incompatible_floorplan_and_size_metadata(self):
        cases = [('site_width', 500, 'step/array'), ('row_height', 700, 'origins'),
                 ('def_scale', 2000, 'units disagree'), ('site_width', 0, 'positive'),
                 ('row_height', 500.5, 'integer')]
        original = self.die.copy()
        for key, value, message in cases:
            self.die = dict(original, **{key: value}); self.flush()
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ModelError, message):
                generate(self.args)
            self.assertFalse(self.output.exists())

    def test_clock_use_is_preserved_and_routes_removed(self):
        self.original.write_text(self.original.read_text().replace(
            '- a ( PIN a ) ( u1 A ) ;',
            '- a ( PIN a ) ( u1 A ) + USE CLOCK + ROUTED metal1 ( 0 0 ) ( 100 0 ) ;'))
        generate(self.args)
        text = (self.output / 'reduced.def').read_text()
        self.assertIn('+ USE CLOCK ;', text)
        self.assertNotIn('ROUTED', text)


if __name__ == '__main__':
    unittest.main()
