#!/usr/bin/env python3
"""Generate reproducible original-DB initial-position checkpoints.

Uniform mode spreads each movable cell's lower-left coordinate over the die,
respecting its width and height at the upper boundary.  The explicit original
checkpoint is then projected to the reduced DB; A and C never rely on separate
RNG streams with incompatible node-array lengths.
"""
import argparse
import json
from pathlib import Path

import numpy as np

from CompareTimingPlacement import physical
from PlacementState import id_digest


def write_table(path, field, values, digest):
    with path.open('x') as stream:
        stream.write('# cell_names_sha256=' + digest + '\n')
        stream.write('cell_id\t%s\n' % field)
        for node, value in enumerate(values):
            stream.write('%d\t%.17g\n' % (node, float(value)))


def generate(db_path, seeds, output, distribution='uniform'):
    db = physical(db_path)
    movable = db.meta['num_physical_nodes'] - db.meta['num_terminals'] - db.meta['num_terminal_NIs']
    db.num_physical_nodes = len(db.node_names); db.num_movable_nodes = movable
    out = Path(output)
    if out.exists():
        raise FileExistsError('Choose a new output directory: ' + str(out))
    out.mkdir(parents=True)
    digest = id_digest(db)
    records = []
    for seed in seeds:
        rng = np.random.RandomState(seed)
        if distribution == 'uniform':
            width=np.asarray(db.node_size_x[:movable],dtype=np.float64)
            height=np.asarray(db.node_size_y[:movable],dtype=np.float64)
            xlo=float(db.meta['xl']);ylo=float(db.meta['yl'])
            xhi=np.maximum(xlo,np.asarray(db.meta['xh']-width,dtype=np.float64))
            yhi=np.maximum(ylo,np.asarray(db.meta['yh']-height,dtype=np.float64))
            x=xlo+rng.random_sample(movable)*(xhi-xlo)
            y=ylo+rng.random_sample(movable)*(yhi-ylo)
        elif distribution == 'center-gaussian':
            x = rng.normal((db.meta['xl'] + db.meta['xh']) / 2,
                           (db.meta['xh'] - db.meta['xl']) * .001, movable)
            y = rng.normal((db.meta['yl'] + db.meta['yh']) / 2,
                           (db.meta['yh'] - db.meta['yl']) * .001, movable)
        else:
            raise ValueError('Unsupported distribution: '+distribution)
        case = out / ('seed_%d' % seed); case.mkdir()
        write_table(case/'original.posX.tsv', 'posX', x, digest)
        write_table(case/'original.posY.tsv', 'posY', y, digest)
        records.append(dict(seed=seed, posX=str((case/'original.posX.tsv').resolve()),
                            posY=str((case/'original.posY.tsv').resolve())))
    (out/'manifest.json').write_text(json.dumps(dict(schema=1, original_db=str(Path(db_path).resolve()),
        distribution=distribution,
        note='Explicit A positions; project the same checkpoint to C. gp_noise_ratio must be zero.',
        cases=records), indent=2) + '\n')
    return records


def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--original-db',required=True);p.add_argument('--output',required=True)
    p.add_argument('--seeds',required=True,nargs='+',type=int)
    p.add_argument('--distribution',choices=('uniform','center-gaussian'),default='uniform')
    return p.parse_args(argv)


if __name__=='__main__':
    a=parse_args(); rows=generate(a.original_db,a.seeds,a.output,a.distribution)
    print('Saved %d explicit initial placements: %s'%(len(rows),a.output))
