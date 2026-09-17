"""Critical protection tests; set DREAMPLACE_TIMING_CPP to test a built .so too."""
import csv
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import numpy as np

from TimingCluster import ClusterEngine


def path(slack, cells):
    return dict(slack=slack, split="MAX", points=[
        dict(pin=(cell + ":A") if cell else "in", cell=cell,
             transition="rise", arrival=0.1) for cell in cells])


class CriticalPathsTest(unittest.TestCase):
    def fixture(self, **params):
        db = SimpleNamespace(num_physical_nodes=4, num_movable_nodes=4,
            node_names=np.array([b'D', b'S', b'other', b'FF']),
            node_name2id_map={b'D': 0, b'S': 1, b'other': 2, b'FF': 3},
            net_names=np.array([b'n']),
            pin_names=np.array([b'D:Y', b'S:A', b'other:A', b'FF:D']),
            pin_direct=np.array([b'OUTPUT', b'INPUT', b'INPUT', b'INPUT']),
            pin2node_map=np.arange(4), net2pin_map=[np.arange(4)])
        engine = ClusterEngine(SimpleNamespace(**params), db)
        engine.candidate = np.array([True, True, True, False])
        engine.domains[:] = engine.launch_domains[:] = 1
        engine.clocks = {'clk': 'clk'}
        return engine

    def timer(self, paths):
        def report(n, split):
            self.assertTrue(split)
            return sorted(paths, key=lambda p: p['slack'])[:n]
        return SimpleNamespace(time_unit=lambda: 1e-9, report_timing_paths=report,
                               raw_timer=SimpleNamespace(report_slack=lambda *a: 0.1))

    def test_exact_union_units_sorted_ids_and_anchor(self):
        engine = self.fixture(timing_cluster_critical_paths=2)
        timer = self.timer([path(.03, ['S', 'FF']), path(-.01, [None, 'D', 'D', 'S', 'FF']),
                            path(.2, ['other'])])
        with tempfile.TemporaryDirectory() as directory:
            meta = engine.extract_critical_cells(timer, directory)
            with open(Path(directory) / 'critical_cells.tsv') as stream:
                rows = list(csv.DictReader(stream, delimiter='\t'))
            with open(Path(directory) / 'critical_paths.jsonl') as stream:
                records = [json.loads(line) for line in stream]
        self.assertEqual(engine.critical_nodes, {0, 1, 3})
        self.assertEqual([r['cell_id'] for r in rows], ['0', '1', '3'])
        self.assertEqual([r['path_count'] for r in rows], ['1', '2', '2'])
        self.assertAlmostEqual(float(rows[1]['worst_path_slack_ps']), -10.)
        self.assertEqual(rows[2]['was_cluster_candidate'], '0')
        self.assertTrue(meta['path_budget_limited'])
        self.assertEqual(meta['protected_candidate_cells'], 2)
        self.assertIsNone(records[0]['points'][0]['cell_id'])

    def test_threshold_and_positive_slack_default(self):
        engine = self.fixture(timing_cluster_critical_paths=2, timing_cluster_critical_slack_ps=0.)
        timer = self.timer([path(-.01, ['D']), path(.01, ['S'])])
        with tempfile.TemporaryDirectory() as directory:
            meta = engine.extract_critical_cells(timer, directory)
            self.assertEqual(engine.critical_nodes, {0})
            self.assertEqual(meta['selected_paths'], 1)
            engine.params.timing_cluster_critical_slack_ps = None
            engine.extract_critical_cells(timer, directory)
            self.assertEqual(engine.critical_nodes, {0, 1})

    def test_fixed_macro_shape_resolves_through_actual_pin_owner(self):
        engine = self.fixture()
        macro = 'A1_B1_C1_D1_E1_F4_G3_H2_o765619'
        shape = macro + '.DREAMPlace.Shape0'
        engine.nmove = 3
        engine.db.node_names = np.array([b'D', b'S', b'other', shape.encode()])
        engine.db.node_name2id_map = {name: i for i, name in enumerate(engine.db.node_names)}
        engine.db.pin_name2id_map = {(macro + ':A').encode(): 3}
        timer = self.timer([path(-.01, ['D', macro, macro]), path(0., [macro])])
        with tempfile.TemporaryDirectory() as directory:
            meta = engine.extract_critical_cells(timer, directory)
            with open(Path(directory) / 'critical_cells.tsv') as stream:
                rows = list(csv.DictReader(stream, delimiter='\t'))
            with open(Path(directory) / 'critical_paths.jsonl') as stream:
                points = json.loads(next(stream))['points']
        self.assertEqual(engine.critical_nodes, {0, 3})
        self.assertEqual(meta['fixed_shape_mapped_cells'], 1)
        self.assertEqual(meta['protected_candidate_cells'], 1)
        self.assertEqual(rows[1]['cell_name'], shape)
        self.assertEqual(rows[1]['timing_cell_name'], macro)
        self.assertEqual(rows[1]['mapping_method'], 'fixed_shape_pin')
        self.assertEqual(rows[1]['path_count'], '2')
        self.assertEqual(rows[1]['was_cluster_candidate'], '0')
        self.assertEqual(points[1]['cell'], macro)
        self.assertEqual(points[1]['physical_cell_name'], shape)
        self.assertEqual(points[1]['cell_id'], 3)
        self.assertEqual(rows[0]['timing_cell_name'], 'D')
        self.assertEqual(rows[0]['mapping_method'], 'exact_name')

    def test_shape_fallback_rejects_missing_wrong_or_movable_pin_owner(self):
        engine = self.fixture()
        engine.nmove = 3
        engine.db.node_names = np.array([b'D', b'S', b'other', b'macro.DREAMPlace.Shape0'])
        engine.db.node_name2id_map = {name.decode(): i for i, name in enumerate(engine.db.node_names)}
        # Existence of Shape0 alone must not allow guessing a mapping.
        with self.assertRaisesRegex(ValueError, 'missing from PlaceDB'):
            engine._resolve_timing_cell('macro', 'macro:A')
        engine.db.pin_name2id_map = {'macro:A': 1}  # A movable, unrelated node.
        with self.assertRaises(ValueError):
            engine._resolve_timing_cell('macro', 'macro:A')
        engine.db.pin_name2id_map['macro:A'] = 3
        self.assertEqual(engine._resolve_timing_cell('macro', 'macro:A'), (3, 'fixed_shape_pin'))
        # Arbitrary prefix matches and unrelated shape owners are not accepted.
        for wrong in ['macro.DREAMPlace.Shape0_extra', 'macro2.DREAMPlace.Shape0']:
            engine.db.node_names = np.array([b'D', b'S', b'other', wrong.encode()])
            with self.assertRaises(ValueError):
                engine._resolve_timing_cell('macro', 'macro:A')
        engine.db.node_names = np.array([b'D', b'S', b'other', b'macro.DREAMPlace.Shape0'])
        engine.nmove = 4  # A shape-looking movable node is not a fixed macro.
        with self.assertRaises(ValueError):
            engine._resolve_timing_cell('macro', 'macro:A')

    def test_protected_sink_keeps_original_fanout_and_sibling(self):
        engine = self.fixture()
        timer = self.timer([path(-.01, ['S'])])
        with tempfile.TemporaryDirectory() as directory:
            engine.extract_critical_cells(timer, directory)
            stats = engine.build_timing_edgelist(timer, directory)
            with open(Path(directory) / 'timing_edges.csv') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['sink'], 'other')
            self.assertEqual(rows[0]['fanout'], '3')
            self.assertEqual(stats['critical_sink_edges_skipped'], 1)
            engine.critical_nodes = {0}
            stats = engine.build_timing_edgelist(timer, directory)
            self.assertEqual(stats['critical_driver_nets_skipped'], 1)
            with open(Path(directory) / 'timing_edges.csv') as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])

    def test_missing_paths_names_and_invalid_policy_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            for paths, pattern in [([], 'no MAX'), ([path(0., ['unknown'])], 'missing from PlaceDB'),
                                   ([path(float('nan'), ['D'])], 'finite MAX')]:
                with self.assertRaisesRegex(ValueError, pattern):
                    self.fixture().extract_critical_cells(self.timer(paths), directory)
            for params in [dict(timing_cluster_critical_paths=-1),
                           dict(timing_cluster_critical_slack_ps=float('inf'))]:
                with self.assertRaises(ValueError):
                    self.fixture(**params).extract_critical_cells(self.timer([]), directory)
            engine = self.fixture(timing_cluster_critical_paths=0)
            meta = engine.extract_critical_cells(self.timer([]), directory)
            self.assertEqual(meta['status'], 'disabled')
            self.assertEqual(engine.critical_nodes, set())

    def test_greedy_cannot_bridge_a_protected_vertex(self):
        # A -> critical -> B, plus A -> B. Deleting critical before sorting
        # would incorrectly allow {A,B}, which is non-convex in the full graph.
        engine = self.fixture()
        engine.critical_nodes = {1}
        engine._topological_order = lambda: ([0, 1, 2], [3])
        engine._predecessors = lambda n: {0: [], 1: [0], 2: [0, 1], 3: []}[n]
        engine._boundary = lambda members: (set(), set())
        engine.critical_nodes.add(3)  # Also skip protected residuals.
        clusters = engine.cluster()
        self.assertEqual([c['members'] for c in clusters], [[0], [2]])
        self.assertEqual(list(engine.assignment), [0, -1, 1, -1])

    @unittest.skipUnless(os.environ.get('DREAMPLACE_TIMING_CPP'), 'set DREAMPLACE_TIMING_CPP for native API test')
    def test_native_path_pins_do_not_expand_fanout(self):
        import torch  # Load extension dependencies first.
        from test_reduced_liberty import LIB
        spec = importlib.util.spec_from_file_location('timing_cpp', os.environ['DREAMPLACE_TIMING_CPP'])
        cpp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cpp)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'cells.lib').write_text(LIB)
            (root / 'design.v').write_text('module top(a,y,z); input a; output y,z; wire n; '
                'INV u1(.A(a),.Y(n)); INV u2(.A(n),.Y(y)); INV side(.A(n),.Y(z)); endmodule\n')
            (root / 'design.sdc').write_text('create_clock -name clk -period 1\n'
                'set_input_delay 0 -clock clk [get_ports a]\n'
                'set_input_transition 0.01 [get_ports a]\n'
                'set_output_delay 0.8 -clock clk [get_ports y]\n'
                'set_output_delay 0 -clock clk [get_ports z]\n'
                'set_load 0.01 [get_ports y]\nset_load 0.01 [get_ports z]\n')
            raw = cpp.io_forward(['test', '--lib_input', str(root / 'cells.lib'),
                                  '--verilog_input', str(root / 'design.v'),
                                  '--sdc_input', str(root / 'design.sdc')])
            raw.update_timing()
            paths = cpp.report_timing_paths(raw, 1, True)
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0]['split'], 'MAX')
            cells = {p['cell'] for p in paths[0]['points'] if p['cell'] is not None}
            self.assertEqual(cells, {'u1', 'u2'})
            self.assertEqual(paths[0]['points'][-1]['pin'], 'y')
            self.assertLess(paths[0]['slack'], 0.)
            self.assertAlmostEqual(paths[0]['slack'], raw.report_slack('y', True,
                paths[0]['points'][-1]['transition'] == 'fall'), places=6)
            with self.assertRaises(ValueError):
                cpp.report_timing_paths(raw, 0, True)


if __name__ == '__main__':
    unittest.main()
