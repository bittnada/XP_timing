"""Final-state evaluation and adapter for the unchanged shared-memory congestion code."""
import copy
import logging

import numpy as np
import torch


def synchronize_pin_geometry(placer, db):
    """Apply final raw-DB row flips to pin offsets before final HPWL/RC/RUDY."""
    from PlacementState import orient_offsets, text
    old = placer._metric_pin_orientations
    new = np.array([text(o) for o in db.node_orient[:db.num_movable_nodes]])
    # UNKNOWN has historically been interpreted as canonical N for MakeDB.
    old = np.where(old == 'UNKNOWN', 'N', old)
    new = np.where(new == 'UNKNOWN', 'N', new)
    changed = np.flatnonzero(old != new)
    if not len(changed):
        return
    turns = {'W', 'E', 'FW', 'FE'}
    if any((a in turns) != (b in turns) for a, b in zip(old[changed], new[changed])):
        raise ValueError('Final legalization changed 90-degree orientation; node sizes must be rebuilt')
    movable_pins = db.pin2node_map < db.num_movable_nodes
    pin_ids = np.flatnonzero(movable_pins)
    nodes = db.pin2node_map[pin_ids]
    inverse = {'N': 'N', 'S': 'S', 'FN': 'FN', 'FS': 'FS',
               'W': 'E', 'E': 'W', 'FW': 'FW', 'FE': 'FE'}
    # Transform by orientation pair, not a million-cell Python loop.
    for previous in np.unique(old[changed]):
        for following in np.unique(new[changed]):
            selected = pin_ids[(old[nodes] == previous) & (new[nodes] == following)]
            if previous == following or not len(selected):
                continue
            owners = db.pin2node_map[selected]
            x, y, w, h = orient_offsets(db.pin_offset_x[selected], db.pin_offset_y[selected],
                db.node_size_x[owners], db.node_size_y[owners], inverse[previous])
            x, y, _, _ = orient_offsets(x, y, w, h, following)
            db.pin_offset_x[selected], db.pin_offset_y[selected] = x, y
    with torch.no_grad():
        for name in ('pin_offset_x', 'pin_offset_y'):
            target = getattr(placer.data_collections, name)
            target.copy_(torch.as_tensor(getattr(db, name), dtype=target.dtype, device=target.device))


def congestion_view(params, db):
    # Lazy import at final evaluation; does not load the reference project or
    # require its presence on the machine at runtime.
    from SharedCongestion import PlaceDB
    view = PlaceDB()
    view.params = params
    for name in ('xl', 'yl', 'xh', 'yh', 'site_width', 'row_height',
                 'num_physical_nodes', 'num_terminal_NIs', 'num_nets',
                 'node_size_x', 'node_size_y', 'pin_offset_x', 'pin_offset_y',
                 'pin2node_map', 'flat_net2pin_map', 'flat_net2pin_start_map',
                 'net_names', 'net_weights', 'unit_horizontal_capacity', 'unit_vertical_capacity'):
        setattr(view, name, getattr(db, name))
    if view.site_width <= 0 or view.row_height <= 0:
        raise ValueError('Final congestion requires positive site_width and row_height')
    if int((view.xh - view.xl) / view.site_width) < 1 or int((view.yh - view.yl) / view.row_height) < 1:
        raise ValueError('Final congestion grid must contain at least one bin per axis')
    view._static_congestion_bin_masks = {}
    raw = getattr(db, 'rawdb', None)
    source = getattr(raw, 'data', None)
    meta = getattr(source, 'congestion_metadata', None)
    view.blockageInfo = copy.deepcopy((meta or {}).get('blockageInfo', {}))
    view.plot_fixed_macros = np.asarray((meta or {}).get('plot_fixed_macros', []), dtype=float).reshape(-1, 4)
    # MakeDB metadata is saved in original DEF coordinates, while evaluation
    # uses the same scaled placement coordinates as NonLinearPlace.
    shift = np.asarray(params.shift_factor)
    scale = float(params.scale_factor)
    for info in view.blockageInfo.values():
        coords = info.get('coords', [])
        if len(coords) >= 4 and len(coords) % 2 == 0:
            info['coords'] = ((np.asarray(coords).reshape(-1, 2) - shift) * scale).ravel().tolist()
    if len(view.plot_fixed_macros):
        view.plot_fixed_macros[:, :2] -= shift
        view.plot_fixed_macros *= scale

    counts = ('num_movable_std_cell', 'num_movable_macro', 'num_fixed_std_cell',
              'num_fixed_macro', 'num_blockage')
    if all(hasattr(db, name) for name in counts):
        for name in counts:
            setattr(view, name, int(getattr(db, name)))
        view._node_type_ranges_for_congestion()  # Validate the original ID ordering.
        order = np.arange(db.num_physical_nodes)
        if meta is None:
            logging.warning('Old physical DB lacks congestion blockage metadata; '
                            'node blockages are retained, but routing-only blockages may be missing. '
                            'Re-export with def + binary_write for complete metadata.')
    else:
        # Native Bookshelf DB has no explicit MakeDB type ordering. Adapt only
        # the evaluation view; never reorder the real placement or exported IDs.
        n = db.num_movable_nodes
        macro = np.asarray(db.movable_macro_mask, dtype=bool)
        movable = np.arange(n)
        fixed_end = db.num_physical_nodes - db.num_terminal_NIs
        order = np.concatenate((movable[~macro], movable[macro], np.arange(n, db.num_physical_nodes)))
        view.num_movable_std_cell = int((~macro).sum())
        view.num_movable_macro = int(macro.sum())
        view.num_fixed_std_cell = 0
        view.num_fixed_macro = fixed_end - n
        view.num_blockage = 0
        inverse = np.empty_like(order)
        inverse[order] = np.arange(len(order))
        view.node_size_x = db.node_size_x[:db.num_physical_nodes][order]
        view.node_size_y = db.node_size_y[:db.num_physical_nodes][order]
        view.pin2node_map = inverse[db.pin2node_map]

    if view._get_congestion_calculation_method() == 'legacy':
        # Duplicate endpoints cannot change the cell bbox; use the pin CSR to
        # provide the original interface without an extra per-net Python loop.
        view.netInfoCellIdxList = view.pin2node_map[view.flat_net2pin_map]
        view.netInfoCellIdxList_startIdx = view.flat_net2pin_start_map[:-1]
    return view, order


def evaluate_congestion(params, db, pos):
    placedb, order = congestion_view(params, db)
    cur_pos = pos.detach().cpu().numpy()
    cur_posX = cur_pos[:db.num_nodes][:db.num_physical_nodes][order]
    cur_posY = cur_pos[db.num_nodes:][:db.num_physical_nodes][order]
    # Backend selection/calls copied from the reference NonLinearPlace.py.
    congestion_backend = str(getattr(params, "congestion_backend", "auto")).lower()
    if congestion_backend == "auto":
        use_cuda_congestion = bool(getattr(params, "gpu", False) and torch.cuda.is_available())
    elif congestion_backend in {"cuda", "gpu"}:
        if not torch.cuda.is_available():
            raise RuntimeError("congestion_backend is set to cuda/gpu, but torch.cuda.is not available")
        use_cuda_congestion = True
    elif congestion_backend in {"numba", "cpu"}:
        use_cuda_congestion = False
    else:
        raise ValueError("Unsupported congestion_backend '{}'. Use auto, cuda, gpu, numba, or cpu".format(congestion_backend))
    print("[NonLinearPlace ] congestion backend: {}".format("cuda" if use_cuda_congestion else "numba"))
    if use_cuda_congestion:
        stats = placedb.calc_congestion_map_cuda(cur_posX, cur_posY, placedb.site_width,
                                               placedb.row_height, return_stats=True)
    else:
        stats = placedb.calc_congestion_numba(cur_posX, cur_posY, placedb.site_width,
                                            placedb.row_height, return_stats=True)
    return stats


def evaluate(placer, params, db, iteration):
    """Recompute, do not reuse GP/best/previous-STA metrics; do not update weights."""
    from PlaceObj import PlaceObj
    pos = placer.pos[0].detach()
    data = placer.data_collections
    with torch.no_grad():
        # Build fresh overflow so routability inflation/cached fixed density and
        # global_place_flag=0 cannot leave us with an absent or stale operator.
        overflow_op = PlaceObj.build_electric_overflow(
            placer, params, db, data, params.num_bins_x, params.num_bins_y)
        overflow, max_density = overflow_op(pos)
        hpwl = placer.op_collections.hpwl_op(pos).item() / params.scale_factor
        area = float(db.total_movable_node_area)
        result = {
            'wns': (None, 'ps_late'), 'tns': (None, 'ps_late'),
            'hpwl': (hpwl, 'weighted_original_db_length'),
            'overflow': (overflow.item() / area if area > 0 else None, 'ratio'),
            'max_density': (max_density.item(), 'ratio'),
        }
        timing_op = placer.op_collections.timing_op
        if timing_op is not None and timing_op.timer is not None:
            timing_op(pos.clone().cpu())  # Rebuild RC at the final coordinates.
            timing_op.timer.update_timing()
            # OpenTimer reports in its library time unit (seconds per unit).
            ps_per_unit = timing_op.timer.time_unit() * 1e12
            for name, report in (('wns', timing_op.timer.report_wns),
                                 ('tns', timing_op.timer.report_tns_elw)):
                value = report(split=1)
                value = None if value is None else float(value) * ps_per_unit
                if value is not None and not np.isfinite(value):
                    logging.warning('Final %s is nonfinite/unavailable from OpenTimer; writing NA', name)
                    value = None
                result[name] = (value, 'ps_late')
        else:
            logging.warning('Final WNS/TNS unavailable: STA is disabled; writing NA')
        stats = evaluate_congestion(params, db, pos)
        for name, value in zip(('congestion_max', 'congestion_total',
                                'macro_congestion_max', 'macro_congestion_total'), stats):
            result[name] = (float(value), 'reference_congestion')
        result['iteration'] = (iteration, 'count')
    for name, (value, unit) in result.items():
        logging.info('[Final placement] %s=%s (%s)', name, value if value is not None else 'NA', unit)
    db.final_placement_metrics = result
    return result
