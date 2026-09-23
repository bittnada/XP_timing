#!/usr/bin/env python3
"""Master process: publish two-DB binaries into shared memory and keep them alive.

Clients attach with shared_memory_role=client and shared_memory_dir pointing
here. They still read only per-run files such as read_posX/read_posY.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import signal
import time

from SharedSnapshot import collect_two_db_files, publish

LOG = logging.getLogger(__name__)


def publish_and_hold(timing_db, placement_db, mapping, output, name=None):
    files, sources = collect_two_db_files(timing_db, placement_db, mapping)
    snapshot = publish(files, output, name=name, sources=sources)
    (Path(output) / 'ready').write_text(snapshot.manifest['shm_name'] + '\n')

    def stop(signum, _frame):
        LOG.info('master received signal %s; unlinking shared memory', signum)
        snapshot.close(unlink=True)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    LOG.info('master holding %s; start clients with shared_memory_dir=%s',
             snapshot.manifest['shm_name'], output)
    while True:
        time.sleep(3600)


def publish_from_params(params):
    output = getattr(params, 'shared_memory_dir', '') or 'results/shared_memory'
    return publish_and_hold(params.timing_db_path, params.placement_db_path,
                            params.placement_timing_mapping_path, output)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--timing-db', required=True)
    p.add_argument('--placement-db', required=True)
    p.add_argument('--mapping', required=True)
    p.add_argument('--output', required=True, help='new directory for the tiny manifest only')
    p.add_argument('--name', help='POSIX shm name; default is derived from --output')
    p.add_argument('--config', help='optional two-DB JSON used only to fill missing paths')
    return p.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    args = parse_args(argv)
    timing, placement, mapping = args.timing_db, args.placement_db, args.mapping
    if args.config:
        cfg = json.loads(Path(args.config).read_text())
        timing = timing or cfg.get('timing_db_path')
        placement = placement or cfg.get('placement_db_path')
        mapping = mapping or cfg.get('placement_timing_mapping_path')
    publish_and_hold(timing, placement, mapping, args.output, name=args.name)


if __name__ == '__main__':
    main()
