"""Coordinate/weight bridge tests, including independently permuted IDs."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS
import unittest

import numpy as np

from TwoDBTiming import TwoDBTiming, restore_params, refresh_optimizer
import test_placement_timing_mapping as mapping_tests
import PlacementTimingMapping as mapping


class BridgeTest(unittest.TestCase):
    def setUp(self):
        f = mapping_tests.MappingTest(); f.setUp()
        self.addCleanup(f.doCleanups)
        f.build()
        self.p, self.t = f.reduced, f.original
        maps = mapping.load_mapping(f.output, self.t, self.p)
        self.maps = maps
        for db in (self.p, self.t):
            db.num_physical_nodes = len(db.node_names)
            db.num_nodes = db.num_physical_nodes
            db.node_x = np.zeros(db.num_nodes, dtype=np.float64)
            db.node_size_x = np.zeros(db.num_nodes)
            db.node_size_y = np.zeros(db.num_nodes)
            db.pin_offset_x = np.zeros(len(db.pin_names))
            db.pin_offset_y = np.zeros(len(db.pin_names))
            db.rawdb = NS(defUnit=lambda: 1000)
            for name in ('net_weights', 'net_weight_deltas', 'net_criticality', 'net_criticality_deltas'):
                setattr(db, name, np.ones(len(db.net_names)))
        self.p.num_nodes += 2  # Fillers must not shift the Y slice incorrectly.
        self.p.node_size_x[1] = 4
        self.p.node_size_y[1] = 6
        self.t.pin_offset_x[:] = 99  # These must never leak into clustered pins.
        self.params = NS(scale_factor=.5, shift_factor=[100, 200],
                         timing_update_start=500, timing_update_interval=15)
        self.b = TwoDBTiming(self.params, self.p, self.t, maps, NS(), None)
        self.pos = np.array([1, 10, 3, -999, -999, 4, 20, 6, -999, -999], dtype=float)

    def test_centers_offsets_units_permutation_and_fillers(self):
        self.p.pin_offset_x[0] = .25
        projected = self.b.project_positions(self.pos).reshape(2, -1)
        np.testing.assert_allclose(projected[0], [102, 124, 106, 124])
        np.testing.assert_allclose(projected[1], [208, 246, 212, 246])
        np.testing.assert_allclose(self.b.pin_offset_x, [0, 0, .5, 0, 0, 0])
        np.testing.assert_array_equal(self.t.pin_offset_x, np.full(6, 99))
        self.assertEqual(self.maps['timing_pin_to_placement_pin'][3], -1)
        self.assertEqual(projected[0, self.t.pin2node_map[3]], 124)

    def test_different_dbu_units(self):
        self.b.unit_ratio = 2
        np.testing.assert_allclose(self.b.project_positions(self.pos)[:4], [204, 248, 212, 248])

    def test_current_live_geometry_used(self):
        data = copy.deepcopy(self.p)
        data.node_size_x[1] = 8
        data.pin_offset_y[0] = 1.5
        op = NS()
        self.b.bind(data, op)
        result = self.b.project_positions(self.pos)
        self.assertEqual(result[1], 128)
        self.assertEqual(self.b.pin_offset_y[2], 3)
        self.assertIs(op.pin_offset_x, self.b.rc_offset_x)

    def test_weight_copy_omits_internal_net_and_preserves_array_identity(self):
        arr = self.p.net_weights
        self.t.net_weights[:] = [2, 99, 5]
        self.b.copy_weights_to_placement()
        np.testing.assert_array_equal(self.p.net_weights, [2, 5])
        self.assertIs(self.p.net_weights, arr)
        self.t.net_weights[0] = np.nan
        with self.assertRaisesRegex(ValueError, 'invalid net weights'):
            self.b.copy_weights_to_placement()
        np.testing.assert_array_equal(self.p.net_weights, [2, 5])

    def test_schedule_and_bad_positions(self):
        self.assertEqual([i for i in range(490, 531) if self.b.due(i)], [500, 515, 530])
        with self.assertRaises(ValueError): self.b.project_positions(self.pos[:-1])
        self.pos[0] = np.nan
        with self.assertRaises(ValueError): self.b.project_positions(self.pos)

    def test_restore_params_are_isolated_and_read_only(self):
        p = NS(db_option='two_timing_placement_db', mode='binary_write',
               read_posX='placement.x', write_orient='out.o', shift_factor=[1, 2], gpu=1)
        t = restore_params(p, '/tmp/t', timing=True)
        r = restore_params(p, '/tmp/p')
        self.assertEqual((t.db_option, t.mode, t.gpu, t.read_posX), ('binary', 'default', 0, ''))
        self.assertEqual(r.read_posX, 'placement.x')
        self.assertEqual(p.mode, 'binary_write')
        self.assertEqual(p.shift_factor, [1, 2])

    def test_refresh_nesterov_with_and_without_cached_gradients(self):
        import torch
        for cached in (False, True):
            group = {}
            for name, grad, obj in (('v_k', 'g_k', 'obj_k'), ('v_k_1', 'g_k_1', 'obj_k_1')):
                group[name] = [torch.tensor([2.])]
                group[grad] = [torch.tensor([0.])] if cached else []
                group[obj] = [torch.tensor(0.)]
            optimizer = NS(param_groups=[group], obj_and_grad_fn=lambda p: ((p*p).sum(), 2*p))
            refresh_optimizer(optimizer)
            self.assertEqual(group['obj_k'][0].item(), 4.)
            if cached: self.assertEqual(group['g_k'][0].item(), 4.)


@unittest.skipUnless(os.environ.get('DREAMPLACE_INSTALL'), 'set DREAMPLACE_INSTALL for full CPU placement test')
class TwoDBPlacementTest(unittest.TestCase):
    def test_binary_only_restore_feedback_and_final_sta(self):
        from test_cluster_placement import ClusterPlacementTest
        from ClusterPlacement import generate
        from PreparePlacementDB import prepare, parse_args
        from test_timing_cache import fixture as timing_fixture
        from test_reduced_liberty import LIB
        f = ClusterPlacementTest(); f.setUp()
        self.addCleanup(f.doCleanups)
        root = f.root
        with contextlib.redirect_stdout(io.StringIO()):
            generate(f.args)
            for target, def_file, lefs in (
                    ('original', root/'tiny.def', [root/'tiny.lef']),
                    ('reduced', f.output/'reduced.def', [f.output/'placement.lef'])):
                prepare(parse_args(['--def-input', str(def_file), '--lef-input', *map(str, lefs),
                                    '--output', str(root/target), '--threads', '1']))
        mapping.build(root/'original', root/'reduced', f.output, root/'maps')
        t = timing_fixture(root)
        Path(t.lib_input).write_text(LIB.replace('time_unit : "1ns";',
            'time_unit : "1ns"; pulling_resistance_unit : "1kohm";'))
        t.save_path = str(root/'original')
        config = root/'export.json'; config.write_text(json.dumps(vars(t)))
        install = Path(os.environ['DREAMPLACE_INSTALL'])
        env = dict(os.environ, PYTHONPATH=str(install), OMP_NUM_THREADS='1',
                   MPLCONFIGDIR=str(root/'mpl'), NUMBA_NUM_THREADS='1')
        def command(module, cfg):
            result = subprocess.run([sys.executable, '-B', str(install/'dreamplace'/module), str(cfg)],
                cwd=install, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout[-7000:] + result.stderr[-7000:])
            return result.stdout + result.stderr
        command('TimingCache.py', config)
        # Prove no LIB/Verilog/SDC/DEF/LEF parsing on restore.
        for path in (Path(t.lib_input), Path(t.verilog_input), Path(t.sdc_input),
                     root/'tiny.def', root/'tiny.lef', f.output/'reduced.def', f.output/'placement.lef'):
            path.unlink()
        before = {p: p.stat().st_mtime_ns for base in ('original', 'reduced', 'maps')
                  for p in (root/base).rglob('*') if p.is_file()}
        data = dict(db_option='two_timing_placement_db', placement_db_path=str(root/'reduced'),
                    timing_db_path=str(root/'original'), placement_timing_mapping_path=str(root/'maps'),
                    gpu=0, num_threads=1, num_bins_x=8, num_bins_y=8, enable_fillers=0,
                    global_place_flag=1, legalize_flag=0, detailed_place_flag=0,
                    detailed_place_engine='', plot_flag=0, random_center_init_flag=0, gp_noise_ratio=0,
                    timing_opt_flag=1, enable_net_weighting=1, timing_update_start=1, timing_update_interval=1,
                    wire_resistance_per_micron=100., wire_capacitance_per_micron=1e-15,
                    target_density=1., result_dir=str(root/'results'),
                    write_orient=str(root/'final.orient'),
                    global_place_stages=[dict(num_bins_x=8, num_bins_y=8, iteration=3,
                        learning_rate=.01, wirelength='weighted_average', optimizer='nesterov')])
        cfg = root/'run.json'; cfg.write_text(json.dumps(data))
        log = command('Placer.py', cfg)
        self.assertIn('Two-DB feedback #1', log)
        self.assertIn('[Final placement] wns=', log)
        self.assertNotIn('[Final placement] wns=NA', log)
        self.assertTrue((root/'final.orient').is_file())
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})


if __name__ == '__main__':
    unittest.main()
