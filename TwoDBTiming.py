"""CPU original STA coupled to reduced placement; never export/reparse on restore.

All identity maps use saved physical IDs. Original cell coordinates below are
virtual timing anchors, NOT an unclustered legal placement. Only the reduced DB
is passed to BasicPlace/PlaceDataCollection and thus copied to the GPU.
"""
import copy
import logging
from pathlib import Path

import numpy as np

MODE = 'two_timing_placement_db'
LOG = logging.getLogger(__name__)


def enabled(params):
    return getattr(params, 'db_option', '') == MODE


def validate_options(params):
    for field in ('placement_db_path', 'timing_db_path', 'placement_timing_mapping_path'):
        if not getattr(params, field, ''):
            raise ValueError(MODE + ' requires ' + field)
    for field in ('placement_db_path', 'timing_db_path'):
        root = Path(getattr(params, field))
        if not (root / 'physical_db/manifest.json').is_file():
            raise ValueError(field + ' must contain a saved physical_db/manifest.json: ' + str(root))
    if not params.timing_opt_flag or not params.enable_net_weighting:
        raise ValueError(MODE + ' requires timing_opt_flag=1 and enable_net_weighting=1')
    # Inflating cell/pin geometry for routability or halo placement is a
    # different abstraction; do not silently project that geometry into STA.
    for flag in ('routability_opt_flag', 'macro_place_flag', 'macro_halo_x', 'macro_halo_y',
                 'timing_clustering_flag'):
        if getattr(params, flag, 0):
            raise ValueError(MODE + ' does not support ' + flag)
    for field in ('timing_update_start', 'timing_update_interval'):
        value = getattr(params, field)
        if isinstance(value, bool) or int(value) != value or value < 1:
            raise ValueError(field + ' must be a positive integer')
    for field in ('wire_resistance_per_micron', 'wire_capacitance_per_micron'):
        value = getattr(params, field)
        if value is None or not np.isfinite(value) or value < 0:
            raise ValueError(field + ' must be finite and nonnegative (ohm/um or F/um)')
        if value == 0:
            LOG.warning('%s is zero; wire RC feedback will be incomplete', field)


def restore_params(params, path, timing=False):
    p = copy.deepcopy(params)
    p.db_option = 'binary'
    p.mode = 'default'
    p.save_path = str(Path(path).resolve())
    # Saved snapshots own topology. Never fall back to text sources or a
    # template from the other design. Initial position flags apply ONLY to P.
    for field in ('aux_input', 'def_input', 'def_path', 'def_template_input',
                  'verilog_input', 'sdc_input', 'lib_input', 'early_lib_input', 'late_lib_input'):
        setattr(p, field, '')
    p.timing_clustering_flag = 0
    if timing:
        p.gpu = 0
        p.global_place_flag = p.legalize_flag = p.detailed_place_flag = 0
        for field in list(vars(p)):
            if field.startswith(('read_', 'write_', 'wrtie_', 'wriate_')):
                setattr(p, field, '')
        p.scale_factor = 1.0  # Original timing view stays in original DEF DBU.
        p.shift_factor = [0.0, 0.0]
    return p


def load(params):
    """Restore two independent PlaceDB objects, but initialize only P for placement."""
    import PlaceDB
    import MakeDBAdapter
    import Timer
    from PlacementTimingMapping import load_mapping
    from TimingCache import checked_model
    from dreamplace.ops.timing import timing_cpp

    validate_options(params)
    store = None
    role = getattr(params, 'shared_memory_role', '') or ''
    if role == 'client':
        from SharedSnapshot import attach
        directory = getattr(params, 'shared_memory_dir', '')
        if not directory:
            raise ValueError(MODE + ' client mode requires shared_memory_dir')
        store = attach(directory)
        LOG.info('Two-DB client attached to shared snapshot %s', store.manifest['shm_name'])
    elif role and role != 'master':
        raise ValueError('shared_memory_role must be master, client, or empty')
    else:
        # Fail before allocating the large physical DBs if native cache is stale.
        checked_model(Path(params.timing_db_path) / 'timing_cache', timing_cpp)
    pp = restore_params(params, params.placement_db_path)
    tp = restore_params(params, params.timing_db_path, timing=True)
    if store is not None:
        pp._shared_snapshot = tp._shared_snapshot = store
        pp._shared_prefix, tp._shared_prefix = 'placement', 'timing'
    placement = PlaceDB.PlaceDB()
    placement(pp)
    original = PlaceDB.PlaceDB()
    original.read(tp)
    original.num_filler_nodes = 0
    MakeDBAdapter.finish_initial(original, tp)
    # No original.initialize(), filler/density structures, or GPU collection.
    maps = load_mapping(params.placement_timing_mapping_path, original, placement, store=store)
    timer = Timer.Timer()
    if store is not None:
        runtime = Path(directory) / 'runtime'
        model = store.materialize('timing_cache/model.bin', runtime / 'model.bin')
        timer.raw_timer = timing_cpp.load_timing_model(str(model))
        timer.placedb = original
        original.timing_cache_info = {'status': 'restored_shared', 'model': str(model)}
        LOG.info('timing DB restored from shared memory: %s', model)
    else:
        timer(tp, original)
    timer.update_timing()
    for field in ('scale_factor', 'shift_factor', 'num_bins_x', 'num_bins_y', 'target_density',
                  '_physical_design_name', '_physical_has_def_template'):
        setattr(params, field, copy.deepcopy(getattr(pp, field)))
    bridge = TwoDBTiming(params, placement, original, maps, tp, timer)
    placement.two_db_timing = bridge
    LOG.info('Two-DB restored: placement nodes=%d pins=%d nets=%d; '
             'CPU timing nodes=%d pins=%d nets=%d', placement.num_physical_nodes,
             placement.num_pins, placement.num_nets, original.num_physical_nodes,
             original.num_pins, original.num_nets)
    LOG.info('Two-DB feedback starts after %d optimizer steps, every %d steps; '
             'clustered original pins use cluster centers',
             params.timing_update_start, params.timing_update_interval)
    return placement, timer


def as_numpy(value):
    if hasattr(value, 'detach'):
        return value.detach().cpu().numpy()
    return np.asarray(value)


class TwoDBTiming:
    """TimingOpt-compatible bridge, also used by final/legalized STA evaluation."""
    def __init__(self, params, placement, original, maps, timing_params, timer):
        self.params, self.placement, self.original = params, placement, original
        self.maps, self.timing_params, self.timer = maps, timing_params, timer
        self.data = None
        self.original_op = None
        self.feedback_count = 0
        self.num_timing_nets = len(original.net_names)
        self.node_map = maps['timing_node_to_placement_node']
        self.clustered = maps['timing_node_cluster_id'] >= 0
        self.cluster_pins = self.clustered[original.pin2node_map]
        mapped_pins = maps['timing_pin_to_placement_pin']
        self.retained_pin_ids = np.flatnonzero(~self.cluster_pins & (mapped_pins >= 0))
        self.retained_placement_pins = mapped_pins[self.retained_pin_ids]
        self.pin_offset_x = np.array(original.pin_offset_x, copy=True)
        self.pin_offset_y = np.array(original.pin_offset_y, copy=True)
        self.pin_offset_x[self.cluster_pins] = 0
        self.pin_offset_y[self.cluster_pins] = 0
        self.rc_offset_x = np.empty_like(self.pin_offset_x)
        self.rc_offset_y = np.empty_like(self.pin_offset_y)
        self.unit_ratio = original.rawdb.defUnit() / placement.rawdb.defUnit()
        if not np.isfinite(self.unit_ratio) or self.unit_ratio <= 0:
            raise ValueError('Invalid DEF units for two-DB projection')
        self.last_positions = None

    def due(self, completed_steps):
        start, interval = self.params.timing_update_start, self.params.timing_update_interval
        return completed_steps >= start and (completed_steps - start) % interval == 0

    def bind(self, data, original_op):
        self.data, self.original_op = data, original_op
        # Private offsets: never overwrite the saved/original DB geometry.
        original_op.pin_offset_x = self.rc_offset_x
        original_op.pin_offset_y = self.rc_offset_y
        # RC's FLUTE interface multiplies coordinates by 1000 before int32
        # conversion. Feeding raw million-DBU coordinates could overflow.
        # Use microns for the native call (unit_to_micron = scale * DBU = 1).
        original_op.scale_factor = 1.0 / self.original.rawdb.defUnit()
        return self

    def project_positions(self, pos):
        p, t = self.placement, self.original
        a = as_numpy(pos).reshape(-1)
        if a.size != 2 * p.num_nodes or not np.isfinite(a).all():
            raise ValueError('Invalid reduced placement coordinate tensor')
        scale = float(self.params.scale_factor)
        if not np.isfinite(scale) or scale <= 0:
            raise ValueError('Invalid placement coordinate scale')
        geometry = self.data if self.data is not None else p
        result = np.empty((2, t.num_physical_nodes), dtype=t.node_x.dtype)
        for axis, shift in enumerate(self.params.shift_factor):
            suffix = 'x' if axis == 0 else 'y'
            values = a[axis * p.num_nodes:(axis + 1) * p.num_nodes][self.node_map].copy()
            sizes = as_numpy(getattr(geometry, 'node_size_' + suffix))
            values[self.clustered] += sizes[self.node_map[self.clustered]] * 0.5
            result[axis] = (values / scale + shift) * self.unit_ratio
            offsets = getattr(self, 'pin_offset_' + suffix)
            reduced_offsets = as_numpy(getattr(geometry, 'pin_offset_' + suffix))
            offsets[self.retained_pin_ids] = (
                reduced_offsets[self.retained_placement_pins] / scale * self.unit_ratio)
        if not np.isfinite(result).all():
            raise ValueError('Nonfinite projected timing coordinates')
        self.last_positions = result.reshape(-1)
        return self.last_positions

    def __call__(self, pos):
        import torch
        if self.original_op is None:
            raise RuntimeError('TwoDBTiming is not bound to an original timing operator')
        projected = self.project_positions(pos)
        unit = self.original.rawdb.defUnit()
        np.divide(self.pin_offset_x, unit, out=self.rc_offset_x)
        np.divide(self.pin_offset_y, unit, out=self.rc_offset_y)
        return self.original_op(torch.from_numpy(projected / unit))

    def copy_weights_to_placement(self):
        ids = self.maps['placement_net_to_timing_net']
        if np.any(ids < 0) or np.any(ids >= len(self.original.net_weights)):
            raise ValueError('Placement net lacks original timing weight')
        weights = self.original.net_weights[ids]
        if not np.isfinite(weights).all() or np.any(weights < 0):
            raise ValueError('STA generated invalid net weights; placement weights not changed')
        for name in ('net_weights', 'net_weight_deltas', 'net_criticality', 'net_criticality_deltas'):
            np.copyto(getattr(self.placement, name), getattr(self.original, name)[ids])

    def update_net_weights(self, max_net_weight=np.inf, n=1):
        result = self.original_op.update_net_weights(max_net_weight=max_net_weight, n=n)
        self.copy_weights_to_placement()
        self.feedback_count += 1
        LOG.info('Two-DB feedback #%d: %d original net weights -> %d placement nets '
                 '(internal omitted nets are not copied)', self.feedback_count,
                 self.num_timing_nets, len(self.placement.net_weights))
        return result

    def report_timing(self, n=1):
        return self.original_op.report_timing(n)


def refresh_optimizer(optimizer):
    """Refresh cached Nesterov gradients after the objective's weights changed."""
    if not hasattr(optimizer, 'obj_and_grad_fn'):
        return
    for group in optimizer.param_groups:
        for key, gkey, fkey in (('v_k', 'g_k', 'obj_k'), ('v_k_1', 'g_k_1', 'obj_k_1')):
            for i, pos in enumerate(group[key]):
                obj, grad = optimizer.obj_and_grad_fn(pos)
                if i < len(group[gkey]):  # BB variant recomputes gradients each step.
                    group[gkey][i].copy_(grad.detach())
                group[fkey][i].copy_(obj.detach())
