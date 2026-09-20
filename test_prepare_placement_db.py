import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import MakeDBAdapter
from Params import Params
from PreparePlacementDB import prepare, parse_args
from PlacementTimingMapping import snapshot
from test_makedb_adapter import fixture


class PrepareDBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = fixture(self.root)
        self.argv = ['--def-input', p.def_path, '--lef-input', str(self.root / 'tiny.lef'),
                     '--output', str(self.root / 'prepared'), '--threads', '1']
        self.args = parse_args(self.argv)

    def run_prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return prepare(self.args)

    def test_prepare_restore_and_config_without_sta_or_gpu(self):
        config = self.root / 'base.json'
        config.write_text(json.dumps(dict(gpu=1, timing_opt_flag=1,
            timing_clustering_flag=1, verilog_input='/missing.v', sdc_input='/missing.sdc',
            lib_input='/missing.lib', db_option='binary', mode='read',
            read_node_names='/missing.names', read_posX='/missing.x', write_posY='/do-not-write')))
        self.args.config = str(config)
        before = {p: p.read_bytes() for p in self.root.glob('tiny.*')}
        result = self.run_prepare()
        out = Path(self.args.output)
        self.assertEqual(result['nodes'], 4)
        self.assertEqual(result['pins'], 6)
        self.assertFalse(result['placement_executed'])
        self.assertFalse(result['timing_executed'])
        self.assertFalse((out / 'timing_cache').exists())
        db = snapshot(out)
        self.assertEqual(list(db.node_names), ['u1', 'u2', 'a', 'y'])
        p = Params(); p.load(out / 'load_binary.json')
        self.assertEqual(p.save_path, str(out.resolve()))
        self.assertEqual(p.db_option, 'binary')
        self.assertEqual(p.mode, 'default')
        self.assertEqual((p.gpu, p.timing_opt_flag, p.lib_input), (0, 0, ''))
        self.assertNotIn('read_node_names', vars(p))
        with contextlib.redirect_stdout(io.StringIO()):
            _, restored = MakeDBAdapter.read(p)
        self.assertEqual(list(restored.pin_names), list(db.pin_names))
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_existing_partial_or_complete_output_preserved(self):
        out = Path(self.args.output); out.mkdir()
        marker = out / 'user-file'; marker.write_text('do not overwrite')
        with self.assertRaises(FileExistsError):
            self.run_prepare()
        self.assertEqual(marker.read_text(), 'do not overwrite')

    def test_missing_file_and_duplicate_master_fail_before_output(self):
        self.args.def_input = str(self.root / 'missing.def')
        with self.assertRaises(FileNotFoundError):
            self.run_prepare()
        self.args.def_input = str(self.root / 'tiny.def')
        other = self.root / 'copy.lef'; other.write_text((self.root / 'tiny.lef').read_text())
        self.args.lef_input.append(str(other))
        with self.assertRaisesRegex(ValueError, 'Duplicate LEF master'):
            self.run_prepare()
        self.assertFalse(Path(self.args.output).exists())

    def test_cli(self):
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('PreparePlacementDB.py')),
                                 *self.argv], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((Path(self.args.output) / 'physical_db/manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
