"""Bounded synthetic benchmark; all inputs/outputs live in a temporary directory.

Example: python3 benchmark_makedb.py --reader-dir ../bin/dreamplace --cells 20000 --ports 1000
Use --export to include the explicitly requested metadata/binary writes.
The checksum covers IDs, geometry, connectivity and weights, not execution time.
"""
import argparse
import contextlib
import hashlib
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reader-dir', default=str(Path(__file__).resolve().parent))
    parser.add_argument('--cells', type=int, default=20000)
    parser.add_argument('--ports', type=int, default=1000)
    parser.add_argument('--threads', type=int, default=1)
    parser.add_argument('--export', action='store_true')
    args = parser.parse_args()
    if not 2 <= args.ports <= args.cells or args.cells > 100000 or not 1 <= args.threads <= 32:
        parser.error('require 2 <= ports <= cells <= 100000 and 1 <= threads <= 32')
    sys.path.insert(0, str(Path(args.reader_dir).resolve()))
    import numpy as np
    from Params import Params
    import MakeDBAdapter as adapter

    with tempfile.TemporaryDirectory(prefix='makedb-benchmark-') as tmp:
        root = Path(tmp)
        lef = '''VERSION 5.8 ;
MACRO INV
 CLASS CORE ;
 SIZE 1 BY 1 ;
 SYMMETRY X Y ;
 PIN A
  DIRECTION INPUT ;
  USE SIGNAL ;
  PORT
   LAYER metal4 ;
   RECT 0.05 0.4 0.15 0.6 ;
  END
 END A
 PIN Y
  DIRECTION OUTPUT ;
  USE SIGNAL ;
  PORT
   LAYER metal4 ;
   RECT 0.85 0.4 0.95 0.6 ;
  END
 END Y
END INV
END LIBRARY
'''
        (root / 'cells.lef').write_text(lef)
        rows = max(2, (args.cells + 999) // 1000)
        lines = ['VERSION 5.8 ;', 'DESIGN bench ;', 'UNITS DISTANCE MICRONS 1000 ;',
                 'DIEAREA ( 0 0 ) ( 1000000 %d ) ;' % (rows * 1000)]
        lines += ['ROW r%d core 0 %d %s DO 1000 BY 1 STEP 1000 0 ;' %
                  (i, i * 1000, 'N' if i % 2 == 0 else 'FS') for i in range(rows)]
        lines += ['COMPONENTS %d ;' % args.cells]
        lines += ['- u%d INV + PLACED ( %d %d ) N ;' %
                  (i, i % 1000 * 1000, i // 1000 * 1000) for i in range(args.cells)]
        lines += ['END COMPONENTS', 'PINS %d ;' % args.ports]
        lines += ['- p%d + NET n%d + DIRECTION %s + USE SIGNAL + LAYER metal4 ( -50 -50 ) ( 50 50 ) + FIXED ( 0 %d ) N ;' %
                  (i, i, 'INPUT' if i == 0 else 'OUTPUT', i * 10) for i in range(args.ports)]
        lines += ['END PINS', 'NETS %d ;' % args.ports, '- n0 ( PIN p0 )']
        lines += [' ( u%d A )' % i for i in range(args.cells)]
        lines += [' + USE SIGNAL ;']
        lines += ['- n%d ( u%d Y ) ( PIN p%d ) + USE SIGNAL ;' % (i, i, i)
                  for i in range(1, args.ports)]
        lines += ['END NETS', 'END DESIGN']
        (root / 'design.def').write_text('\n'.join(lines) + '\n')
        p = Params()
        p.db_option = 'def'; p.mode = 'binary_write' if args.export else 'default'
        p.def_path = str(root / 'design.def'); p.lef_dir_path = [str(root / 'cells.lef')]
        p.save_path = str(root / 'saved'); p.def_parse_num_threads = args.threads
        capture = io.StringIO()
        logging.basicConfig(level=logging.INFO, stream=capture)
        with contextlib.redirect_stdout(capture):
            started = time.perf_counter()
            _, db = adapter.read(p)
            if args.export:
                adapter.save(db, p)
            seconds = time.perf_counter() - started
        checksum = hashlib.sha256()
        for field in adapter.FLOATS + adapter.INTS + adapter.STRINGS:
            checksum.update(field.encode())
            checksum.update(np.asarray(getattr(db, field)).tobytes())
        print(json.dumps(dict(reader_dir=args.reader_dir, cells=args.cells, ports=args.ports,
                              threads=args.threads, export=args.export, seconds=round(seconds, 4),
                              checksum=checksum.hexdigest(),
                              stages=[line for line in capture.getvalue().splitlines()
                                      if 'FINISHED' in line or line.startswith('INFO:')]), indent=2))


if __name__ == '__main__':
    main()
