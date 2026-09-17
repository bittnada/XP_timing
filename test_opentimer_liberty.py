"""Native LUT inheritance/bounds regression; optional OPENTIMER_LIBERTY_PROBE."""
import os
import json
from pathlib import Path
import resource
import shutil
import subprocess
import tempfile
import unittest


TEMPLATE = '''lu_table_template(t) {
 variable_1 : input_net_transition;
 variable_2 : total_output_net_capacitance;
 index_1("0,1"); index_2("10,20,30");
}'''


def library(body='values("1,2,3", "4,5,6");', template=TEMPLATE, name='t'):
    return '''library(test) {
 time_unit : "1ns"; capacitive_load_unit(1,pf);
 %s
 cell(BUF) {
  pin(A) { direction : input; capacitance : 0.01; }
  pin(Y) { direction : output;
   timing() { related_pin : "A"; timing_sense : positive_unate;
    cell_rise(%s) { %s }
   }
  }
 }
}''' % (template, name, body)


class OpenTimerLibertyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='ot_lut_tests_')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        supplied = os.environ.get('OPENTIMER_LIBERTY_PROBE')
        if supplied:
            cls.probe = Path(supplied).resolve()
            return
        source = Path(__file__).resolve().parent
        ot = source.parent / 'thirdparty/OpenTimer'
        archive = ot / 'lib/libOpenTimer.a'
        compiler = shutil.which('g++')
        if not compiler or not archive.is_file():
            raise unittest.SkipTest('Build OpenTimer or set OPENTIMER_LIBERTY_PROBE')
        cls.probe = cls.root / 'read_probe'
        subprocess.run([compiler, '-std=c++17', '-D_GLIBCXX_USE_CXX11_ABI=0',
                        '-pthread', '-I', str(ot),
                        str(source / 'examples/opentimer_liberty/read_probe.cpp'),
                        str(archive), '-o', str(cls.probe)], check=True, capture_output=True)

    def run_parser(self, text, error=None, mode='inspect'):
        path = self.root / 'input.lib'
        path.write_text(text)
        def no_core():
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        result = subprocess.run([str(self.probe), str(path), mode], text=True,
                                capture_output=True, timeout=20, preexec_fn=no_core)
        if error is not None:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(error, result.stderr)
            self.assertNotIn('AddressSanitizer:', result.stderr)
            return None
        self.assertEqual(result.returncode, 0, result.stderr)
        if mode == 'audit':
            return json.loads(result.stdout)
        return {row.split()[0]: list(map(float, row.split()[1:]))
                for row in result.stdout.splitlines()}

    def test_template_axes_and_interpolation(self):
        result = self.run_parser(library())
        self.assertEqual(result['axis1'], [0, 1])
        self.assertEqual(result['axis2'], [10, 20, 30])
        self.assertEqual(result['values'], [1, 2, 3, 4, 5, 6])
        self.assertEqual(result['sample'], [3])

    def test_explicit_override(self):
        result = self.run_parser(library('index_1("2,4,6"); index_2("8,9"); values("1,2","3,4","5,6");'))
        self.assertEqual(result['axis1'], [2, 4, 6])
        self.assertEqual(result['axis2'], [8, 9])

    def test_one_axis_override(self):
        result = self.run_parser(library('index_1("2"); values("7,8,9");'))
        self.assertEqual(result['axis1'], [2])
        self.assertEqual(result['axis2'], [10, 20, 30])

    def test_values_before_overrides(self):
        result = self.run_parser(library('values("1,2"); index_1("3"); index_2("4,5");'))
        self.assertEqual(result['axis1'], [3])
        self.assertEqual(result['axis2'], [4, 5])

    def test_one_dimensional_template(self):
        result = self.run_parser(library('values("2,4");',
            'lu_table_template(t) { variable_1 : input_net_transition; index_1("0,1"); }'))
        self.assertEqual(result['axis1'], [0, 1])
        self.assertEqual(result['axis2'], [0])
        self.assertEqual(result['sample'], [3])

    def test_scalar(self):
        result = self.run_parser(library('values("7");', '', 'scalar'))
        self.assertEqual(result['axis1'], [0])
        self.assertEqual(result['axis2'], [0])
        self.assertEqual(result['sample'], [7])

    def test_too_many_values(self):
        self.run_parser(library('values("1,2,3,4,5,6,7");'), 'expected 6, got 7')

    def test_too_few_values(self):
        self.run_parser(library('values("1,2");'), 'expected 6, got 2')

    def test_missing_values(self):
        self.run_parser(library(''), 'missing values')

    def test_duplicate_values(self):
        self.run_parser(library('values("1,2,3,4,5,6"); values("7");'), 'duplicate values')

    def test_duplicate_axis(self):
        self.run_parser(library('index_1("0,1"); index_1("2"); values("1");'), 'duplicate index_1')

    def test_missing_template(self):
        self.run_parser(library(template=''), 'unknown lut template')

    def test_empty_axis(self):
        self.run_parser(library('index_1(""); values("1");'), 'index_1')

    def test_missing_axis_in_template_and_table(self):
        self.run_parser(library('values("1");',
            'lu_table_template(t) { variable_1 : input_net_transition; }'), 'missing index_1')

    def test_bus_ranges_quoted_names_and_conditions(self):
        text = '''library(test) { cell(MEM) {
          pin(CK) { direction : input; }
          pin(EN) { direction : input; }
          bus(DO) { pin(DO[0:3]) { direction : output;
            timing() { related_pin : "CK"; when : "EN & !CK";
              sdf_cond : "MODE==0 && enabled"; cell_rise(scalar) { values("2"); } }
            timing() { related_pin : "CK"; when : "!EN & !CK";
              cell_rise(scalar) { values("3"); } }
          } }
          bus(DI) { pin("DI[3:0]") { direction : input; } }
          bus(WEM) { pin(WEM[0:3]) { direction : input; } }
          pin("A[7]") { direction : input; }
        } }'''
        cell = self.run_parser(text, mode='audit')['cells'][0]
        pins = {p['name']: p for p in cell['pins']}
        self.assertEqual(len(pins), 15)
        for i in range(4):
            self.assertEqual(pins['DO[%d]' % i]['direction'], 'output')
            self.assertEqual(pins['DI[%d]' % i]['direction'], 'input')
            timings = pins['DO[%d]' % i]['timings']
            self.assertEqual([t['when'] for t in timings], ['EN & !CK', '!EN & !CK'])
            self.assertEqual(timings[0]['sdf_cond'], 'MODE==0 && enabled')
            self.assertEqual(timings[1]['tables']['cell_rise']['values'], [3])
        self.assertIn('A[7]', pins)

    def test_related_pin_lists_and_ranges(self):
        text = '''library(test) { cell(MEM) {
          pin(A[1:0]) { direction : input; } pin(CK) { direction : input; }
          pin(Y) { direction : output; timing() {
            related_pin : "A[1:0] CK"; cell_rise(scalar) { values("1"); }
          } }
        } }'''
        pins = self.run_parser(text, mode='audit')['cells'][0]['pins']
        y = next(p for p in pins if p['name'] == 'Y')
        self.assertEqual([t['related_pin'] for t in y['timings']], ['A[1]', 'A[0]', 'CK'])

    def test_comment_markers_inside_conditions_are_preserved(self):
        text = library().replace('related_pin : "A";', 'related_pin : "A"; sdf_cond : "x/*a*/ && y//b #c";')
        pins = self.run_parser(text, mode='audit')['cells'][0]['pins']
        y = next(p for p in pins if p['name'] == 'Y')
        self.assertEqual(y['timings'][0]['sdf_cond'], 'x/*a*/ && y//b #c')

    def test_duplicate_expanded_pin_rejected(self):
        self.run_parser('library(t){cell(M){pin(A[1:0]){direction:input;} pin(A[0]){direction:input;}}}',
                        'duplicate Liberty pin', mode='audit')

    def test_type_only_bus_fails_instead_of_disappearing(self):
        self.run_parser('library(t){cell(M){bus(A){bus_type:t; direction:input;}}}',
                        'without explicit pin members', mode='audit')

    def test_unknown_related_pin_rejected(self):
        self.run_parser(library().replace('related_pin : "A";', 'related_pin : "MISSING";'),
                        'unknown related_pin', mode='audit')


if __name__ == '__main__':
    unittest.main()
