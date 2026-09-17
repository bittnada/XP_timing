"""SCC protection in TimingCluster, including standalone --edges-only path."""
import csv
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from TimingCluster import ClusterEngine, run


def engine_from_edges(n, edges, unicode=False):
    nets = {}
    for src, dst in edges:
        nets.setdefault(src, []).append(dst)
    pins, nodepins = [], [[] for _ in range(n)]
    directions, nodes, pin2net, pin_names = [], [], [], []
    for net, (src, sinks) in enumerate(nets.items()):
        group = []
        for node, direction in [(src, 'OUTPUT')] + [(s, 'INPUT') for s in sinks]:
            p = len(nodes); group.append(p); nodepins[node].append(p)
            nodes.append(node); directions.append(direction); pin2net.append(net)
            pin_names.append('u%d:p%d' % (node, p))
        pins.append(np.asarray(group, dtype=np.int32))
    db = SimpleNamespace(num_physical_nodes=n, num_movable_nodes=n, num_nets=len(nets),
        node_names=np.array(['u%d' % i for i in range(n)], dtype='U20' if unicode else 'S20'),
        net_names=np.array(['n%d' % i for i in range(len(nets))], dtype='S20'),
        pin_names=np.array(pin_names,dtype='S30'), pin_direct=np.array(directions,dtype='U8' if unicode else 'S8'),
        pin2node_map=np.asarray(nodes,dtype=np.int32), pin2net_map=np.asarray(pin2net,dtype=np.int32),
        net2pin_map=pins, node2pin_map=[np.asarray(p,dtype=np.int32) for p in nodepins])
    e=ClusterEngine(SimpleNamespace(timing_cluster_critical_paths=0),db)
    e.candidate=np.ones(n,dtype=bool);e.domains[:]=1;e.launch_domains[:]=1
    e.clocks={'clk':'clk'};e.driver=np.asarray(list(nets),dtype=np.int32)
    return e


def timer():
    return SimpleNamespace(time_unit=lambda:1e-9,raw_timer=SimpleNamespace(report_slack=lambda *a:.1),
                           update_timing=lambda:None)


class TimingCyclesTest(unittest.TestCase):
    def test_exact_scc_not_downstream_and_self_feedback(self):
        e=engine_from_edges(7,[(0,1),(1,0),(1,2),(2,3),(4,0),(4,5),(6,6)])
        original=e.db.pin2node_map.copy(); candidates=e.candidate.copy()
        meta=e.detect_cyclic_cells()
        self.assertEqual(e.cyclic_nodes,{0,1,6})
        self.assertEqual(meta['cyclic_sccs'],2)
        self.assertEqual(meta['cyclic_cells'],3)
        np.testing.assert_array_equal(e.db.pin2node_map,original)
        np.testing.assert_array_equal(e.candidate,candidates)
        self.assertNotIn(2,e.cyclic_nodes);self.assertNotIn(3,e.cyclic_nodes)

    def test_edges_exclude_both_ends_and_keep_original_fanout(self):
        e=engine_from_edges(7,[(0,1),(1,0),(1,2),(2,3),(4,0),(4,5),(6,6)])
        with tempfile.TemporaryDirectory() as directory:
            meta=e.build_timing_edgelist(timer(),directory)
            with open(Path(directory)/'timing_edges.csv') as f: rows=list(csv.DictReader(f))
            with open(Path(directory)/'cyclic_cells.tsv') as f: cycles=list(csv.DictReader(f,delimiter='\t'))
            stored=json.loads((Path(directory)/'cyclic_cells_summary.json').read_text())
        self.assertEqual({(int(r['driver_id']),int(r['sink_id'])) for r in rows},{(2,3),(4,5)})
        row=next(r for r in rows if r['driver_id']=='4')
        self.assertEqual(row['fanout'],'2')
        self.assertAlmostEqual(float(row['fanout_weight']),1/np.sqrt(2))
        self.assertEqual([int(r['cell_id']) for r in cycles],[0,1,6])
        self.assertEqual([int(r['scc_cells']) for r in cycles],[2,2,1])
        self.assertEqual(meta['cyclic_driver_nets_skipped'],3)
        self.assertEqual(meta['cyclic_sink_edges_skipped'],1)
        self.assertEqual(meta['cycle_protection'],stored)

    def test_sequential_boundary_breaks_feedback(self):
        e=engine_from_edges(3,[(0,1),(1,2),(2,0)])
        e.sequential_nodes={1};e.candidate[1]=False
        self.assertEqual(e.detect_cyclic_cells()['cyclic_cells'],0)

    def test_fixed_macro_critical_and_domain_filters_do_not_hide_return_path(self):
        for excluded in ('critical','fixed','macro','clock','domain'):
            e=engine_from_edges(3,[(0,1),(1,2),(2,0)])
            if excluded=='critical': e.critical_nodes={1}
            elif excluded=='domain': e.domains[1]=2
            else:
                e.candidate[1]=False
                if excluded=='macro':e.macro_nodes={1}
                if excluded=='clock':e.clock_cells={1}
            with self.subTest(excluded=excluded):
                e.detect_cyclic_cells();self.assertEqual(e.cyclic_nodes,{0,1,2})

    def test_greedy_never_emits_cycle_members(self):
        e=engine_from_edges(7,[(0,1),(1,0),(1,2),(2,3),(4,5),(6,6)])
        e.critical_nodes={0}  # Overlapping protections must not double-count.
        clusters=e.cluster()
        self.assertFalse({0,1,6} & {n for c in clusters for n in c['members']})
        self.assertEqual(set(n for c in clusters for n in c['members']),{2,3,4,5})
        self.assertTrue(all(e.assignment[n]==-1 for n in (0,1,6)))
        with tempfile.TemporaryDirectory() as directory:
            e.extract_cyclic_cells(directory);summary=e.save(directory)
        self.assertEqual(summary['eligible_combinational_cells'],4)
        self.assertTrue(summary['constraints']['cyclic_cells_excluded'])

    def test_edges_only_still_protects_cycles_before_sta(self):
        e=engine_from_edges(4,[(0,1),(1,0),(2,3)])
        with tempfile.TemporaryDirectory() as directory:
            t=timer()
            t.update_timing=lambda:self.assertTrue((Path(directory)/'cyclic_cells.tsv').is_file())
            with patch('TimingCluster.ClusterEngine',return_value=e), \
                 patch.object(e,'prepare'), \
                 patch.object(e,'cluster',side_effect=AssertionError('greedy must not run')):
                result=run(e.params,e.db,directory,directory,timer=t,edges_only=True)
        self.assertEqual(result['edges_written'],1)
        self.assertEqual(e.cyclic_nodes,{0,1})

    def test_empty_dag_unicode_and_cached_scc(self):
        for e in (engine_from_edges(0,[]), engine_from_edges(3,[(0,1),(1,2)],unicode=True)):
            self.assertEqual(e.detect_cyclic_cells()['cyclic_cells'],0)
            with tempfile.TemporaryDirectory() as directory, \
                 patch('scipy.sparse.csgraph.connected_components',side_effect=AssertionError('recomputed')):
                e.build_timing_edgelist(timer(),directory)
                self.assertEqual((Path(directory)/'cyclic_cells.tsv').read_text().count('\n'),1)

    def test_multiple_driver_net_is_conservative_not_arbitrarily_selected(self):
        e=engine_from_edges(3,[(0,2),(2,0)])
        # Add a second output pin on the first net, without a return path to u1.
        e.db.pin2node_map=np.append(e.db.pin2node_map,1)
        e.db.pin2net_map=np.append(e.db.pin2net_map,0)
        e.db.pin_direct=np.append(e.db.pin_direct,b'OUTPUT')
        e.db.net2pin_map[0]=np.append(e.db.net2pin_map[0],len(e.db.pin_direct)-1)
        meta=e.detect_cyclic_cells()
        self.assertEqual(e.cyclic_nodes,{0,2})
        self.assertEqual(meta['ambiguous_nets_conservative'],1)


if __name__=='__main__':
    unittest.main()
