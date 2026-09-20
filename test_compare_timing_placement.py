import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np

import CompareTimingPlacement as compare
import PlacementTimingMapping as mapping
from PreparePlacementDB import prepare, parse_args as prepare_args
import test_cluster_placement as cluster_tests
from ClusterPlacement import generate as cluster_generate


class ComparisonTest(unittest.TestCase):
    def setUp(self):
        f = cluster_tests.ClusterPlacementTest(); f.setUp()
        self.addCleanup(f.doCleanups)
        self.root, self.f = f.root, f
        with contextlib.redirect_stdout(io.StringIO()):
            cluster_generate(f.args)
            for role, deffile, lefs in [('original',f.original,[self.root/'tiny.lef']),
                                      ('reduced',f.output/'reduced.def',[f.output/'placement.lef'])]:
                prepare(prepare_args(['--def-input',str(deffile),'--lef-input',*map(str,lefs),
                                      '--output',str(self.root/role),'--threads','1']))
        mapping.build(self.root/'original',self.root/'reduced',f.output,self.root/'maps')
        self.args=compare.parse_args(['--original-db',str(self.root/'original'),
            '--placement-db',str(self.root/'reduced'),'--mapping',str(self.root/'maps'),
            '--original-def',str(f.original),'--placement-def',str(f.output/'reduced.def'),
            '--output',str(self.root/'comparison'),'--top','2'])

    def test_three_cases_sorted_tables_and_no_overwrite(self):
        r=compare.generate(self.args)
        self.assertIsNone(r['sta'])
        self.assertEqual(r['geometry']['internal_omitted']['cases']['C_two_db']['sum_um'],0)
        self.assertEqual(r['geometry']['internal_omitted']['cases']['B_original_centers']['sum_um'],0)
        self.assertGreater(r['geometry']['internal_omitted']['cases']['A_original']['sum_um'],0)
        self.assertEqual(r['area']['placement']['movable_area_um2'],10)
        out=Path(self.args.output)
        with (out/'net_comparison.tsv').open() as stream:
            rows=list(csv.DictReader(stream,delimiter='\t'))
        delta=[float(row['C_minus_A_um']) for row in rows]
        self.assertEqual(delta,sorted(delta,reverse=True))
        self.assertEqual(len(rows),3)
        self.assertIn('Not recomputed',(out/'report.md').read_text())
        self.assertIsNone(r['runs']['placement']['feedback_count'])
        with self.assertRaises(FileExistsError): compare.generate(self.args)

    def test_projection_numerical_values_and_orientation(self):
        o,p=compare.physical(self.args.original_db),compare.physical(self.args.placement_db)
        maps=mapping.load_mapping(self.args.mapping,o,p)
        pin=list(o.pin_names).index('u1 Y')
        o.pin_offset_x=o.pin_offset_x.copy();o.pin_offset_x[pin]=900
        og,pg=compare.geometry(o,self.args.original_def),compare.geometry(p,self.args.placement_def)
        cases=compare.projected_cases(o,p,maps,og,pg)
        np.testing.assert_allclose(cases['A_original'][:,pin],[2.9,.5])
        np.testing.assert_allclose(cases['B_original_centers'][:,pin],[3.5,.5])
        np.testing.assert_allclose(cases['C_two_db'][:,pin],[3.5,1.])
        path=Path(self.args.original_def)
        path.write_text(path.read_text().replace('( 2000 0 ) N','( 2000 0 ) FN'))
        new=compare.geometry(o,path)
        np.testing.assert_allclose(new['pins'][:,pin],[2.1,.5])

    def test_reject_changed_io_master_units_missing_and_unplaced(self):
        db=compare.physical(self.args.original_db)
        path=Path(self.args.original_def); original=path.read_text()
        changes=[('9000 500','9001 500'),('u1 INV','u1 WRONG'),
                 ('MICRONS 1000','MICRONS 2000'),('u1 INV','unknown INV'),
                 ('+ PLACED ( 2000 0 ) N','+ UNPLACED')]
        for before,after in changes:
            path.write_text(original.replace(before,after))
            with self.subTest(after=after),self.assertRaises(ValueError): compare.geometry(db,path)
        path.write_text(original)

    def test_log_fragment_not_zero_feedback_and_config_is_unverified(self):
        log=self.root/'fragment'
        log.write_text('[INFO] [Final placement] wns=-10 (ps_late)\n')
        cfg=self.root/'config.json';cfg.write_text('{"timing_update_start":20}')
        r,rows=compare.read_run(log,cfg)
        self.assertIsNone(r['feedback_count'])
        self.assertEqual(r['parameter_source'],'provided_config_not_verified_as_run')
        log.write_text("parameters = {'timing_update_start': 500}\n"
                       'apply lilith net-weighting scheme...\n'
                       'Two-DB feedback #1: ...\n'
                       'iteration 499, TNS -2 (1e+5 ps), WNS -3 (1e+3 ps)\n')
        r,rows=compare.read_run(log,cfg)
        self.assertEqual(r['feedback_count'],1)
        self.assertEqual(r['parameters']['timing_update_start'],500)
        self.assertEqual(rows,[(499,-3000.,-200000.)])

    def test_corrupt_mapping_and_sta_without_explicit_rc_rejected(self):
        self.args.sta=True
        with self.assertRaisesRegex(ValueError,'explicit'):compare.generate(self.args)
        self.args.sta=False
        p=self.root/'maps/timing_node_to_placement_node.npy'
        p.write_bytes(b'corrupted')
        with self.assertRaises(ValueError):compare.generate(self.args)
        self.assertFalse(Path(self.args.output).exists())

    @unittest.skipUnless(os.environ.get('DREAMPLACE_TIMING_CPP'),'set DREAMPLACE_TIMING_CPP')
    def test_native_three_case_sta_and_paths(self):
        import torch
        from TimingCache import TimingCacheMixin
        from test_timing_cache import fixture
        from test_reduced_liberty import LIB
        spec=importlib.util.spec_from_file_location('timing_cpp',os.environ['DREAMPLACE_TIMING_CPP'])
        cpp=importlib.util.module_from_spec(spec);spec.loader.exec_module(cpp)
        params=fixture(self.root);params.save_path=str(self.root/'original')
        Path(params.lib_input).write_text(LIB.replace('time_unit : "1ns";',
            'time_unit : "1ns"; pulling_resistance_unit : "1kohm";'))
        TimingCacheMixin().save_timing_cache(params,cpp)
        self.args.sta=True;self.args.rc_r=100.;self.args.rc_c=1e-15
        self.args.timing_cpp=os.environ['DREAMPLACE_TIMING_CPP']
        before=os.getcwd()
        try:
            os.chdir(Path(__file__).resolve().parent.parent)
            result=compare.generate(self.args)
        finally:os.chdir(before)
        for case in compare.CASES:
            self.assertIsNotNone(result['sta']['cases'][case]['wns_ps_late'])
            data=json.loads((Path(self.args.output)/(case+'.paths.json')).read_text())
            self.assertGreater(len(data['paths']),0)
            self.assertAlmostEqual(data['time_unit_seconds'],1e-9)


if __name__=='__main__':unittest.main()
