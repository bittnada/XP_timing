#!/usr/bin/env python3
"""Project an original initial placement into a clustered placement DB.

Unclustered singleton cells preserve the exact original lower-left coordinate.
Cluster locations use area-weighted member-center centroids.  This uses an
initial condition, never an original final placement.
"""
import argparse
import json
from pathlib import Path

import numpy as np

from CompareTimingPlacement import physical
from PlacementTimingMapping import load_mapping, file_hash
from PlacementState import id_digest, read_table


def prepare(db):
    db.num_physical_nodes=len(db.node_names)
    db.num_movable_nodes=(db.meta['num_physical_nodes']-db.meta['num_terminals']-
                          db.meta['num_terminal_NIs'])
    db.dtype=np.asarray(db.node_x).dtype
    return db


def project_coordinates(original, placement, node_map, x, y):
    om, pm = original.num_movable_nodes, placement.num_movable_nodes
    source=np.arange(om,dtype=np.int64); target=np.asarray(node_map[:om],dtype=np.int64)
    if np.any(target<0) or np.any(target>=pm):
        raise ValueError('Movable original cells must map to movable placement nodes')
    weight=np.asarray(original.node_size_x[:om],float)*np.asarray(original.node_size_y[:om],float)
    if np.any(weight<=0) or not np.isfinite(weight).all(): raise ValueError('Invalid original movable area')
    total=np.bincount(target,weights=weight,minlength=pm)
    if np.any(total<=0): raise ValueError('A movable placement node has no original member')
    cx=np.bincount(target,weights=weight*(x+np.asarray(original.node_size_x[:om])/2),minlength=pm)/total
    cy=np.bincount(target,weights=weight*(y+np.asarray(original.node_size_y[:om])/2),minlength=pm)/total
    px=cx-np.asarray(placement.node_size_x[:pm])/2
    py=cy-np.asarray(placement.node_size_y[:pm])/2
    # An unclustered cell must start at exactly the same lower-left coordinate
    # in A and C.  Cluster geometry may be larger than a member, so only true
    # singleton mappings receive this override.
    count=np.bincount(target,minlength=pm)
    singleton=count==1
    if np.any(singleton):
        order=np.argsort(target,kind='stable')
        first=np.empty(pm,dtype=np.int64);first[target[order]]=source[order]
        px[singleton]=x[first[singleton]];py[singleton]=y[first[singleton]]
    rawx,rawy=px.copy(),py.copy()
    px=np.clip(px,placement.meta['xl'],placement.meta['xh']-np.asarray(placement.node_size_x[:pm]))
    py=np.clip(py,placement.meta['yl'],placement.meta['yh']-np.asarray(placement.node_size_y[:pm]))
    return px,py,int(np.count_nonzero((px!=rawx)|(py!=rawy)))


def write_table(path,field,values,digest):
    with path.open('x') as f:
        f.write('# cell_names_sha256='+digest+'\ncell_id\t'+field+'\n')
        for i,v in enumerate(values): f.write('%d\t%.17g\n'%(i,float(v)))


def generate(args):
    original,placement=prepare(physical(args.original_db)),prepare(physical(args.placement_db))
    maps=load_mapping(args.mapping,original,placement)
    digest=id_digest(original)
    x=read_table(args.original_posX,'posX',original,digest)[0][:original.num_movable_nodes]
    y=read_table(args.original_posY,'posY',original,digest)[0][:original.num_movable_nodes]
    px,py,clipped=project_coordinates(original,placement,maps['timing_node_to_placement_node'],x,y)
    prefix=Path(args.output_prefix); prefix.parent.mkdir(parents=True,exist_ok=True)
    xp=Path(str(prefix)+'.posX.tsv');yp=Path(str(prefix)+'.posY.tsv')
    if xp.exists() or yp.exists(): raise FileExistsError('Output checkpoint already exists')
    pdigest=id_digest(placement);write_table(xp,'posX',px,pdigest);write_table(yp,'posY',py,pdigest)
    manifest=Path(str(prefix)+'.manifest.json')
    manifest.write_text(json.dumps(dict(schema=1,
        method='singleton_exact_lower_left_else_area_weighted_member_centers',
        original_db=str(Path(args.original_db).resolve()),placement_db=str(Path(args.placement_db).resolve()),
        mapping=str(Path(args.mapping).resolve()),original_posX_sha256=file_hash(args.original_posX),
        original_posY_sha256=file_hash(args.original_posY),placement_posX=str(xp.resolve()),
        placement_posY=str(yp.resolve()),clipped_nodes=clipped),indent=2)+'\n')
    return xp,yp,clipped


def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for k in ('original-db','placement-db','mapping','original-posX','original-posY','output-prefix'):
        p.add_argument('--'+k,required=True)
    return p.parse_args(argv)


if __name__=='__main__':
    a=parse_args();x,y,n=generate(a);print('Saved projected initial placement:',x,y,'clipped=',n)
