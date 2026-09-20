#!/usr/bin/env python3
"""Prepare one paired A/C multi-start case without running placement."""
import argparse
import json
from pathlib import Path
import shlex


def generate(args):
    seed_dir=Path(args.seed_dir);out=Path(args.output)
    if out.exists():raise FileExistsError('Choose a new case output directory: '+str(out))
    paths={
        'ax':seed_dir/'original.posX.tsv','ay':seed_dir/'original.posY.tsv',
        'cx':seed_dir/'clustered.posX.tsv','cy':seed_dir/'clustered.posY.tsv'}
    for path in paths.values():
        if not path.is_file():raise FileNotFoundError(path)
    a=json.loads(Path(args.original_config).read_text())
    c=json.loads(Path(args.placement_config).read_text())
    result_root=Path(args.result_root)
    for cfg,x,y,role in ((a,paths['ax'],paths['ay'],'A'),(c,paths['cx'],paths['cy'],'C')):
        cfg.update(random_seed=args.seed,random_center_init_flag=0,gp_noise_ratio=0.0,
                   read_posX=str(x.resolve()),read_posY=str(y.resolve()),
                   result_dir=str((result_root/('seed_%d'%args.seed)/role).resolve()))
    out.mkdir(parents=True)
    acfg=out/'A.json';ccfg=out/'C.json'
    acfg.write_text(json.dumps(a,indent=2)+'\n');ccfg.write_text(json.dumps(c,indent=2)+'\n')
    alog=out/'A.log';clog=out/'C.log';compare=out/'comparison_sta'
    design=args.design
    adef=Path(a['result_dir'])/design/(design+'.gp.def')
    cdef=Path(c['result_dir'])/design/(design+'.gp.def')
    physical=Path(c['placement_db_path']).parent
    q=shlex.quote
    commands='''#!/bin/sh
set -eu
cd %s
python3 dreamplace/Placer.py %s 2>&1 | tee %s
TC_CONFIG=%s sh ./run_two_db_timing.sh 2>&1 | tee %s
TC_PHYSICAL=%s \\
TC_ORIGINAL_DEF=%s \\
TC_TWO_DB_DEF=%s \\
TC_ORIGINAL_LOG=%s \\
TC_TWO_DB_LOG=%s \\
TC_ORIGINAL_CONFIG=%s \\
TC_TWO_DB_CONFIG=%s \\
TC_COMPARE=%s \\
sh ./run_compare_timing_placement.sh --sta --rc-r %s --rc-c %s --ignore-net-degree %d --paths %d
'''%(q(str(Path(args.bin_dir).resolve())),q(str(acfg.resolve())),q(str(alog.resolve())),
     q(str(ccfg.resolve())),q(str(clog.resolve())),q(str(physical)),q(str(adef)),q(str(cdef)),
     q(str(alog.resolve())),q(str(clog.resolve())),q(str(acfg.resolve())),q(str(ccfg.resolve())),
     q(str(compare.resolve())),a['wire_resistance_per_micron'],a['wire_capacitance_per_micron'],
     args.ignore_net_degree,args.paths)
    script=out/'run.sh';script.write_text(commands);script.chmod(0o755)
    (out/'pair_row.tsv').write_text('case_id\toriginal_log\tplacement_log\tcomparison_summary\n'+
        'seed_%d\t%s\t%s\t%s\n'%(args.seed,alog.resolve(),clog.resolve(),(compare/'summary.json').resolve()))
    (out/'case.json').write_text(json.dumps(dict(seed=args.seed,A_config=str(acfg.resolve()),
        C_config=str(ccfg.resolve()),run_script=str(script.resolve()),pair_row=str((out/'pair_row.tsv').resolve())),indent=2)+'\n')
    return script


def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seed',type=int,required=True);p.add_argument('--seed-dir',required=True)
    p.add_argument('--original-config',required=True);p.add_argument('--placement-config',required=True)
    p.add_argument('--output',required=True);p.add_argument('--result-root',required=True)
    p.add_argument('--bin-dir',default='/mnt/hdd1/XP_timing_4.1/bin');p.add_argument('--design',default='superblue1')
    p.add_argument('--ignore-net-degree',type=int,default=100);p.add_argument('--paths',type=int,default=10)
    return p.parse_args(argv)


if __name__=='__main__':
    a=parse_args();print('Prepared paired case:',generate(a))
