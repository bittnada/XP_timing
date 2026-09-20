#!/usr/bin/env python3
"""Compare original and reduced final placements on the SAME original nets.

A = original final pin geometry; B = A with clustered pins at area-weighted
member-center centroids; C = reduced final geometry projected to original pins.
Default is geometry/log analysis only. --sta explicitly recomputes original
RC/STA for A/B/C with identical settings; no optimization or weight updates.
"""
import argparse
import ast
import csv
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time

import numpy as np

from PlacementTimingMapping import snapshot, load_mapping, file_hash, text
from PlacementState import orient_offsets, ORIENTS
from ReducedDEF import events, tokens, dname, component

LOG = logging.getLogger(__name__)
CASES = ('A_original', 'B_original_centers', 'C_two_db')
CONFIG_FIELDS = ('db_option', 'gpu', 'random_center_init_flag', 'gp_noise_ratio',
    'num_bins_x', 'num_bins_y', 'global_place_stages', 'target_density', 'density_weight',
    'gamma', 'random_seed', 'timing_opt_flag', 'enable_net_weighting',
    'timing_update_start', 'timing_update_interval', 'ignore_net_degree',
    'wire_resistance_per_micron', 'wire_capacitance_per_micron', 'net_weighting_scheme',
    'momentum_decay_factor', 'max_net_weight', 'legalize_flag', 'detailed_place_flag',
    'congestion_calculation_method')


def physical(path):
    db = snapshot(path)
    root = Path(db.source_path)
    meta = json.loads((root / 'manifest.json').read_text())
    db.meta = meta
    for name in ('node_x', 'node_y', 'node_size_x', 'node_size_y', 'node_orient',
                 'pin_offset_x', 'pin_offset_y', 'pin_direct',
                 'flat_net2pin_map', 'flat_net2pin_start_map'):
        setattr(db, name, np.load(root / (name + '.npy'), mmap_mode='r', allow_pickle=False))
    n, p = len(db.node_names), len(db.pin_names)
    for name in ('node_x', 'node_y', 'node_size_x', 'node_size_y', 'node_orient'):
        if getattr(db, name).shape != (n,): raise ValueError('Invalid ' + name)
    for name in ('pin_offset_x', 'pin_offset_y', 'pin_direct'):
        if getattr(db, name).shape != (p,): raise ValueError('Invalid ' + name)
    starts, flat = db.flat_net2pin_start_map, db.flat_net2pin_map
    if (starts.shape != (len(db.net_names)+1,) or starts[0] != 0 or starts[-1] != p
            or np.any(np.diff(starts) <= 0) or len(flat) != p
            or not np.array_equal(np.sort(flat), np.arange(p))
            or not np.array_equal(db.pin2net_map[flat], np.repeat(np.arange(len(starts)-1), np.diff(starts)))):
        raise ValueError('Invalid net-to-pin CSR')
    if not np.isfinite(meta['def_scale']) or meta['def_scale'] <= 0:
        raise ValueError('Invalid DEF DBU per micron')
    return db


def placement_records(path):
    """Read placement sections only; saved DB remains authoritative for NETS."""
    unit = None
    for kind, section, record, count in events(path):
        if kind == 'raw' and section is None:
            match = re.search(r'\bUNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;', record)
            if match: unit = int(match[1]); yield 'units', unit
        if kind == 'header' and section in ('NETS', 'SPECIALNETS'):
            break
        if kind == 'record' and section in ('COMPONENTS', 'PINS'):
            yield section, record
    if unit is None: raise ValueError('Missing DEF units: ' + str(path))


def geometry(db, path):
    """Use final COMPONENTS; require unchanged IO records and cell masters.

Unsupported/missing/replaced components are errors, not silently filled with
saved positions. Top-level pins are held at their saved geometry only after
verifying final PINS against the saved template (including PORT shapes).
"""
    index = {text(v): i for i, v in enumerate(db.node_names)}
    n = len(index)
    xy = np.array([db.node_x, db.node_y], dtype=np.float64)
    orientations = np.array([text(o) for o in db.node_orient], dtype='U7')
    seen = np.zeros(n, dtype=bool)
    masters = np.empty(n, dtype=object); masters[:] = None
    master_pool, ports, count = {}, {}, 0
    def add_port(target, record):
        ts = tokens(record); name = dname(ts[1])
        if name in target: raise ValueError('Duplicate IO: ' + name)
        target[name] = ts
    for kind, record in placement_records(path):
        if kind == 'units':
            if record != db.meta['def_scale']: raise ValueError('DEF units disagree with saved DB: ' + str(path))
        elif kind == 'PINS': add_port(ports, record)
        else:
            name, master, state, x, y, _ = component(record)
            if name not in index: raise ValueError('Unknown final component: ' + name)
            i = index[name]
            if seen[i]: raise ValueError('Duplicate final component: ' + name)
            if state == 'UNPLACED': raise ValueError('Unplaced final component: ' + name)
            ts = tokens(record)
            k = next(j for j in range(len(ts)-2) if ts[j:j+2] == ['+', state])
            orient = ts[k+6]
            if orient not in ORIENTS or orient == 'UNKNOWN': raise ValueError('Unsupported orientation: ' + orient)
            xy[:, i] = x, y; orientations[i] = orient; seen[i] = True
            masters[i] = master_pool.setdefault(master, master)
            count += 1
            if count % 250000 == 0:
                LOG.info('Read %d final components: %s', count, path)
    expected, saved_ports = np.zeros(n, dtype=bool), {}
    for kind, record in placement_records(Path(db.source_path) / 'template.def'):
        if kind == 'PINS': add_port(saved_ports, record)
        elif kind == 'COMPONENTS':
            ts = tokens(record); name, master = dname(ts[1]), dname(ts[2])
            if name not in index: raise ValueError('Saved template component absent from DB: ' + name)
            i = index[name]
            if expected[i] or not seen[i] or masters[i] != master:
                raise ValueError('Component missing/duplicated or master changed: ' + name)
            expected[i] = True
    if not np.array_equal(seen, expected): raise ValueError('Unexpected final component set')
    if ports != saved_ports:
        raise ValueError('Final DEF PINS differ from saved template; moved/edited IO is not supported')
    missing = set(index) - {text(db.node_names[i]) for i in np.flatnonzero(seen)} - set(ports)
    if missing: raise ValueError('Nodes with no validated final coordinate: ' + ', '.join(sorted(missing)[:5]))
    sizes = np.array([db.node_size_x, db.node_size_y], dtype=np.float64)
    offset = np.array([db.pin_offset_x, db.pin_offset_y], dtype=np.float64)
    owners = db.pin2node_map; pin_orient = orientations[owners]
    for orient in np.unique(orientations):
        if orient in ('N', 'UNKNOWN'): continue
        ids = np.flatnonzero(pin_orient == orient); nodes = owners[ids]
        offset[0, ids], offset[1, ids], _, _ = orient_offsets(
            offset[0, ids], offset[1, ids], sizes[0, nodes], sizes[1, nodes], orient)
        if orient in ('W', 'E', 'FW', 'FE'):
            ids = np.flatnonzero(orientations == orient)
            sizes[:, ids] = sizes[::-1, ids]
    unit = db.meta['def_scale']
    if not all(np.isfinite(value).all() for value in (xy, sizes, offset)) or np.any(sizes < 0):
        raise ValueError('Nonfinite geometry or negative node size')
    LOG.info('Validated %d final components and %d unchanged IOs: %s', seen.sum(), len(ports), path)
    return dict(xy=xy/unit, sizes=sizes/unit, offsets=offset/unit,
                pins=(xy[:, owners]+offset)/unit)


def projected_cases(original, placement, maps, og, pg):
    owner = original.pin2node_map
    node_map = maps['timing_node_to_placement_node']
    clustered = maps['timing_node_cluster_id'] >= 0
    clpins = clustered[owner]
    # B uses the original placement's member centers; centroid weights are
    # actual member areas, not reduced/inflated cluster areas.
    weight = np.prod(og['sizes'][:, clustered], axis=0)
    if np.any(weight <= 0): raise ValueError('Cluster members must have positive area')
    counts = np.bincount(node_map[clustered], weights=weight, minlength=len(placement.node_names))
    b = og['pins'].copy()
    c = pg['xy'][:, node_map[owner]].copy()
    c[:, clpins] += pg['sizes'][:, node_map[owner[clpins]]] / 2
    pm = maps['timing_pin_to_placement_pin']
    kept = ~clpins & (pm >= 0)
    c[:, kept] = pg['pins'][:, pm[kept]]
    missing = ~clpins & (pm < 0)
    c[:, missing] += og['offsets'][:, missing]
    for axis in range(2):
        center = og['xy'][axis, clustered] + og['sizes'][axis, clustered]/2
        sums = np.bincount(node_map[clustered], weights=weight*center, minlength=len(counts))
        centers = np.divide(sums, counts, out=np.zeros_like(sums), where=counts>0)
        b[axis, clpins] = centers[node_map[owner[clpins]]]
    return dict(zip(CASES, (og['pins'], b, c)))


def hpwl(db, pins):
    ordered = pins[:, db.flat_net2pin_map]
    starts = db.flat_net2pin_start_map[:-1]
    return (np.maximum.reduceat(ordered, starts, axis=1)-np.minimum.reduceat(ordered, starts, axis=1)).sum(axis=0)


def read_run(log=None, config=None):
    report = dict(parameters={}, parameter_source='unavailable', metrics={}, feedback_count=None,
                  feedback_iterations=[], warnings=[])
    if config:
        report['parameters'] = json.loads(Path(config).read_text())
        report['parameter_source'] = 'provided_config_not_verified_as_run'
    rows, parameter_blocks = [], 0
    if log:
        native_count, bridge_count = 0, 0
        with open(log) as stream:
            for line in stream:
                if 'parameters = ' in line:
                    parameter_blocks += 1
                    try: report['parameters'] = ast.literal_eval(line.split('parameters = ',1)[1].strip())
                    except (SyntaxError, ValueError) as exc: raise ValueError('Cannot parse logged parameters') from exc
                    report['parameter_source'] = 'run_log'
                if 'apply lilith net-weighting scheme' in line or 'apply adams net-weighting scheme' in line:
                    native_count += 1
                if 'Two-DB feedback #' in line: bridge_count += 1
                m = re.search(r'\[Final placement\]\s+(\w+)=([^\s]+)\s+\(([^)]+)\)', line)
                if m:
                    if m[1] in report['metrics']: raise ValueError('Multiple runs/final sections in log: ' + str(log))
                    value = None if m[2] == 'NA' else float(m[2])
                    report['metrics'][m[1]] = dict(value=value if value is None or np.isfinite(value) else None, unit=m[3])
                m = re.search(r'iteration\s+(\d+),.*TNS\s+([-+\d.eE]+).*WNS\s+([-+\d.eE]+)', line)
                if m:
                    rows.append((int(m[1]), float(m[3])*1000, float(m[2])*100000))
        if parameter_blocks > 1: raise ValueError('Multiple parameter blocks: supply a single-run log')
        # A final-only fragment is NOT evidence of zero feedback.
        if native_count or bridge_count:
            report['feedback_count'] = bridge_count or native_count
        report['feedback_iterations'] = [r[0] for r in rows]
        if not parameter_blocks: report['warnings'].append('Log lacks run parameters; configuration/cadence cannot be confirmed.')
    return report, rows


def statistics(a):
    return dict(count=len(a), sum_um=float(a.sum()), **{
        label: float(np.percentile(a, q)) if len(a) else None
        for label, q in (('p50_um',50), ('p90_um',90), ('p99_um',99), ('max_um',100))})


def run_sta(args, original, cases, out):
    """Sequential fresh timer per case; same cache, topology and RC parameters."""
    import torch
    from TimingCache import checked_model
    from MakeDBAdapter import timing_pin_name
    if args.timing_cpp:
        spec = importlib.util.spec_from_file_location('timing_cpp', args.timing_cpp)
        cpp = importlib.util.module_from_spec(spec); spec.loader.exec_module(cpp)
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from dreamplace.ops.timing import timing_cpp as cpp
    model, manifest = checked_model(Path(original.source_path).parent / 'timing_cache', cpp)
    excluded = set(original.placement_only_nets)
    net_names = ['' if text(n) in excluded else text(n) for n in original.net_names]
    pin_names = [timing_pin_name(p) for p in original.pin_names]
    flat = torch.tensor(np.asarray(original.flat_net2pin_map), dtype=torch.int32)
    starts = torch.tensor(np.asarray(original.flat_net2pin_start_map), dtype=torch.int32)
    owner = torch.arange(len(pin_names), dtype=torch.int32)
    offsets = torch.zeros(len(pin_names), dtype=torch.float64)
    results = {}
    for name, pos in cases.items():
        if not np.isfinite(pos).all() or np.any(np.abs(pos)*1000 >= np.iinfo(np.int32).max):
            raise ValueError('Pin coordinates exceed safe FLUTE int32 range: ' + name)
        LOG.info('Recompute original RC/STA: %s (no weight updates)', name)
        begin = time.monotonic(); timer = cpp.load_timing_model(str(model)); timer.update_timing()
        # Explicit pin coordinates in microns, with one anchor per original pin.
        cpp.forward(timer, torch.from_numpy(np.ascontiguousarray(pos.reshape(-1))), net_names,
                    pin_names, flat, starts, owner, offsets, offsets,
                    args.rc_r, args.rc_c, 1/original.meta['def_scale'],
                    int(original.meta['lef_scale']), int(original.meta['def_scale']), args.ignore_net_degree)
        timer.update_timing()
        def metric(method):
            value = getattr(timer, method)(True)
            return float(value)*timer.time_unit()*1e12 if value is not None and np.isfinite(value) else None
        results[name] = dict(wns_ps_late=metric('report_wns_el'), tns_ps_late=metric('report_tns_elw'),
                             seconds=time.monotonic()-begin)
        paths = cpp.report_timing_paths(timer, args.paths, True)
        with (out/(name+'.paths.json')).open('w') as f:
            json.dump(dict(time_unit_seconds=timer.time_unit(), paths=paths), f, indent=2)
        del timer
    return dict(model_file=str(model.resolve()), model_sha256=manifest['model_sha256'],
                backend_sha256=manifest['inputs']['backend_sha256'], cases=results)


def generate(args):
    started = time.monotonic(); out = Path(args.output)
    if os.path.lexists(out): raise FileExistsError('Choose a new output directory: ' + str(out))
    if args.top < 1 or args.paths < 1 or args.ignore_net_degree < 2: raise ValueError('Invalid positive limits')
    if args.sta and (args.rc_r is None or args.rc_c is None or not np.isfinite([args.rc_r,args.rc_c]).all()
                     or args.rc_r < 0 or args.rc_c < 0):
        raise ValueError('--sta requires explicit nonnegative --rc-r (ohm/um) and --rc-c (F/um)')
    LOG.info('Loading saved DB arrays and validating ID mapping')
    original, placement = physical(args.original_db), physical(args.placement_db)
    maps = load_mapping(args.mapping, original, placement)
    inputs = {key: str(Path(getattr(args,key)).resolve()) for key in
              ('original_db','placement_db','mapping','original_def','placement_def')}
    fingerprints = {key: file_hash(inputs[key]) for key in ('original_def','placement_def')}
    og, pg = geometry(original, args.original_def), geometry(placement, args.placement_def)
    cases = projected_cases(original, placement, maps, og, pg)
    lengths = {name: hpwl(original, pins) for name, pins in cases.items()}
    degree = np.diff(original.flat_net2pin_start_map)
    external = maps['timing_net_to_placement_net'] >= 0
    electrically_real = ~np.isin(original.net_names, original.placement_only_nets)
    clustered = maps['timing_node_cluster_id'] >= 0
    any_cluster = np.maximum.reduceat(clustered[original.pin2node_map][original.flat_net2pin_map],
                                      original.flat_net2pin_start_map[:-1])
    groups = dict(all=np.ones(len(degree),bool), external=external, internal_omitted=~external,
                  external_degree_within_limit=external & (degree<=args.ignore_net_degree),
                  unclustered_only=~any_cluster, timing_nets_degree_within_limit=electrically_real & (degree<=args.ignore_net_degree))
    summary = {}
    for name, mask in groups.items():
        a,c = lengths[CASES[0]][mask], lengths[CASES[2]][mask]
        summary[name] = dict(cases={k: statistics(v[mask]) for k,v in lengths.items()},
            C_over_A=float(c.sum()/a.sum()) if a.sum()>0 else None,
            longer_fraction=float(np.mean(c>a)) if len(a) else None,
            longer_than_2x_fraction=float(np.mean(c>2*a)) if len(a) else None)
    logs, progression = {}, {}
    for role in ('original','placement'):
        logs[role], progression[role] = read_run(getattr(args,role+'_log'), getattr(args,role+'_config'))
        for kind in ('log','config'):
            path = getattr(args,role+'_'+kind)
            if path:
                logs[role][kind+'_file'] = str(Path(path).resolve())
                logs[role][kind+'_sha256'] = file_hash(path)
    differences = [dict(field=k, original=logs['original']['parameters'].get(k),
                        placement=logs['placement']['parameters'].get(k)) for k in CONFIG_FIELDS
                   if logs['original']['parameters'].get(k) != logs['placement']['parameters'].get(k)]
    areas = {}
    for role,db,g in (('original',original,og), ('placement',placement,pg)):
        n = db.meta['num_physical_nodes']-db.meta['num_terminals']-db.meta['num_terminal_NIs']
        area = float(np.prod(g['sizes'][:,:n],axis=0).sum())
        areas[role] = dict(movable_nodes=n, movable_area_um2=area,
                          utilization=area/(db.meta['total_space_area']/db.meta['def_scale']**2))
    report = dict(status='complete', inputs=inputs, final_def_sha256=fingerprints,
        cases=dict(zip(CASES, ('Original final placement, real original offsets',
                              'Original placement, clustered pins at area-weighted member-center centroid',
                              'Reduced final placement projected to original pins, cluster center offsets zero'))),
        geometry=summary, area=areas, runs=logs, config_differences=differences,
        sta=None, ignore_net_degree=args.ignore_net_degree,
        warnings=['HPWL is unweighted geometry, not RC delay or signoff timing.',
                  'Final DEF supplies COMPONENTS geometry; saved DB supplies connectivity.',
                  'B isolates center projection on the original layout; it is not a legal clustered placement.',
                  'Logged WNS/TNS are observations, not matched-setting STA recomputations.',
                  'Provided config files without matching log parameters are not proof of executed settings.',
                  'Weighted HPWL, congestion and overflow from different runs need matching models/settings; no automatic ratio is asserted.'])
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.compare-timing-', dir=out.parent) as tmp:
        stage = Path(tmp)
        a,b,c = [lengths[name] for name in CASES]
        delta = c-a
        order = np.lexsort((np.arange(len(a)), -delta))
        def netrow(i):
            return [int(i), text(original.net_names[i]), int(degree[i]), int(external[i]),
                    bool(any_cluster[i]), a[i], b[i], c[i], delta[i], c[i]-b[i], c[i]/a[i] if a[i]>0 else '']
        header = ['original_net_id','net_name','degree','in_placement','touches_cluster',
                  'A_hpwl_um','B_hpwl_um','C_hpwl_um','C_minus_A_um','C_minus_B_um','C_over_A']
        for filename, ids in (('net_comparison.tsv',order),
                              ('top_external_nets.tsv',order[external[order]][:args.top])):
            with (stage/filename).open('w') as f:
                w=csv.writer(f,delimiter='\t'); w.writerow(header)
                for i in ids: w.writerow(netrow(i))
        with (stage/'top_net_pins.tsv').open('w') as f:
            w=csv.writer(f,delimiter='\t');w.writerow(['net_id','net_name','pin_id','pin_name','direction',
                'node_id','node_name','cluster_id','placement_node_id','placement_node_name',
                'A_x_um','A_y_um','B_x_um','B_y_um','C_x_um','C_y_um'])
            for i in order[external[order]][:args.top]:
                for pin in original.flat_net2pin_map[original.flat_net2pin_start_map[i]:original.flat_net2pin_start_map[i+1]]:
                    node=original.pin2node_map[pin]; dest=maps['timing_node_to_placement_node'][node]
                    w.writerow([i,text(original.net_names[i]),pin,text(original.pin_names[pin]),text(original.pin_direct[pin]),
                        node,text(original.node_names[node]),maps['timing_node_cluster_id'][node],dest,
                        text(placement.node_names[dest]), *[v for name in CASES for v in cases[name][:,pin]]])
        for name, value in lengths.items(): np.save(stage/(name+'.hpwl.npy'),value,allow_pickle=False)
        for role,rows in progression.items():
            with (stage/(role+'_timing_progress.tsv')).open('w') as f:
                w=csv.writer(f,delimiter='\t');w.writerow(['logged_iteration','wns_ps_late','tns_ps_late']);w.writerows(rows)
        if args.sta:
            report['sta'] = dict(rc_r_ohm_per_um=args.rc_r,rc_c_f_per_um=args.rc_c,
                                **run_sta(args,original,cases,stage))
        for key in fingerprints:
            if file_hash(inputs[key]) != fingerprints[key]: raise ValueError('Input DEF changed during comparison')
        report['seconds'] = time.monotonic()-started
        (stage/'summary.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
        (stage/'report.md').write_text(markdown(report))
        # Publish into a newly reserved destination; never overwrite reports.
        out.mkdir()
        for path in stage.iterdir(): path.rename(out/path.name)
    return report


def markdown(report):
    lines=['# Placement/timing comparison','', 'Geometry uses the same original net IDs; all lengths are unweighted microns.', '',
           '| Group | Nets | A sum | B sum | C sum | C/A |', '|---|---:|---:|---:|---:|---:|']
    for name,row in report['geometry'].items():
        values=[row['cases'][k]['sum_um'] for k in CASES]
        ratio='NA' if row['C_over_A'] is None else '%.4f'%row['C_over_A']
        lines.append('| %s | %d | %.3f | %.3f | %.3f | %s |'%(name,row['cases'][CASES[0]]['count'],*values,ratio))
    lines += ['', '## Cases',''] + ['- **%s**: %s'%(k,v) for k,v in report['cases'].items()]
    lines += ['', '## Movable area','', '| Design | Nodes | Area (um2) | Utilization |',
              '|---|---:|---:|---:|']
    for role,row in report['area'].items():
        lines.append('| %s | %d | %.3f | %.4f |'%(role,row['movable_nodes'],row['movable_area_um2'],row['utilization']))
    lines += ['', '## Run evidence','']
    for role,r in report['runs'].items():
        lines.append('- %s: parameters=%s; observed weight updates=%s'%(role,r['parameter_source'],r['feedback_count']))
        for name in ('wns','tns','iteration'):
            if name in r['metrics']: lines.append('  - %s: %s %s'%(name,r['metrics'][name]['value'],r['metrics'][name]['unit']))
    lines += ['', '## Configuration differences','', 'Missing values mean unknown/default-unresolved, not equal settings.','']
    lines += ['- `%s`: original=%s; placement=%s'%(d['field'],d['original'],d['placement']) for d in report['config_differences']]
    lines += ['', '## Matched RC/STA','']
    if report['sta']:
        for name,r in report['sta']['cases'].items(): lines.append('- %s: WNS=%s ps; TNS=%s ps'%(name,r['wns_ps_late'],r['tns_ps_late']))
    else: lines.append('Not recomputed. Use --sta with explicit --rc-r and --rc-c. Logged timing is not a controlled comparison.')
    lines += ['', '## Limits',''] + ['- '+w for w in report['warnings']]
    return '\n'.join(lines)+'\n'


def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for field in ('original-db','placement-db','mapping','original-def','placement-def','output'):
        p.add_argument('--'+field,required=True)
    for role in ('original','placement'):
        p.add_argument('--'+role+'-log');p.add_argument('--'+role+'-config')
    p.add_argument('--top',type=int,default=100)
    p.add_argument('--ignore-net-degree',type=int,default=100)
    p.add_argument('--sta',action='store_true')
    p.add_argument('--rc-r',type=float,help='ohms/micron, required with --sta')
    p.add_argument('--rc-c',type=float,help='farads/micron, required with --sta')
    p.add_argument('--paths',type=int,default=10,help='worst late paths saved per recomputed STA case')
    p.add_argument('--timing-cpp',help='optional explicit native extension path')
    return p.parse_args(argv)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='[%(levelname)s] %(message)s')
    try:
        args=parse_args(); result=generate(args)
        print('Saved comparison:',args.output)
        print('External net HPWL C/A:',result['geometry']['external']['C_over_A'])
    except (ValueError,OSError,RuntimeError,KeyError) as exc:
        LOG.error('%s',exc);sys.exit(1)
