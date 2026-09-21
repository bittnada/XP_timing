from types import SimpleNamespace
import copy
import unittest

import numpy as np
import torch

import BestPlacement


class BestPlacementTest(unittest.TestCase):
    def params(self, best_checkpoint=None, **kwargs):
        config = copy.deepcopy(BestPlacement.DEFAULT_CONFIG)
        config['hpwl']['max_net_degree'] = 0
        config['hpwl']['exclude_net_regex'] = r'(?i)^MODCSA|clk'

        def merge(target, source):
            for key, value in source.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)
                else:
                    target[key] = value

        if best_checkpoint is not None:
            merge(config, best_checkpoint)
        values = dict(ignore_net_degree=4, timing_opt_flag=1, stop_overflow=.1,
                      best_checkpoint=config)
        values.update(kwargs)
        return SimpleNamespace(**values)

    def test_filtered_mask_excludes_synthetic_clock_large_and_invalid_nets(self):
        names = ['data', 'MODCSA_x', 'clock_by_use', 'my_clk_net',
                 'placement_helper', 'degree4', 'degree1']
        degrees = np.array([2, 2, 2, 2, 2, 4, 1])
        db = SimpleNamespace(
            net_names=np.asarray(names), net_uses=np.asarray(
                ['SIGNAL', 'SIGNAL', 'CLOCK', 'SIGNAL', 'SIGNAL', 'SIGNAL', 'SIGNAL']),
            flat_net2pin_start_map=np.r_[0, np.cumsum(degrees)],
            rawdb=SimpleNamespace(data=SimpleNamespace(
                placement_only_nets=['placement_helper'])))
        mask, stats = BestPlacement.filtered_net_mask(self.params(), db)
        np.testing.assert_array_equal(mask, [1, 0, 0, 0, 0, 0, 0])
        self.assertEqual(stats['included'], 1)
        self.assertEqual(stats['clock'], 1)
        self.assertEqual(stats['placement_only'], 1)

    def test_quality_requires_feasibility_and_improves_normalized_score(self):
        tracker = BestPlacement.Tracker(self.params())
        a = torch.tensor([1., 2.])
        b = torch.tensor([3., 4.])
        self.assertFalse(tracker.update_quality(-10, -100, 1000, .21, a, 1))
        self.assertTrue(tracker.update_quality(-10, -100, 1000, .2, a, 2))
        self.assertFalse(tracker.update_quality(-11, -110, 1100, .2, b, 3))
        self.assertTrue(tracker.update_quality(-5, -50, 900, .1, b, 4))
        pos, kind, metrics = tracker.selected()
        torch.testing.assert_close(pos, b)
        self.assertEqual(kind, 'quality')
        self.assertEqual(metrics['iteration'], 4)

    def test_small_overflow_regression_can_win_on_score(self):
        tracker = BestPlacement.Tracker(self.params())
        old = torch.tensor([1.])
        tempting = torch.tensor([2.])
        self.assertTrue(tracker.update_quality(-100, -1000, 1000, .15, old, 1))
        # Floor=.15 and tolerance=.015.  Overflow is already in the score, so
        # no second degradation-proportional improvement penalty is charged.
        self.assertTrue(tracker.update_quality(-1, -1, 1, .155, tempting, 2))
        pos, kind, metrics = tracker.selected()
        torch.testing.assert_close(pos, tempting)
        self.assertEqual(kind, 'quality')
        self.assertEqual(metrics['overflow'], .155)
        self.assertAlmostEqual(tracker.overflow_floor, .15)

    def test_small_overflow_regression_rejects_insufficient_score_gain(self):
        tracker = BestPlacement.Tracker(self.params())
        old = torch.tensor([1.])
        candidate = torch.tensor([2.])
        self.assertTrue(tracker.update_quality(-100, -1000, 1000, .15, old, 1))
        # Unchanged timing/HPWL plus worse overflow makes the score worse.
        self.assertFalse(tracker.update_quality(-100, -1000, 1000, .155, candidate, 2))
        torch.testing.assert_close(tracker.selected()[0], old)

    def test_overflow_is_not_double_penalized(self):
        tracker = BestPlacement.Tracker(self.params())
        old = torch.tensor([1.])
        candidate = torch.tensor([2.])
        self.assertTrue(tracker.update_quality(-100, -1000, 1000, .15, old, 1))
        # This score improves just over the common 0.1% threshold after its
        # overflow score penalty.  No additional overflow surcharge applies.
        self.assertTrue(tracker.update_quality(-99.4, -994, 994, .155, candidate, 2))
        self.assertEqual(tracker.selected()[2]['required_score_improvement'], .001)

    def test_overflow_tolerance_is_anchored_and_does_not_drift(self):
        tracker = BestPlacement.Tracker(self.params())
        first = torch.tensor([1.])
        accepted = torch.tensor([2.])
        drift = torch.tensor([3.])
        self.assertTrue(tracker.update_quality(-100, -1000, 1000, .15, first, 1))
        self.assertTrue(tracker.update_quality(-10, -10, 10, .16, accepted, 2))
        # The cap remains .165 from the historical .15 floor; it does not
        # move merely because .16 was accepted.
        self.assertFalse(tracker.update_quality(-1, -1, 1, .166, drift, 3))
        torch.testing.assert_close(tracker.selected()[0], accepted)

    def test_stop_overflow_becomes_cap_after_floor_reaches_target(self):
        tracker = BestPlacement.Tracker(self.params())
        at_target = torch.tensor([1.])
        regressed = torch.tensor([2.])
        self.assertTrue(tracker.update_quality(-100, -1000, 1000, .099,
                                               at_target, 1))
        self.assertFalse(tracker.update_quality(-1, -1, 1, .101,
                                                regressed, 2))
        torch.testing.assert_close(tracker.selected()[0], at_target)

    def test_zero_tolerance_reproduces_strict_monotonic_overflow(self):
        tracker = BestPlacement.Tracker(self.params(best_checkpoint={
            'overflow': {'absolute_tolerance': 0, 'relative_tolerance': 0}}))
        old = torch.tensor([1.])
        tempting = torch.tensor([2.])
        self.assertTrue(tracker.update_quality(-100, -1000, 1000, .15, old, 1))
        self.assertFalse(tracker.update_quality(-1, -1, 1, .1501, tempting, 2))
        torch.testing.assert_close(tracker.selected()[0], old)

    def test_partial_group_uses_defaults_and_rejects_unknown_keys(self):
        tracker = BestPlacement.Tracker(self.params(best_checkpoint={
            'overflow': {'hard_limit': .25}}))
        self.assertEqual(tracker.overflow_limit, .25)
        self.assertEqual(tracker.overflow_abs_tolerance, .01)
        with self.assertRaisesRegex(ValueError, 'unknown keys'):
            BestPlacement.Tracker(self.params(best_checkpoint={
                'overflow': {'absolute_tolerence': .01}}))

    def test_legacy_individual_keys_are_rejected(self):
        params = self.params()
        params.best_checkpoint_flag = 1
        with self.assertRaisesRegex(ValueError, 'Legacy best-checkpoint'):
            BestPlacement.Tracker(params)

    def test_density_is_fallback_only_for_timing_flow(self):
        tracker = BestPlacement.Tracker(self.params())
        tracker.update_density(.4, torch.tensor([1.]))
        self.assertEqual(tracker.selected()[1], 'density')
        tracker = BestPlacement.Tracker(self.params(timing_opt_flag=0))
        tracker.update_density(.1, torch.tensor([1.]))
        self.assertEqual(tracker.selected(), (None, None, None))


if __name__ == '__main__':
    unittest.main()
