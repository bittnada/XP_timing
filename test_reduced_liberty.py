"""Integration tests use the existing OpenTimer executable (no torch required)."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ReducedLiberty import (generate, ModelError, parse_liberty, cell_models, read_membership,
                            index_liberty, parse_liberty_text, discover_libraries, characterize)


LIB = '''library(test) {
 delay_model : table_lookup;
 time_unit : "1ns";
 capacitive_load_unit(1,pf);
 slew_lower_threshold_pct_rise : 20;
 slew_lower_threshold_pct_fall : 20;
 slew_upper_threshold_pct_rise : 80;
 slew_upper_threshold_pct_fall : 80;
 input_threshold_pct_rise : 50;
 input_threshold_pct_fall : 50;
 output_threshold_pct_rise : 50;
 output_threshold_pct_fall : 50;
 lu_table_template(t) {
  variable_1 : input_net_transition;
  variable_2 : total_output_net_capacitance;
  index_1("0, 1"); index_2("0, 1");
 }
 cell(INV) {
  area : 1;
  pin(A) { direction : input; capacitance : 0.01; }
  pin(Y) { direction : output; function : "!A";
   timing() {
    related_pin : "A"; timing_sense : negative_unate;
    cell_rise(t) { index_1("0, 1"); index_2("0, 1"); values("0.1, 0.4", "0.3, 0.6"); }
    cell_fall(t) { index_1("0, 1"); index_2("0, 1"); values("0.1, 0.4", "0.3, 0.6"); }
    rise_transition(t) { index_1("0, 1"); index_2("0, 1"); values("0.05, 0.15", "0.45, 0.55"); }
    fall_transition(t) { index_1("0, 1"); index_2("0, 1"); values("0.05, 0.15", "0.45, 0.55"); }
   }
  }
 }
}
'''


class ReducedLibertyTest(unittest.TestCase):
    def fallback_fixtures(self, root):
        args = self.mixed_fixtures(root)
        (root / 'design.v').write_text(
            'module top(a,y,z); input a; output y,z; wire n,m; '
            'INV u1(.A(a),.Y(n)); NAND u2(.A(a),.B(n),.Y(y)); '
            'INV u3(.A(y),.Y(m)); INV u4(.A(m),.Y(z)); endmodule\n')
        (root / 'clusters.txt').write_text('cell_id cluster_id\n1 7\n2 7\n3 8\n4 8\n')
        (root / 'cell_names.csv').write_text('cell_id,cell_name\n1,u1\n2,u2\n3,u3\n4,u4\n')
        args.cluster_sizes = root / 'sizes.tsv'
        args.cluster_sizes.write_text('cluster_id\twidth\theight\n7\t4\t2\n8\t3.00\t2\n')
        args.on_cluster_failure = 'retain-original'
        return args

    def test_retain_failed_joint_cluster_and_generate_matching_lef(self):
        from ReducedLEF import generate as generate_lef, parse_args
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args = self.fallback_fixtures(root)
            def corrupted(*a, **kw):
                values = characterize(*a, **kw)
                # Let the early corner succeed before failing the late one.
                if a[5] == 'late' and kw.get('joint') and kw.get('abstract'):
                    values[next(iter(values))] += 1.
                return values
            with patch('ReducedLiberty.characterize', side_effect=corrupted):
                report = generate(args)
            self.assertEqual(report['status'], 'complete')
            self.assertEqual([c['cluster_id'] for c in report['clusters']], [8])
            self.assertEqual(report['retained_original_cells'], 2)
            failure = json.loads((root / 'result/failed_clusters.jsonl').read_text())
            self.assertEqual((failure['cluster_id'], failure['action']), (7, 'retained_original'))
            self.assertIn('joint-transition', failure['reason'])
            self.assertEqual((root / 'result/final_clusters.tsv').read_text(), 'cell_id\tcluster_id\n3\t8\n4\t8\n')
            self.assertEqual((root / 'result/cluster_sizes.tsv').read_text(), 'cluster_id\twidth\theight\n8\t3.00\t2\n')
            retained = (root / 'result/retained_cells.tsv').read_text()
            self.assertIn('1\t7\tu1\tINV', retained)
            self.assertIn('2\t7\tu2\tNAND', retained)
            for corner in ('Early', 'Late'):
                self.assertEqual(set(cell_models(parse_liberty(root / ('result/clusters_%s.lib' % corner)))), {'TC_8'})
            netlist = (root / 'result/reduced.v').read_text()
            self.assertIn('INV u1(.A(a),.Y(n));', netlist)
            self.assertIn('NAND u2(.A(a),.B(n),.Y(y));', netlist)
            self.assertIn('TC_8 __tc_cluster_8 (.I0(y), .O0(z));', netlist)
            self.assertNotIn('TC_7', netlist)
            self.assertEqual(report['reduced_verilog']['reduced_instances'], 3)
            lef_args = parse_args(['--input', str(root / 'result/cluster_mapping.jsonl'),
                                  '--sizes', str(root / 'result/cluster_sizes.tsv'), '--pin-layer', 'metal2',
                                  '--output', str(root / 'cluster.lef')])
            self.assertEqual(generate_lef(lef_args)['cluster_count'], 1)
            self.assertIn('MACRO TC_8', (root / 'cluster.lef').read_text())
            self.assertNotIn('TC_7', (root / 'cluster.lef').read_text())

    def test_retain_unsupported_cluster_without_changing_original_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args = self.fallback_fixtures(root)
            args.mixed_polarity = 'reject'
            report = generate(args)
            self.assertEqual(report['status'], 'complete')
            self.assertEqual(report['failed_clusters'][0]['cluster_id'], 7)
            self.assertIn('Mixed-polarity', report['failed_clusters'][0]['reason'])
            self.assertEqual(report['reduced_verilog']['retained_instances'], 2)

    def test_infrastructure_errors_are_not_silently_retained(self):
        errors = [ModelError('OpenTimer failed'), OSError('disk full'),
                  subprocess.TimeoutExpired('ot-shell', 1)]
        for error in errors:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); args = self.fallback_fixtures(root)
                with patch('ReducedLiberty.characterize', side_effect=error):
                    with self.assertRaises(ModelError):
                        generate(args)
                report = json.loads((root / 'result/manifest.json').read_text())
                self.assertEqual(report['status'], 'failed_partial_do_not_use')
                self.assertEqual(report['retained_original_cells'], 0)
                self.assertFalse((root / 'result/reduced.v').exists())

    def test_no_verified_clusters_fails_with_retention_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args = self.mixed_fixtures(root)
            args.on_cluster_failure = 'retain-original'; args.mixed_polarity = 'reject'
            with self.assertRaisesRegex(ModelError, 'No clusters passed verification'):
                generate(args)
            report = json.loads((root / 'result/manifest.json').read_text())
            self.assertEqual(report['retained_original_cells'], 2)
            self.assertEqual(report['status'], 'failed_partial_do_not_use')
            self.assertFalse((root / 'result/reduced.v').exists())

    def test_missing_or_duplicate_sizes_fail_before_characterization(self):
        for content, pattern in [('7 4 2\n', 'Missing cluster sizes'),
                                 ('7 4 2\n7 5 2\n8 3 2\n', 'Duplicate cluster size ID'),
                                 ('7 4 2\n8 nan 2\n', 'finite and positive')]:
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); args = self.fallback_fixtures(root)
                args.cluster_sizes.write_text(content)
                with self.assertRaisesRegex(ModelError, pattern):
                    generate(args)
                self.assertFalse(Path(args.output).exists())

    def test_successful_run_exports_sizes_and_empty_failure_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args = self.fallback_fixtures(root)
            report = generate(args)
            self.assertEqual(report['failed_clusters'], [])
            self.assertEqual(report['retained_original_cells'], 0)
            self.assertEqual((root / 'result/failed_clusters.jsonl').read_text(), '')
            self.assertEqual((root / 'result/cluster_sizes.tsv').read_text(), args.cluster_sizes.read_text())

    def mixed_fixtures(self, root):
        args=self.fixtures(root, 'module top(a,y); input a; output y; wire n; '
            'INV u1(.A(a),.Y(n)); NAND u2(.A(a),.B(n),.Y(y)); endmodule\n')
        inv=LIB[LIB.index(' cell(INV)'):LIB.rfind('}')]
        nand=inv.replace('cell(INV)','cell(NAND)').replace('function : "!A"','function : "!(A & B)"')
        nand=nand.replace('  pin(Y)','  pin(B) { direction : input; capacitance : 0.02; }\n  pin(Y)')
        timing=inv[inv.index('   timing()'):inv.rfind('  }')]
        timing=timing.replace('related_pin : "A"','related_pin : "B"').replace(
            '"0.1, 0.4", "0.3, 0.6"','"1.1, 1.4", "1.3, 1.6"')
        nand=nand[:nand.rfind('  }')]+timing+nand[nand.rfind('  }'):]
        (root/'original.lib').write_text(LIB[:LIB.rfind('}')]+nand+'\n}\n')
        return args

    def test_mixed_paths_have_distinct_transition_tables_and_joint_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);args=self.mixed_fixtures(root)
            report=generate(args)
            item=report['clusters'][0]
            self.assertEqual((item['arcs'],item['logical_io_pairs'],item['mixed_polarity_pairs']),(2,1,1))
            self.assertEqual(report['status'],'complete')
            for corner in ('Early','Late'):
                pins=cell_models(parse_liberty(root/('result/clusters_%s.lib'%corner)))['TC_7'][1]
                arcs=[g for g in pins['O0'].children if g.kind=='timing']
                self.assertEqual({a.attrs['timing_sense'] for a in arcs},{'positive_unate','negative_unate'})
                self.assertEqual({a.attrs['related_pin'] for a in arcs},{'I0'})
                first={a.attrs['timing_sense']:float(next(t for t in a.children if t.kind=='cell_rise').calls['values'][0].split(',')[0]) for a in arcs}
                self.assertGreater(first['positive_unate']-first['negative_unate'],.5)
            self.assertTrue(all(v['joint_transition_max_error_ps']<.001 for v in item['verification']))
            mapping=json.loads((root/'result/cluster_mapping.jsonl').read_text())
            self.assertEqual(len(mapping['arcs']),2)

    def test_mixed_reject_policy_keeps_old_fail_closed_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);args=self.mixed_fixtures(root);args.mixed_polarity='reject'
            with self.assertRaisesRegex(ModelError,'Mixed-polarity reconvergence'):
                generate(args)
            self.assertEqual(json.loads((root/'result/manifest.json').read_text())['status'],'failed_partial_do_not_use')

    def test_joint_mismatch_does_not_publish_bad_cell(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);args=self.mixed_fixtures(root)
            def corrupted(*a,**kw):
                values=characterize(*a,**kw)
                if kw.get('joint') and kw.get('abstract'):
                    key=next(iter(values));values[key]+=1.
                return values
            with patch('ReducedLiberty.characterize',side_effect=corrupted):
                with self.assertRaisesRegex(ModelError,'joint-transition round-trip mismatch'):
                    generate(args)
            report=json.loads((root/'result/manifest.json').read_text())
            self.assertEqual(report['status'],'failed_partial_do_not_use')
            self.assertEqual(report['clusters'],[])
            self.assertEqual((root/'result/cluster_mapping.jsonl').read_text(),'')

    def test_source_non_unate_remains_unsupported(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);args=self.fixtures(root)
            (root/'original.lib').write_text(LIB.replace('negative_unate','non_unate'))
            with self.assertRaisesRegex(ModelError,'Non-unate/unspecified'):
                generate(args)

    def fixtures(self, root, verilog=None):
        (root / 'original.lib').write_text(LIB)
        (root / 'design.v').write_text(verilog or 'module top(a,y); input a; output y; wire n; INV u1(.A(a),.Y(n)); INV u2(.A(n),.Y(y)); endmodule\n')
        (root / 'clusters.txt').write_text('cell_id cluster_id\n1 7\n2 7\n')
        (root / 'cell_names.csv').write_text('cell_id,cell_name\n1,u1\n2,u2\n')
        shell = Path(__file__).resolve().parent.parent / 'thirdparty/OpenTimer/bin/ot-shell'
        return SimpleNamespace(clusters=root / 'clusters.txt', cell_names=root / 'cell_names.csv',
                               early_lib=root / 'original.lib', late_lib=root / 'original.lib',
                               verilog=root / 'design.v', output=root / 'result', ot_shell=shell,
                               slews_ps=[10, 50, 100], loads_ff=[1, 5, 10], cluster_ids=None, timeout=30)

    def directory_fixtures(self, root):
        args = self.fixtures(root)
        folder = root / 'libs'
        (folder / 'nested').mkdir(parents=True)
        (folder / 'a.lib').write_text(LIB)
        (folder / 'nested/b.LIB').write_text(LIB.replace('cell(INV)', 'cell(INV2)'))
        # This unused body is not supported by the full parser. Indexing must
        # not parse it, even though comments/strings contain structural tokens.
        (folder / 'unused.lib').write_text('library(unused) { /* cell(FAKE) { } */ '
                                         'cell(UNUSED) { strange : "brace } //"; unsupported !; } }')
        (root / 'design.v').write_text('module top(a,y); input a; output y; wire n; '
                                      'INV u1(.A(a),.Y(n)); INV2 u2(.A(n),.Y(y)); endmodule\n')
        args.early_lib = args.late_lib = None
        args.lib_dir = [folder]
        return args, folder

    def test_recursive_directory_shared_min_max(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, folder = self.directory_fixtures(root)
            report = generate(args)
            self.assertEqual(report['library_mode'], 'shared_min_max')
            self.assertEqual(len(report['library_sets']['early']['files']), 3)
            self.assertEqual(set(report['library_sets']['early']['cell_sources']), {'INV', 'INV2'})
            self.assertEqual(len(report['library_sets']['early']['active_internal_files']), 2)
            pins = cell_models(parse_liberty(root / 'result/clusters_Late.lib'))['TC_7'][1]
            arc = pins['O0'].children[0]
            delay = next(t for t in arc.children if t.kind == 'cell_rise')
            self.assertAlmostEqual(float(delay.calls['values'][0].split(',')[0]), .2163, places=5)
            self.assertEqual(discover_libraries([folder, folder / 'a.lib']), discover_libraries([folder]))

    def test_duplicate_requires_explicit_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, folder = self.directory_fixtures(root)
            (folder / 'duplicate.lib').write_text(LIB.replace('area : 1', 'area : 9'))
            with self.assertRaisesRegex(ModelError, 'Duplicate required cell INV'):
                generate(args)
            self.assertFalse(Path(args.output).exists())
            args.cell_override = ['INV=' + str(folder / 'a.lib')]
            report = generate(args)
            self.assertEqual(report['library_sets']['early']['cell_sources']['INV'], str(folder / 'a.lib'))
            cell = cell_models(parse_liberty(root / 'result/clusters_Late.lib'))['TC_7'][0]
            self.assertEqual(float(cell.attrs['area']), 2)

    def test_unused_duplicate_recorded_and_bad_override_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, folder = self.directory_fixtures(root)
            (folder / 'unused_duplicate.lib').write_text('library(other) { cell(UNUSED) {} }')
            args.cell_override = ['INV=' + str(folder / 'unused.lib')]
            with self.assertRaisesRegex(ModelError, 'does not identify a source'):
                generate(args)
            args.cell_override = None
            report = generate(args)
            self.assertEqual(len(report['library_sets']['early']['unused_duplicate_cells']['UNUSED']), 2)

    def test_invalid_lut_dimensions_rejected_before_opentimer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.fixtures(root)
            (root / 'original.lib').write_text(LIB.replace('"0.1, 0.4", "0.3, 0.6"', '"0.1, 0.4"'))
            with self.assertRaisesRegex(ModelError, 'Invalid NLDM table dimensions'):
                generate(args)
            self.assertFalse(Path(args.output).exists())

    def test_incompatible_library_sets(self):
        changes = [('"1ns"', '"1ps"', 'Incompatible time/capacitance'),
                   ('input_threshold_pct_rise : 50', 'input_threshold_pct_rise : 40', 'Incompatible input_threshold'),
                   ('library(test) {', 'library(test) { nom_voltage : 0.8;', 'Incompatible nom_voltage')]
        for old, new, pattern in changes:
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args, folder = self.directory_fixtures(root)
                (folder / 'nested/b.LIB').write_text(LIB.replace('cell(INV)', 'cell(INV2)').replace(old, new))
                with self.assertRaisesRegex(ModelError, pattern):
                    generate(args)

    def test_template_namespacing_and_ccs_omission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, folder = self.directory_fixtures(root)
            for index, (path, master) in enumerate([(folder / 'a.lib', 'INV'), (folder / 'nested/b.LIB', 'INV2')]):
                text = LIB.replace('cell(INV)', 'cell(%s)' % master)
                # Same template name, different default axes. The second
                # source relies on its own template rather than explicit axes.
                text = text.replace(' index_1("0, 1"); index_2("0, 1");\n }',
                                    ' index_1("0, %d"); index_2("0, 1");\n }' % (index + 1))
                text = text.replace('cell(%s)' % master,
                                    'normalized_driver_waveform(shared) { index_1("%d"); } cell(%s)' % (index, master))
                text = text.replace('related_pin : "A";', 'output_current_rise() { vector(dummy) { values("1,2"); } } related_pin : "A";')
                if index:
                    text = text.replace('{ index_1("0, 1"); index_2("0, 1"); values', '{ values')
                path.write_text(text)
            generate(args)
            reduced = parse_liberty(root / 'result/clusters_Late.lib')
            names = {g.args[0] for g in reduced.children if g.kind == 'lu_table_template'}
            self.assertTrue({'TC_src0_t', 'TC_src1_t'}.issubset(names))
            self.assertNotIn('normalized_driver_waveform', (root / 'result/clusters_Late.lib').read_text())
            pins = cell_models(reduced)['TC_7'][1]
            table = next(t for t in pins['O0'].children[0].children if t.kind == 'cell_rise')
            self.assertAlmostEqual(float(table.calls['values'][0].split(',')[0]), .2108, places=5)

    def test_source_specific_default_input_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, folder = self.directory_fixtures(root)
            for path, master, cap in [(folder / 'a.lib', 'INV', '.01'), (folder / 'nested/b.LIB', 'INV2', '.02')]:
                text = LIB.replace('cell(INV)', 'cell(%s)' % master).replace('capacitance : 0.01;', '')
                path.write_text(text.replace('library(test) {', 'library(test) { default_input_pin_cap : %s;' % cap))
            generate(args)
            pins = cell_models(parse_liberty(root / 'result/clusters_Late.lib'))['TC_7'][1]
            self.assertEqual(float(pins['I0'].attrs['capacitance']), .01)
            delay = next(t for t in pins['O0'].children[0].children if t.kind == 'cell_rise')
            self.assertAlmostEqual(float(delay.calls['values'][0].split(',')[0]), .2195, places=5)

    def test_boundary_library_does_not_change_cluster_corner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, folder = self.directory_fixtures(root)
            (folder / 'boundary.lib').write_text('library(block) { nom_voltage : 1.8; cell(BLOCK) { '
                                                'pin(A) { direction : input; } pin(Y) { direction : output; } } }')
            (root / 'design.v').write_text('module top(a,y); input a; output y; wire n,z; '
                                          'INV u1(.A(a),.Y(n)); INV2 u2(.A(n),.Y(z)); BLOCK u3(.A(z),.Y(y)); endmodule\n')
            report = generate(args)
            sources = report['library_sets']['early']
            self.assertIn('BLOCK', sources['cell_sources'])
            self.assertNotIn(str(folder / 'boundary.lib'), sources['active_internal_files'])

    def test_separate_directory_sets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.fixtures(root)
            for split in ('early', 'late'):
                (root / split).mkdir()
                (root / split / 'cells.lib').write_text(LIB if split == 'early' else LIB.replace('"0.1, 0.4", "0.3, 0.6"', '"0.2, 0.5", "0.4, 0.7"'))
            args.early_lib, args.late_lib = [root / 'early'], [root / 'late']
            report = generate(args)
            self.assertEqual(report['library_mode'], 'separate_early_late')
            values = []
            for split in ('Early', 'Late'):
                pins = cell_models(parse_liberty(root / ('result/clusters_%s.lib' % split)))['TC_7'][1]
                table = next(t for t in pins['O0'].children[0].children if t.kind == 'cell_rise')
                values.append(float(table.calls['values'][0].split(',')[0]))
            self.assertAlmostEqual(values[1] - values[0], .2, places=5)

    def test_index_comments_quoted_braces_and_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.lib'
            path.write_text('/* { */ library(x) { note : "a } \\" b"; cell("C") { pin(A) { direction : input; } } '
                            '// }\n cell(D) {} lu_table_template(t) {} }')
            indexed = index_liberty(path)
            self.assertEqual(set(indexed['cells']), {'C', 'D'})
            self.assertEqual([g.kind for g in parse_liberty_text(indexed['header']).children], ['lu_table_template'])
            for text in ['library(x) { cell(C) {', 'library(x) {} library(y) {}', 'library(x) { cell(C) {} cell(C) {} }']:
                path.write_text(text)
                with self.assertRaises(ModelError):
                    index_liberty(path)

    def test_missing_files_masters_and_invalid_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, folder = self.directory_fixtures(root)
            args.early_lib = root / 'original.lib'
            with self.assertRaisesRegex(ModelError, 'not both modes'):
                generate(args)
            args.early_lib = None
            (folder / 'nested/b.LIB').unlink()
            with self.assertRaisesRegex(ModelError, 'Missing library master: INV2'):
                generate(args)
            with self.assertRaisesRegex(ModelError, 'No .lib files'):
                discover_libraries([folder / 'nested'])
            with self.assertRaisesRegex(ModelError, 'does not exist'):
                discover_libraries([root / 'missing'])

    def test_two_inverters_units_capacitance_and_tables(self):
        with tempfile.TemporaryDirectory(prefix='tc_lib_test_') as directory:
            root = Path(directory)
            args = self.fixtures(root)
            report = generate(args)
            self.assertEqual(report['status'], 'complete')
            mapping = json.loads((root / 'result/cluster_mapping.jsonl').read_text())
            self.assertEqual(mapping['arcs'], [dict(input='I0', output='O0', sense='positive_unate')])
            for corner in ('Early', 'Late'):
                cells = cell_models(parse_liberty(root / ('result/clusters_%s.lib' % corner)))
                _, pins = cells['TC_7']
                self.assertAlmostEqual(float(pins['I0'].attrs['capacitance']), .01)
                timing = next(g for g in pins['O0'].children if g.kind == 'timing')
                for table in timing.children:
                    values = [[float(v) for v in row.split(',')] for row in table.calls['values']]
                    for s, slew_ps in enumerate(args.slews_ps):
                        for l, load_ff in enumerate(args.loads_ff):
                            slew, load = slew_ps / 1000, load_ff / 1000
                            expected = (.2132 + .28 * slew + .3 * load) if table.kind.startswith('cell_') else (.0704 + .16 * slew + .1 * load)
                            self.assertAlmostEqual(values[s][l], expected, places=5)
            for item in report['clusters'][0]['verification']:
                self.assertLess(item['midpoint_max_error_ps'], .01)

    def test_missing_mapping_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.fixtures(root)
            (root / 'cell_names.csv').write_text('cell_id,cell_name\n1,u1\n')
            with self.assertRaisesRegex(ModelError, 'Missing cell names'):
                read_membership(args.clusters, args.cell_names)

    def test_two_inputs_are_characterized_independently(self):
        with tempfile.TemporaryDirectory(prefix='tc_lib_multi_') as directory:
            root = Path(directory)
            args = self.fixtures(root, 'module top(a,b,y); input a,b; output y; wire n; INV u1(.A(a),.Y(n)); NAND u2(.A(n),.B(b),.Y(y)); endmodule\n')
            inv = LIB[LIB.index(' cell(INV)'):LIB.rfind('}')]
            nand = inv.replace('cell(INV)', 'cell(NAND)').replace('function : "!A"', 'function : "!(A & B)"')
            nand = nand.replace('  pin(Y)', '  pin(B) { direction : input; capacitance : 0.02; }\n  pin(Y)')
            timing = inv[inv.index('   timing()'):inv.rfind('  }')]
            slow_timing = timing.replace('related_pin : "A"', 'related_pin : "B"').replace('"0.1, 0.4", "0.3, 0.6"', '"1.1, 1.4", "1.3, 1.6"')
            nand = nand[:nand.rfind('  }')] + slow_timing + nand[nand.rfind('  }'):]
            (root / 'original.lib').write_text(LIB[:LIB.rfind('}')] + nand + '\n}\n')
            generate(args)
            models = cell_models(parse_liberty(root / 'result/clusters_Late.lib'))
            pins = models['TC_7'][1]
            self.assertEqual(float(pins['I1'].attrs['capacitance']), .02)
            arcs = {a.attrs['related_pin']: a for a in pins['O0'].children}
            self.assertEqual(arcs['I0'].attrs['timing_sense'], 'positive_unate')
            self.assertEqual(arcs['I1'].attrs['timing_sense'], 'negative_unate')
            a_delay = next(t for t in arcs['I0'].children if t.kind == 'cell_rise')
            b_delay = next(t for t in arcs['I1'].children if t.kind == 'cell_rise')
            self.assertAlmostEqual(float(a_delay.calls['values'][0].split(',')[0]), .2163, places=5)
            self.assertAlmostEqual(float(b_delay.calls['values'][0].split(',')[0]), 1.1023, places=5)

    def test_reject_sequential_and_coupled_outputs(self):
        for kind in ('sequential', 'coupled'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args = self.fixtures(root)
                if kind == 'sequential':
                    (root / 'original.lib').write_text(LIB.replace('cell(INV) {', 'cell(INV) { ff(q,qn) { next_state : "A"; clocked_on : "A"; }'))
                    pattern = 'Sequential cell'
                else:
                    (root / 'design.v').write_text('module top(a,n,y); input a; output n,y; INV u1(.A(a),.Y(n)); INV u2(.A(n),.Y(y)); endmodule\n')
                    pattern = 'Boundary output feeds another output'
                with self.assertRaisesRegex(ModelError, pattern):
                    generate(args)
                self.assertEqual(json.loads((root / 'result/manifest.json').read_text())['status'], 'failed_partial_do_not_use')


if __name__ == '__main__':
    unittest.main()
