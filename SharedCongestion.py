"""Verbatim congestion methods from XP_shared_memory/bin/dreamplace/PlaceDB.py.

Copied 2026-09-17. Do not edit the numerical implementation here.
FinalPlacementMetrics.py adapts the current database to this original interface.
The local class name intentionally stays PlaceDB for original static calls.
"""
import math
import time
import numpy as np
import torch
from numba import njit


class PlaceDB:
    def _node_type_ranges_for_congestion(self):
        """Return node index ranges for the explicit node ordering."""
        movable_std_start = 0
        movable_std_end = movable_std_start + self.num_movable_std_cell
        movable_macro_start = self.num_movable_std_cell
        movable_macro_end = movable_macro_start + self.num_movable_macro
        fixed_macro_start = movable_macro_end + self.num_fixed_std_cell
        fixed_macro_end = fixed_macro_start + self.num_fixed_macro
        blockage_start = fixed_macro_end
        blockage_end = blockage_start + self.num_blockage
        pin_start = self.num_physical_nodes - self.num_terminal_NIs
        if blockage_end != pin_start:
            raise ValueError(
                "node type counts do not match node ordering: blockage_end={}, pin_start={}".format(
                    blockage_end,
                    pin_start,
                )
            )
        return {
            "movable_std_cell": (movable_std_start, movable_std_end),
            "movable_macro": (movable_macro_start, movable_macro_end),
            "fixed_std_cell": (movable_macro_end, fixed_macro_start),
            "fixed_macro": (fixed_macro_start, fixed_macro_end),
            "blockage": (blockage_start, blockage_end),
            "pin": (pin_start, self.num_physical_nodes),
        }

    def _get_congestion_weight(self, attr_name, default_value):
        params = getattr(self, "params", None)
        if params is not None and hasattr(params, attr_name):
            return float(getattr(params, attr_name))
        return float(getattr(self, attr_name, default_value))

    @staticmethod
    def _clip_bin_indices(xl, xh, yl, yh, ncols, nrows):
        xl = max(0, int(xl))
        xh = min(int(xh), ncols - 1)
        yl = max(0, int(yl))
        yh = min(int(yh), nrows - 1)
        return xl, xh, yl, yh

    @staticmethod
    def _build_rect_bin_mask(nrows, ncols, rects, site_width, row_height):
        mask = np.zeros((nrows, ncols), dtype=np.bool_)
        for rect in rects:
            if len(rect) < 4:
                continue
            xl, yl, xh, yh = [float(v) for v in rect[:4]]
            bin_xl = math.floor(xl / site_width)
            bin_xh = math.ceil(xh / site_width)
            bin_yl = math.floor(yl / row_height)
            bin_yh = math.ceil(yh / row_height)
            bin_xl, bin_xh, bin_yl, bin_yh = PlaceDB._clip_bin_indices(
                bin_xl, bin_xh, bin_yl, bin_yh, ncols, nrows
            )
            if bin_xl <= bin_xh and bin_yl <= bin_yh:
                mask[bin_yl:bin_yh + 1, bin_xl:bin_xh + 1] = True
        return mask

    @staticmethod
    def _build_node_bin_mask(nrows, ncols, x, y, node_size_x, node_size_y, node_start, node_end, site_width, row_height):
        if node_start >= node_end:
            return np.zeros((nrows, ncols), dtype=np.bool_)

        node_x = x[node_start:node_end]
        node_y = y[node_start:node_end]
        size_x = node_size_x[node_start:node_end]
        size_y = node_size_y[node_start:node_end]
        bin_index_xl = np.maximum(np.floor(node_x / site_width).astype(np.int32), 0)
        bin_index_xh = np.minimum(np.ceil((node_x + size_x) / site_width).astype(np.int32), ncols - 1)
        bin_index_yl = np.maximum(np.floor(node_y / row_height).astype(np.int32), 0)
        bin_index_yh = np.minimum(np.ceil((node_y + size_y) / row_height).astype(np.int32), nrows - 1)
        return PlaceDB._build_macro_bin_mask(
            nrows,
            ncols,
            bin_index_xl,
            bin_index_xh,
            bin_index_yl,
            bin_index_yh,
        )

    def _blockage_rects_by_type(self, blockage_type):
        rects = []
        blockage_info = getattr(self, "blockageInfo", {})
        if not isinstance(blockage_info, dict):
            return rects

        for blockage in blockage_info.values():
            if not isinstance(blockage, dict):
                continue
            if str(blockage.get("type", "")).upper() != blockage_type:
                continue
            coords = blockage.get("coords", [])
            if len(coords) == 4:
                rects.append(coords)
            elif len(coords) > 4:
                xs = [float(v) for v in coords[0::2]]
                ys = [float(v) for v in coords[1::2]]
                rects.append([min(xs), min(ys), max(xs), max(ys)])
        return rects

    def _get_static_congestion_bin_masks(self, x, y, node_size_x, node_size_y, site_width, row_height,
                                         congestionMapRowSize, congestionMapColumnSize, node_type_ranges):
        cache_key = (
            congestionMapColumnSize,
            congestionMapRowSize,
            float(site_width),
            float(row_height),
        )
        cached = self._static_congestion_bin_masks.get(cache_key)
        if cached is not None:
            return cached

        placement_blockage_mask = PlaceDB._build_rect_bin_mask(
            congestionMapColumnSize,
            congestionMapRowSize,
            self._blockage_rects_by_type("PLACEMENT"),
            site_width,
            row_height,
        )
        routing_blockage_mask = PlaceDB._build_rect_bin_mask(
            congestionMapColumnSize,
            congestionMapRowSize,
            self._blockage_rects_by_type("LAYER"),
            site_width,
            row_height,
        )

        blockage_start, blockage_end = node_type_ranges["blockage"]
        placement_blockage_mask |= PlaceDB._build_node_bin_mask(
            congestionMapColumnSize,
            congestionMapRowSize,
            x,
            y,
            node_size_x,
            node_size_y,
            blockage_start,
            blockage_end,
            site_width,
            row_height,
        )

        cached = {
            "placement_blockage": placement_blockage_mask,
            "routing_blockage": routing_blockage_mask,
        }
        self._static_congestion_bin_masks[cache_key] = cached
        return cached

    def _build_congestion_bin_masks(self, x, y, node_size_x, node_size_y, site_width, row_height,
                                    congestionMapRowSize, congestionMapColumnSize):
        node_type_ranges = self._node_type_ranges_for_congestion()
        macro_bin_mask = np.zeros((congestionMapColumnSize, congestionMapRowSize), dtype=np.bool_)
        macro_bin_mask |= PlaceDB._build_node_bin_mask(
            congestionMapColumnSize,
            congestionMapRowSize,
            x,
            y,
            node_size_x,
            node_size_y,
            *node_type_ranges["movable_macro"],
            site_width,
            row_height,
        )
        macro_bin_mask |= PlaceDB._build_node_bin_mask(
            congestionMapColumnSize,
            congestionMapRowSize,
            x,
            y,
            node_size_x,
            node_size_y,
            *node_type_ranges["fixed_macro"],
            site_width,
            row_height,
        )
        # convert_macro2port removes fixed macros from the node list, but they still
        # block routing. Reinstate their footprint from plot-only geometry.
        plot_fixed = getattr(self, "plot_fixed_macros", None)
        if plot_fixed is not None and len(plot_fixed):
            plot_fixed = np.asarray(plot_fixed, dtype=np.float64).reshape(-1, 4)
            fixed_macro_rects = [
                [float(xx), float(yy), float(xx + ww), float(yy + hh)]
                for xx, yy, ww, hh in plot_fixed
                if ww > 0.0 and hh > 0.0
            ]
            if fixed_macro_rects:
                macro_bin_mask |= PlaceDB._build_rect_bin_mask(
                    congestionMapColumnSize,
                    congestionMapRowSize,
                    fixed_macro_rects,
                    site_width,
                    row_height,
                )

        static_masks = self._get_static_congestion_bin_masks(
            x,
            y,
            node_size_x,
            node_size_y,
            site_width,
            row_height,
            congestionMapRowSize,
            congestionMapColumnSize,
            node_type_ranges,
        )
        placement_blockage_mask = static_masks["placement_blockage"]
        routing_blockage_mask = static_masks["routing_blockage"]

        return macro_bin_mask, placement_blockage_mask, routing_blockage_mask

    def _build_congestion_bin_weight_maps(self, x, y, node_size_x, node_size_y, site_width, row_height,
                                          congestionMapRowSize, congestionMapColumnSize):
        macro_bin_mask, placement_blockage_mask, routing_blockage_mask = self._build_congestion_bin_masks(
            x,
            y,
            node_size_x,
            node_size_y,
            site_width,
            row_height,
            congestionMapRowSize,
            congestionMapColumnSize,
        )

        weight_map = np.ones((congestionMapColumnSize, congestionMapRowSize), dtype=np.float32)
        weight_map[placement_blockage_mask] *= self._get_congestion_weight("congestion_placement_blockage_weight", 2.0)
        weight_map[routing_blockage_mask] *= self._get_congestion_weight("congestion_routing_blockage_weight", 2.0)
        weight_map[macro_bin_mask] *= self._get_congestion_weight("congestion_macro_weight", 2.0)

        return weight_map, macro_bin_mask, placement_blockage_mask, routing_blockage_mask

    def calc_congestion_map_cuda(self, x, y, site_width, row_height, return_stats=False):
        """
        Calculate final congestion with the selected calculation method.

        The final congestion model is selected by params.congestion_calculation_method.
        Both methods are kept on the Numba path so CPU/GPU callers see the same
        congestion values for a selected method.
        """
        method = self._get_congestion_calculation_method()
        print(f"[calc_congestion_map_cuda] {method} congestion uses calc_congestion_numba for CPU/GPU consistency")
        return self.calc_congestion_numba(x, y, site_width, row_height, return_stats=return_stats)

    def _get_congestion_calculation_method(self):
        params = getattr(self, "params", None)
        method = "rudy_pin_bbox"
        if params is not None and hasattr(params, "congestion_calculation_method"):
            method = str(params.congestion_calculation_method)
        method = method.strip().lower()
        aliases = {
            "rudy": "rudy_pin_bbox",
            "pin_bbox": "rudy_pin_bbox",
            "rudy_pin": "rudy_pin_bbox",
            "legacy_difference_map": "legacy",
            "difference_map": "legacy",
            "cell_bbox": "legacy",
            "old": "legacy",
        }
        method = aliases.get(method, method)
        if method not in {"legacy", "rudy_pin_bbox"}:
            raise ValueError(
                "Unsupported congestion_calculation_method '{}'. Use 'legacy' or 'rudy_pin_bbox'".format(method)
            )
        return method

    def _get_congestion_rudy_exact_max_bbox_bins(self):
        params = getattr(self, "params", None)
        if params is not None and hasattr(params, "congestion_rudy_exact_max_bbox_bins"):
            return int(params.congestion_rudy_exact_max_bbox_bins)
        return 4096

    def _normalize_congestion_rudy_stat_source(self, mode):
        """Normalize source of RUDY max/sum: h, v, h_plus_v, or hv_max."""
        mode = str(mode).strip().lower()
        aliases = {
            "horizontal": "h",
            "horiz": "h",
            "vertical": "v",
            "vert": "v",
            "sum": "h_plus_v",
            "hv": "h_plus_v",
            "h+v": "h_plus_v",
            "add": "h_plus_v",
            "max": "hv_max",
            "maximum": "hv_max",
            "hvmax": "hv_max",
        }
        mode = aliases.get(mode, mode)
        if mode not in {"h", "v", "h_plus_v", "hv_max"}:
            raise ValueError(
                "Unsupported congestion RUDY stat source '{}'. "
                "Use 'h', 'v', 'h_plus_v', or 'hv_max'".format(mode)
            )
        return mode

    @staticmethod
    def _describe_congestion_rudy_stat_source(source):
        """Human-readable description of how the congestion map bin is formed."""
        descriptions = {
            "h": "horizontal map (H only)",
            "v": "vertical map (V only)",
            "h_plus_v": "H+V map (per-bin H+V)",
            "hv_max": "hv_max map (per-bin max(H,V))",
        }
        return descriptions.get(source, source)

    def _get_congestion_rudy_max_source(self):
        """Which RUDY map provides reported max congestion (default: hv_max)."""
        params = getattr(self, "params", None)
        mode = "hv_max"
        if params is not None and hasattr(params, "congestion_rudy_max_source"):
            mode = params.congestion_rudy_max_source
        elif params is not None and hasattr(params, "congestion_rudy_aggregate"):
            # Backward compatible with the previous single aggregate knob.
            mode = params.congestion_rudy_aggregate
        return self._normalize_congestion_rudy_stat_source(mode)

    def _get_congestion_rudy_sum_source(self):
        """Which RUDY map provides reported total congestion (default: hv_max)."""
        params = getattr(self, "params", None)
        mode = "hv_max"
        if params is not None and hasattr(params, "congestion_rudy_sum_source"):
            mode = params.congestion_rudy_sum_source
        elif params is not None and hasattr(params, "congestion_rudy_aggregate"):
            mode = params.congestion_rudy_aggregate
        return self._normalize_congestion_rudy_stat_source(mode)

    @staticmethod
    def _build_rudy_source_map(horizontal_map, vertical_map, source, out=None):
        """Build (or reuse) the map selected by a RUDY stat source."""
        if source == "h":
            return horizontal_map
        if source == "v":
            return vertical_map
        if source == "h_plus_v":
            if out is None:
                return horizontal_map + vertical_map
            np.add(horizontal_map, vertical_map, out=out)
            return out
        if source == "hv_max":
            if out is None:
                return np.maximum(horizontal_map, vertical_map)
            np.maximum(horizontal_map, vertical_map, out=out)
            return out
        raise ValueError("Unsupported congestion RUDY stat source '{}'".format(source))

    @staticmethod
    def _rudy_map_max(horizontal_map, vertical_map, source):
        if source == "h":
            return float(np.max(horizontal_map))
        if source == "v":
            return float(np.max(vertical_map))
        if source == "h_plus_v":
            # max(H+V) needs per-bin H+V; no need to keep the map permanently.
            return float(np.max(horizontal_map + vertical_map))
        if source == "hv_max":
            return float(np.max(np.maximum(horizontal_map, vertical_map)))
        raise ValueError("Unsupported congestion RUDY stat source '{}'".format(source))

    @staticmethod
    def _rudy_map_sum(horizontal_map, vertical_map, source):
        if source == "h":
            return float(np.sum(horizontal_map))
        if source == "v":
            return float(np.sum(vertical_map))
        if source == "h_plus_v":
            # sum(H+V) == sum(H)+sum(V); no extra map needed.
            return float(np.sum(horizontal_map) + np.sum(vertical_map))
        if source == "hv_max":
            return float(np.sum(np.maximum(horizontal_map, vertical_map)))
        raise ValueError("Unsupported congestion RUDY stat source '{}'".format(source))

    @staticmethod
    def _rudy_macro_max(horizontal_map, vertical_map, macro_bin_mask, source):
        if not np.any(macro_bin_mask):
            return 0.0
        h = horizontal_map[macro_bin_mask]
        v = vertical_map[macro_bin_mask]
        return PlaceDB._rudy_map_max(h, v, source)

    @staticmethod
    def _rudy_macro_sum(horizontal_map, vertical_map, macro_bin_mask, source):
        if not np.any(macro_bin_mask):
            return 0.0
        h = horizontal_map[macro_bin_mask]
        v = vertical_map[macro_bin_mask]
        return PlaceDB._rudy_map_sum(h, v, source)

    def _build_congestion_net_exclude_mask(self):
        """True marks nets excluded from final congestion (e.g. MODCSA)."""
        num_nets = self.num_nets
        exclude_mask = np.zeros(num_nets, dtype=np.bool_)
        if self.net_names is None:
            return exclude_mask
        for net_idx in range(num_nets):
            net_name = self.net_names[net_idx]
            if isinstance(net_name, bytes):
                net_name = net_name.decode()
            if str(net_name).startswith("MODCSA"):
                exclude_mask[net_idx] = True
        excluded = int(np.sum(exclude_mask))
        if excluded > 0:
            print("[calc_congestion] excluding {} MODCSA nets from congestion".format(excluded))
        return exclude_mask

    @staticmethod
    def _build_macro_bin_mask(nrows, ncols, macro_bin_index_xl, macro_bin_index_xh, macro_bin_index_yl, macro_bin_index_yh):
        macro_bin_mask = np.zeros((nrows, ncols), dtype=np.bool_)
        for i in range(len(macro_bin_index_xl)):
            xl = int(macro_bin_index_xl[i])
            xh = int(macro_bin_index_xh[i])
            yl = int(macro_bin_index_yl[i])
            yh = int(macro_bin_index_yh[i])
            macro_bin_mask[yl:yh + 1, xl:xh + 1] = True
        return macro_bin_mask

    @staticmethod
    @njit(fastmath=True)
    def _calc_legacy_congestion_numba_core(diff_congestion_map, netInfoCellIdxList, netInfoCellIdxList_startIdx,
                                           bin_index_xl, bin_index_xh, bin_index_yl, bin_index_yh,
                                           flat_net2pin_start_map, num_net_pins, net_exclude_mask,
                                           congestionMapRowSize, congestionMapColumnSize):
        """
        Legacy final congestion core using cell bboxes and a 2D difference map.
        Each net contributes num_pins / bbox_bin_area to all bins in its cell bbox.
        """
        num_nets = len(netInfoCellIdxList_startIdx)

        for net_idx in range(num_nets):
            if len(net_exclude_mask) > net_idx and net_exclude_mask[net_idx]:
                continue

            idx_start = netInfoCellIdxList_startIdx[net_idx]
            if net_idx == num_nets - 1:
                idx_end = len(netInfoCellIdxList)
            else:
                idx_end = netInfoCellIdxList_startIdx[net_idx + 1]

            if idx_start >= idx_end:
                continue

            pin_start = flat_net2pin_start_map[net_idx]
            pin_end = flat_net2pin_start_map[net_idx + 1]
            num_pins = pin_end - pin_start
            if num_pins <= 0:
                continue

            first_cell_idx = netInfoCellIdxList[idx_start]
            xl_min = bin_index_xl[first_cell_idx]
            xh_max = bin_index_xh[first_cell_idx]
            yl_min = bin_index_yl[first_cell_idx]
            yh_max = bin_index_yh[first_cell_idx]

            for i in range(idx_start + 1, idx_end):
                cell_idx = netInfoCellIdxList[i]
                xl = bin_index_xl[cell_idx]
                xh = bin_index_xh[cell_idx]
                yl = bin_index_yl[cell_idx]
                yh = bin_index_yh[cell_idx]
                if xl < xl_min:
                    xl_min = xl
                if xh > xh_max:
                    xh_max = xh
                if yl < yl_min:
                    yl_min = yl
                if yh > yh_max:
                    yh_max = yh

            if xl_min < 0:
                xl_min = 0
            if yl_min < 0:
                yl_min = 0
            if xh_max >= congestionMapRowSize:
                xh_max = congestionMapRowSize - 1
            if yh_max >= congestionMapColumnSize:
                yh_max = congestionMapColumnSize - 1
            if xl_min > xh_max or yl_min > yh_max:
                continue

            num_bins_x = xh_max - xl_min + 1
            num_bins_y = yh_max - yl_min + 1
            bbox_area = num_bins_x * num_bins_y
            if bbox_area <= 0:
                continue
            congestion_value = float(num_pins) / float(bbox_area)

            yl = yl_min
            yh = yh_max + 1
            xl = xl_min
            xh = xh_max + 1
            diff_congestion_map[yl, xl] += congestion_value
            diff_congestion_map[yl, xh] -= congestion_value
            diff_congestion_map[yh, xl] -= congestion_value
            diff_congestion_map[yh, xh] += congestion_value

        return diff_congestion_map

    @staticmethod
    @njit(fastmath=True)
    def _calc_rudy_congestion_numba_core(horizontal_demand_map, vertical_demand_map,
                                         horizontal_diff_map, vertical_diff_map,
                                         approximate_net_count,
                                         x, y, pin_offset_x, pin_offset_y, pin2node_map,
                                         flat_net2pin_map, flat_net2pin_start_map, net_weights,
                                         net_exclude_mask,
                                         xl, yl, site_width, row_height,
                                         congestionMapRowSize, congestionMapColumnSize,
                                         num_nets, exact_max_bbox_bins):
        """
        RUDY-style routing demand from pin bounding boxes.
        Small bboxes use exact overlap. Large bboxes use a rectangle difference-map
        approximation that preserves total horizontal/vertical demand.
        """
        for net_idx in range(num_nets):
            if len(net_exclude_mask) > net_idx and net_exclude_mask[net_idx]:
                continue

            pin_start = flat_net2pin_start_map[net_idx]
            pin_end = flat_net2pin_start_map[net_idx + 1]
            num_pins = pin_end - pin_start
            if num_pins <= 0:
                continue

            first_pin = flat_net2pin_map[pin_start]
            first_node = pin2node_map[first_pin]
            x_min = x[first_node] + pin_offset_x[first_pin]
            x_max = x_min
            y_min = y[first_node] + pin_offset_y[first_pin]
            y_max = y_min

            for i in range(pin_start + 1, pin_end):
                pin_id = flat_net2pin_map[i]
                node_id = pin2node_map[pin_id]
                pin_x = x[node_id] + pin_offset_x[pin_id]
                pin_y = y[node_id] + pin_offset_y[pin_id]
                if pin_x < x_min:
                    x_min = pin_x
                if pin_x > x_max:
                    x_max = pin_x
                if pin_y < y_min:
                    y_min = pin_y
                if pin_y > y_max:
                    y_max = pin_y

            bbox_width = x_max - x_min
            bbox_height = y_max - y_min
            if bbox_width <= 0.0 and bbox_height <= 0.0:
                continue
            if bbox_width <= 0.0:
                bbox_width = site_width
            if bbox_height <= 0.0:
                bbox_height = row_height

            bin_index_xl = int((x_min - xl) / site_width)
            bin_index_xh = int((x_max - xl) / site_width) + 1
            if bin_index_xl < 0:
                bin_index_xl = 0
            if bin_index_xh > congestionMapRowSize:
                bin_index_xh = congestionMapRowSize

            bin_index_yl = int((y_min - yl) / row_height)
            bin_index_yh = int((y_max - yl) / row_height) + 1
            if bin_index_yl < 0:
                bin_index_yl = 0
            if bin_index_yh > congestionMapColumnSize:
                bin_index_yh = congestionMapColumnSize

            if bin_index_xl >= bin_index_xh or bin_index_yl >= bin_index_yh:
                continue

            bbox_bin_count = (bin_index_xh - bin_index_xl) * (bin_index_yh - bin_index_yl)
            if bbox_bin_count <= 0:
                continue

            if num_pins <= 3:
                weight = 1.0000
            elif num_pins == 4:
                weight = 1.0828
            elif num_pins == 5:
                weight = 1.1536
            elif num_pins == 6:
                weight = 1.2206
            elif num_pins == 7:
                weight = 1.2823
            elif num_pins == 8:
                weight = 1.3385
            elif num_pins == 9:
                weight = 1.3991
            elif num_pins == 10:
                weight = 1.4493
            elif num_pins <= 15:
                weight = 1.6899
            elif num_pins <= 20:
                weight = 1.8924
            elif num_pins <= 25:
                weight = 2.0743
            elif num_pins <= 30:
                weight = 2.2334
            elif num_pins <= 35:
                weight = 2.3892
            elif num_pins <= 40:
                weight = 2.5356
            elif num_pins <= 45:
                weight = 2.6625
            else:
                weight = 2.7933
            if len(net_weights) > net_idx:
                weight *= net_weights[net_idx]

            if exact_max_bbox_bins > 0 and bbox_bin_count > exact_max_bbox_bins:
                horizontal_value = bbox_width * weight / float(bbox_bin_count)
                vertical_value = bbox_height * weight / float(bbox_bin_count)
                yl0 = bin_index_yl
                yh0 = bin_index_yh
                xl0 = bin_index_xl
                xh0 = bin_index_xh
                horizontal_diff_map[yl0, xl0] += horizontal_value
                horizontal_diff_map[yl0, xh0] -= horizontal_value
                horizontal_diff_map[yh0, xl0] -= horizontal_value
                horizontal_diff_map[yh0, xh0] += horizontal_value
                vertical_diff_map[yl0, xl0] += vertical_value
                vertical_diff_map[yl0, xh0] -= vertical_value
                vertical_diff_map[yh0, xl0] -= vertical_value
                vertical_diff_map[yh0, xh0] += vertical_value
                approximate_net_count[0] += 1
                continue

            for bx in range(bin_index_xl, bin_index_xh):
                bin_xl = xl + bx * site_width
                bin_xh = bin_xl + site_width
                overlap_x = min(x_max, bin_xh) - max(x_min, bin_xl)
                if overlap_x <= 0.0:
                    continue
                for by in range(bin_index_yl, bin_index_yh):
                    bin_yl = yl + by * row_height
                    bin_yh = bin_yl + row_height
                    overlap_y = min(y_max, bin_yh) - max(y_min, bin_yl)
                    if overlap_y <= 0.0:
                        continue
                    overlap = overlap_x * overlap_y * weight
                    horizontal_demand_map[by, bx] += overlap / bbox_height
                    vertical_demand_map[by, bx] += overlap / bbox_width

        return horizontal_demand_map, vertical_demand_map, horizontal_diff_map, vertical_diff_map, approximate_net_count

    def calc_congestion_numba(self, x, y, site_width, row_height, return_stats=False):
         method = self._get_congestion_calculation_method()
         if method == "legacy":
             return self._calc_legacy_congestion_numba(x, y, site_width, row_height, return_stats=return_stats)
         return self._calc_rudy_pin_bbox_congestion_numba(x, y, site_width, row_height, return_stats=return_stats)

    def _calc_legacy_congestion_numba(self, x, y, site_width, row_height, return_stats=False):
         """
         Calculate legacy final congestion using cell bboxes and a difference map.
         """
         start_time = time.time()

         site_width *= 1
         row_height *= 1
         congestionMapRowSize = int((self.xh - self.xl) / site_width)
         congestionMapColumnSize = int((self.yh - self.yl) / row_height)

         if torch.is_tensor(x):
             x = x.detach().cpu().numpy()
         if torch.is_tensor(y):
             y = y.detach().cpu().numpy()
         x = np.asarray(x, dtype=np.float32)
         y = np.asarray(y, dtype=np.float32)

         bin_index_xl = np.maximum(np.floor(x / site_width).astype(np.int32), 0)
         bin_index_xh = np.minimum(
             np.ceil((x + site_width) / site_width).astype(np.int32),
             congestionMapRowSize - 1,
         )
         bin_index_yl = np.maximum(np.floor(y / row_height).astype(np.int32), 0)
         bin_index_yh = np.minimum(
             np.ceil((y + row_height) / row_height).astype(np.int32),
             congestionMapColumnSize - 1,
         )

         node_size_x = np.asarray(self.node_size_x, dtype=np.float32)
         node_size_y = np.asarray(self.node_size_y, dtype=np.float32)
         weight_map, macro_bin_mask, _, _ = self._build_congestion_bin_weight_maps(
             x,
             y,
             node_size_x,
             node_size_y,
             site_width,
             row_height,
             congestionMapRowSize,
             congestionMapColumnSize,
         )

         diff_congestion_map = np.zeros((congestionMapColumnSize + 1, congestionMapRowSize + 1), dtype=np.float32)
         netInfoCellIdxList = np.array(self.netInfoCellIdxList, dtype=np.int32)
         netInfoCellIdxList_startIdx = np.array(self.netInfoCellIdxList_startIdx, dtype=np.int32)
         flat_net2pin_start_map = np.array(self.flat_net2pin_start_map, dtype=np.int32)
         net_exclude_mask = self._build_congestion_net_exclude_mask()

         num_net = self.num_nets
         assert num_net == len(netInfoCellIdxList_startIdx), print("[ERROR ] size difference")
         assert len(flat_net2pin_start_map) == num_net + 1, print("[ERROR ] flat_net2pin_start_map must have length num_nets + 1")

         diff_congestion_map = PlaceDB._calc_legacy_congestion_numba_core(
             diff_congestion_map,
             netInfoCellIdxList,
             netInfoCellIdxList_startIdx,
             np.array(bin_index_xl, dtype=np.int32),
             np.array(bin_index_xh, dtype=np.int32),
             np.array(bin_index_yl, dtype=np.int32),
             np.array(bin_index_yh, dtype=np.int32),
             flat_net2pin_start_map,
             np.int32(len(self.flat_net2pin_map)),
             net_exclude_mask,
             congestionMapRowSize,
             congestionMapColumnSize
         )

         np.cumsum(diff_congestion_map, axis=0, out=diff_congestion_map)
         np.cumsum(diff_congestion_map, axis=1, out=diff_congestion_map)
         base_congestion_map = diff_congestion_map[:congestionMapColumnSize, :congestionMapRowSize]
         weighted_congestion_map = base_congestion_map * weight_map

         if return_stats:
             max_congestion = float(np.max(weighted_congestion_map))
             total_sum_congestion = float(np.sum(weighted_congestion_map))
             if np.any(macro_bin_mask):
                 macro_values = weighted_congestion_map[macro_bin_mask]
                 max_macro_congestion = float(np.max(macro_values))
                 total_sum_macro_congestion = float(np.sum(macro_values))
             else:
                 max_macro_congestion = 0.0
                 total_sum_macro_congestion = 0.0

             end_time = time.time()
             print(f"[calc_congestion_numba] legacy difference-map stats calculation completed in {end_time - start_time:.4f} seconds")
             return max_congestion, total_sum_congestion, max_macro_congestion, total_sum_macro_congestion

         initial_macro_congestion_map = np.zeros_like(base_congestion_map)
         initial_macro_congestion_map[macro_bin_mask] = weighted_congestion_map[macro_bin_mask]
         initial_congestion_map = weighted_congestion_map.copy()

         end_time = time.time()
         print(f"[calc_congestion_numba] legacy difference-map calculation completed in {end_time - start_time:.4f} seconds")

         return initial_congestion_map, initial_macro_congestion_map

    def _calc_rudy_pin_bbox_congestion_numba(self, x, y, site_width, row_height, return_stats=False):
         """    
         Calculate RUDY-style pin-bbox congestion map using Numba.
         This method has the same signature as calc_congestion_map_cpu.

         Args:
             x: array of x coordinates
             y: array of y coordinates
             site_width: width of a site
             row_height: height of a row

         Returns:
             initial_congestion_map: 2D numpy array of congestion values
         """
         start_time = time.time()

         site_width *= 1
         row_height *= 1
         congestionMapRowSize = int((self.xh - self.xl) / site_width)
         congestionMapColumnSize = int((self.yh - self.yl) / row_height)

         if torch.is_tensor(x):
             x = x.detach().cpu().numpy()
         if torch.is_tensor(y):
             y = y.detach().cpu().numpy()
         x = np.asarray(x, dtype=np.float32)
         y = np.asarray(y, dtype=np.float32)

         node_size_x = np.asarray(self.node_size_x, dtype=np.float32)
         node_size_y = np.asarray(self.node_size_y, dtype=np.float32)
         weight_map, macro_bin_mask, _, _ = self._build_congestion_bin_weight_maps(
             x,
             y,
             node_size_x,
             node_size_y,
             site_width,
             row_height,
             congestionMapRowSize,
             congestionMapColumnSize,
         )

         horizontal_demand_map = np.zeros((congestionMapColumnSize, congestionMapRowSize), dtype=np.float32)
         vertical_demand_map = np.zeros((congestionMapColumnSize, congestionMapRowSize), dtype=np.float32)
         exact_max_bbox_bins = np.int32(self._get_congestion_rudy_exact_max_bbox_bins())
         if exact_max_bbox_bins > 0:
             horizontal_diff_map = np.zeros((congestionMapColumnSize + 1, congestionMapRowSize + 1), dtype=np.float32)
             vertical_diff_map = np.zeros((congestionMapColumnSize + 1, congestionMapRowSize + 1), dtype=np.float32)
         else:
             horizontal_diff_map = np.zeros((1, 1), dtype=np.float32)
             vertical_diff_map = np.zeros((1, 1), dtype=np.float32)
         approximate_net_count = np.zeros(1, dtype=np.int32)
         pin_offset_x = np.asarray(self.pin_offset_x, dtype=np.float32)
         pin_offset_y = np.asarray(self.pin_offset_y, dtype=np.float32)
         pin2node_map = np.asarray(self.pin2node_map, dtype=np.int32)
         flat_net2pin_map = np.asarray(self.flat_net2pin_map, dtype=np.int32)
         flat_net2pin_start_map = np.array(self.flat_net2pin_start_map, dtype=np.int32)
         if self.net_weights is None:
             net_weights = np.asarray([], dtype=np.float32)
         else:
             net_weights = np.asarray(self.net_weights, dtype=np.float32)
         net_exclude_mask = self._build_congestion_net_exclude_mask()

         num_net = self.num_nets
         assert len(flat_net2pin_start_map) == num_net + 1, print("[ERROR ] flat_net2pin_start_map must have length num_nets + 1")

         horizontal_demand_map, vertical_demand_map, horizontal_diff_map, vertical_diff_map, approximate_net_count = PlaceDB._calc_rudy_congestion_numba_core(
             horizontal_demand_map,
             vertical_demand_map,
             horizontal_diff_map,
             vertical_diff_map,
             approximate_net_count,
             x,
             y,
             pin_offset_x,
             pin_offset_y,
             pin2node_map,
             flat_net2pin_map,
             flat_net2pin_start_map,
             net_weights,
             net_exclude_mask,
             np.float32(self.xl),
             np.float32(self.yl),
             np.float32(site_width),
             np.float32(row_height),
             congestionMapRowSize,
             congestionMapColumnSize,
             np.int32(num_net),
             exact_max_bbox_bins
         )

         if exact_max_bbox_bins > 0 and approximate_net_count[0] > 0:
             np.cumsum(horizontal_diff_map, axis=0, out=horizontal_diff_map)
             np.cumsum(horizontal_diff_map, axis=1, out=horizontal_diff_map)
             horizontal_demand_map += horizontal_diff_map[:congestionMapColumnSize, :congestionMapRowSize]
             np.cumsum(vertical_diff_map, axis=0, out=vertical_diff_map)
             np.cumsum(vertical_diff_map, axis=1, out=vertical_diff_map)
             vertical_demand_map += vertical_diff_map[:congestionMapColumnSize, :congestionMapRowSize]

         unit_horizontal_capacity = float(self.unit_horizontal_capacity) if self.unit_horizontal_capacity else 1.0
         unit_vertical_capacity = float(self.unit_vertical_capacity) if self.unit_vertical_capacity else 1.0
         bin_area = float(site_width * row_height)
         horizontal_demand_map /= np.float32(bin_area * unit_horizontal_capacity)
         vertical_demand_map /= np.float32(bin_area * unit_vertical_capacity)
         np.abs(horizontal_demand_map, out=horizontal_demand_map)
         np.abs(vertical_demand_map, out=vertical_demand_map)
         horizontal_weighted = horizontal_demand_map * weight_map
         vertical_weighted = vertical_demand_map * weight_map

         max_source = self._get_congestion_rudy_max_source()
         sum_source = self._get_congestion_rudy_sum_source()

         if return_stats:
             max_congestion = PlaceDB._rudy_map_max(horizontal_weighted, vertical_weighted, max_source)
             total_sum_congestion = PlaceDB._rudy_map_sum(horizontal_weighted, vertical_weighted, sum_source)
             max_h = float(np.max(horizontal_weighted))
             total_h = float(np.sum(horizontal_weighted))
             max_v = float(np.max(vertical_weighted))
             total_v = float(np.sum(vertical_weighted))
             max_macro_congestion = PlaceDB._rudy_macro_max(
                 horizontal_weighted, vertical_weighted, macro_bin_mask, max_source
             )
             total_sum_macro_congestion = PlaceDB._rudy_macro_sum(
                 horizontal_weighted, vertical_weighted, macro_bin_mask, sum_source
             )

             end_time = time.time()
             print(
                 "[calc_congestion_numba] RUDY-style pin-bbox stats calculation completed in "
                 "{:.4f} seconds (approx_nets={}, exact_max_bbox_bins={})".format(
                     end_time - start_time,
                     int(approximate_net_count[0]),
                     int(exact_max_bbox_bins),
                 )
             )
             print(
                 "[calc_congestion_numba] congestion maps: "
                 "H-only max/total={}/{}, V-only max/total={}/{}".format(
                     max_h, total_h, max_v, total_v
                 )
             )
             print(
                 "[calc_congestion_numba] reported max = np.max({}) = {} "
                 "[congestion_rudy_max_source={}]".format(
                     PlaceDB._describe_congestion_rudy_stat_source(max_source),
                     max_congestion,
                     max_source,
                 )
             )
             print(
                 "[calc_congestion_numba] reported total = np.sum({}) = {} "
                 "[congestion_rudy_sum_source={}]".format(
                     PlaceDB._describe_congestion_rudy_stat_source(sum_source),
                     total_sum_congestion,
                     sum_source,
                 )
             )
             print(
                 "[calc_congestion_numba] reported macro max/total from same sources: {} / {}".format(
                     max_macro_congestion, total_sum_macro_congestion
                 )
             )
             return max_congestion, total_sum_congestion, max_macro_congestion, total_sum_macro_congestion

         # Map return path keeps H/V separate until the selected source is materialized.
         weighted_congestion_map = PlaceDB._build_rudy_source_map(
             horizontal_weighted,
             vertical_weighted,
             max_source,
         )
         initial_macro_congestion_map = np.zeros_like(weighted_congestion_map)
         initial_macro_congestion_map[macro_bin_mask] = weighted_congestion_map[macro_bin_mask]
         initial_congestion_map = weighted_congestion_map

         end_time = time.time()
         print(
             "[calc_congestion_numba] RUDY-style pin-bbox calculation completed in "
             "{:.4f} seconds (approx_nets={}, exact_max_bbox_bins={}, map_source={})".format(
                 end_time - start_time,
                 int(approximate_net_count[0]),
                 int(exact_max_bbox_bins),
                 max_source,
             )
         )

         return initial_congestion_map, initial_macro_congestion_map
