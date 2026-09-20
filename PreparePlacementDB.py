#!/usr/bin/env python3
"""Explicit DEF + binary_write export of a physical DB, without placement/STA.

The generated complete placement LEF must be supplied. No missing binary cache
is rebuilt implicitly; this command is the explicit prepare step.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time

from Params import Params
import MakeDBAdapter
from ReducedDEF import discover_lefs


def prepare(args):
    requested_output = Path(args.output).expanduser()
    if os.path.lexists(requested_output):
        raise FileExistsError('Output already exists; choose a new save_path: ' + str(requested_output))
    output = requested_output.resolve()
    original = Path(args.def_input).resolve()
    if not original.is_file():
        raise FileNotFoundError('DEF does not exist: ' + str(original))
    lefs = list(dict.fromkeys(path for item in args.lef_input for path in discover_lefs(item)))
    # The reference ReadLEF merges dicts. Refuse duplicate master declarations
    # before that could silently select a master from a previous cluster run.
    masters = {}
    for path in lefs:
        with path.open() as stream:
            for line in stream:
                match = re.fullmatch(r'\s*MACRO\s+(\S+)\s*(?:#[^\n]*)?\s*', line)
                if match:
                    name = match[1]
                    if name in masters:
                        raise ValueError('Duplicate LEF master %s in %s and %s' % (name, masters[name], path))
                    masters[name] = str(path)
    if args.threads < 1:
        raise ValueError('--threads must be positive')
    p = Params()
    if args.config:
        p.load(args.config)
    # This entry point uses adapter.read/save only. It never constructs PlaceDB,
    # BasicPlace, a GPU tensor, or a native OpenTimer object.
    for key in list(vars(p)):
        if key.startswith(('read_', 'write_', 'wrtie_', 'wriate_')):
            delattr(p, key)
    for key in ('lib_input', 'early_lib_input', 'late_lib_input', 'verilog_input',
                'sdc_input', 'aux_input', 'def_template_input', 'lef_input'):
        setattr(p, key, '')
    for key in ('gpu', 'timing_opt_flag', 'timing_clustering_flag', 'global_place_flag',
                'legalize_flag', 'detailed_place_flag', 'enable_fillers', 'plot_flag'):
        setattr(p, key, 0)
    p.db_option = 'def'; p.mode = 'binary_write'
    p.def_path = p.def_input = str(original)
    p.lef_dir_path = [str(path) for path in lefs]
    p.def_parse_num_threads = p.num_threads = args.threads
    started = time.monotonic()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep incomplete exports in an isolated staging directory. Existing saved
    # data is never touched; only completed exports are published to output.
    with tempfile.TemporaryDirectory(prefix='.prepare-placement-', dir=output.parent) as scratch:
        stage = Path(scratch) / 'save'
        p.save_path = str(stage)
        logging.info('Preparing physical DB only: DEF + binary_write (GPU/STA disabled)')
        _, db = MakeDBAdapter.read(p)
        MakeDBAdapter.save(db, p)
        report = dict(status='complete', db_option='def', mode='binary_write',
                      def_input=str(original), lef_inputs=p.lef_dir_path,
                      save_path=str(output), nodes=len(db.node_names), pins=len(db.pin_names),
                      nets=len(db.net_names), seconds=time.monotonic() - started,
                      placement_executed=False, timing_executed=False)
        (stage / 'prepare_summary.json').write_text(json.dumps(report, indent=2) + '\n')
        p.save_path = str(output); p.db_option = 'binary'; p.mode = 'default'
        p.dump(str(stage / 'load_binary.json'))
        output.mkdir()  # exclusive reservation: do not replace a concurrent user's directory
        for path in stage.iterdir():
            if path.name != 'physical_db':
                os.replace(path, output / path.name)
        # physical_db/manifest.json becomes visible only with all its arrays.
        os.replace(stage / 'physical_db', output / 'physical_db')
    return report


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--def-input', required=True)
    p.add_argument('--lef-input', required=True, nargs='+', help='complete generated placement LEF; directories scanned recursively')
    p.add_argument('--output', required=True, help='new save_path; existing directories are never overwritten')
    p.add_argument('--config', help='optional base Params JSON for parser/filter settings; paths, read/write overrides, GPU and timing options are overridden')
    p.add_argument('--threads', type=int, default=8)
    return p.parse_args(argv)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    try:
        print(json.dumps(prepare(parse_args()), indent=2))
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit('ERROR: ' + str(exc))
