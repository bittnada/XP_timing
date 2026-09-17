"""Real Leiden execution, split-only acyclic repair and saved-DB integration."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from scipy import sparse

from LeidenCluster import (boundaries, boundary_output_dependencies, candidates, generate, leiden_modules, parse_args,
                           prepare_constraints, quotient, repair, topological_rank, weighted_topological_split)
from LeidenGraph import load_design
from ReducedLiberty import ModelError
from ReducedLEF import generate as lef_generate, parse_args as lef_args
from ReducedVerilog import check_cluster_cycles, load_mapping


def options(**kw):
    values = dict(max_cells=32, max_boundary_pins=100, max_timing_arcs=1000,
                  max_area=float('inf'), repair_rounds=5)
    values.update(kw)
    return SimpleNamespace(**values)


def model(n, edges):
    a, b = np.asarray(edges, dtype=np.int64).T
    graph = sparse.csr_matrix((np.ones(len(a), dtype=bool), (a, b)), shape=(n, n))
    # One net per driver vertex, matching ordinary star-expanded connectivity.
    return dict(graph=graph, weighted=graph.astype(float) + graph.T,
                areas=np.ones(n), net_driver=np.arange(n), sink_net=a, sink_node=b)


class RepairTest(unittest.TestCase):
    def test_tapped_output_keeps_upstream_three_cells(self):
        d=model(6,[(4,0),(0,1),(1,2),(2,3),(2,5),(3,5)])
        labels=np.array([0,0,0,0,1,2])
        bad,_=boundary_output_dependencies(d,labels,np.array([True,False,False]))
        self.assertEqual(list(np.flatnonzero(bad)),[0])
        final,history,_,_=repair(d,labels,topological_rank(d['graph']),options())
        self.assertEqual(final[0],final[1]);self.assertEqual(final[1],final[2])
        self.assertNotEqual(final[2],final[3])
        self.assertEqual(history[0]['output_dependency_violations'],1)
        remaining,_=boundary_output_dependencies(d,final,np.bincount(final)>1)
        self.assertFalse(remaining.any())

    def test_independent_outputs_and_shared_upstream_are_allowed(self):
        d=model(6,[(4,0),(0,1),(0,2),(1,5),(2,5)])
        labels=np.array([0,0,0,1,2,3])
        bad,_=boundary_output_dependencies(d,labels,np.bincount(labels)>1)
        self.assertFalse(bad.any())

    def test_multihop_dependency_and_repeated_new_boundaries(self):
        d=model(7,[(5,0),(0,1),(1,2),(2,3),(3,4),(0,6),(2,6),(4,6)])
        labels=np.array([0,0,0,0,0,1,2])
        final,history,_,_=repair(d,labels,topological_rank(d['graph']),options())
        self.assertGreaterEqual(sum(h['output_dependency_violations'] for h in history),2)
        bad,_=boundary_output_dependencies(d,final,np.bincount(final)>1)
        self.assertFalse(bad.any())

    def test_multioutput_cell_does_not_seed_sibling_net(self):
        d=model(4,[(2,0),(0,1),(0,3),(1,3)])
        # Cell 0 has distinct output nets: one goes outside, one only to cell 1.
        # The boundary net must not seed the internal sibling net's sink.
        d.update(net_driver=np.array([2,0,0,1]),sink_net=np.array([0,1,2,3]),
                 sink_node=np.array([0,3,1,3]))
        labels=np.array([0,0,1,2])
        bad,_=boundary_output_dependencies(d,labels,np.bincount(labels)>1)
        self.assertFalse(bad.any())

    def test_does_not_follow_paths_outside_cluster(self):
        d=model(5,[(4,0),(0,1),(1,2),(2,3)])
        labels=np.array([0,1,0,2,3])
        bad,_=boundary_output_dependencies(d,labels,np.bincount(labels)>1)
        self.assertFalse(bad.any())  # The separate quotient-cycle check handles re-entry.

    def test_output_check_matches_bruteforce_random_dags(self):
        rng=np.random.default_rng(17)
        for _ in range(30):
            n=20;a,b=np.nonzero(np.triu(rng.random((n,n))<.2,k=1))
            d=model(n,list(zip(a,b)));labels=rng.integers(0,4,size=n)
            mutable=np.bincount(labels,minlength=4)>1
            actual,_=boundary_output_dependencies(d,labels,mutable)
            expected=np.zeros(4,dtype=bool)
            outside={int(u) for u,v in zip(a,b) if labels[u]!=labels[v]}
            for source in outside:
                group=labels[source]
                if not mutable[group]:continue
                todo=[int(v) for u,v in zip(a,b) if u==source and labels[v]==group];seen=set()
                while todo:
                    u=todo.pop()
                    if u in seen:continue
                    seen.add(u)
                    if u in outside and u!=source:expected[group]=True
                    todo.extend(int(v) for x,v in zip(a,b) if x==u and labels[v]==group)
            np.testing.assert_array_equal(actual,expected)

    def test_two_way_cycle_local_repair_unaffected_group_preserved(self):
        d = model(8, [(0,1),(2,3),(0,3),(2,1),(4,0),(4,2),(1,5),(3,5),(4,6),(6,7),(7,5)])
        labels = np.array([0,0,1,1,2,3,4,4])
        with self.assertRaises(ModelError):
            topological_rank(quotient(d['graph'], labels))
        final, history, _, _ = repair(d, labels, topological_rank(d['graph']), options())
        topological_rank(quotient(d['graph'], final))
        self.assertTrue(history)
        self.assertEqual(final[6], final[7])
        self.assertNotEqual(final[0], final[2])
        self.assertTrue(all(len(set(labels[final == g])) == 1 for g in set(final)))

    def test_long_cycle_singleton_fallback(self):
        d = model(8, [(6,0),(6,2),(6,4),(0,1),(2,3),(4,5),(0,3),(2,5),(4,1),(1,7),(3,7),(5,7)])
        labels = np.array([0,0,1,1,2,2,3,4])
        final, history, _, _ = repair(d, labels, topological_rank(d['graph']), options(repair_rounds=0))
        self.assertTrue(history[0]['singleton_fallback'])
        self.assertEqual(len(set(final)), 8)
        topological_rank(quotient(d['graph'], final))

    def test_original_cycle_and_self_loop_fail(self):
        for edges in ([(0,1),(1,0)], [(0,0)]):
            with self.assertRaisesRegex(ModelError, 'before clustering'):
                topological_rank(model(2, edges)['graph'])

    def test_protected_intermediate_node_is_not_dropped(self):
        d = model(5, [(3,0),(0,1),(1,2),(0,2),(2,4)])
        labels = np.array([0,1,0,2,3])
        final, _, _, _ = repair(d, labels, topological_rank(d['graph']), options())
        self.assertNotEqual(final[0], final[2])

    def test_limits_and_boundary_fanout_deduplication(self):
        d = model(6, [(0,1),(0,2),(1,2),(2,3),(2,4),(3,5),(4,5)])
        labels = np.array([0,1,1,2,2,3])
        ins, outs = boundaries(d, labels)
        self.assertEqual((ins[1], outs[1]), (1,1))
        for kw in ({'max_cells':1}, {'max_area':1.5}, {'max_boundary_pins':1}):
            final, _, _, _ = repair(d, labels, topological_rank(d['graph']), options(**kw))
            self.assertEqual(len(set(final)), 6)

    def test_weighted_cut_prefers_low_weight(self):
        d = model(4, [(0,1),(1,2),(2,3)])
        d['weighted'][0,1] = d['weighted'][1,0] = 100
        d['weighted'][1,2] = d['weighted'][2,1] = .1
        d['weighted'][2,3] = d['weighted'][3,2] = 100
        a, b = weighted_topological_split(np.arange(4), np.arange(4), d['weighted'])
        np.testing.assert_array_equal(a, [0,1]); np.testing.assert_array_equal(b, [2,3])

    def test_preexisting_scc_can_be_quarantined_without_removing_edges(self):
        d=model(6,[(0,1),(1,0),(1,2),(2,3),(3,4),(4,5)])
        d.update(eligible=np.ones(6,dtype=bool), metadata={}, vertex_names=list(map(str,range(6))))
        original=d['graph'].copy()
        rank=prepare_constraints(d,'retain')
        self.assertEqual(d['metadata']['original_cyclic_vertices'],2)
        self.assertFalse(d['eligible'][:2].any())
        labels=d['fixed_components'].copy(); labels[2:4]=10
        labels=np.unique(labels,return_inverse=True)[1]
        final,_,_,_=repair(d,labels,rank,options())
        self.assertEqual(final[0],final[1])
        self.assertEqual(final[2],final[3])
        self.assertEqual((original != d['graph']).nnz,0)
        topological_rank(quotient(d['graph'],final))

    def test_random_dags_repair_terminates_and_only_splits(self):
        rng = np.random.default_rng(5)
        for _ in range(30):
            n = 25
            a,b = np.nonzero(np.triu(rng.random((n,n)) < .14, k=1))
            d = model(n, list(zip(a,b)))
            initial = rng.integers(0, 6, size=n)
            final, _, _, _ = repair(d, initial, topological_rank(d['graph']), options(repair_rounds=2))
            topological_rank(quotient(d['graph'], final))
            self.assertTrue(all(len(set(initial[final == g])) == 1 for g in set(final)))


class SavedDesignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.save = self.root/'save'; self.save.mkdir()
        self.cells = {n:dict(macro_id='INV', placed_state='PLACED') for n in ('u2','u0','u3','u1')}
        self.lef = {'INV':dict(width=1.,height=1.,**{'class':'CORE'},
                              pin={'A':dict(direction='INPUT'), 'Y':dict(direction='OUTPUT')})}
        self.ports = {'a':dict(direction='INPUT'), 'y':dict(direction='OUTPUT')}
        self.nets = {'a':dict(cell_list=['PIN a','u0 A']),
                     'n0':dict(cell_list=['u0 Y','u1 A']),
                     'n1':dict(cell_list=['u1 Y','u2 A']),
                     'n2':dict(cell_list=['u2 Y','u3 A']),
                     'y':dict(cell_list=['u3 Y','PIN y'])}
        self.flush()
        (self.root/'cells.lib').write_text('library(test) { cell(INV) { } cell(DFF) { ff(IQ,IQN) { clocked_on : "CK"; } } }')
        (self.root/'names.tsv').write_text('cell_id\tcell_name\n10\tu0\n20\tu1\n30\tu2\n40\tu3\n')
        (self.root/'edges.tsv').write_text('driver_id\tsink_id\tweight\n10\t20\t5\n20\t30\t1\n30\t40\t5\n')
        (self.root/'timing.csv').write_text('driver_id,driver,sink_id,sink,launch_domain_mask,capture_domain_mask\n'
             '10,u0,20,u1,1,1\n20,u1,30,u2,1,1\n30,u2,40,u3,1,1\n')
        (self.root/'critical_cells.tsv').write_text('cell_id\tcell_name\n')
        self.args = parse_args(['--edges',str(self.root/'edges.tsv'), '--timing-edges',str(self.root/'timing.csv'),
             '--cell-names',str(self.root/'names.tsv'), '--saved-db',str(self.save), '--lib-dir',str(self.root/'cells.lib'),
             '--output',str(self.root/'result'), '--max-cells','2'])

    def flush(self):
        for name,obj in [('cells_info',self.cells), ('lef_info',self.lef),
                         ('netlist_info',self.nets), ('ext_pin_info',self.ports)]:
            (self.save/(name+'.json')).write_text(json.dumps(obj))

    def run_generate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return generate(self.args)

    def test_actual_leiden_determinism_and_reduced_lef_mapping(self):
        leiden_modules()  # Real backend, no mocked community assignments.
        report = self.run_generate()
        self.assertEqual(report['candidate']['mode'], 'leiden')
        self.assertTrue(report['final_quotient_dag'])
        self.assertTrue(report['boundary_output_independence']['verified'])
        self.assertEqual(report['eligible_cells'], report['merged_cells']+report['retained_eligible_cells'])
        first = Path(self.args.output)/'clusters.tsv'
        self.assertGreater(report['clusters'], 0)
        old = self.args.output; self.args.output = str(self.root/'result2')
        self.run_generate()
        self.assertEqual(first.read_text(), (Path(self.args.output)/'clusters.tsv').read_text())
        args = lef_args(['--input',str(first),'--cell-names',str(self.root/'names.tsv'),
             '--saved-db',str(self.save),'--sizes',str(Path(old)/'cluster_sizes.tsv'),
             '--pin-layer','metal1','--output',str(self.root/'clusters.lef')])
        with contextlib.redirect_stdout(io.StringIO()):
            result = lef_generate(args)
        clusters,_ = load_mapping(result['mapping_output'])
        check_cluster_cycles(clusters)

    def test_existing_membership_and_critical_protection(self):
        (self.root/'initial.tsv').write_text('10 7\n20 7\n30 7\n40 7\n')
        (self.root/'critical_cells.tsv').write_text('cell_id\tcell_name\n20\tu1\n')
        self.args.initial_clusters=str(self.root/'initial.tsv')
        report=self.run_generate()
        self.assertEqual(report['candidate']['ineligible_members_retained'],1)
        self.assertNotIn('20\t', (Path(self.args.output)/'clusters.tsv').read_text())
        self.assertIn('20\tu1\t-1\t-1\tretained_ineligible', (Path(self.args.output)/'cell_assignments.tsv').read_text())
        self.assertEqual(len((Path(self.args.output)/'cell_assignments.tsv').read_text().splitlines()),5)

    def test_recursive_export_and_hard_limit_after_cutoff_stop(self):
        self.args.modularity_cutoff=1.
        report=self.run_generate()
        self.assertEqual(report['candidate']['mode'],'recursive_modularity')
        self.assertEqual(report['candidate']['leaf_groups'],1)
        self.assertTrue(report['repair_history'])  # Parent size 4 must still obey cap 2.
        self.assertTrue(report['final_quotient_dag'])
        trace=(Path(self.args.output)/'modularity_tree.tsv').read_text()
        self.assertIn('below_cutoff',trace)
        self.assertEqual(report['merged_cells']+report['retained_eligible_cells'],4)

    def test_recursive_validation_and_unlimited_cells(self):
        for value in (float('nan'),float('inf'),-.6,1.1):
            self.args.modularity_cutoff=value
            with self.assertRaisesRegex(ModelError,'modularity-cutoff'):
                self.run_generate()
        self.args.modularity_cutoff=.7
        self.args.initial_clusters='unused'
        with self.assertRaisesRegex(ModelError,'cannot be combined'):
            self.run_generate()
        self.args.initial_clusters=None
        self.args.max_cells=-1
        with self.assertRaisesRegex(ModelError,'max-cells'):
            self.run_generate()
        self.args.max_cells=0
        report=self.run_generate()
        self.assertEqual(report['limits']['max_cells'],0)
        self.assertEqual(report['clusters'],1)

    def test_sequential_split_allows_register_feedback(self):
        self.cells['ff']=dict(macro_id='DFF',placed_state='PLACED')
        self.lef['DFF']=dict(width=2.,height=1.,**{'class':'CORE'},
                             pin={'D':dict(direction='INPUT'),'Q':dict(direction='OUTPUT')})
        self.nets['n1']['cell_list']=['u1 Y','ff D']
        self.nets['q']=dict(cell_list=['ff Q','u2 A'])
        self.nets['a']['cell_list']=['PIN a']
        self.nets['y']['cell_list'].append('u0 A')
        self.flush()
        (self.root/'edges.tsv').write_text('10 20 5\n30 40 5\n')
        d=load_design(self.args)
        topological_rank(d['graph'])
        ff=d['names'].index('ff')
        self.assertFalse(d['eligible'][ff])
        self.assertIn('ff:<SEQ_OUTPUT>', d['vertex_names'])

    def test_domain_partition_and_inconsistent_masks(self):
        (self.root/'timing.csv').write_text('driver_id,driver,sink_id,sink,launch_domain_mask,capture_domain_mask\n'
             '10,u0,20,u1,1,1\n30,u2,40,u3,2,2\n')
        (self.root/'initial.tsv').write_text('10 7\n20 7\n30 7\n40 7\n')
        self.args.initial_clusters=str(self.root/'initial.tsv')
        report=self.run_generate()
        self.assertEqual(report['clusters'],2)
        self.args.output=str(self.root/'bad')
        with (self.root/'timing.csv').open('a') as f:
            f.write('20,u1,30,u2,1,1\n')
        with self.assertRaisesRegex(ModelError,'Inconsistent domain'):
            self.run_generate()
        self.assertFalse(Path(self.args.output).exists())

    def test_invalid_ids_stale_edges_and_no_overwrite(self):
        original=(self.root/'timing.csv').read_text()
        (self.root/'timing.csv').write_text(original.replace('10,u0','10,u1'))
        with self.assertRaisesRegex(ModelError,'ID/name mismatch'):
            self.run_generate()
        (self.root/'timing.csv').write_text(original)
        (self.root/'edges.tsv').write_text('10 40 1\n')
        with self.assertRaisesRegex(ModelError,'absent from saved connectivity'):
            self.run_generate()
        Path(self.args.output).mkdir()
        with self.assertRaisesRegex(ModelError,'already exists'):
            self.run_generate()

    def test_missing_protection_and_original_cycle(self):
        (self.root/'critical_cells.tsv').unlink()
        with self.assertRaisesRegex(ModelError,'critical-cells'):
            self.run_generate()
        self.args.no_critical_protection=True
        self.nets['a']['cell_list']=['PIN a']
        self.nets['y']['cell_list'].append('u0 A'); self.flush()
        with self.assertRaisesRegex(ModelError,'before clustering'):
            self.run_generate()
        self.assertFalse(Path(self.args.output).exists())

    def test_original_cycle_retain_export_explicitly_marks_scope(self):
        self.nets['a']['cell_list']=['PIN a']
        self.nets['y']['cell_list'].append('u0 A'); self.flush()
        self.args.original_cycles='retain'
        self.args.initial_clusters=str(self.root/'initial.tsv')
        (self.root/'initial.tsv').write_text('10 7\n20 7\n30 7\n40 7\n')
        report=self.run_generate()
        self.assertFalse(report['original_dag'])
        self.assertIn('original loops remain',report['dag_scope'])
        self.assertEqual(report['clusters'],0)
        self.assertEqual((Path(self.args.output)/'cell_assignments.tsv').read_text().count('retained_original_cycle'),4)


class RecursiveCutoffTest(unittest.TestCase):
    def design(self):
        d=model(8,[(0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7)])
        d.update(eligible=np.array([False,True,True,True,True,True,True,False]),
                 domains={i:(1,1) for i in range(1,7)})
        d['weighted'][1,2]=d['weighted'][2,1]=100
        d['weighted'][3,4]=d['weighted'][4,3]=100
        d['weighted'][5,6]=d['weighted'][6,5]=100
        return d

    def args(self,cutoff):
        return options(initial_clusters=None,modularity_cutoff=cutoff,
                       seed=42,resolution=1.,iterations=2)

    def test_stop_retains_parent_not_proposed_partition(self):
        d=self.design()
        labels,meta=candidates(d,self.args(1.))
        self.assertEqual(len(set(labels[1:7])),1)
        self.assertGreater(d['modularity_trace'][0][6],1)
        self.assertEqual(meta['stop_counts'],{'below_cutoff':1})
        self.assertNotIn(labels[0],labels[1:7]);self.assertNotIn(labels[7],labels[1:7])

    def test_recurse_induced_scores_and_determinism(self):
        d=self.design()
        labels,meta=candidates(d,self.args(.5))
        self.assertEqual(meta['maximum_depth'],1)
        self.assertEqual(meta['leaf_groups'],3)
        self.assertEqual(labels[1],labels[2]);self.assertEqual(labels[3],labels[4])
        self.assertEqual(labels[5],labels[6]);self.assertNotEqual(labels[2],labels[3])
        self.assertAlmostEqual(d['modularity_trace'][1][5],0.)  # Local pair Q, not global contribution.
        other=self.design();again,_=candidates(other,self.args(.5))
        np.testing.assert_array_equal(labels,again)
        self.assertEqual(d['modularity_trace'],other['modularity_trace'])

    def test_equality_recurses_and_single_community_terminates(self):
        d=self.design();candidates(d,self.args(1.))
        exact=d['modularity_trace'][0][5]
        _,meta=candidates(self.design(),self.args(exact))
        self.assertGreater(meta['maximum_depth'],0)
        _,meta=candidates(self.design(),self.args(-.5))
        self.assertGreater(meta['stop_counts'].get('one_community',0),0)

    def test_domain_roots_and_empty_edges(self):
        d=self.design();d['domains'][3]=(2,2);d['domains'][4]=(2,2)
        labels,meta=candidates(d,self.args(1.))
        self.assertEqual(meta['domain_roots'],2)
        self.assertNotEqual(labels[1],labels[3])
        d['weighted']=sparse.csr_matrix((8,8))
        labels,meta=candidates(d,self.args(.7))
        self.assertEqual(len(set(labels)),8)
        self.assertEqual(meta['stop_counts'],{'edgeless':2})
        d['eligible'][:]=False
        with self.assertRaisesRegex(ModelError,'No eligible'):
            candidates(d,self.args(.7))

    def test_resolution_and_size_cap_do_not_change_cutoff_score_definition(self):
        d=self.design();args=self.args(1.);args.resolution=2.;args.max_cells=1
        ig,la=leiden_modules()
        original=la.find_partition
        with patch.object(la,'find_partition',wraps=original) as call:
            labels,meta=candidates(d,args)
        self.assertEqual(call.call_args.kwargs['max_comm_size'],0)
        self.assertEqual(call.call_args.kwargs['resolution_parameter'],2.)
        self.assertEqual(meta['modularity_score_resolution'],1.)
        self.assertEqual(len(set(labels[1:7])),1)

    def test_original_cycle_and_protected_vertex_survive_recursive_repair(self):
        d=model(8,[(0,1),(1,0),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7)])
        d.update(eligible=np.ones(8,dtype=bool),metadata={},vertex_names=list(map(str,range(8))),
                 domains={i:(1,1) for i in range(8)})
        d['eligible'][4]=False
        rank=prepare_constraints(d,'retain')
        labels,_=candidates(d,self.args(1.))
        self.assertEqual(labels[0],labels[1])
        self.assertNotIn(labels[4],labels[d['eligible']])
        final,_,_,_=repair(d,labels,rank,options(max_cells=0))
        topological_rank(quotient(d['graph'],final))
        for group in set(final[d['eligible']]):
            self.assertTrue(d['eligible'][final==group].all())

    def test_unlimited_legacy_mode_still_supported(self):
        args=self.args(None);args.max_cells=0
        labels,meta=candidates(self.design(),args)
        self.assertEqual(meta['mode'],'leiden')
        self.assertEqual(len(labels),8)


if __name__ == '__main__':
    unittest.main()
