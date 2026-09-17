"""Run: python3 -m unittest test_timing_edges -v"""
import csv
import tempfile
import unittest
from types import SimpleNamespace
import numpy as np
from TimingCluster import ClusterEngine


class TimingEdgesTest(unittest.TestCase):
    def test_sink_slack_filtering_and_full_fanout(self):
        # D drives two eligible pins, a FF, and a different-domain sink.
        db = SimpleNamespace(num_physical_nodes=5, num_movable_nodes=5,
            node_names=np.array([b'D', b'S1', b'S2', b'FF', b'CDC']),
            net_names=np.array([b'n']),
            pin_names=np.array([b'D:o', b'S1:a', b'S2:b', b'FF:d', b'CDC:a']),
            pin_direct=np.array([b'OUTPUT', b'INPUT', b'INPUT', b'INPUT', b'INPUT']),
            pin2node_map=np.arange(5), net2pin_map=[np.arange(5)])
        engine = ClusterEngine(SimpleNamespace(timing_edge_tau_ps=100., timing_edge_alpha=4.), db)
        engine.candidate = np.array([True, True, True, False, True])
        engine.domains[:] = [1, 1, 1, 1, 2]
        engine.launch_domains[:] = 1
        engine.clocks = {'clk': 'clk'}
        def report(name, split, tran):
            self.assertTrue(split)  # Never mix early/hold into the weight.
            return {('S1:a', False): -.01, ('S1:a', True): .2}.get((name, tran), float('nan'))
        timer = SimpleNamespace(time_unit=lambda: 1e-9, raw_timer=SimpleNamespace(report_slack=report))
        with tempfile.TemporaryDirectory() as directory:
            stats = engine.build_timing_edgelist(timer, directory)
            with open(directory + '/timing_edges.csv') as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['fanout'], '4')
        self.assertAlmostEqual(float(rows[0]['timing_slack_ps']), -10.)
        self.assertEqual(float(rows[0]['weight']), 2.5)
        self.assertEqual(rows[1]['slack_status'], 'missing')
        self.assertEqual(rows[1]['timing_slack_ps'], '')
        self.assertEqual(float(rows[1]['weight']), .5)
        self.assertEqual(stats['domain_mismatch_edges_skipped'], 1)

    def test_invalid_tau(self):
        db = SimpleNamespace(num_physical_nodes=0, num_movable_nodes=0)
        engine = ClusterEngine(SimpleNamespace(timing_edge_tau_ps=0), db)
        with self.assertRaises(ValueError):
            engine.build_timing_edgelist(None, '/tmp')


if __name__ == '__main__':
    unittest.main()
