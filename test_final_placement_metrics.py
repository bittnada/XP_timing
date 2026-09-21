import ast
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import torch

import FinalPlacementMetrics as final


class FinalMetricsTest(unittest.TestCase):
    def fixture(self):
        params = SimpleNamespace(gpu=0, congestion_backend='cpu', shift_factor=[0, 0],
                                 scale_factor=1, num_bins_x=4, num_bins_y=4)
        db = SimpleNamespace(xl=0., yl=0., xh=8., yh=8., site_width=1., row_height=1.,
            num_physical_nodes=3, num_terminal_NIs=1, num_nets=2, num_nodes=3,
            num_movable_nodes=1, num_movable_std_cell=1, num_movable_macro=0,
            num_fixed_std_cell=0, num_fixed_macro=1, num_blockage=0,
            node_size_x=np.array([1., 2., 0.]), node_size_y=np.array([1., 2., 0.]),
            pin_offset_x=np.array([.5, 1., 0.]), pin_offset_y=np.array([.5, 1., 0.]),
            pin2node_map=np.array([0, 1, 2]), flat_net2pin_map=np.array([0, 1, 1, 2]),
            flat_net2pin_start_map=np.array([0, 2, 4]), net_names=np.array(['data', 'MODCSA_test']),
            net_weights=np.array([1., 1.]), unit_horizontal_capacity=1., unit_vertical_capacity=1.,
            total_movable_node_area=1., rawdb=SimpleNamespace(data=SimpleNamespace(
                congestion_metadata={'blockageInfo': {}, 'plot_fixed_macros': []})))
        pos = torch.tensor([1., 4., 7., 1., 4., 7.])
        return params, db, pos

    def test_original_methods_are_verbatim(self):
        original = Path('/mnt/hdd1/XP_shared_memory/bin/dreamplace/PlaceDB.py')
        if not original.is_file():
            self.skipTest('reference project not present')
        source = original.read_text()
        copied = Path(__file__).with_name('SharedCongestion.py').read_text()

        def methods(text):
            cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == 'PlaceDB')
            lines = text.splitlines()
            return {n.name: lines[min([n.lineno] + [d.lineno for d in n.decorator_list])-1:n.end_lineno]
                    for n in cls.body if isinstance(n, ast.FunctionDef)}

        expected = methods(source)
        actual = methods(copied)
        self.assertGreater(len(actual), 20)
        for name, body in actual.items():
            self.assertEqual(body, expected[name], name)

    def test_reference_maps_and_stats_cpu_cuda_entrypoints(self):
        p, db, pos = self.fixture()
        for method in ('legacy', 'rudy_pin_bbox'):
            for source in ('h', 'v', 'h_plus_v', 'hv_max'):
                p.congestion_calculation_method = method
                p.congestion_rudy_max_source = p.congestion_rudy_sum_source = source
                with self.subTest(method=method, source=source), contextlib.redirect_stdout(io.StringIO()):
                    stats = final.evaluate_congestion(p, db, pos)
                    view, _ = final.congestion_view(p, db)
                    x, y = pos[:3].numpy(), pos[3:].numpy()
                    maps = view.calc_congestion_numba(x, y, 1., 1.)
                    expected = (maps[0].max(), maps[0].sum(), maps[1].max(), maps[1].sum())
                    np.testing.assert_allclose(stats, expected, rtol=1e-6)
                    cuda_entry = view.calc_congestion_map_cuda(x, y, 1., 1., return_stats=True)
                    np.testing.assert_allclose(stats, cuda_entry, rtol=1e-6)
                    self.assertGreater(stats[0], 0)

    def test_latest_weights_and_metadata_scaling(self):
        p, db, pos = self.fixture()
        with contextlib.redirect_stdout(io.StringIO()):
            before = final.evaluate_congestion(p, db, pos)
            db.net_weights *= 2
            after = final.evaluate_congestion(p, db, pos)
        np.testing.assert_allclose(after, np.asarray(before) * 2, rtol=1e-6)
        p.shift_factor = [100, 200]; p.scale_factor = .5
        db.rawdb.data.congestion_metadata = {'blockageInfo': {'b': {
            'type': 'LAYER', 'coords': [102, 204, 110, 212]}},
            'plot_fixed_macros': [[102, 204, 8, 8]]}
        view, _ = final.congestion_view(p, db)
        np.testing.assert_array_equal(view.blockageInfo['b']['coords'], [1, 2, 5, 6])
        np.testing.assert_array_equal(view.plot_fixed_macros, [[1, 2, 4, 4]])
        self.assertEqual(db.rawdb.data.congestion_metadata['blockageInfo']['b']['coords'][0], 102)

    def test_final_sta_rebuilt_without_reweight_and_ps_units(self):
        p, db, pos = self.fixture()
        timer = Mock()
        timer.time_unit.return_value = 1e-9
        timer.report_wns.return_value = -.02
        timer.report_tns_elw.return_value = -.05
        timing_op = Mock(timer=timer)
        placer = SimpleNamespace(pos=[pos], data_collections=SimpleNamespace(),
            op_collections=SimpleNamespace(hpwl_op=lambda _: torch.tensor(30.),
                filtered_unweighted_hpwl_op=lambda _: torch.tensor(20.), timing_op=timing_op))
        overflow_op = Mock(return_value=(torch.tensor(.1), torch.tensor(1.2)))
        fake_placeobj = SimpleNamespace(PlaceObj=SimpleNamespace(build_electric_overflow=Mock(return_value=overflow_op)))
        weights = db.net_weights.copy()
        with patch.dict('sys.modules', {'PlaceObj': fake_placeobj}), contextlib.redirect_stdout(io.StringIO()):
            metrics = final.evaluate(placer, p, db, 42)
        torch.testing.assert_close(timing_op.call_args.args[0], pos)
        timer.update_timing.assert_called_once()
        timing_op.update_net_weights.assert_not_called()
        np.testing.assert_array_equal(weights, db.net_weights)
        self.assertAlmostEqual(metrics['wns'][0], -20.)
        self.assertAlmostEqual(metrics['tns'][0], -50.)
        self.assertEqual(metrics['hpwl'][0], 30.)
        self.assertEqual(metrics['filtered_unweighted_hpwl'][0], 20.)
        self.assertEqual(metrics['iteration'][0], 42)
        self.assertIs(db.final_placement_metrics, metrics)
        timer.report_wns.return_value = float('nan')
        with patch.dict('sys.modules', {'PlaceObj': fake_placeobj}), contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(final.evaluate(placer, p, db, 43)['wns'][0])
        placer.op_collections.timing_op.timer = None
        with patch.dict('sys.modules', {'PlaceObj': fake_placeobj}), contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(final.evaluate(placer, p, db, 43)['wns'][0])

    def test_native_view_reorders_only_evaluation_copy(self):
        p, db, pos = self.fixture()
        for name in ('num_movable_std_cell', 'num_movable_macro', 'num_fixed_std_cell',
                     'num_fixed_macro', 'num_blockage'):
            delattr(db, name)
        db.num_movable_nodes = 2
        db.movable_macro_mask = np.array([True, False])
        original = db.pin2node_map.copy()
        view, order = final.congestion_view(p, db)
        np.testing.assert_array_equal(order, [1, 0, 2])
        np.testing.assert_array_equal(view.pin2node_map, [1, 0, 2])
        np.testing.assert_array_equal(db.pin2node_map, original)
        self.assertEqual(view._node_type_ranges_for_congestion()['movable_macro'], (1, 2))

    def test_final_row_flip_updates_offsets_for_rc_hpwl_and_congestion(self):
        _, db, _ = self.fixture()
        db.node_orient = np.array([b'FN', b'N', b'N'])
        db.pin_offset_x[0] = .2
        data = SimpleNamespace(pin_offset_x=torch.tensor(db.pin_offset_x.copy()),
                               pin_offset_y=torch.tensor(db.pin_offset_y.copy()))
        placer = SimpleNamespace(_metric_pin_orientations=np.array(['N']), data_collections=data)
        final.synchronize_pin_geometry(placer, db)
        self.assertAlmostEqual(db.pin_offset_x[0], .8)
        self.assertAlmostEqual(data.pin_offset_x[0].item(), .8)
        self.assertEqual(db.pin_offset_x[1], 1.)

    @unittest.skipUnless(os.environ.get('DREAMPLACE_INSTALL'), 'set DREAMPLACE_INSTALL')
    def test_real_sta_without_gp_and_after_internal_dp(self):
        from test_makedb_adapter import fixture as physical_fixture
        from test_timing_cache import fixture as timing_fixture
        install = Path(os.environ['DREAMPLACE_INSTALL'])
        source = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            physical = physical_fixture(root)
            timing = timing_fixture(root)
            lib = Path(timing.lib_input)
            lib.write_text(lib.read_text().replace('time_unit : "1ns";',
                'time_unit : "1ns"; pulling_resistance_unit : "1kohm";'))
            config = vars(timing).copy()
            config.update(def_path=physical.def_path, lef_dir_path=physical.lef_dir_path,
                mode='default', gpu=0, num_threads=1, num_bins_x=8, num_bins_y=8,
                timing_opt_flag=1, global_place_flag=0, legalize_flag=0, detailed_place_flag=0,
                detailed_place_engine='', plot_flag=0, enable_fillers=0, target_density=1.,
                wire_resistance_per_micron=100., wire_capacitance_per_micron=1e-15,
                random_center_init_flag=0, gp_noise_ratio=0, result_dir=str(root / 'results'))
            env = dict(os.environ, PYTHONPATH=str(install), OMP_NUM_THREADS='1', MPLCONFIGDIR=str(root / 'mpl'))
            for dp in (0, 1):
                config['legalize_flag'] = config['detailed_place_flag'] = dp
                (root / 'config.json').write_text(json.dumps(config))
                orient = root / ('final%d.orient' % dp)
                run = subprocess.run([sys.executable, str(source / 'Placer.py'), str(root / 'config.json'),
                    '--wrtie_orient', str(orient)], cwd=install, env=env, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
                self.assertEqual(run.returncode, 0, run.stdout[-14000:])
                metrics = {fields[2]: fields[3:] for line in orient.read_text().splitlines()
                           if line.startswith('# metric\t') for fields in [line.split()]}
                for name in ('wns', 'tns', 'hpwl', 'overflow', 'congestion_max', 'congestion_total'):
                    self.assertTrue(np.isfinite(float(metrics[name][0])), (name, metrics))
                self.assertEqual(metrics['wns'][1], 'ps_late')
                self.assertEqual(run.stdout.count('[Final placement] wns='), 1)
                # Removing write_orient must not disable evaluation or alter any
                # final metric; no checkpoint/metric file may be created.
                before = {f.relative_to(root) for f in root.rglob('*') if f.is_file()}
                no_output = subprocess.run([sys.executable, str(source / 'Placer.py'),
                    str(root / 'config.json')], cwd=install, env=env, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
                self.assertEqual(no_output.returncode, 0, no_output.stdout[-14000:])
                final_lines = lambda output: [line.split('[Final placement] ', 1)[1]
                                              for line in output.splitlines() if '[Final placement] ' in line]
                self.assertEqual(final_lines(no_output.stdout), final_lines(run.stdout))
                self.assertEqual(no_output.stdout.count('[Final placement] wns='), 1)
                after = {f.relative_to(root) for f in root.rglob('*') if f.is_file()}
                self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
