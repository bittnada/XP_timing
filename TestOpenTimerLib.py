#!/usr/bin/env python3
"""Read one Liberty file with DREAMPlace's OpenTimer Python binding."""
import argparse
from pathlib import Path
import sys
import time


def read_lib(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError('LIB file not found: %s' % path)
    if path.stat().st_size == 0:
        raise ValueError('LIB file is empty: %s' % path)

    # Allow execution from either the source tree or installed bin/dreamplace.
    root = Path(__file__).resolve().parent.parent
    if (root / 'bin/dreamplace/ops/timing').is_dir():
        root = root / 'bin'
    sys.path.insert(0, str(root))
    import torch  # Load shared libraries required by timing_cpp.
    from dreamplace.ops.timing import timing_cpp as opentimer

    print('Reading LIB with OpenTimer: %s' % path, flush=True)
    begin = time.perf_counter()
    timer = opentimer.io_forward(['lib_test', '--lib_input', str(path)])
    # read_celllib schedules parsing; this call actually executes it.
    # No Verilog, SDC, placement, RC construction or net-weight update is used.
    timer.update_timing()
    timer.dump_timer()  # Library pin counts are separate from netlist counts.
    print('SUCCESS: OpenTimer parsed LIB (%.3f s); this is not mode-specific STA validation.' %
          (time.perf_counter() - begin), flush=True)
    return timer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='one .lib file to test')
    args = parser.parse_args()
    try:
        read_lib(args.input)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, 'ERROR: %s\n' % exc)


if __name__ == '__main__':
    main()
