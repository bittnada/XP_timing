"""Stable checkpoint selection for timing-driven global placement.

The optimizer's net weights are intentionally not used here: changing weights
would make HPWL values from different timing updates incomparable.
"""
import logging
import re

import numpy as np


DEFAULT_CONFIG = {
    'enabled': 1,
    'overflow': {
        'hard_limit': 0.0,
        'absolute_tolerance': 0.01,
        'relative_tolerance': 0.10,
        'lock_at_stop': 1,
    },
    'hpwl': {
        'max_net_degree': 0,
        'exclude_net_regex': (
            r'(?i)^(?:MODCSA|CriticalPathNet)|'
            r'(?:^|[/_.])(?:clk|clock)(?:$|[/_.0-9])'),
    },
    'score': {
        'weights': {'wns': 0.4, 'tns': 0.3, 'hpwl': 0.2, 'overflow': 0.1},
        'minimum_improvement': 0.001,
    },
}

_LEGACY_KEYS = (
    'best_checkpoint_flag', 'best_checkpoint_overflow',
    'best_checkpoint_max_net_degree', 'best_checkpoint_exclude_net_regex',
    'best_checkpoint_wns_weight', 'best_checkpoint_tns_weight',
    'best_checkpoint_hpwl_weight', 'best_checkpoint_overflow_weight',
    'best_checkpoint_min_improvement',
    'best_checkpoint_overflow_abs_tolerance',
    'best_checkpoint_overflow_rel_tolerance',
    'best_checkpoint_overflow_tradeoff_score',
)


def _merge_config(defaults, supplied, path='best_checkpoint'):
    if not isinstance(supplied, dict):
        raise ValueError('%s must be a JSON object' % path)
    unknown = sorted(set(supplied) - set(defaults))
    if unknown:
        raise ValueError('%s contains unknown keys: %s' % (path, ', '.join(unknown)))
    result = {}
    for key, default in defaults.items():
        value = supplied.get(key, default)
        if isinstance(default, dict):
            result[key] = _merge_config(default, value, path + '.' + key)
        else:
            result[key] = value
    return result


def checkpoint_config(params):
    """Return validated grouped configuration with nested defaults applied."""
    legacy = [key for key in _LEGACY_KEYS if hasattr(params, key)]
    if legacy:
        raise ValueError('Legacy best-checkpoint parameters are no longer supported; '
                         'move them under the best_checkpoint group: ' + ', '.join(legacy))
    return _merge_config(DEFAULT_CONFIG, getattr(params, 'best_checkpoint', {}))


def _text(value):
    return value.decode() if isinstance(value, bytes) else str(value)


def filtered_net_mask(params, db):
    """Return a stable unweighted-HPWL mask and exclusion statistics."""
    config = checkpoint_config(params)['hpwl']
    degree = np.diff(np.asarray(db.flat_net2pin_start_map))
    limit = int(config['max_net_degree'] or getattr(params, 'ignore_net_degree', 100))
    if limit < 2:
        raise ValueError('best_checkpoint.hpwl.max_net_degree/ignore_net_degree '
                         'must be at least 2')
    names = np.asarray([_text(v) for v in db.net_names])
    uses = np.asarray([_text(v).replace('USE ', '').strip().upper()
                       for v in getattr(db, 'net_uses', ['SIGNAL'] * len(names))])
    if len(uses) != len(names):
        raise ValueError('net_uses length differs from net_names')
    placement_only = set(map(_text, getattr(getattr(getattr(db, 'rawdb', None), 'data', None),
                                            'placement_only_nets', [])))
    pattern_text = str(config['exclude_net_regex'] or '')
    try:
        pattern = re.compile(pattern_text) if pattern_text else None
    except re.error as exc:
        raise ValueError('Invalid best_checkpoint.hpwl.exclude_net_regex: ' + str(exc)) from exc
    valid_degree = (degree >= 2) & (degree < limit)
    clock = uses == 'CLOCK'
    synthetic = np.asarray([name in placement_only for name in names])
    regex = np.asarray([bool(pattern.search(name)) if pattern else False for name in names])
    mask = valid_degree & ~clock & ~synthetic & ~regex
    stats = dict(total=len(names), included=int(mask.sum()), invalid_degree=int((~valid_degree).sum()),
                 clock=int(clock.sum()), placement_only=int(synthetic.sum()), regex=int(regex.sum()),
                 max_degree_exclusive=limit)
    return mask, stats


class Tracker:
    """Keep aligned density and timing-quality checkpoints for one GP stage."""

    def __init__(self, params):
        config = checkpoint_config(params)
        overflow_config = config['overflow']
        score_config = config['score']
        self.enabled = bool(config['enabled'])
        self.timing_enabled = bool(getattr(params, 'timing_opt_flag', 0))
        requested = float(overflow_config['hard_limit'])
        self.overflow_limit = requested if requested > 0 else max(
            2.0 * float(params.stop_overflow), float(params.stop_overflow) + 0.05)
        score_weights = score_config['weights']
        self.weights = np.asarray([
            float(score_weights['wns']), float(score_weights['tns']),
            float(score_weights['hpwl']), float(score_weights['overflow']),
        ])
        if np.any(self.weights < 0) or not self.weights.sum() > 0:
            raise ValueError('best checkpoint weights must be nonnegative with a positive sum')
        self.weights /= self.weights.sum()
        self.min_improvement = float(score_config['minimum_improvement'])
        if not 0 <= self.min_improvement < 1:
            raise ValueError('best_checkpoint.score.minimum_improvement must be in [0, 1)')
        self.overflow_abs_tolerance = float(overflow_config['absolute_tolerance'])
        self.overflow_rel_tolerance = float(overflow_config['relative_tolerance'])
        self.lock_at_stop = bool(overflow_config['lock_at_stop'])
        if self.overflow_abs_tolerance < 0 or self.overflow_rel_tolerance < 0:
            raise ValueError('best checkpoint overflow tolerances must be nonnegative')
        self.stop_overflow = float(params.stop_overflow)
        self.density_overflow = None
        self.density_pos = None
        self.quality = None
        self.quality_pos = None
        self.reference = None
        self.overflow_floor = None

    @staticmethod
    def _number(value):
        return float(value.item() if hasattr(value, 'item') else value)

    def update_density(self, overflow, pos):
        value = self._number(overflow)
        if np.isfinite(value) and (self.density_overflow is None or value < self.density_overflow):
            self.density_overflow = value
            self.density_pos = pos.detach().clone()
            return True
        return False

    def update_quality(self, wns_ps, tns_ps, hpwl, overflow, pos, iteration):
        """Update using metrics all evaluated at ``pos``; lower score is better."""
        values = np.asarray([max(0.0, -float(wns_ps)), max(0.0, -float(tns_ps)),
                             float(hpwl), float(overflow)], dtype=np.float64)
        if not self.enabled or not np.isfinite(values).all() or values[3] > self.overflow_limit:
            return False

        # Keep the physical constraint anchored to the best overflow ever
        # observed at a feasible timing checkpoint.  It is deliberately not
        # anchored to the last accepted quality position: otherwise repeated
        # tolerance-sized updates could accumulate into unbounded density
        # regression.
        if self.overflow_floor is None or values[3] < self.overflow_floor:
            self.overflow_floor = values[3]
        tolerance = max(self.overflow_abs_tolerance,
                        self.overflow_rel_tolerance * self.overflow_floor)
        allowed_overflow = min(self.overflow_limit, self.overflow_floor + tolerance)
        if self.lock_at_stop and self.overflow_floor <= self.stop_overflow:
            allowed_overflow = min(allowed_overflow, self.stop_overflow)
        epsilon = max(1e-12, abs(allowed_overflow) * 1e-9)
        if values[3] > allowed_overflow + epsilon:
            logging.info('Reject checkpoint at iter=%d: overflow %.9g exceeds '
                         'floor-based limit %.9g (floor=%.9g tolerance=%.9g)',
                         iteration, values[3], allowed_overflow,
                         self.overflow_floor, tolerance)
            return False

        if self.reference is None:
            self.reference = np.maximum(values, np.asarray([1.0, 1.0, 1.0, 1e-6]))
        normalized = values / self.reference
        score = float(np.dot(self.weights, normalized))
        # Overflow is already a normalized score component.  Do not charge it
        # again by increasing the score-improvement threshold.
        required_improvement = self.min_improvement
        if self.quality is not None:
            old = self.quality['score']
            threshold = old * (1.0 - required_improvement) if old > 0 else old
            if score >= threshold:
                logging.info('Reject checkpoint at iter=%d: score %.6g does not improve '
                             '%.3f%% (overflow floor=%.9g candidate=%.9g)',
                             iteration, score, 100.0 * required_improvement,
                             self.overflow_floor, values[3])
                return False
        self.quality = dict(score=score, iteration=int(iteration), wns_ps=float(wns_ps),
                            tns_ps=float(tns_ps), filtered_unweighted_hpwl=float(hpwl),
                            overflow=float(overflow),
                            overflow_floor_at_selection=float(self.overflow_floor),
                            overflow_tolerance=float(tolerance),
                            required_score_improvement=float(required_improvement))
        self.quality_pos = pos.detach().clone()
        logging.info('Best-quality checkpoint: iter=%d score=%.6g WNS=%.3f ps '
                     'TNS=%.3f ps filtered-uHPWL=%.6g overflow=%.6g',
                     iteration, score, wns_ps, tns_ps, hpwl, overflow)
        return True

    def selected(self):
        if not self.timing_enabled:
            return None, None, None
        if self.enabled and self.quality_pos is not None:
            return self.quality_pos, 'quality', self.quality
        if self.enabled and self.density_pos is not None:
            return self.density_pos, 'density', {'overflow': self.density_overflow}
        return None, None, None
