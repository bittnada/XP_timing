"""Run native checks with DREAMPLACE_TIMING_CPP pointing to the built .so."""
import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from TimingCache import TimingCacheMixin, cache_location, library_files, timing_sources
from test_reduced_liberty import LIB


class DB(TimingCacheMixin):
    pass


def fixture(root):
    (root / "cells.lib").write_text(LIB)
    (root / "design.v").write_text(
        "module top(a,y); input a; output y; wire n; "
        "INV u1(.A(a),.Y(n)); INV u2(.A(n),.Y(y)); endmodule\n")
    (root / "design.sdc").write_text(
        "create_clock -name clk -period 1\n"
        "set_input_delay 0.02 -clock clk [get_ports a]\n"
        "set_input_transition 0.01 [get_ports a]\n"
        "set_output_delay 0.8 -clock clk [get_ports y]\n"
        "set_load 0.01 [get_ports y]\n")
    return SimpleNamespace(lib_input=str(root / "cells.lib"),
                           verilog_input=str(root / "design.v"),
                           sdc_input=str(root / "design.sdc"),
                           db_option="def", mode="binary_write", save_path=str(root / "db"))


class FakeBackend:
    timing_cache_schema = 1
    calls = 0

    def compile_timing_model(self, libs, verilog, sdc, output):
        self.calls += 1
        Path(output).write_bytes(b"parsed binary model")

    def load_timing_model(self, path):
        if Path(path).read_bytes() != b"parsed binary model":
            raise RuntimeError("bad model")
        return object()


class CacheTest(unittest.TestCase):
    def test_mode_matrix_and_no_legacy_override(self):
        for option in ("", "def", "binary", "binary_wo_pos", "master", "client"):
            for mode in ("", "read", "binary_write", "read/binary_write"):
                p = SimpleNamespace(db_option=option, mode=mode, save_path="/tmp/db",
                                    timing_cache_mode="rebuild", timing_cache_dir="/tmp/wrong")
                expected = "read" if option in ("binary", "binary_wo_pos") else (
                    "write" if option == "def" and "binary_write" in mode else "text")
                self.assertEqual(cache_location(p),
                    (Path("/tmp/db/timing_cache"), expected) if expected != "text" else (None, "text"))
        self.assertIsNone(DB().initialize_timing_db(SimpleNamespace(), FakeBackend()))
        for option in ("def", "binary", "binary_wo_pos"):
            with self.assertRaisesRegex(ValueError, "save_path"):
                cache_location(SimpleNamespace(db_option=option, mode="binary_write"))

    def test_export_is_explicit_and_repeated_export_reparses(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = fixture(Path(tmp)); backend = FakeBackend(); db = DB()
            p.mode = "read"
            self.assertIsNone(db.initialize_timing_db(p, backend))
            self.assertFalse(Path(p.save_path).exists())
            with self.assertRaises(ValueError):
                db.save_timing_cache(p, backend)
            p.mode = "binary_write"
            db.initialize_timing_db(p, backend)
            self.assertEqual(db.timing_cache_info["status"], "saved")
            self.assertEqual(backend.calls, 1)
            old = db.timing_cache_info["model"]
            db.initialize_timing_db(p, backend)
            self.assertEqual(backend.calls, 2)
            self.assertNotEqual(old, db.timing_cache_info["model"])
            self.assertTrue(Path(old).exists())

    def test_binary_modes_restore_without_sources_or_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = fixture(Path(tmp)); backend = FakeBackend(); db = DB()
            db.save_timing_cache(p, backend)
            for source in (p.lib_input, p.verilog_input, p.sdc_input): Path(source).unlink()
            def snapshot():
                return {str(f): (f.stat().st_mtime_ns, f.read_bytes())
                        for f in Path(p.save_path).rglob("*") if f.is_file()}
            before = snapshot()
            for option in ("binary", "binary_wo_pos"):
                p.db_option = option  # mode=binary_write must NOT enable writes.
                with patch("TimingCache.fingerprint", side_effect=AssertionError("source access")):
                    db.initialize_timing_db(p, backend)
                self.assertEqual(db.timing_cache_info["status"], "restored")
                self.assertEqual(snapshot(), before)
                self.assertEqual(backend.calls, 1)
                with self.assertRaises(ValueError): db.save_timing_cache(p, backend)

    def test_missing_corrupt_and_incompatible_db_never_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = fixture(Path(tmp)); backend = FakeBackend(); db = DB()
            for option in ("binary", "binary_wo_pos"):
                p.db_option = option
                with self.assertRaisesRegex(RuntimeError, "Export first"):
                    db.initialize_timing_db(p, backend)
                self.assertFalse(Path(p.save_path).exists())
            self.assertEqual(backend.calls, 0)
            p.db_option = "def"
            db.save_timing_cache(p, backend)
            model = Path(db.timing_cache_info["model"])
            model.write_bytes(b"damaged")
            for option in ("binary", "binary_wo_pos"):
                p.db_option = option
                with self.assertRaisesRegex(RuntimeError, "checksum"):
                    db.initialize_timing_db(p, backend)
            self.assertEqual(backend.calls, 1)
            p.db_option = "def"
            db.save_timing_cache(p, backend)
            manifest_path = Path(p.save_path) / "timing_cache/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["inputs"]["native_schema"] = 99
            manifest_path.write_text(json.dumps(manifest))
            p.db_option = "binary"
            with self.assertRaisesRegex(RuntimeError, "incompatible native"):
                db.initialize_timing_db(p, backend)
            self.assertEqual(backend.calls, 2)

    def test_preparation_exports_without_optimization_and_handoff_is_single_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = fixture(Path(tmp)); backend = FakeBackend(); db = DB()
            p.timing_opt_flag = 0
            db.prepare_timing_db(p, backend)
            self.assertEqual(backend.calls, 1)
            self.assertEqual(db.timing_cache_info["status"], "saved")
            p.timing_opt_flag = 1
            db.prepare_timing_db(p, backend)
            self.assertEqual(backend.calls, 2)
            db.initialize_timing_db(p, backend)
            self.assertEqual(backend.calls, 2)  # Timer receives prepared model.
            db.initialize_timing_db(p, backend)
            self.assertEqual(backend.calls, 3)  # Explicit later export is not a cache hit.

    def test_failed_build_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = fixture(Path(tmp)); backend = FakeBackend(); db = DB()
            db.save_timing_cache(p, backend)
            manifest = Path(p.save_path) / "timing_cache/manifest.json"
            before = manifest.read_bytes()
            def fail(*args):
                raise RuntimeError("build failure")
            backend.compile_timing_model = fail
            with self.assertRaises(RuntimeError):
                db.save_timing_cache(p, backend)
            self.assertEqual(before, manifest.read_bytes())

    def test_directories_corner_selection_and_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = fixture(root)
            (root / "b.lib").write_text("lib2")
            self.assertEqual(library_files([root, root / "b.lib"]),
                             [str(root / "b.lib"), str(root / "cells.lib")])
            p.early_lib_input = p.lib_input
            with self.assertRaises(ValueError):
                timing_sources(p)
            p.early_lib_input = ""
            dependency = root / "constraints.inc"; dependency.write_text("old")
            p.timing_cache_dependencies = [str(dependency)]
            backend = FakeBackend(); db = DB()
            db.save_timing_cache(p, backend)
            dependency.write_text("new")
            db.save_timing_cache(p, backend)
            self.assertEqual(backend.calls, 2)


@unittest.skipUnless(os.environ.get("DREAMPLACE_TIMING_CPP"), "set DREAMPLACE_TIMING_CPP")
class NativeCacheTest(unittest.TestCase):
    def test_bus_and_conditional_metadata_survive_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = fixture(root)
            library = Path(p.lib_input)
            library.write_text('''library(test) {
              time_unit : "1ns"; capacitive_load_unit(1,pf);
              cell(MEM) {
                pin(CK) { direction:input; capacitance:0.01; }
                pin(EN) { direction:input; capacitance:0.01; }
                bus(DO) { pin(DO[1:0]) { direction:output;
                  timing() { related_pin:"CK"; when:"EN & !CK";
                    sdf_cond:"MODE==0 && enabled"; timing_sense:positive_unate;
                    cell_rise(scalar){values("0.1");} cell_fall(scalar){values("0.1");}
                    rise_transition(scalar){values("0.05");} fall_transition(scalar){values("0.05");}
                  }
                  timing() { related_pin:"CK"; when:"!EN & !CK";
                    timing_sense:positive_unate;
                    cell_rise(scalar){values("0.2");} cell_fall(scalar){values("0.2");}
                    rise_transition(scalar){values("0.06");} fall_transition(scalar){values("0.06");}
                  }
                } }
              }
            }''')
            Path(p.verilog_input).write_text('module top(ck,en,q); input ck,en; output [1:0] q; '
                                           'MEM u(.CK(ck),.EN(en),.DO(q)); endmodule\n')
            model = root / 'conditional.bin'
            self.cpp.compile_timing_model([(str(library), -1)], p.verilog_input, '', str(model))
            timer = self.cpp.load_timing_model(str(model)); timer.update_timing()
            dump = root / 'loaded.lib'
            timer.dump_celllib_file(str(dump))
            text = dump.read_text()
            self.assertIn('pin ("DO[0]")', text)
            self.assertIn('pin ("DO[1]")', text)
            self.assertEqual(text.count('when : "EN & !CK"'), 2)
            self.assertEqual(text.count('when : "!EN & !CK"'), 2)
            self.assertEqual(text.count('sdf_cond : "MODE==0 && enabled"'), 2)
            self.assertGreater(timer.num_arcs(), 0)

    @classmethod
    def setUpClass(cls):
        import torch  # Load shared dependencies.
        spec = importlib.util.spec_from_file_location("timing_cpp", os.environ["DREAMPLACE_TIMING_CPP"])
        cls.cpp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cpp)

    def compare(self, a, b):
        a.update_timing(); b.update_timing()
        for name in ("num_gates", "num_pins", "num_nets", "num_arcs", "num_tests"):
            self.assertEqual(getattr(a, name)(), getattr(b, name)())
        for pin in ("a", "u1:A", "u1:Y", "u2:A", "u2:Y", "y"):
            for split in (False, True):
                for tran in (False, True):
                    for method in ("report_at", "report_slack", "report_slew"):
                        x = getattr(a, method)(pin, split, tran)
                        y = getattr(b, method)(pin, split, tran)
                        if math.isnan(x): self.assertTrue(math.isnan(y))
                        else: self.assertAlmostEqual(x, y, places=6)
        self.assertEqual(self.cpp.report_timing_paths(a, 4, True),
                         self.cpp.report_timing_paths(b, 4, True))

    def test_text_vs_binary_and_restore_without_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = fixture(root)
            text = self.cpp.io_forward(["test", "--lib_input", p.lib_input,
                                       "--verilog_input", p.verilog_input, "--sdc_input", p.sdc_input])
            text.update_timing()
            db = DB(); cached = db.save_timing_cache(p, self.cpp)
            self.compare(text, cached)
            p.db_option = "binary"
            again = db.load_timing_cache(p, self.cpp)
            self.assertEqual(db.timing_cache_info["status"], "restored")
            self.compare(text, again)
            # Prove native restore does not reopen any LIB/Verilog/SDC source.
            for source in (p.lib_input, p.verilog_input, p.sdc_input): Path(source).unlink()
            for option in ("binary", "binary_wo_pos"):
                p.db_option = option
                restored = db.initialize_timing_db(p, self.cpp)
                self.compare(text, restored)

    def test_separate_corners(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = fixture(root)
            p.early_lib_input = p.lib_input
            p.late_lib_input = str(root / "late.lib")
            Path(p.late_lib_input).write_text(LIB.replace('"0.1, 0.4"', '"0.3, 0.8"'))
            p.lib_input = ""
            text = self.cpp.io_forward(["test", "--early_lib_input", p.early_lib_input,
                "--late_lib_input", p.late_lib_input, "--verilog_input", p.verilog_input, "--sdc_input", p.sdc_input])
            self.compare(text, DB().save_timing_cache(p, self.cpp))

    def test_multiple_libraries_and_duplicate_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = fixture(root)
            reference = self.cpp.io_forward(["test", "--lib_input", p.lib_input,
                "--verilog_input", p.verilog_input, "--sdc_input", p.sdc_input])
            reference.update_timing()
            second = root / "second.lib"
            # Same template name, opposite axes and transposed tables: the
            # mathematical model is identical, but sharing template pointers
            # between these two libraries would give incorrect interpolation.
            second.write_text(LIB.replace("cell(INV)", "cell(INV2)")
                .replace("variable_1 : input_net_transition;", "variable_1 : total_output_net_capacitance;")
                .replace("variable_2 : total_output_net_capacitance;", "variable_2 : input_net_transition;")
                .replace('"0.1, 0.4", "0.3, 0.6"', '"0.1, 0.3", "0.4, 0.6"')
                .replace('"0.05, 0.15", "0.45, 0.55"', '"0.05, 0.45", "0.15, 0.55"'))
            p.lib_input = [p.lib_input, str(second)]
            Path(p.verilog_input).write_text(Path(p.verilog_input).read_text().replace("INV u2", "INV2 u2"))
            db = DB(); timer = db.save_timing_cache(p, self.cpp); timer.update_timing()
            self.assertEqual(timer.num_gates(), 2)
            self.assertTrue(math.isfinite(timer.report_slack("y", True, True)))
            self.compare(reference, timer)
            second.write_text(LIB)
            with self.assertRaisesRegex(RuntimeError, "Duplicate cell"):
                db.save_timing_cache(p, self.cpp)

    def test_truncated_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = fixture(root); db = DB()
            db.save_timing_cache(p, self.cpp)
            damaged = root / "truncated.bin"
            damaged.write_bytes(Path(db.timing_cache_info["model"]).read_bytes()[:80])
            with self.assertRaises(RuntimeError):
                self.cpp.load_timing_model(str(damaged))

    def test_sdc_does_not_overwrite_user_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = fixture(root)
            config = root / "design.json"; config.write_text('{"user": "configuration"}')
            DB().save_timing_cache(p, self.cpp)
            self.assertEqual(config.read_text(), '{"user": "configuration"}')

    def test_sequential_checks_and_clock_roundtrip(self):
        example = Path(__file__).resolve().parent.parent / "thirdparty/OpenTimer/example/simple"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("simple.v", "simple.sdc", "osu018_stdcells.lib"):
                (root / name).write_bytes((example / name).read_bytes())
            p = SimpleNamespace(lib_input=str(root / "osu018_stdcells.lib"),
                verilog_input=str(root / "simple.v"), sdc_input=str(root / "simple.sdc"),
                db_option="def", mode="binary_write", save_path=str(root / "db"))
            text = self.cpp.io_forward(["test", "--lib_input", p.lib_input,
                "--verilog_input", p.verilog_input, "--sdc_input", p.sdc_input])
            text.update_timing()
            cached = DB().save_timing_cache(p, self.cpp); cached.update_timing()
            self.assertGreater(text.num_tests(), 0)
            self.assertEqual(text.num_tests(), cached.num_tests())
            for split in (False, True):
                for pin in ("f1:D", "f1:CLK", "f1:Q", "out"):
                    for tran in (False, True):
                        for method in ("report_at", "report_slack", "report_slew"):
                            x = getattr(text, method)(pin, split, tran)
                            y = getattr(cached, method)(pin, split, tran)
                            if math.isnan(x): self.assertTrue(math.isnan(y))
                            else: self.assertAlmostEqual(x, y, places=6)

    def test_superposed_rc_is_zero_and_keeps_pin_loads(self):
        import numpy as np
        import torch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = fixture(root)
            Path(p.lib_input).write_text(LIB.replace('time_unit : "1ns";',
                'time_unit : "1ns"; pulling_resistance_unit : "1kohm";'))
            Path(p.verilog_input).write_text('module top(a,y,z); input a; output y,z; wire n; '
                'INV u1(.A(a),.Y(n)); INV u2(.A(n),.Y(y)); INV u3(.A(n),.Y(z)); endmodule')
            timer = self.cpp.io_forward(['test', '--lib_input', p.lib_input,
                '--verilog_input', p.verilog_input, '--sdc_input', p.sdc_input])
            timer.update_timing()
            pins = ['a', 'u1:A', 'u2:A', 'u3:A', 'u1:Y', 'u2:Y', 'y', 'u3:Y', 'z']
            nets = ['a', 'n', 'y', 'z']
            owners = torch.tensor([0, 1, 2, 3, 1, 2, 4, 3, 5], dtype=torch.int32)
            flat = torch.arange(9, dtype=torch.int32)
            starts = torch.tensor([0, 2, 5, 7, 9], dtype=torch.int32)
            offsets = torch.zeros(9, dtype=torch.float64)
            def run(x, y, resistance=100.):
                previous = os.getcwd()
                try:
                    os.chdir(Path(__file__).resolve().parent.parent)
                    self.cpp.forward(timer, torch.tensor(x+y, dtype=torch.float64), nets, pins,
                        flat, starts, owners, offsets, offsets, resistance, 1e-15, 1., 1, 1, 1000)
                finally:
                    os.chdir(previous)
                timer.update_timing()
                return np.array([timer.report_at(pin, True, True) for pin in pins])
            baseline = run([0.]*6, [0.]*6)
            translated = run([123.]*6, [456.]*6)
            np.testing.assert_allclose(translated, baseline, atol=1e-7)
            # n driver is LAST in DEF order; sink and driver ATs must match.
            self.assertAlmostEqual(translated[2], translated[4], places=7)
            self.assertAlmostEqual(translated[3], translated[4], places=7)
            self.assertGreater(translated[4] - translated[1], .1)  # Cell delay/load retained.
            dump = root / 'rc.txt'; timer.dump_rctree_file(str(dump))
            for pin in pins: self.assertIn(pin, dump.read_text())
            # Mixed distinct/duplicate coordinates, then all co-located again.
            mixed = run([0, 10, 30, 30, 50, 60], [0]*6)
            mixed_shift = run([100, 110, 130, 130, 150, 160], [200]*6)
            np.testing.assert_allclose(mixed_shift, mixed, atol=1e-7)
            self.assertAlmostEqual(mixed[2], mixed[3], places=7)
            np.testing.assert_allclose(run([123.]*6, [456.]*6), baseline, atol=1e-7)

    def test_rc_and_net_weight_updates_after_restore(self):
        import torch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = fixture(root)
            Path(p.lib_input).write_text(LIB.replace('time_unit : "1ns";',
                'time_unit : "1ns"; pulling_resistance_unit : "1kohm";'))
            text = self.cpp.io_forward(["test", "--lib_input", p.lib_input,
                "--verilog_input", p.verilog_input, "--sdc_input", p.sdc_input])
            text.update_timing()
            db = DB(); db.save_timing_cache(p, self.cpp)
            p.db_option = "binary"
            cached = db.load_timing_cache(p, self.cpp); cached.update_timing()
            pins = ["a", "u1:A", "u1:Y", "u2:A", "u2:Y", "y"]
            nets = ["a", "n", "y"]
            flat = torch.arange(6, dtype=torch.int32)
            starts = torch.tensor([0, 2, 4, 6], dtype=torch.int32)
            owners = torch.tensor([0, 1, 1, 2, 2, 3], dtype=torch.int32)
            offsets = torch.zeros(6, dtype=torch.float64)
            def rc(timer, distance):
                pos = torch.tensor([0., distance, 2 * distance, 3 * distance, 0, 0, 0, 0], dtype=torch.float64)
                previous = os.getcwd()
                try:
                    os.chdir(Path(__file__).resolve().parent.parent)
                    self.cpp.forward(timer, pos, nets, pins, flat, starts, owners,
                                     offsets, offsets, 100., 1e-15, 1., 1, 1, 1000)
                finally:
                    os.chdir(previous)
                timer.update_timing()
            rc(text, 10); rc(cached, 10); self.compare(text, cached)
            before = cached.report_slack("y", True, True)
            rc(cached, 100)
            self.assertNotAlmostEqual(before, cached.report_slack("y", True, True), places=6)
            rc(text, 100); self.compare(text, cached)
            # MakeDB's DEF-order pins need not have the driver first.
            flat = torch.tensor([1, 0, 3, 2, 5, 4], dtype=torch.int32)
            rc(cached, 100); self.compare(text, cached)
            def weights(timer):
                w = torch.ones(3, dtype=torch.float64)
                arrays = [torch.zeros(3, dtype=torch.float64) for _ in range(3)]
                self.cpp.update_net_weights(timer, 2, dict(zip(nets, range(3))),
                    arrays[0], arrays[1], w, arrays[2], starts[1:] - starts[:-1],
                    1, .5, 10., 1000)
                return w
            a, b = weights(text), weights(cached)
            torch.testing.assert_close(a, b)
            self.assertTrue(bool((b > 1).any()))


@unittest.skipUnless(os.environ.get("DREAMPLACE_INSTALL"), "set DREAMPLACE_INSTALL")
class PlacerCacheTest(unittest.TestCase):
    def test_explicit_export_and_both_restore_modes_in_placer_and_cli(self):
        install = Path(os.environ["DREAMPLACE_INSTALL"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = fixture(root)
            (root / "tiny.lef").write_text('''VERSION 5.8 ;
BUSBITCHARS "[]" ;
DIVIDERCHAR "/" ;
UNITS
 DATABASE MICRONS 1000 ;
END UNITS
LAYER metal1
 TYPE ROUTING ;
 DIRECTION HORIZONTAL ;
 PITCH 0.1 ;
 WIDTH 0.05 ;
END metal1
SITE core
 CLASS CORE ;
 SIZE 1 BY 1 ;
 SYMMETRY Y ;
END core
MACRO INV
 CLASS CORE ;
 ORIGIN 0 0 ;
 SIZE 1 BY 1 ;
 SYMMETRY X Y ;
 SITE core ;
 PIN A
  DIRECTION INPUT ;
  USE SIGNAL ;
  PORT
   LAYER metal1 ;
   RECT 0.05 0.4 0.15 0.6 ;
  END
 END A
 PIN Y
  DIRECTION OUTPUT ;
  USE SIGNAL ;
  PORT
   LAYER metal1 ;
   RECT 0.85 0.4 0.95 0.6 ;
  END
 END Y
END INV
END LIBRARY
''')
            (root / "tiny.def").write_text('''VERSION 5.8 ;
DIVIDERCHAR "/" ;
BUSBITCHARS "[]" ;
DESIGN top ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 10000 2000 ) ;
ROW row0 core 0 0 N DO 10 BY 1 STEP 1000 0 ;
ROW row1 core 0 1000 FS DO 10 BY 1 STEP 1000 0 ;
COMPONENTS 2 ;
- u1 INV + PLACED ( 2000 0 ) N ;
- u2 INV + PLACED ( 4000 0 ) N ;
END COMPONENTS
PINS 2 ;
- a + NET a + DIRECTION INPUT + USE SIGNAL
 + LAYER metal1 ( -50 -50 ) ( 50 50 ) + FIXED ( 0 500 ) N ;
- y + NET y + DIRECTION OUTPUT + USE SIGNAL
 + LAYER metal1 ( -50 -50 ) ( 50 50 ) + FIXED ( 9000 500 ) N ;
END PINS
NETS 3 ;
- a ( PIN a ) ( u1 A ) ;
- n ( u1 Y ) ( u2 A ) ;
- y ( u2 Y ) ( PIN y ) ;
END NETS
END DESIGN
''')
            config = vars(p).copy()
            config.update(lef_input=str(root / "tiny.lef"), def_input=str(root / "tiny.def"), gpu=0, num_threads=1,
                num_bins_x=8, num_bins_y=8, global_place_flag=1, legalize_flag=1,
                detailed_place_flag=0, timing_opt_flag=0, detailed_place_engine="", plot_flag=0,
                enable_fillers=0, random_center_init_flag=0, gp_noise_ratio=0,
                target_density=1., result_dir=str(root / "results"),
                global_place_stages=[dict(num_bins_x=8, num_bins_y=8, iteration=3,
                    learning_rate=.01, wirelength="weighted_average", optimizer="nesterov")])
            cfg = root / "config.json"; cfg.write_text(json.dumps(config))
            env = dict(os.environ, PYTHONPATH=str(install), OMP_NUM_THREADS="1", MPLCONFIGDIR=str(root / "mpl"))
            command = [sys.executable, str(install / "dreamplace/Placer.py"), str(cfg)]
            for option, expected in (("def", "saved"), ("binary", "restored"), ("binary_wo_pos", "restored")):
                config["db_option"] = option
                if option != "def":
                    config["timing_opt_flag"] = 1
                    # Both physical and timing restoration are source-free.
                    for key in ("lef_input", "def_input", "lib_input", "verilog_input", "sdc_input"):
                        if config[key]:
                            Path(config[key]).unlink()
                            config[key] = ""
                cfg.write_text(json.dumps(config))
                result = subprocess.run(command, cwd=install, env=env, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout[-10000:])
                self.assertEqual(result.stdout.count("timing DB " + expected + ":"), 1, result.stdout)
            result = subprocess.run([sys.executable, str(install / "dreamplace/TimingCache.py"), str(cfg)],
                cwd=install, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('"status": "restored"', result.stdout)


if __name__ == "__main__":
    unittest.main()
