"""Structural reduction and complete reduced-netlist OpenTimer integration."""
import json
from pathlib import Path
import tempfile
import unittest

import test_reduced_liberty as fixtures
from ReducedLiberty import ModelError, generate, shell_values
from ReducedVerilog import reduce_verilog


def entry(cid=7, members=('u1', 'u2'), inputs=('a',), outputs=('y',)):
    return dict(cluster_id=cid, liberty_cell='TC_%d' % cid,
                members=[dict(cell_id=cid * 100 + i, cell_name=n) for i, n in enumerate(members)],
                inputs=[dict(pin='I%d' % i, net=n) for i, n in enumerate(inputs)],
                outputs=[dict(pin='O%d' % i, net=n) for i, n in enumerate(outputs)])


class ReducedVerilogTest(unittest.TestCase):
    def files(self, root, design, entries=None):
        original, mapping, reduced = root / 'original.v', root / 'cluster_mapping.jsonl', root / 'reduced.v'
        original.write_text(design)
        mapping.write_text(''.join(json.dumps(e) + '\n' for e in (entries or [entry()])))
        return original, mapping, reduced

    def test_remove_internal_wire_keep_ff_macro_fanout_and_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.files(root,
                'module top(a,clk,y,z); input a,clk; output y,z; wire n,b; '
                'DFF ff0(.D(b),.CK(clk),.Q(y)); MACRO __tc_cluster_7(.A(b),.Y(z)); '
                'INV u1(.A(a),.Y(n)); INV u2(.A(n),.Y(b)); endmodule',
                [entry(outputs=('b',))])
            report = reduce_verilog(*paths)
            text = paths[2].read_text()
            self.assertIn('DFF ff0(.D(b),.CK(clk),.Q(y));', text)
            self.assertIn('MACRO __tc_cluster_7(.A(b),.Y(z));', text)
            self.assertIn('TC_7 __tc_cluster_7_1 (.I0(a), .O0(b));', text)
            self.assertIn('wire b;', text)
            self.assertNotIn('INV u', text)
            self.assertEqual(report['original_instances'], 4)
            self.assertEqual(report['reduced_instances'], 3)
            self.assertEqual(report['removed_scalar_wire_declarations'], 1)
            self.assertEqual(report['retained_master_counts'], {'DFF': 1, 'MACRO': 1})

    def test_shared_pi_and_intercluster_net(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.files(root,
                'module top(a,y,z); input a; output y,z; wire n; '
                'INV u1(.A(a),.Y(n)); NAND u2(.A(n),.B(a),.Y(y)); INV u3(.A(a),.Y(z)); endmodule',
                [entry(1, ('u1',), ('a',), ('n',)), entry(2, ('u2',), ('n','a'), ('y',))])
            report = reduce_verilog(*paths)
            text = paths[2].read_text()
            self.assertIn('TC_1 __tc_cluster_1 (.I0(a), .O0(n));', text)
            self.assertIn('TC_2 __tc_cluster_2 (.I0(n), .I1(a), .O0(y));', text)
            self.assertIn('INV u3', text)
            self.assertEqual(report['removed_instances'], 2)

    def test_bus_ports_and_escaped_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.files(root,
                'module top(a,y); input [1:0] a; output y; wire \\inside/net ; '
                'INV \\u/1 (.A(a[0]),.Y(\\inside/net )); INV u2(.A(\\inside/net ),.Y(y)); endmodule',
                [entry(members=('\\u/1','u2'), inputs=('a[0]',))])
            reduce_verilog(*paths)
            text = paths[2].read_text()
            self.assertIn('input [1:0] a;', text)
            self.assertIn('.I0(a[0])', text)
            self.assertNotIn('inside/net', text)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.files(root,
                'module top(\\a/in ,y); input \\a/in ; output y; wire n; '
                'INV u1(.A(\\a/in ),.Y(n)); INV u2(.A(n),.Y(y)); endmodule',
                [entry(inputs=('\\a/in',))])
            reduce_verilog(*paths)
            self.assertIn('.I0(\\a/in )', paths[2].read_text())

    def test_missing_boundary_member_and_collision_rejected(self):
        cases = [
            ('module top(a,y,z); input a; output y,z; wire n; INV u1(.A(a),.Y(n)); '
             'INV u2(.A(n),.Y(y)); INV keep(.A(n),.Y(z)); endmodule', entry(), 'omits external net'),
            ('module top(a,y); input a; output y; INV u1(.A(a),.Y(y)); endmodule', entry(), 'not found'),
            ('module top(a,y); input a; output y; wire n; TC_7 u1(.A(a),.Y(n)); '
             'INV u2(.A(n),.Y(y)); endmodule', entry(), 'collides'),
            ('module top(a,y); input a; output y; wire n; INV u1(.A(a),.Y(n)); '
             'INV u2(.A(n),.Y(y)); endmodule', entry(inputs=('wrong',)), 'not connected'),
        ]
        for design, mapping, pattern in cases:
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as directory:
                paths = self.files(Path(directory), design, [mapping])
                with self.assertRaisesRegex(ModelError, pattern):
                    reduce_verilog(*paths)
                self.assertFalse(paths[2].exists())

    def test_cycle_and_incomplete_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.files(root, 'module top(); endmodule',
                               [entry(1, ('u1',), ('b',), ('a',)), entry(2, ('u2',), ('a',), ('b',))])
            with self.assertRaisesRegex(ModelError, 'directed cycle'):
                reduce_verilog(*paths)
            (root / 'manifest.json').write_text('{"status":"failed_partial_do_not_use"}')
            with self.assertRaisesRegex(ModelError, 'incomplete Liberty'):
                reduce_verilog(*paths)

    def test_member_master_change_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapping = entry()
            mapping['members'][0]['original_master'] = 'BUFFER'
            paths = self.files(root, 'module top(a,y); input a; output y; wire n; '
                               'INV u1(.A(a),.Y(n)); INV u2(.A(n),.Y(y)); endmodule', [mapping])
            with self.assertRaisesRegex(ModelError, 'master changed'):
                reduce_verilog(*paths)
            self.assertFalse(paths[2].exists())

    def test_do_not_overwrite_and_reject_unsupported_syntax(self):
        for design in ['module top(input a, output y); INV u1(.A(a),.Y(y)); endmodule',
                       'module top(a,y); input a; output y; assign y=a; endmodule',
                       'module top(a,y); input a; output y; INV u1(.A(a),.Y(y));']:
            with tempfile.TemporaryDirectory() as directory:
                paths = self.files(Path(directory), design)
                with self.assertRaises(ModelError):
                    reduce_verilog(*paths)
        with tempfile.TemporaryDirectory() as directory:
            paths = self.files(Path(directory), 'module top(); endmodule')
            paths[2].write_text('user data')
            with self.assertRaisesRegex(ModelError, 'already exists'):
                reduce_verilog(*paths)
            self.assertEqual(paths[2].read_text(), 'user data')

    def test_generated_full_netlist_arrival_and_slack_in_opentimer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = fixtures.ReducedLibertyTest().fixtures(root,
                'module top(a,y); input a; output y; wire n1,n2,n3,n4; '
                'INV u1(.A(a),.Y(n1)); INV u2(.A(n1),.Y(n2)); INV u3(.A(n2),.Y(n3)); '
                'INV u4(.A(n3),.Y(n4)); INV retained(.A(n4),.Y(y)); endmodule')
            (root / 'clusters.txt').write_text('cell_id cluster_id\n1 7\n2 7\n3 8\n4 8\n')
            (root / 'cell_names.csv').write_text('cell_id,cell_name\n1,u1\n2,u2\n3,u3\n4,u4\n')
            report = generate(args)
            self.assertEqual(report['reduced_verilog']['original_instances'], 5)
            self.assertEqual(report['reduced_verilog']['reduced_instances'], 3)
            self.assertIn('INV retained', (root / 'result/reduced.v').read_text())
            outputs = []
            for reduced in (False, True):
                commands = ['set_num_threads 1', 'read_celllib original.lib']
                if reduced:
                    commands += ['read_celllib -early result/clusters_Early.lib',
                                 'read_celllib -late result/clusters_Late.lib']
                commands += ['read_verilog ' + ('result/reduced.v' if reduced else 'design.v')]
                for split in ('early', 'late'):
                    for rf in ('rise', 'fall'):
                        commands += ['set_at -pin a -%s -%s 0' % (split, rf),
                                     'set_slew -pin a -%s -%s .01' % (split, rf),
                                     'set_load -pin y -%s -%s .01' % (split, rf),
                                     'set_rat -pin y -%s -%s 2' % (split, rf)]
                commands += ['update_timing']
                for split in ('early', 'late'):
                    for metric in ('at', 'slack'):
                        for rf in ('rise', 'fall'):
                            commands += ['report_%s -pin y -%s -%s' % (metric, split, rf)]
                values = shell_values(args.ot_shell, commands, root, 30)
                self.assertEqual(len(values), 8)
                outputs.append(values)
            for original, reduced in zip(*outputs):
                self.assertAlmostEqual(original, reduced, places=4)


if __name__ == '__main__':
    unittest.main()
