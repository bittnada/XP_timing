import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

import PlacementState as state


class PlacementStateTest(unittest.TestCase):
    def fixture(self):
        orientations = [0, 4, 0]
        raw = SimpleNamespace(
            node=lambda i: SimpleNamespace(orient=lambda: orientations[i]),
            setNodeOrient=lambda i, o: orientations.__setitem__(i, o))
        db = SimpleNamespace(num_physical_nodes=3, num_movable_nodes=2, dtype=np.float64,
            node_names=np.array([b'A', b'B', b'fixed.DREAMPlace.Shape0']),
            node_x=np.array([1., 2., 9.]), node_y=np.array([3., 4., 10.]),
            node_orient=np.array([b'N', b'FN', b'N']),
            node_size_x=np.array([8., 8., 4.]), node_size_y=np.array([12., 12., 4.]),
            pin_offset_x=np.array([2., 3.]), pin_offset_y=np.array([4., 5.]),
            node2pin_map=[np.array([0]), np.array([1]), np.array([], dtype=int)],
            node2orig_node_map=np.arange(3), rawdb=raw)
        db.unscale_pl = lambda shift, scale: (db.node_x / scale + shift[0], db.node_y / scale + shift[1])
        return db

    def test_cli_and_typo_alias(self):
        args = state.parse_cli(['a.json', '--read_psoX', 'x', '--read_posY', 'y',
                               '--read_orient', 'o', '--write_posX', 'xx',
                               '--write_posY', 'yy', '--write_orient', 'oo'])
        self.assertEqual(args.read_posX, 'x')
        self.assertEqual(args.write_orient, 'oo')
        self.assertEqual(state.parse_cli(['a.json', '--wrtie_orient', 'oo']).write_orient, 'oo')

    def test_coordinate_restart_and_unchanged_fixed_cells(self):
        db = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'x.tsv'
            path.write_text('cell_id posX\n1 20.5\n0 10.25\n2 9\n')
            p = SimpleNamespace(read_posX=str(path), random_center_init_flag=1, gp_noise_ratio=.025)
            state.read_initial(p, db)
        np.testing.assert_array_equal(db.node_x, [10.25, 20.5, 9.])
        np.testing.assert_array_equal(db.node_y, [3., 4., 10.])
        self.assertEqual(p.random_center_init_flag, 0)
        self.assertEqual(p.gp_noise_ratio, 0.)

    def test_bad_inputs_do_not_mutate_db(self):
        for data in ('0 1\n', '0 1\n0 2\n1 3\n', '0 nan\n1 2\n',
                     '0 1\n1 inf\n', '0 1\n1 2\n3 0\n',
                     '0 1\n1 2\n2 99\n', '# cell_names_sha256=wrong\n0 1\n1 2\n'):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                db = self.fixture()
                path = Path(directory) / 'x.tsv'
                path.write_text(data)
                with self.assertRaises(ValueError):
                    state.read_initial(SimpleNamespace(read_posX=str(path)), db)
                np.testing.assert_array_equal(db.node_x, [1., 2., 9.])

    def test_orientation_changes_pin_geometry_and_raw_label(self):
        db = self.fixture()
        enum = SimpleNamespace(**{name: i for i, name in enumerate(state.ORIENTS)})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'o.tsv'
            path.write_text('cell_id orient\n0 FN\n1 FS\n2 N\n')
            with patch.dict(sys.modules, {'dreamplace.ops.place_io.place_io': SimpleNamespace(OrientEnum=enum)}):
                state.read_initial(SimpleNamespace(read_orient=str(path)), db)
        np.testing.assert_array_equal(db.pin_offset_x, [6., 3.])
        np.testing.assert_array_equal(db.pin_offset_y, [4., 7.])
        self.assertEqual(list(db.node_orient), [b'FN', b'FS', b'N'])
        self.assertEqual(state.final_orientations(db), ['FN', 'FS', 'N'])

    def test_all_eight_orientation_transforms(self):
        expected = {'N': (2, 3, 8, 12), 'S': (6, 9, 8, 12),
                    'FN': (6, 3, 8, 12), 'FS': (2, 9, 8, 12),
                    'W': (9, 2, 12, 8), 'E': (3, 6, 12, 8),
                    'FW': (3, 2, 12, 8), 'FE': (9, 6, 12, 8)}
        for orient, result in expected.items():
            self.assertEqual(state.orient_offsets(2, 3, 8, 12, orient), result)

    def test_rotation_and_unknown_rejected_for_row_legalization(self):
        for orient in ('W', 'E', 'FE', 'FW', 'UNKNOWN', 'invalid'):
            with self.subTest(orient=orient), tempfile.TemporaryDirectory() as directory:
                db = self.fixture()
                path = Path(directory) / 'o.tsv'
                path.write_text('0 %s\n1 N\n' % orient)
                with self.assertRaises(ValueError):
                    state.read_initial(SimpleNamespace(read_orient=str(path), legalize_flag=1), db)

    def test_outputs_native_units_sorted_ids_checksum_and_actual_orientation(self):
        db = self.fixture()
        # Raw final orientation differs from stale Python label.
        db.rawdb.setNodeOrient(0, 5)
        db.final_placement_metrics = {'wns': (-12.5, 'ps_late'), 'tns': (None, 'ps_late'),
                                      'congestion_max': (2.5, 'reference_congestion')}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p = SimpleNamespace(write_posX=str(root / 'x.tsv'), write_posY=str(root / 'y.tsv'),
                                write_orient=str(root / 'o.tsv'), shift_factor=[100, 200], scale_factor=.5)
            state.write_final(p, db)
            x, seen = state.read_table(root / 'x.tsv', 'posX', db, state.id_digest(db))
            np.testing.assert_array_equal(x, [102, 104, 118])
            self.assertTrue(seen.all())
            orient, _ = state.read_table(root / 'o.tsv', 'orient', db, state.id_digest(db))
            self.assertEqual(list(orient), ['FS', 'FN', 'N'])
            self.assertIn('# metric\twns\t-12.5\tps_late', (root / 'o.tsv').read_text())
            self.assertIn('# metric\ttns\tNA\tps_late', (root / 'o.tsv').read_text())
            self.assertIn('2\tfixed.DREAMPlace.Shape0', (root / 'x.tsv.cells.tsv').read_text())
            with self.assertRaises(FileExistsError):
                state.write_final(p, db)

    def test_path_collisions_fail_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'x.tsv')
            with self.assertRaises(ValueError):
                state.validate_paths(SimpleNamespace(write_posX=path, write_posY=path))
            Path(path).write_text('0 1\n')
            with self.assertRaises(ValueError):
                state.validate_paths(SimpleNamespace(read_posX=path, write_posX=path))

    @unittest.skipUnless(os.environ.get('DREAMPLACE_INSTALL'), 'set DREAMPLACE_INSTALL for native placement test')
    def test_native_placement_roundtrip(self):
        install = Path(os.environ['DREAMPLACE_INSTALL'])
        source = Path(__file__).resolve().parent
        aux = install / 'unittest/ops/place_io_unittest/simple/simple.aux'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Enable GP with zero iterations to exercise its actual initial
            # position construction without changing the supplied locations.
            config = dict(aux_input=str(aux), gpu=0, num_threads=1, num_bins_x=8, num_bins_y=8,
                          global_place_flag=1, legalize_flag=0, detailed_place_flag=0,
                          timing_opt_flag=0, detailed_place_engine='', plot_flag=0,
                          enable_fillers=0, random_center_init_flag=1, gp_noise_ratio=.025,
                          target_density=1., scale_factor=.5, result_dir=str(root / 'results'),
                          global_place_stages=[dict(num_bins_x=8, num_bins_y=8, iteration=0,
                              learning_rate=.01, wirelength='weighted_average', optimizer='nesterov')])
            (root / 'config.json').write_text(json.dumps(config))
            # Unit tests use 8 movables and 2 fixed objects; all inputs are native units.
            (root / 'x.in').write_text(''.join('%d %d\n' % (i, 4 + 6 * i) for i in range(8)))
            (root / 'y.in').write_text(''.join('%d 24\n' % i for i in range(8)))
            (root / 'o.in').write_text(''.join('%d FN\n' % i for i in range(8)))
            env = dict(os.environ, PYTHONPATH=str(install), OMP_NUM_THREADS='1', MPLCONFIGDIR=str(root / 'mpl'))
            command = [sys.executable, str(source / 'Placer.py'), str(root / 'config.json'),
                       '--read_psoX', str(root / 'x.in'), '--read_posY', str(root / 'y.in'),
                       '--read_orient', str(root / 'o.in'), '--write_posX', str(root / 'x.out'),
                       '--write_posY', str(root / 'y.out'), '--write_orient', str(root / 'o.out')]
            result = subprocess.run(command, cwd=install, env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout[-12000:])
            def rows(path):
                return [line.split() for line in path.read_text().splitlines()
                        if line and not line.startswith('#')][1:]
            xrows = rows(root / 'x.out')
            self.assertEqual(len(xrows), 10)
            self.assertEqual([int(r[0]) for r in xrows], list(range(10)))
            np.testing.assert_allclose([float(r[1]) for r in xrows[:8]], [4 + 6 * i for i in range(8)])
            np.testing.assert_allclose([float(r[1]) for r in rows(root / 'y.out')[:8]], [24] * 8)
            self.assertTrue(all(r[1] in ('FN', 'S') for r in rows(root / 'o.out')[:8]))
            self.assertIn('disabled random-center', result.stdout)
            self.assertNotIn('move cells to the center', result.stdout)
            self.assertTrue((root / 'x.out.cells.tsv').exists())
            self.assertIn('# metric\twns\tNA\tps_late', (root / 'o.out').read_text())
            self.assertIn('# metric\tcongestion_max\t', (root / 'o.out').read_text())
            # Reload the exported checkpoint (including fixed cells/checksum),
            # then actually perform global placement iterations and export again.
            config['global_place_stages'][0]['iteration'] = 2
            (root / 'config.json').write_text(json.dumps(config))
            second = [sys.executable, str(source / 'Placer.py'), str(root / 'config.json'),
                      '--read_posX', str(root / 'x.out'), '--read_posY', str(root / 'y.out'),
                      '--read_orient', str(root / 'o.out'), '--write_posX', str(root / 'x.next'),
                      '--write_posY', str(root / 'y.next'), '--write_orient', str(root / 'o.next')]
            result = subprocess.run(second, cwd=install, env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout[-12000:])
            for axis in ('x', 'y'):
                final = rows(root / (axis + '.next'))
                self.assertEqual(len(final), 10)
                self.assertTrue(np.isfinite([float(r[1]) for r in final]).all())
                self.assertEqual(final[8:], rows(root / (axis + '.out'))[8:])


if __name__ == '__main__':
    unittest.main()
