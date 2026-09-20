"""LEF pin USE defaults, pin-local state, PORT order and obstruction isolation."""
from pathlib import Path
import tempfile
import unittest

from ReadLEF import ReadLEFinfo


def port(layer='metal1'):
    return ' PORT\n  LAYER %s ;\n  RECT 0.1 0.1 0.2 0.2 ;\n END\n' % layer


def pin(name, use=None, ports=None, late=False):
    attr = ' USE %s ;\n' % use if use else ''
    return (' PIN %s\n DIRECTION INPUT ;\n' % name + ('' if late else attr)
            + (ports if ports is not None else port()) + (attr if late else '') + ' END %s\n' % name)


def macro(name, body):
    return 'MACRO %s\n CLASS CORE ;\n SIZE 1 BY 1 ;\n%sEND %s\n' % (name, body, name)


class LefUseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'cells.lef'

    def read(self, body):
        self.path.write_text('VERSION 5.8 ;\n' + body + 'END LIBRARY\n')
        self.reader = ReadLEFinfo([str(self.path)])
        self.reader.get_total_macro_info()

    def get_pin(self, master, name):
        r = self.reader
        cell = r.total_lef_info[r.lef_macro_name_to_id[master]]
        return cell.get_pin(r.lef_pin_name_to_id[name])

    def test_missing_use_is_stored_with_one_warning(self):
        with self.assertLogs(level='WARNING') as logs:
            self.read(macro('X', pin('A')))
        self.assertEqual(self.get_pin('X', 'A').get_use('metal1'), 'SIGNAL')
        self.assertEqual(len(logs.output), 1)
        self.assertIn("PIN 'X/A' has no USE; forcing USE to SIGNAL", logs.output[0])
        self.assertEqual(self.get_pin('X', 'A').get_layers()['metal1']['rectangles'].tolist(),
                         [[.1, .1, .2, .2]])

    def test_explicit_use_does_not_leak_to_next_pin(self):
        body = ''.join(pin('P%d' % i, use) for i, use in enumerate(
            ['POWER', None, 'CLOCK', None, 'GROUND', None, 'SIGNAL']))
        with self.assertLogs(level='WARNING') as logs:
            self.read(macro('X', body))
        self.assertEqual(len(logs.output), 3)
        self.assertEqual([self.get_pin('X', 'P%d' % i).get_use('metal1') for i in range(7)],
                         ['POWER', 'SIGNAL', 'CLOCK', 'SIGNAL', 'GROUND', 'SIGNAL', 'SIGNAL'])

    def test_use_does_not_leak_between_macros(self):
        with self.assertLogs(level='WARNING'):
            self.read(macro('X', pin('A', 'POWER')) + macro('Y', pin('A')))
        self.assertEqual(self.get_pin('Y', 'A').get_use('metal1'), 'SIGNAL')

    def test_multiple_ports_warn_once_and_set_every_layer(self):
        with self.assertLogs(level='WARNING') as logs:
            self.read(macro('X', pin('A', ports=port() + port('metal2'))))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.get_pin('X', 'A').use, {'metal1': 'SIGNAL', 'metal2': 'SIGNAL'})

    def test_use_after_port_is_not_misclassified_as_missing(self):
        with self.assertNoLogs(level='WARNING'):
            self.read(macro('X', pin('A', 'CLOCK', port() + port('metal2'), late=True)))
        self.assertEqual(self.get_pin('X', 'A').use, {'metal1': 'CLOCK', 'metal2': 'CLOCK'})

    def test_obstruction_is_not_power_or_signal(self):
        obs = ' OBS\n LAYER metal1 ;\n RECT 0.3 0.3 0.4 0.4 ;\n END\n'
        with self.assertLogs(level='WARNING'):
            self.read(macro('X', pin('VDD', 'POWER') + obs + pin('A')))
        self.assertEqual(self.get_pin('X', 'OBS').get_use('metal1'), 'OBS')
        self.assertEqual(self.get_pin('X', 'A').get_use('metal1'), 'SIGNAL')


if __name__ == '__main__':
    unittest.main()
