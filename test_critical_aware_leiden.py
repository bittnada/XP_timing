"""Focused tests for the separate critical-aware Leiden policy."""
import csv
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import numpy as np
from scipy import sparse

from CriticalAwareLeidenCluster import (apply_critical_policy, enforce_band_caps,
                                        generate, parse_args)


def chain_design(n):
    src = np.arange(n - 1, dtype=np.int64)
    dst = src + 1
    graph = sparse.csr_matrix((np.ones(n - 1, dtype=bool), (src, dst)), shape=(n, n))
    return dict(graph=graph, weighted=graph.astype(float) + graph.T,
                eligible=np.ones(n, dtype=bool), names=['u%d' % i for i in range(n)],
                ids=np.arange(n, dtype=np.int64), id_to_node={i: i for i in range(n)},
                domains={i: (0, 1) for i in range(n)}, metadata={})


class CriticalPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.critical = self.root / 'critical.tsv'
        self.critical.write_text('cell_id\tcell_name\n')

    def args(self, **updates):
        values = dict(critical_cells=str(self.critical), timing_edges=str(self.root / 'timing.csv'),
            protect_slack_below_ps=0., critical_hops=1, protect_fanout=0,
            control_net_regex='', saved_db=str(self.root), scope_map=None, macro_halo_um=0.,
            protect_missing_slack=True, slack_t1_ps=100., slack_t2_ps=500.,
            max_cells_critical=4, max_cells_near=16, max_cells_noncritical=32,
            max_cells=32)
        values.update(updates)
        return SimpleNamespace(**values)

    def write_slacks(self, values):
        with (self.root / 'timing.csv').open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(('driver_id', 'sink_id', 'timing_slack_ps'))
            for driver, sink, slack in values:
                writer.writerow((driver, sink, slack))

    def test_negative_slack_and_one_hop_are_protected(self):
        design = chain_design(6)
        self.write_slacks([(0, 1, 600), (1, 2, -10), (2, 3, 600),
                           (3, 4, 600), (4, 5, 600)])
        report = apply_critical_policy(design, self.args())
        np.testing.assert_array_equal(np.flatnonzero(~design['eligible']), [0, 1, 2, 3])
        self.assertEqual(report['newly_protected_eligible_cells'], 4)
        self.assertEqual(report['slack_bands'][2]['cells'], 2)
        self.assertEqual(design['domains'][4], ((0, 1), 2))

    def test_missing_slack_and_high_fanout_are_protected(self):
        design = chain_design(5)
        design['graph'] = sparse.csr_matrix((np.ones(4, dtype=bool),
            (np.zeros(4, dtype=np.int64), np.arange(1, 5))), shape=(5, 5))
        design['weighted'] = design['graph'].astype(float) + design['graph'].T
        self.write_slacks([(0, 1, 600), (0, 2, 600), (0, 3, 600)])
        report = apply_critical_policy(design, self.args(critical_hops=0, protect_fanout=4))
        self.assertFalse(design['eligible'][0])
        self.assertFalse(design['eligible'][4])
        self.assertEqual(report['reason_counts']['high_fanout'], 1)
        self.assertEqual(report['reason_counts']['missing_slack'], 1)

    def test_adaptive_cap_splits_without_merging(self):
        design = chain_design(12)
        design['critical_policy'] = dict(band=np.zeros(12, dtype=np.int8),
                                         caps=np.array([4, 8, 12]))
        labels, splits = enforce_band_caps(design, np.zeros(12, dtype=np.int64), np.arange(12))
        self.assertGreater(splits, 0)
        self.assertLessEqual(np.bincount(labels).max(), 4)
        # Every final group remains a contiguous subset of the original path.
        for label in np.unique(labels):
            nodes = np.flatnonzero(labels == label)
            self.assertTrue(np.all(np.diff(nodes) == 1))


class CriticalSavedDesignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); save = self.root / 'save'; save.mkdir()
        cells = {'u%d' % i: dict(macro_id='INV', placed_state='PLACED',
                                 cell_type='CORE', position=[i * 2000., 0.]) for i in range(6)}
        lef = {'INV': dict(width=1., height=1., **{'class': 'CORE'},
                          pin={'A': dict(direction='INPUT'), 'Y': dict(direction='OUTPUT')})}
        nets = {'a': dict(cell_list=['PIN a', 'u0 A'])}
        for i in range(5):
            nets['n%d' % i] = dict(cell_list=['u%d Y' % i, 'u%d A' % (i + 1)])
        nets['y'] = dict(cell_list=['u5 Y', 'PIN y'])
        for filename, value in [('cells_info', cells), ('lef_info', lef),
                                ('netlist_info', nets),
                                ('ext_pin_info', {'a': dict(direction='INPUT'),
                                                  'y': dict(direction='OUTPUT')})]:
            (save / (filename + '.json')).write_text(json.dumps(value))
        (self.root / 'cells.lib').write_text('library(test) { cell(INV) { } }')
        (self.root / 'names.tsv').write_text('cell_id\tcell_name\n' +
            ''.join('%d\tu%d\n' % (i, i) for i in range(6)))
        (self.root / 'edges.tsv').write_text('driver_id\tsink_id\tweight\n' +
            ''.join('%d\t%d\t1\n' % (i, i + 1) for i in range(5)))
        with (self.root / 'timing.csv').open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(('driver_id', 'driver', 'sink_id', 'sink',
                             'launch_domain_mask', 'capture_domain_mask', 'timing_slack_ps'))
            for i in range(5):
                writer.writerow((i, 'u%d' % i, i + 1, 'u%d' % (i + 1), 0, 1, 600))
        (self.root / 'critical.tsv').write_text('cell_id\tcell_name\n0\tu0\n')
        self.args = parse_args(['--edges', str(self.root / 'edges.tsv'),
            '--timing-edges', str(self.root / 'timing.csv'), '--cell-names', str(self.root / 'names.tsv'),
            '--saved-db', str(save), '--lib-dir', str(self.root / 'cells.lib'),
            '--critical-cells', str(self.root / 'critical.tsv'), '--critical-hops', '0',
            '--protect-fanout', '0', '--control-net-regex', '', '--macro-halo-um', '0',
            '--max-cells-critical', '2', '--max-cells-near', '2', '--max-cells-noncritical', '2',
            '--output', str(self.root / 'result')])

    def test_end_to_end_writes_separate_policy_audit(self):
        report = generate(self.args)
        output = Path(self.args.output)
        self.assertEqual(report['critical_policy']['policy'], 'critical-aware-v1')
        self.assertTrue((output / 'clusters.tsv').is_file())
        self.assertTrue((output / 'critical_protection.tsv').is_file())
        self.assertIn('listed_critical', (output / 'critical_protection.tsv').read_text())
        self.assertNotIn('0\t', (output / 'clusters.tsv').read_text())


if __name__ == '__main__':
    unittest.main()
