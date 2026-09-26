"""MakeDB integration tests; no production benchmark is modified."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np
import MakeDBAdapter as adapter
from Params import Params


def fixture(root):
    # Reuse the LEF/DEF fixture in the native timing round-trip regression.
    tree = ast.parse(Path(__file__).with_name('test_timing_cache.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith('VERSION 5.8 ;'):
                extension = 'def' if 'DESIGN top' in node.value else 'lef'
                (root / ('tiny.' + extension)).write_text(node.value)
    p = Params()
    p.db_option = 'def'; p.mode = 'binary_write'
    p.save_path = str(root / 'cache')
    p.def_path = str(root / 'tiny.def'); p.lef_dir_path = [str(root / 'tiny.lef')]
    p.gpu = 0; p.num_threads = 1
    return p


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.p = fixture(self.root)

    def read(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return adapter.read(self.p)

    def test_ids_round_trip_without_sources_or_writes(self):
        raw, db = self.read()
        self.assertEqual(list(db.node_names), ['u1', 'u2', 'a', 'y'])
        self.assertEqual(list(db.pin_names), ['PIN a', 'u1 A', 'u1 Y', 'u2 A', 'u2 Y', 'PIN y'])
        self.assertEqual(list(db.net_names), ['a', 'n', 'y'])
        self.assertEqual(list(db.net_uses), ['SIGNAL', 'SIGNAL', 'SIGNAL'])
        self.assertEqual(len(db.net_weight_deltas), 3)
        adapter.save(db, self.p)
        self.assertFalse((self.root / 'cache/physical_db/net2pin_map.npy').exists())
        (self.root / 'tiny.def').unlink(); (self.root / 'tiny.lef').unlink()
        before = {str(p): p.stat().st_mtime_ns for p in self.root.rglob('*') if p.is_file()}
        for option in ('binary', 'binary_wo_pos'):
            self.p.db_option = option
            _, restored = self.read()
            self.assertEqual(restored.congestion_metadata, db.congestion_metadata)
            np.testing.assert_array_equal(restored.net_uses, db.net_uses)
            for field in adapter.INTS + adapter.STRINGS:
                np.testing.assert_array_equal(getattr(db, field), getattr(restored, field))
            if option == 'binary':
                np.testing.assert_array_equal(restored.node_x, db.node_x)
            else:
                np.testing.assert_array_equal(restored.node_x[:2], [0, 0])
                np.testing.assert_array_equal(restored.node_x[2:], db.node_x[2:])
        after = {str(p): p.stat().st_mtime_ns for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_def_clock_net_is_absent_from_placement_binary(self):
        path = self.root / 'tiny.def'
        path.write_text(path.read_text().replace(
            '- n ( u1 Y ) ( u2 A ) ;', '- n ( u1 Y ) ( u2 A ) + USE CLOCK ;'))
        _, db = self.read()
        self.assertEqual(list(db.net_names), ['a', 'y'])
        self.assertEqual(list(db.net_uses), ['SIGNAL', 'SIGNAL'])
        adapter.save(db, self.p)
        self.p.db_option = 'binary'
        _, restored = self.read()
        self.assertEqual(list(restored.net_names), ['a', 'y'])
        self.assertEqual(list(restored.net_uses), ['SIGNAL', 'SIGNAL'])

    def test_normal_read_does_not_export(self):
        from unittest.mock import patch
        self.p.mode = 'default'
        # Temporary files are still an I/O cost: ordinary reads must not dump
        # the large JSON/TSV/NPY sidecars even into a temporary directory.
        with patch('json.dump', side_effect=AssertionError('unexpected metadata write')), \
             patch('numpy.save', side_effect=AssertionError('unexpected array write')), \
             patch('pandas.DataFrame.to_csv', side_effect=AssertionError('unexpected table write')):
            self.read()
        self.assertFalse(Path(self.p.save_path).exists())

    def test_threads_and_export_preserve_arrays_and_metadata(self):
        from unittest.mock import patch
        self.p.def_parse_num_threads = 1
        _, reference = self.read()
        root = Path(self.p.save_path)
        self.assertTrue((root / 'node_names.txt').is_file())
        pins = json.loads((root / 'pinInfo_dict.json').read_text())
        for i, name in enumerate(reference.pin_names):
            self.assertEqual(pins[str(i)]['node_idx'], int(reference.pin2node_map[i]))
            self.assertEqual(pins[str(i)]['offset_x'], reference.pin_offset_x[i])
        self.p.mode = 'default'
        for threads in (1, 2, 4, 8):
            self.p.def_parse_num_threads = threads
            with self.subTest(threads=threads), patch('json.dump', side_effect=AssertionError('write')):
                _, db = self.read()
            for field in adapter.FLOATS + adapter.INTS + adapter.STRINGS:
                np.testing.assert_array_equal(getattr(db, field), getattr(reference, field), err_msg=field)

    def test_external_pin_lookup_does_not_materialize_all_node_keys(self):
        from ReadDEF import ReadDEFinfo
        from NameIdMap import NameIdMap
        from unittest.mock import patch
        reader = ReadDEFinfo(self.p)
        with patch.object(NameIdMap, 'keys', side_effect=AssertionError('full ID-list allocation')), \
             contextlib.redirect_stdout(io.StringIO()):
            reader.read()
        self.assertEqual(len(reader.extPinInfo), 2)

    def test_record_merge_is_def_order_and_single_thread_avoids_executor(self):
        from ReadDEF import ReadDEFinfo
        from unittest.mock import patch
        from concurrent.futures import Future
        reader = ReadDEFinfo(self.p)
        lines = ['- n0', '( u1 A ) ;', '- n1 ( u2 A ) ;', '- n2', '( u1 Y )', ';', '- n3 ;']
        with contextlib.redirect_stdout(io.StringIO()), \
             patch('ReadDEF.ThreadPoolExecutor', side_effect=AssertionError('unnecessary thread pool')):
            expected = reader._parallel_extract_record_groups(lines, 0, len(lines), 1, 'NETS')

        class ReverseCompletionPool:
            def __init__(self, max_workers): self.pending = []; self.count = max_workers
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def submit(self, fn, *args):
                future = Future()
                self.pending.append((future, fn, args))
                if len(self.pending) == self.count:
                    for f, work, arguments in reversed(self.pending):
                        f.set_result(work(*arguments))
                return future

        with contextlib.redirect_stdout(io.StringIO()), patch('ReadDEF.ThreadPoolExecutor', ReverseCompletionPool):
            actual = reader._parallel_extract_record_groups(lines, 0, len(lines), 4, 'NETS')
        self.assertEqual(actual, expected)

    def test_high_fanout_pin_membership_keeps_order_and_deduplicates(self):
        from Cell import LEF, Cell
        from Pin import Pin
        lef = LEF(0); pin = Pin(1, lef)
        cells = [Cell(i) for i in range(200)]
        for cell in cells + list(reversed(cells)):
            pin.add_net(cell, 7)
        self.assertEqual(pin.get_net_list()[7], ['%d 1' % i for i in range(200)])
        self.assertEqual(len(pin._large_net_members[7]), 200)
        pin.add_net(cells[0], 8)
        self.assertNotIn(8, pin._large_net_members)
        pin.reset_net()
        self.assertEqual(pin.get_net_list(), {})
        self.assertEqual(pin._large_net_members, {})
        pin.add_net(cells[0], 7)
        self.assertEqual(pin.get_net_list()[7], ['0 1'])

    def test_net_uses_indexed_lef_pin_lookup(self):
        from Cell import LEF, Cell
        from Pin import Pin
        from Net import Net
        from unittest.mock import patch
        lef = LEF(0); lef.set_cell_type('CORE')
        pin = Pin(1, lef)
        cell = Cell(3); cell.set_lef_info(lef)
        net = Net(7)
        with patch.object(lef, 'get_pins', side_effect=AssertionError('linear pin scan')):
            net.add_cell_pin(cell, 1)
            with self.assertRaises(ValueError):
                net.add_cell_pin(cell, 9)
        self.assertIs(net.components['3 1']['pin'], pin)
        self.assertEqual(net.get_stdCell_list(), [cell])
        self.assertNotIn('3 9', net.components)

    def test_explicit_net_weights_preserved(self):
        path = self.root / 'weights.json'
        path.write_text(json.dumps({'a': 1.25, 'n': 2.5, 'y': 3.75}))
        self.p.net_weight = str(path)
        _, db = self.read()
        np.testing.assert_array_equal(db.net_weights, [1.25, 2.5, 3.75])

    def test_missing_external_pin_use_defaults_to_signal_with_warning(self):
        path = self.root / 'tiny.def'
        path.write_text(path.read_text().replace(' + USE SIGNAL', '').replace('LAYER metal1', 'LAYER metal4'))
        with self.assertLogs(level='WARNING') as logs:
            _, db = self.read()
        warnings = [record.getMessage() for record in logs.records if 'has no USE' in record.getMessage()]
        self.assertEqual(warnings, [
            "[ReadDEF] PINS 'a' has no USE; forcing USE to SIGNAL",
            "[ReadDEF] PINS 'y' has no USE; forcing USE to SIGNAL",
        ])
        self.assertEqual(list(db.node_names), ['u1', 'u2', 'a', 'y'])
        self.assertEqual(db.ext_pin_info['a']['use'], 'SIGNAL')
        self.assertEqual(db.ext_pin_info['a']['layer0']['layerName'], 'metal4')
        self.assertEqual(list(db.pin_names), ['PIN a', 'u1 A', 'u1 Y', 'u2 A', 'u2 Y', 'PIN y'])

    def test_explicit_external_pin_use_is_preserved_without_warning(self):
        from ReadDEF import ReadDEFinfo
        from NameIdMap import NameIdMap
        from unittest.mock import patch
        for use in ('SIGNAL', 'POWER', 'GROUND', 'CLOCK'):
            for after_layer in (False, True):
                with self.subTest(use=use, after_layer=after_layer):
                    reader = ReadDEFinfo(self.p)
                    reader.cellInfo4LEF = {}
                    reader.lef_macro_name_to_id = NameIdMap('LEF macro')
                    reader.lef_pin_name_to_id = NameIdMap('LEF pin')
                    geometry = '+ LAYER metal4 ( -50 -50 ) ( 50 50 )'
                    usage = '+ USE ' + use
                    attributes = geometry + ' ' + usage if after_layer else usage + ' ' + geometry
                    record = '- port + NET port + DIRECTION INPUT ' + attributes + ' + FIXED ( 0 500 ) N ;'
                    with patch('ReadDEF.logging.warning') as warning:
                        reader._commit_pin_record_text(record)
                    warning.assert_not_called()
                    pin = next(iter(reader.extPinInfo.values()))['pin_info']
                    self.assertEqual(pin.get_use('metal4'), use)

    def test_writer_preserves_nets_and_updates_components(self):
        raw, db = self.read()
        raw.write_def(self.root / 'out.def', [3000, 5000, 0, 9000], db.node_y)
        out = (self.root / 'out.def').read_text()
        self.assertIn('u1 INV + PLACED ( 3000 0 ) N', out)
        self.assertEqual(out.split('NETS 3')[1], db.def_template.split('NETS 3')[1])

    def legacy(self, db):
        root = Path(self.p.save_path)
        for field in adapter.FLOATS + adapter.INTS + adapter.STRINGS:
            if field in ('node_x', 'node_y', 'node_orient'):
                continue
            value = getattr(db, field)
            if field in ('rows', 'flat_region_boxes', 'node2fence_region_map'):
                np.save(root / (field + '.npy'), np.asarray(value, dtype=object))
            else:
                (root / (field + '.txt')).write_text(''.join(str(v) + '\n' for v in value))
        for kind in ('node', 'pin', 'net'):
            name = 'pin_name2id_map_list' if kind == 'pin' else kind + '_name2id_list'
            (root / (name + '.txt')).write_text(''.join(str(i) + '\n' for i in range(len(getattr(db, kind + '_names')))))
        np.save(root / 'num_node_info.npy', [4, 0, 2, 4, 4, 0, 0, 0, 2, 0, 0, 0])
        np.save(root / 'die_info.npy', [db.xl, db.yl, db.xh, db.yh, db.row_height, db.site_width, db.lef_scale, db.def_scale])
        np.save(root / 'area_info.npy', [2e6, 0, db.total_space_area])

    def test_legacy_read_only_and_one_column_coordinates(self):
        import PlacementState
        _, db = self.read()
        self.legacy(db)
        self.p.db_option = 'binary_wo_pos'
        before = {str(p): p.stat().st_mtime_ns for p in Path(self.p.save_path).rglob('*') if p.is_file()}
        raw, loaded = self.read()
        for field in adapter.INTS:
            np.testing.assert_array_equal(getattr(db, field), getattr(loaded, field))
        np.testing.assert_array_equal(loaded.pin_offset_x, db.pin_offset_x)
        self.assertEqual(self.p.solution_file_suffix(), 'pl')
        after = {str(p): p.stat().st_mtime_ns for p in Path(self.p.save_path).rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        loaded.rawdb = raw; loaded.num_movable_nodes = 2; loaded.dtype = np.float32
        path = self.root / 'legacy.x'
        path.write_text('2000\n4000\n50\n9050\n')
        values, seen = PlacementState.read_table(path, 'posX', loaded, PlacementState.id_digest(loaded))
        np.testing.assert_array_equal(values, [2000, 4000, 50, 9050])
        self.assertTrue(seen.all())

    def test_multiple_lef_sources(self):
        p = self.root / 'other'; p.mkdir()
        (p / 'extra.lef').write_text('MACRO UNUSED\n CLASS CORE ;\n SIZE 2 BY 1 ;\nEND UNUSED\n')
        self.p.lef_dir_path.append(str(p))
        _, db = self.read()
        self.assertEqual(db.node_name2id_map['u2'], 1)

    def test_fixed_cell_order_and_orientation(self):
        path = self.root / 'tiny.def'
        value = path.read_text().replace('- u1 INV + PLACED ( 2000 0 ) N ;\n- u2 INV + PLACED ( 4000 0 ) N ;',
            '- u2 INV + FIXED ( 4000 0 ) FN ;\n- u1 INV + PLACED ( 2000 0 ) S ;')
        path.write_text(value)
        _, db = self.read()
        self.assertEqual(db.num_fixed_std_cell, 1)
        self.assertEqual(list(db.node_names), ['u1', 'u2', 'a', 'y'])
        self.assertEqual(list(db.node_orient), ['S', 'FN', 'N', 'N'])

    def test_corrupt_snapshot_is_not_parsed_again(self):
        _, db = self.read(); adapter.save(db, self.p)
        self.p.db_option = 'binary'
        np.save(Path(self.p.save_path) / 'physical_db/pin2node_map.npy', [-1] * 6)
        with self.assertRaisesRegex(ValueError, 'invalid pin2node'):
            self.read()

    @unittest.skipUnless(os.environ.get('DREAMPLACE_INSTALL'), 'set DREAMPLACE_INSTALL')
    def test_placedb_macro_orientation_roundtrip_and_override(self):
        import sys
        sys.path.append(os.environ['DREAMPLACE_INSTALL'])
        from PlaceDB import PlaceDB
        lef = self.root / 'tiny.lef'
        value = lef.read_text()
        macro = value[value.index('MACRO INV'):value.index('END LIBRARY')]
        macro = macro.replace('INV', 'BUF').replace('CLASS CORE', 'CLASS BLOCK').replace('SIZE 1 BY 1', 'SIZE 2 BY 2')
        lef.write_text(value.replace('END LIBRARY', macro + 'END LIBRARY'))
        path = self.root / 'tiny.def'
        path.write_text(path.read_text().replace('u1 INV + PLACED ( 2000 0 ) N',
            'u1 BUF + PLACED ( 2000 0 ) FN').replace('u2 INV + PLACED', 'u2 INV + FIXED'))
        self.p.enable_fillers = 0; self.p.num_bins_x = self.p.num_bins_y = 8
        with contextlib.redirect_stdout(io.StringIO()):
            db = PlaceDB(); db(self.p)
        self.assertEqual(db.num_movable_macro, 1)
        self.assertEqual(db.node_name2id_map['u1'], 0)
        pin = db.pin_name2id_map['u1 A']
        self.assertAlmostEqual(float(db.pin_offset_x[pin]), 1.9, places=5)
        # A repeated binary load must not apply FN twice.
        self.p.db_option = 'binary'
        with contextlib.redirect_stdout(io.StringIO()):
            restored = PlaceDB(); restored(self.p)
        np.testing.assert_array_equal(restored.pin_offset_x, db.pin_offset_x)
        self.assertEqual(restored.timing_pin_name2id_map['u1:A'], pin)
        orient = self.root / 'override.orient'; orient.write_text('0 N\n')
        self.p.read_orient = str(orient)
        with contextlib.redirect_stdout(io.StringIO()):
            overridden = PlaceDB(); overridden(self.p)
        self.assertAlmostEqual(float(overridden.pin_offset_x[pin]), .1, places=5)
        self.assertEqual(overridden.node_orient[0], b'N')
        out = self.root / 'out.def'
        overridden.write(self.p, str(out))
        self.assertIn('u1 BUF + PLACED ( 2000 0 ) N', out.read_text())

    @unittest.skipUnless(os.environ.get('DREAMPLACE_INSTALL'), 'set DREAMPLACE_INSTALL')
    def test_legacy_placedb_and_coordinate_output_without_def(self):
        import sys
        import PlacementState
        sys.path.append(os.environ['DREAMPLACE_INSTALL'])
        from PlaceDB import PlaceDB
        _, db = self.read(); self.legacy(db)
        self.p.db_option = 'binary_wo_pos'
        self.p.enable_fillers = 0; self.p.num_bins_x = self.p.num_bins_y = 8
        self.p.legalize_flag = 1; self.p.detailed_place_flag = 0
        (self.root / 'tiny.def').unlink(); (self.root / 'tiny.lef').unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            restored = PlaceDB(); restored(self.p)
        restored.apply(self.p, restored.node_x.copy(), restored.node_y.copy())
        restored.write(self.p, str(self.root / 'legacy.pl'))
        self.p.write_posX = str(self.root / 'x.tsv')
        PlacementState.write_final(self.p, restored)
        self.assertIn('u1', (self.root / 'legacy.pl').read_text())
        self.assertIn('cell_id\tposX', (self.root / 'x.tsv').read_text())


class SpecialMacroNetWeightTest(unittest.TestCase):
    def test_modcsa_stays_strictly_heaviest_after_timing_cap(self):
        db = SimpleNamespace(
            net_names=np.array(['n0', 'MODCSA_macro', 'n1', 'CriticalPathNet0']),
            net_weights=np.array([1.0, 1000.0, 1.0, 1000.0]),
        )
        self.assertEqual(adapter.pin_special_macro_net_weights(db), 2)
        self.assertGreater(db.net_weights[1], db.net_weights[0])
        self.assertGreater(db.net_weights[3], db.net_weights[2])
        # Lilith multiplies and caps every mapped net at max_net_weight.
        db.net_weights[:] = [1024.0, 1024.0, 800.0, 1024.0]
        adapter.pin_special_macro_net_weights(db)
        other_max = 1024.0
        self.assertGreater(db.net_weights[1], other_max)
        self.assertGreater(db.net_weights[3], other_max)
        self.assertEqual(db.net_weights[1], other_max * adapter.SPECIAL_MACRO_NET_RATIO)
        self.assertEqual(db.net_weights[1], db.net_weights[3])
        self.assertEqual(db.net_weights[0], 1024.0)
        self.assertEqual(db.net_weights[2], 800.0)

    def test_modcsa_name_is_case_insensitive_and_absent_is_noop(self):
        db = SimpleNamespace(
            net_names=np.array(['a', b'modcsa_net', 'b']),
            net_weights=np.array([3.0, 1000.0, 4.0]),
        )
        self.assertEqual(adapter.pin_special_macro_net_weights(db), 1)
        self.assertGreater(db.net_weights[1], max(db.net_weights[0], db.net_weights[2]))
        plain = SimpleNamespace(net_names=np.array(['a', 'b']), net_weights=np.array([1.0, 2.0]))
        self.assertEqual(adapter.pin_special_macro_net_weights(plain), 0)
        np.testing.assert_array_equal(plain.net_weights, [1.0, 2.0])


if __name__ == '__main__':
    unittest.main()
