from ReadDEF import ReadDEFinfo
import pandas as pd
import json
import fnmatch
import os
import logging
import time
from Cell import Cell
from Cell import LEF
from Pin import Pin
from Net import Net
import copy
import virtual_blockage_for_die
import numpy as np
from matplotlib import pyplot as plt
import matplotlib.patches as patches
from shapely.geometry import box, Polygon, MultiPolygon, GeometryCollection
from shapely.ops import unary_union
# Fence regions stored as a bare list of [xl,yl,xh,yh] boxes (not {"area": ...}).
LIST_FORMAT_REGION_NAMES = frozenset({"vr", "evr", "evr_boundary", "evr_center"})
EVR_SPLIT_REGION_NAMES = ("evr_boundary", "evr_center")


class Analysis:
    @staticmethod
    def _iter_polygons(geometry):
        if geometry is None or geometry.is_empty:
            return []
        if isinstance(geometry, Polygon):
            return [geometry]
        if isinstance(geometry, (MultiPolygon, GeometryCollection)):
            return [
                geom
                for geom in geometry.geoms
                if isinstance(geom, Polygon) and not geom.is_empty
            ]
        raise ValueError(
            f"[LEF_DEF_analysis] Unexpected geometry type: {type(geometry)}"
        )

    @staticmethod
    def _polygon_xy_coords(polygon):
        xs = {coord[0] for coord in polygon.exterior.coords}
        ys = {coord[1] for coord in polygon.exterior.coords}
        for interior in polygon.interiors:
            xs.update(coord[0] for coord in interior.coords)
            ys.update(coord[1] for coord in interior.coords)
        return xs, ys

    @staticmethod
    def _is_nearly_rectangle(geometry, rel_tol=1e-9, abs_tol=1e-3):
        if geometry is None or geometry.is_empty:
            return False
        if not isinstance(geometry, Polygon) or geometry.interiors:
            return False
        xl, yl, xh, yh = geometry.bounds
        box_area = max(0.0, (xh - xl) * (yh - yl))
        return abs(geometry.area - box_area) <= max(abs_tol, rel_tol * max(box_area, 1.0))

    @staticmethod
    def _append_preferred_cuts(edge_coords, start, end, pref_size):
        coords = set(edge_coords) if edge_coords else set()
        coords.add(float(start))
        coords.add(float(end))
        if pref_size is None or pref_size <= 0.0 or end <= start:
            return sorted(coords)
        x = float(start)
        # Keep a little slack so we do not add a near-duplicate of `end`.
        while x < end - 1e-6 * max(1.0, abs(pref_size)):
            coords.add(x)
            x += float(pref_size)
        return sorted(coords)

    @staticmethod
    def _finite_rect(bounds):
        if bounds is None or len(bounds) != 4:
            return None
        xl, yl, xh, yh = (float(v) for v in bounds)
        if not np.isfinite([xl, yl, xh, yh]).all():
            return None
        if xh <= xl or yh <= yl:
            return None
        return (xl, yl, xh, yh)

    @staticmethod
    def _merge_adjacent_rectangles(rectangles, rel_tol=1e-9, abs_tol=1.0):
        """
        Merge touching rectangles when their union remains a rectangle.
        Preserves exact area: coordinates are not snapped; abs_tol is only used
        for float equality of shared edges.
        """
        rects = [Analysis._finite_rect(r) for r in rectangles]
        rects = [r for r in rects if r is not None]
        if not rects:
            return []

        def close(a, b):
            return abs(float(a) - float(b)) <= max(
                abs_tol, rel_tol * max(abs(a), abs(b), 1.0)
            )

        def edge_key(v):
            # Bucket for grouping only; never rewrite stored coordinates.
            return round(float(v) / abs_tol)

        changed = True
        while changed:
            changed = False

            by_y = {}
            for xl, yl, xh, yh in rects:
                key = (edge_key(yl), edge_key(yh))
                by_y.setdefault(key, []).append((xl, yl, xh, yh))
            merged = []
            for group in by_y.values():
                group = sorted(group, key=lambda r: (r[0], r[2]))
                cur = list(group[0])
                for rect in group[1:]:
                    if (
                        close(cur[1], rect[1])
                        and close(cur[3], rect[3])
                        and close(cur[2], rect[0])
                    ):
                        cur[2] = max(float(cur[2]), float(rect[2]))
                        changed = True
                    else:
                        merged.append(tuple(cur))
                        cur = list(rect)
                merged.append(tuple(cur))
            rects = merged

            by_x = {}
            for xl, yl, xh, yh in rects:
                key = (edge_key(xl), edge_key(xh))
                by_x.setdefault(key, []).append((xl, yl, xh, yh))
            merged = []
            for group in by_x.values():
                group = sorted(group, key=lambda r: (r[1], r[3]))
                cur = list(group[0])
                for rect in group[1:]:
                    if (
                        close(cur[0], rect[0])
                        and close(cur[2], rect[2])
                        and close(cur[3], rect[1])
                    ):
                        cur[3] = max(float(cur[3]), float(rect[3]))
                        changed = True
                    else:
                        merged.append(tuple(cur))
                        cur = list(rect)
                merged.append(tuple(cur))
            rects = merged

        return sorted(set(rects))

    @staticmethod
    def _snap_sorted_coords(coords, abs_tol=1.0):
        """Collapse nearly-equal coordinates to cut fewer partition strips."""
        if not coords:
            return []
        vals = sorted(float(c) for c in coords)
        out = [vals[0]]
        for v in vals[1:]:
            if abs(v - out[-1]) > abs_tol:
                out.append(v)
            else:
                # Keep the later value so spans still cover the geometry.
                out[-1] = v
        return out

    @staticmethod
    def partition_geometry(geometry, label):
        if geometry.is_empty:
            return []
        polygons = Analysis._iter_polygons(geometry)
        if not polygons and not geometry.is_empty:
            raise ValueError(f"[LEF_DEF_analysis] Unexpected {label} geometry type: {type(geometry)}")

        regions = []
        for polygon in polygons:
            x_coords, _ = Analysis._polygon_xy_coords(polygon)
            x_coords = Analysis._snap_sorted_coords(x_coords, abs_tol=1.0)
            if len(x_coords) < 2:
                continue

            _, yl, _, yh = polygon.bounds
            for xl, xh in zip(x_coords[:-1], x_coords[1:]):
                if xh <= xl:
                    continue
                overlap = polygon.intersection(box(xl, yl, xh, yh))
                if overlap.is_empty or overlap.area <= 0:
                    continue
                # Do not use strip AABB: a Polygon with an interior hole
                # (island fixed macro) has bounds that refill the hole.
                # Cut each strip by Y (including hole edges) instead.
                for piece in Analysis._iter_polygons(overlap):
                    if Analysis._is_nearly_rectangle(piece):
                        rect = Analysis._finite_rect(piece.bounds)
                        if rect is not None:
                            regions.append(rect)
                    else:
                        regions.extend(
                            Analysis._partition_by_y_only(piece, f"{label} strip")
                        )

        regions = Analysis._merge_adjacent_rectangles(regions)
        partition_area = sum((xh - xl) * (yh - yl) for xl, yl, xh, yh in regions)
        # Large DEF units make absolute 1e-3 too tight (float noise); allow relative slack.
        area_diff = abs(partition_area - geometry.area)
        abs_tol = 1e-3
        rel_tol = 1e-9
        if area_diff > max(abs_tol, rel_tol * max(abs(geometry.area), 1.0)):
            raise ValueError(
                f"[LEF_DEF_analysis] {label} partition area mismatch: "
                f"{partition_area} vs {geometry.area}"
            )
        return [list(region) for region in regions]

    @staticmethod
    def partition_geometry_compact(geometry, label):
        """
        Exact rectangle cover with fewer boxes: try X-strip and Y-strip
        partitions, keep the smaller merged result.
        """
        if geometry is None or geometry.is_empty:
            return []
        x_boxes = Analysis.partition_geometry(geometry, label + " x-strips")
        y_raw = Analysis._partition_by_y_only(geometry, label + " y-strips")
        y_merged = Analysis._merge_adjacent_rectangles(y_raw)
        # Area check for Y path (X path already checked inside partition_geometry).
        y_area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in y_merged)
        area_diff = abs(y_area - geometry.area)
        if area_diff > max(1e-3, 1e-9 * max(abs(geometry.area), 1.0)):
            print(
                "[LEF_DEF_analysis] {} y-strip area mismatch ({:.3f} vs {:.3f}); "
                "use x-strips ({} boxes)".format(
                    label, y_area, geometry.area, len(x_boxes)
                )
            )
            return x_boxes
        y_boxes = [list(r) for r in y_merged]
        if len(y_boxes) < len(x_boxes):
            print(
                "[LEF_DEF_analysis] {} compact partition: y-strips {} < x-strips {}".format(
                    label, len(y_boxes), len(x_boxes)
                )
            )
            return y_boxes
        print(
            "[LEF_DEF_analysis] {} compact partition: x-strips {} <= y-strips {}".format(
                label, len(x_boxes), len(y_boxes)
            )
        )
        return x_boxes

    @staticmethod
    def partition_geometry_preferred(geometry, pref_w, pref_h, label):
        """
        Partition a rectilinear region into rectangles, preferring tiles close to
        (pref_w, pref_h).

        Cut lines are spaced by the preferred sizes (plus the polygon bounds).
        Polygon edge coordinates are intentionally NOT injected as global cuts,
        so jagged boundaries do not create thousands of thin scraps.
        Partial cells at the jagged edge are cleaned up with strip partition.
        """
        if geometry is None or geometry.is_empty:
            return []
        pref_w = float(pref_w) if pref_w is not None else 0.0
        pref_h = float(pref_h) if pref_h is not None else 0.0
        if pref_w <= 0.0 or pref_h <= 0.0:
            return Analysis.partition_geometry(geometry, label)

        polygons = Analysis._iter_polygons(geometry)
        if not polygons and not geometry.is_empty:
            raise ValueError(f"[LEF_DEF_analysis] Unexpected {label} geometry type: {type(geometry)}")

        regions = []
        area_tol_rel = 1e-9
        area_tol_abs = 1e-3
        for polygon in polygons:
            minx, miny, maxx, maxy = map(float, polygon.bounds)
            xs = Analysis._append_preferred_cuts(set(), minx, maxx, pref_w)
            ys = Analysis._append_preferred_cuts(set(), miny, maxy, pref_h)
            if len(xs) < 2 or len(ys) < 2:
                continue
            for xl, xh in zip(xs[:-1], xs[1:]):
                if xh <= xl:
                    continue
                for yl, yh in zip(ys[:-1], ys[1:]):
                    if yh <= yl:
                        continue
                    cell = box(xl, yl, xh, yh)
                    overlap = polygon.intersection(cell)
                    if overlap.is_empty or overlap.area <= 0.0:
                        continue
                    cell_area = float(cell.area)
                    if abs(overlap.area - cell_area) <= max(
                        area_tol_abs, area_tol_rel * max(cell_area, 1.0)
                    ):
                        regions.append((xl, yl, xh, yh))
                        continue
                    for piece in Analysis._iter_polygons(overlap):
                        if Analysis._is_nearly_rectangle(piece):
                            regions.append(tuple(piece.bounds))
                        else:
                            for rect in Analysis.partition_geometry(
                                piece, f"{label} partial"
                            ):
                                regions.append(tuple(rect))

        regions = sorted(set(regions))
        partition_area = sum((xh - xl) * (yh - yl) for xl, yl, xh, yh in regions)
        area_diff = abs(partition_area - geometry.area)
        if area_diff > max(area_tol_abs, area_tol_rel * max(abs(geometry.area), 1.0)):
            print(
                f"[LEF_DEF_analysis] {label} preferred partition area mismatch "
                f"({partition_area} vs {geometry.area}); fallback to strip partition"
            )
            return Analysis.partition_geometry(geometry, label)
        return [list(region) for region in regions]

    @staticmethod
    def rectangles_to_geometry(rectangles, clip_geometry, label):
        if not rectangles:
            return GeometryCollection()
        if all(isinstance(value, (int, float)) for value in rectangles):
            if len(rectangles) % 4 != 0:
                raise ValueError(f"[LEF_DEF_analysis] {label} rectangle coordinates should be multiple of 4")
            rect_iter = [rectangles[i:i + 4] for i in range(0, len(rectangles), 4)]
        else:
            rect_iter = rectangles

        boxes = []
        for rect in rect_iter:
            if len(rect) != 4:
                raise ValueError(f"[LEF_DEF_analysis] {label} only supports RECT regions: {rect}")
            xl, yl, xh, yh = [float(value) for value in rect]
            clipped = box(xl, yl, xh, yh).intersection(clip_geometry)
            if not clipped.is_empty:
                boxes.append(clipped)
        return unary_union(boxes) if boxes else GeometryCollection()

    @staticmethod
    def def_rectangles_to_boxes(rectangles, die_rect, label):
        """
        Parse DEF RECT list into boxes clipped to die AABB only.

        DEF regions are already rectangles; clip to die with axis-aligned
        min/max (still a rectangle). No union/re-partition — area preserved.
        """
        if not rectangles:
            return [], GeometryCollection()
        if all(isinstance(value, (int, float)) for value in rectangles):
            if len(rectangles) % 4 != 0:
                raise ValueError(
                    f"[LEF_DEF_analysis] {label} rectangle coordinates should be multiple of 4"
                )
            rect_iter = [rectangles[i : i + 4] for i in range(0, len(rectangles), 4)]
        else:
            rect_iter = rectangles

        dxl, dyl, dxh, dyh = map(float, die_rect.bounds)
        out_boxes = []
        geoms = []
        for rect in rect_iter:
            if len(rect) != 4:
                raise ValueError(
                    f"[LEF_DEF_analysis] {label} only supports RECT regions: {rect}"
                )
            xl = max(float(rect[0]), dxl)
            yl = max(float(rect[1]), dyl)
            xh = min(float(rect[2]), dxh)
            yh = min(float(rect[3]), dyh)
            if xh <= xl or yh <= yl:
                continue
            out_boxes.append([xl, yl, xh, yh])
            geoms.append(box(xl, yl, xh, yh))

        if not out_boxes:
            return [], GeometryCollection()

        out_boxes = [
            list(r) for r in Analysis._merge_adjacent_rectangles(out_boxes, abs_tol=1.0)
        ]
        union_geom = unary_union(geoms) if geoms else GeometryCollection()
        return out_boxes, union_geom

    @staticmethod
    def clip_def_rectangles_to_boxes(rectangles, clip_geometry, label):
        """
        Clip DEF RECT list to clip_geometry and return (box_list, union_geom).

        Each DEF rectangle is handled independently. A clipped piece that is
        already a rectangle is kept as one box; only non-rect remnants are
        locally partitioned. Never unions all DEF rects and re-partitions
        the whole region (that path exploded box counts).
        Area of returned boxes matches union_geom.area.
        """
        if not rectangles or clip_geometry is None or clip_geometry.is_empty:
            return [], GeometryCollection()
        if all(isinstance(value, (int, float)) for value in rectangles):
            if len(rectangles) % 4 != 0:
                raise ValueError(
                    f"[LEF_DEF_analysis] {label} rectangle coordinates should be multiple of 4"
                )
            rect_iter = [rectangles[i : i + 4] for i in range(0, len(rectangles), 4)]
        else:
            rect_iter = rectangles

        out_boxes = []
        geoms = []
        for rect in rect_iter:
            if len(rect) != 4:
                raise ValueError(
                    f"[LEF_DEF_analysis] {label} only supports RECT regions: {rect}"
                )
            xl, yl, xh, yh = map(float, rect)
            if xh <= xl or yh <= yl:
                continue
            clipped = box(xl, yl, xh, yh).intersection(clip_geometry)
            if clipped.is_empty or clipped.area <= 0:
                continue
            geoms.append(clipped)
            for piece in Analysis._iter_polygons(clipped):
                if Analysis._is_nearly_rectangle(piece):
                    r = Analysis._finite_rect(piece.bounds)
                    if r is not None:
                        out_boxes.append(list(r))
                else:
                    out_boxes.extend(
                        Analysis.partition_geometry_compact(
                            piece, f"{label} clipped fragment"
                        )
                    )

        if not out_boxes:
            return [], GeometryCollection()

        out_boxes = [
            list(r) for r in Analysis._merge_adjacent_rectangles(out_boxes, abs_tol=1.0)
        ]
        union_geom = unary_union(geoms) if geoms else GeometryCollection()
        boxes_area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in out_boxes)
        area_diff = abs(boxes_area - union_geom.area)
        if area_diff > max(1e-3, 1e-9 * max(abs(union_geom.area), 1.0)):
            raise ValueError(
                f"[LEF_DEF_analysis] {label} clip box area mismatch: "
                f"{boxes_area} vs {union_geom.area}"
            )
        return out_boxes, union_geom

    @staticmethod
    def rectangles_to_unclipped_geometry(rectangles, label):
        if not rectangles:
            return GeometryCollection()
        if all(isinstance(value, (int, float)) for value in rectangles):
            if len(rectangles) % 4 != 0:
                raise ValueError(f"[LEF_DEF_analysis] {label} rectangle coordinates should be multiple of 4")
            rect_iter = [rectangles[i:i + 4] for i in range(0, len(rectangles), 4)]
        else:
            rect_iter = rectangles

        boxes = []
        for rect in rect_iter:
            if len(rect) != 4:
                raise ValueError(f"[LEF_DEF_analysis] {label} only supports RECT regions: {rect}")
            xl, yl, xh, yh = [float(value) for value in rect]
            if xh > xl and yh > yl:
                boxes.append(box(xl, yl, xh, yh))
        return unary_union(boxes) if boxes else GeometryCollection()

    def apply_group_regions_to_cells(self, region_names):
        region_name_set = set(region_names)
        group_info = self.die_info.get("group", {})
        component_names = {
            self.node_name2index_map[cell_name]: cell_name
            for cell_name in self.node_name2index_map
            if isinstance(cell_name, str)
        }
        for group_name, group in group_info.items():
            region_name = group.get("region") if isinstance(group, dict) else None
            if region_name not in region_name_set:
                continue
            elements = group.get("elements", [])
            for element in elements:
                if element in self.node_name2index_map:
                    self.lef_def.cellInfo[self.node_name2index_map[element]].set_region(region_name)
                    continue
                if "*" not in element and "?" not in element:
                    continue
                for cell_idx, cell_name in component_names.items():
                    if fnmatch.fnmatchcase(cell_name, element):
                        self.lef_def.cellInfo[cell_idx].set_region(region_name)

    def analyze_def_regions(self):
        region_rectangles_by_name = {
            region_name: region_rectangles
            for region_name, region_rectangles in self.die_info.get("region", {}).items()
            if region_name not in {"vr", "evr"}
        }
        region_geometries = {
            region_name: self.rectangles_to_unclipped_geometry(region_rectangles, f"DEF region {region_name}")
            for region_name, region_rectangles in region_rectangles_by_name.items()
        }

        region_summary = {
            region_name: {
                "region_name": region_name,
                "region_area": float(region_geometry.area),
                "cell_area": 0.0,
                "cell_count": 0,
                "region_area_smaller_than_cell_area": False,
            }
            for region_name, region_geometry in region_geometries.items()
        }
        area_issues = []
        fixed_macro_issues = []
        overlap_issues = []
        def_scale = float(self.die_info.get("def_scale", 1.0))
        containment_area_tolerance = 1e-3
        overlap_area_tolerance = 1e-3

        for cell_idx, cell_instance in self.lef_def.cellInfo.items():
            lef_instance = cell_instance.get_lef_info()
            lef_cell_type = lef_instance.get_cell_type()
            if lef_cell_type == "PIN":
                continue

            region_name = cell_instance.get_region()
            if region_name not in region_geometries:
                continue

            cell_name_key = cell_instance.get_name()
            cell_name = self.node_name2index_map[cell_name_key] if cell_name_key in self.node_name2index_map else str(cell_name_key)
            width = float(lef_instance.get_width()) * def_scale
            height = float(lef_instance.get_height()) * def_scale
            cell_area = width * height
            region_summary[region_name]["cell_area"] += cell_area
            region_summary[region_name]["cell_count"] += 1

            placed_state = cell_instance.get_placed_state()
            is_fixed_macro = lef_cell_type == "BLOCK" and placed_state == "FIXED"
            if not is_fixed_macro:
                continue

            pos = cell_instance.get_pos()
            if hasattr(pos, "tolist"):
                pos = pos.tolist()
            if not isinstance(pos, (list, tuple)) or len(pos) < 2:
                fixed_macro_issues.append({
                    "region_name": region_name,
                    "cell_name": cell_name,
                    "reason": "missing_position",
                    "macro_area": float(cell_area),
                    "outside_area": None,
                    "overlap_area": None,
                    "position": pos,
                    "size": [float(width), float(height)],
                })
                continue

            xl = float(pos[0])
            yl = float(pos[1])
            macro_geometry = box(xl, yl, xl + width, yl + height)
            region_geometry = region_geometries[region_name]
            outside_area = float(macro_geometry.difference(region_geometry).area)
            overlap_area = float(macro_geometry.intersection(region_geometry).area)
            if outside_area > containment_area_tolerance:
                fixed_macro_issues.append({
                    "region_name": region_name,
                    "cell_name": cell_name,
                    "reason": "fixed_macro_outside_region",
                    "macro_area": float(cell_area),
                    "outside_area": outside_area,
                    "overlap_area": overlap_area,
                    "position": [xl, yl],
                    "size": [float(width), float(height)],
                })

        for region_name, summary in region_summary.items():
            summary["cell_area"] = float(summary["cell_area"])
            summary["cell_area_minus_region_area"] = float(summary["cell_area"] - summary["region_area"])
            summary["region_area_smaller_than_cell_area"] = bool(
                summary["cell_area"] > summary["region_area"] + containment_area_tolerance
            )
            if summary["region_area_smaller_than_cell_area"]:
                area_issues.append({
                    "region_name": region_name,
                    "region_area": summary["region_area"],
                    "cell_area": summary["cell_area"],
                    "cell_count": summary["cell_count"],
                    "cell_area_minus_region_area": summary["cell_area_minus_region_area"],
                })

        region_names = sorted(region_geometries.keys())
        for i, region_name_a in enumerate(region_names):
            geometry_a = region_geometries[region_name_a]
            for region_name_b in region_names[i + 1:]:
                geometry_b = region_geometries[region_name_b]
                intersection = geometry_a.intersection(geometry_b)
                if intersection.is_empty:
                    continue
                overlap_area = float(intersection.area)
                if overlap_area <= overlap_area_tolerance:
                    continue
                overlap_issues.append({
                    "region_name_a": region_name_a,
                    "region_name_b": region_name_b,
                    "overlap_area": overlap_area,
                    "overlap_bounds": [float(value) for value in intersection.bounds],
                })

        report = {
            "summary": {
                "num_regions": len(region_summary),
                "num_area_issues": len(area_issues),
                "num_fixed_macro_issues": len(fixed_macro_issues),
                "num_region_overlap_issues": len(overlap_issues),
            },
            "regions": region_summary,
            "issues": {
                "region_area_smaller_than_cell_area": area_issues,
                "fixed_macro_outside_region": fixed_macro_issues,
                "region_overlaps": overlap_issues,
            },
        }
        print(
            "[LEF_DEF_analysis] region checks: "
            f"area={len(area_issues)}, fixed_macro={len(fixed_macro_issues)}, overlap={len(overlap_issues)}"
        )
        return report

    def __init__(self, definfo: ReadDEFinfo, params, path_critical_path_info=None, path_convert_name2supercell=None):
        self.lef_def = definfo
        if type(params) == str:
            with open(params, 'r') as f:
                self.params = json.load(f)
        elif type(params) == dict:
            self.params = params
        elif type(params) == object:
            self.params = params
        else:
            self.params = params
            #raise ValueError('params should be str (path to json), dict, or object')
        self.useless_layer_list = self._resolve_useless_layer_list(self.params)
        
        
        #list of the attributes created in class Analysis
        self.region_info = {}
        self.cells_not_in_nets = [] #obtained by self.cell_info
        self.ext_pins_useless = [] #obtained by get_netlist_info()
        self.virtual_cells = []
        self.num_virtual_cell = 0
        
        
        '''
        --- Cell & Pin informations for net with a certain use ---
        netlist_info:
           (1) get the nets for a certain use like 'SIGNAL'
           (2) netlist_info should have external pins not in ext_pins_useless
        net_cell_info: netcell_info should have external pins not in ext_pins_useless
        ext_pin_list_in_nets : external pins defined in self.netlist_info
        cell_net_info: cell_net_info's keys should be standard cell or macro defined in self.netlist_info
        total_cell_info: list up the information of cell and ext pins defined in cell_list_in_nets and ext_pin_list_in_nets
        '''
        self.die_info = None
        self.netlist_info = None 
        self.net_cell_info = None 
        self.cell_list_in_nets = ()
        self.std_cell_list_in_nets = ()
        self.macro_list_in_nets = ()
        self.ext_pin_list_in_nets = () 
        self.cell_net_info = None 
        self.cell_info = None 
        self.total_cell_info = None 
        self.lef_info = None
        # Kept for plotting after mode=convert_macro2port removes fixed macros from components.
        # Each row: [x, y, width, height] in DEF units (orientation already applied to w/h).
        self.plot_fixed_macros = np.zeros((0, 4), dtype=np.float64)
        self.plot_fixed_macro_names = []
        # Parallel to plot_fixed_macros: None = draw AABB; else list of exterior rings.
        self.plot_fixed_macro_footprints = []
        self.center_region = []
        self.region_check_report = {
            "summary": {
                "num_regions": 0,
                "num_area_issues": 0,
                "num_fixed_macro_issues": 0,
                "num_region_overlap_issues": 0,
            },
            "regions": {},
            "issues": {
                "region_area_smaller_than_cell_area": [],
                "fixed_macro_outside_region": [],
                "region_overlaps": [],
            },
        }

        #cell name to index mapping
        self.lef_macro_name2index_map = self.lef_def.lef_macro_name2index_map
        self.pin_name2index_map = self.lef_def.lef_pin_name2index_map   
        self.node_name2index_map = self.lef_def.node_name2index_map
        self.net_name2index_map = self.lef_def.net_name2index_map   
        self.lef_macro_name_to_id = self.lef_def.lef_macro_name_to_id
        self.pin_name_to_id = self.lef_def.lef_pin_name_to_id
        self.node_name_to_id = self.lef_def.node_name_to_id
        self.net_name_to_id = self.lef_def.net_name_to_id
        
        #get the attributes
        self.ext_pin_info = self.get_extPin_info_use(filter_use='SIGNAL')
        # extPinInfo keys are pin ids; ext_pin_info keys are pin names.
        self.ext_pins_useless = [
            int(pin_idx)
            for pin_idx in self.lef_def.extPinInfo.keys()
            if self.node_name2index_map[int(pin_idx)] not in self.ext_pin_info
        ]
        if self.ext_pins_useless:
            sample = [
                self.node_name2index_map[int(pin_idx)]
                for pin_idx in self.ext_pins_useless[:8]
            ]
            print(
                "[LEF_DEF_analysis] ext_pins_useless={} (sample: {})".format(
                    len(self.ext_pins_useless), ", ".join(sample)
                )
            )
        self.die_info = self.get_die_info()

        # Square stretch is applied in PlaceDB after MakeDB builds the DB
        # (node_x/size, rows, regions, …). Analysis stays in original DEF units.
        self.square_stretch = None

        has_polygon_die = len(self.die_info.get('layout', [])) > 4
        has_placement_blockage = any(
            self.die_info['blockage'][blockage_name].get('type') == 'PLACEMENT'
            for blockage_name in self.die_info.get('blockage', {})
        )
        has_def_regions = any(
            region_name not in {"vr", "evr"}
            for region_name in self.die_info.get("region", {})
        )
        if has_polygon_die or has_placement_blockage or has_def_regions:
            # Region/VR/EVR must be built from the original DEF fixed macros
            # before convert_macro2port removes them from the placement DB.
            self.build_region_info()

        if len(self.region_info) > 0:
            self.add_virtual_cell('virtual0', 'virtual_type_0', 'virtual_region0', 'SIGNAL')
            self.add_virtual_cell('virtual1', 'virtual_type_0', 'virtual_region1', 'SIGNAL')
            self.virtual_cells.append('virtual0')
            self.virtual_cells.append('virtual1')
            self.num_virtual_cell = 2
        if path_critical_path_info and path_convert_name2supercell:
           with open(path_critical_path_info, 'r') as f: self.critical_path_info = json.load(f)
           with open(path_convert_name2supercell, 'r') as f: self.convert_name2supercell = json.load(f)
           self.createCriticalPathNet()
       
        started = time.time()
        self.netlist_info, self.net_cell_info, self.cell_net_info, self.cell_list_in_nets, self.std_cell_list_in_nets, self.macro_list_in_nets, self.ext_pin_list_in_nets = self.get_netlist_info(filter_net_use='SIGNAL')
        logging.info("[MakeDB] Analysis netlist/connectivity %.3fs", time.time() - started)
        self.cell_info = self.get_cell_info()
        self.total_cell_info = self.get_total_cell_info()
        self.lef_info = self.get_lef_info()
        if self.die_info.get("region", {}):
            self.region_check_report = self.analyze_def_regions()

        if self._convert_macro2port_enabled():
            # Must run after build_region_info(). Square stretch runs later in PlaceDB.
            # Converts components/netlist only; never rebuilds region_info.
            self.convert_fixed_macros_to_ports()
        
        #read or make center region
        if 'center_region_auto' in self.params.__dict__:
            self.center_region =self.make_center_region(vertices=self.die_info['layout'], area_ratio=self.params.center_region_auto)
        elif 'center_region' in self.params.__dict__:
            self.center_region = self.read_center_region(self.params.center_region)
        else:
            self.center_region=[]
        
        #save the result
        self.save2data()
        
        #save die layout
        #if len(self.region_info):
        #    self.plot_layout(layout_vertices=self.die_info['layout'], \
        #                     df_total_cell_info=self.total_cell_info, \
        #                     extPos=self.ext_pin_info, \
        #                     def_scale=self.die_info['def_scale'], \
        #                     save_name=self.params.save_path + '/die_layout.png',
        #                     virtual_region_list=self.region_info['evr'], \
        #                     virtual_blockage_list = self.region_info['vr'], \
        #                     center_region=self.center_region)
    
    
    def plot_layout(self, layout_vertices: list, df_total_cell_info:pd.DataFrame, extPos: dict, def_scale:float, save_name:str, virtual_region_list=None, virtual_blockage_list=None, center_region=None):
        layout_vertices = np.array(layout_vertices, dtype=np.float32).flatten().reshape(-1,2)
        virtual_region_list = np.array(virtual_region_list, dtype=np.float32) if virtual_region_list != None else np.array([])
        virtual_blockage_list = np.array(virtual_blockage_list, dtype=np.float32) if virtual_blockage_list != None else np.array([])
        def_scale = float(def_scale)
        
        fig, ax = plt.subplots()
        die = patches.Rectangle((0, 0), np.max(layout_vertices[:,0]) - np.min(layout_vertices[:,0]), np.max(layout_vertices[:,1]) - np.min(layout_vertices[:,1]), facecolor="none", edgecolor="black")
        ax.add_patch(die)
        for blockage in virtual_blockage_list:
           block = patches.Rectangle((blockage[0], blockage[1]), (blockage[2]-blockage[0]),(blockage[3]-blockage[1]), facecolor="grey", edgecolor="black")
           ax.add_patch(block)

        for blockage in virtual_region_list:
            block = patches.Rectangle((blockage[0], blockage[1]), (blockage[2]-blockage[0]),(blockage[3]-blockage[1]), edgecolor="purple", facecolor="yellow")
            ax.add_patch(block)
        
        if extPos:
            if isinstance(extPos, dict):
                posList = []
                for pin in extPos.keys():
                    layerList = [i for i in extPos[pin].keys() if i.startswith('layer')]
                    for layer in layerList:
                        posList.append(extPos[pin][layer]['position'])
                posX = np.array(posList, dtype=np.float32).reshape(-1,2)[:,0]
                posY = np.array(posList, dtype=np.float32).reshape(-1,2)[:,1]
                plt.plot(posX, posY, 'ro')
        
        for i in range(len(df_total_cell_info)):
            if df_total_cell_info.loc[i, 'cell_type'] == 'MACRO' and df_total_cell_info.loc[i, 'placed_state'] == 'FIXED':
                cellName = df_total_cell_info.loc[i, 'cell_name']
                if cellName.startswith('BLOCKAGE_BLOCKAGE'):
                    continue
                if cellName not in self.node_name2index_map:
                    continue
                cell_idx = self.node_name2index_map[cellName]
                if cell_idx not in self.lef_def.cellInfo:
                    continue
                cell_instance = self.lef_def.cellInfo[cell_idx]
                pos = cell_instance.get_pos()
                width = float(cell_instance.get_lef_info().get_width())*def_scale
                height = float(cell_instance.get_lef_info().get_height())*def_scale
                block = patches.Rectangle((pos[0], pos[1]), width, height, edgecolor="purple", facecolor="blue")
                ax.add_patch(block)
        
        if center_region:
            for region in center_region:
                pos = region[:2]
                width = region[2]-region[0]
                height = region[3] - region[1]
                block = patches.Rectangle((pos[0], pos[1]), width, height, edgecolor="black", facecolor="green")
                ax.add_patch(block)

        plt.xlim(-10, (np.max(layout_vertices) - np.min(layout_vertices))*1.1)
        plt.ylim(-10, (np.max(layout_vertices) - np.min(layout_vertices))*1.1)
        plt.savefig(save_name, dpi=300)
    
    
    @staticmethod
    def read_center_region(path_center_region):
        total_center_region_box_list=[]
        with open(path_center_region, 'r') as f: total_center_region_vertices=json.load(f)
        for center_region in total_center_region_vertices.keys():
            center_region_vertices = total_center_region_vertices[center_region]
            centerRegion = virtual_blockage_for_die.VirtualDieRegion(center_region_vertices)
            centerRegion.run()
            center_region_box_list = centerRegion.blockList4die
            total_center_region_box_list.extend(center_region_box_list)
            #centerRegion.plot(self.params.save_path+'/center_region_' + center_region+'.png')
        return total_center_region_box_list
    
    @staticmethod
    def make_center_region(center_region_auto, vertices:list, area_ratio:float):
        new_vertice_list = self.shrink_rectilinear_polygon(vertices=vertices, area_ratio=area_ratio)
        centerRegion = virtual_blockage_for_die.VirtualDieRegion(new_vertice_list)
        centerRegion.run()
        center_region_box_list = centerRegion.blockList4die
        return center_region_box_list
    
    
    def shrink_rectilinear_polygon(self, vertices, area_ratio=0.8):
        """
        Create an inner rectilinear polygon with approximately the given area ratio.
        Preserves the constraint that neighboring vertices share x or y coordinates.
        
        Parameters:
        vertices -- List of (x,y) coordinates defining the rectilinear polygon
        area_ratio -- Target ratio of inner polygon area to outer polygon area (default: 0.8)
        
        Returns:
        inner_vertices -- List of (x,y) coordinates defining the inner polygon
        """
        # Convert vertices to numpy array
        print(self.die_info['layout'])
        vertices = np.array(vertices).reshape(-1,2)

        # Find the bounding box
        min_x, min_y = np.min(vertices[:,0]) , np.min(vertices[:,1])
        max_x, max_y = np.max(vertices[:,0]) , np.max(vertices[:,1])
        
        # Calculate the width and height
        width = max_x - min_x
        height = max_y - min_y
        
        # Calculate center of the bounding box
        center_x = (min_x + max_x) / 2
        center_y = (min_y + max_y) / 2
        
        # Calculate the scale factor needed for the desired area
        scale_factor = np.sqrt(area_ratio)
        
        # Calculate how much to shrink on each side
        shrink_x = width * (1 - scale_factor) / 2
        shrink_y = height * (1 - scale_factor) / 2
        
        # Create a new set of vertices by adjusting each coordinate
        inner_vertices = []
        for x, y in vertices:
            # For each vertex, move it toward the center proportionally
            if x < center_x:
                new_x = x + shrink_x
            else:
                new_x = x - shrink_x
                
            if y < center_y:
                new_y = y + shrink_y
            else:
                new_y = y - shrink_y
                
            inner_vertices.append([new_x, new_y])
        
        # For a more complex rectilinear polygon, you might need more sophisticated logic
        # to ensure the x or y coordinates remain aligned between neighbors
        
        return inner_vertices
                 
                
        
    
     
    
    def set_virtualRegion_to_cell(self):
        for cell in self.lef_def.cellInfo.keys():
            if self.lef_def.cellInfo[cell].get_name() == 'virtual0': 
                self.lef_def.cellInfo[cell].set_region('vr0')
            elif self.lef_def.cellInfo[cell].get_name() == 'virtual1':
                self.lef_def.cellInfo[cell].set_region('vr1')
            else:
                self.lef_def.cellInfo[cell].set_region('evr')    
    
    def save2data(self):
        if not getattr(self.params, '_makedb_write_metadata', True):
            logging.info("[MakeDB] Analysis metadata write skipped (no binary_write)")
            return
        started = time.time()
        with open(self.params.save_path + '/netlist_info.json', 'w') as w: json.dump(self.netlist_info, w, indent=4)
        with open(self.params.save_path + '/net_cell_info.json', 'w') as w: json.dump(self.net_cell_info, w, indent=4)
        with open(self.params.save_path + '/cell_net_info.json', 'w') as w: json.dump(self.cell_net_info, w, indent=4)
        with open(self.params.save_path + '/cells_info.json', 'w') as w: json.dump(self.cell_info, w, indent=4)
        with open(self.params.save_path + '/ext_pin_info.json', 'w') as w: json.dump(self.ext_pin_info, w, indent=4)
        with open(self.params.save_path + '/lef_info.json', 'w') as w: json.dump(self.lef_info, w, indent=4)
        with open(self.params.save_path + '/die_info.json', 'w') as w: json.dump(self.die_info, w, indent=4)
        with open(self.params.save_path + '/blockage_info.json', 'w') as w: json.dump(self.die_info.get('blockage', {}), w, indent=4)
        with open(self.params.save_path + '/region_info.json', 'w') as w: json.dump(self.region_info, w, indent=4)
        with open(self.params.save_path + '/group_info.json', 'w') as w: json.dump(self.die_info.get('group', {}), w, indent=4)
        if getattr(self, "square_stretch", None):
            with open(self.params.save_path + '/square_stretch.json', 'w') as w:
                json.dump(self.square_stretch, w, indent=4)
        with open(self.params.save_path + '/region_check_report.json', 'w') as w: json.dump(self.region_check_report, w, indent=4)
        region_check_rows = []
        for issue in self.region_check_report.get("issues", {}).get("region_area_smaller_than_cell_area", []):
            row = {"issue_type": "region_area_smaller_than_cell_area"}
            row.update(issue)
            region_check_rows.append(row)
        for issue in self.region_check_report.get("issues", {}).get("fixed_macro_outside_region", []):
            row = {"issue_type": "fixed_macro_outside_region"}
            row.update(issue)
            region_check_rows.append(row)
        for issue in self.region_check_report.get("issues", {}).get("region_overlaps", []):
            row = {"issue_type": "region_overlaps"}
            row.update(issue)
            region_check_rows.append(row)
        pd.DataFrame(region_check_rows).to_csv(self.params.save_path + '/region_check_issues.tsv', sep='\t', index=None)
        self.total_cell_info.to_csv(self.params.save_path + '/total_cell_info.tsv', sep='\t', index=None)
        np.save(self.params.save_path + '/center_region.npy', np.array(self.center_region, dtype=np.float32))
        logging.info("[MakeDB] Analysis metadata write %.3fs", time.time() - started)
        #with open(self.params.save_path + '/center_region_list.json', 'w') as w: json.dump(self.center_region, w, indent=4)
    
    
    @staticmethod
    def _layout_to_polygon(layout_vertices):
        layout_vertices = np.array(layout_vertices, dtype=np.float64).reshape(-1, 2)
        if len(layout_vertices) < 3:
            return GeometryCollection()
        coords = list(map(tuple, layout_vertices)) + [tuple(layout_vertices[0])]
        polygon = Polygon(coords)
        if polygon.is_empty:
            return GeometryCollection()
        if not polygon.is_valid:
            try:
                from shapely import make_valid as _make_valid
            except ImportError:
                from shapely.validation import make_valid as _make_valid
            polygon = _make_valid(polygon)
        return polygon if not polygon.is_empty else GeometryCollection()

    def _collect_placement_blockage_boxes(self, die_rect):
        blockage_boxes = []
        for blockage_name in self.die_info.get('blockage', {}):
            blockage = self.die_info['blockage'][blockage_name]
            if blockage.get('type') != 'PLACEMENT':
                continue
            coords = blockage['coords']
            if len(coords) != 4:
                raise ValueError(f"[LEF_DEF_analysis] Only RECT placement blockages are supported: {coords}")
            xl, yl, xh, yh = coords
            clipped_box = box(xl, yl, xh, yh).intersection(die_rect)
            if not clipped_box.is_empty:
                blockage_boxes.append(clipped_box)
        return blockage_boxes

    @staticmethod
    def _footprint_exterior_rings(geom):
        """Exterior rings for plot when geom is a non-rect polygon; None => use AABB."""
        from shapely.geometry import MultiPolygon, Polygon

        if geom is None or geom.is_empty:
            return None
        if isinstance(geom, Polygon):
            if Analysis._is_nearly_rectangle(geom):
                return None
            return [[(float(x), float(y)) for x, y in geom.exterior.coords]]
        if isinstance(geom, MultiPolygon):
            polys = [poly for poly in geom.geoms if not poly.is_empty]
            if not polys:
                return None
            # Multi-block OBS (e.g. SDL_TOP upper/lower rects with a gap): export
            # every component ring even when each piece is axis-aligned.
            if len(polys) > 1:
                return [
                    [(float(x), float(y)) for x, y in poly.exterior.coords]
                    for poly in polys
                ]
            rings = []
            for poly in polys:
                if Analysis._is_nearly_rectangle(poly):
                    continue
                rings.append([(float(x), float(y)) for x, y in poly.exterior.coords])
            return rings if rings else None
        return None

    def _iter_fixed_cell_footprints(self, die_rect, cell_types):
        """Yield (cell_name, footprint_geom) for each FIXED cell of given LEF types."""
        from shapely.ops import unary_union
        from shapely.geometry import Polygon

        def_scale = float(self.die_info.get("def_scale", 1.0))
        cell_types = {str(t).upper() for t in cell_types}
        index_to_name = {
            int(idx): str(name)
            for name, idx in self.node_name2index_map.items()
            if isinstance(name, str)
        }
        for cell_key in self.lef_def.cellInfo.keys():
            cell_instance = self.lef_def.cellInfo[cell_key]
            name = index_to_name.get(int(cell_key), str(cell_key))
            if name in {"virtual0", "virtual1"}:
                continue
            if isinstance(name, str) and name.startswith("BLOCKAGE_BLOCKAGE"):
                continue
            if cell_instance.get_placed_state() != "FIXED":
                continue
            lef_instance = cell_instance.get_lef_info()
            cell_type = str(lef_instance.get_cell_type()).upper()
            if cell_type not in cell_types:
                continue
            pos = cell_instance.get_pos()
            if hasattr(pos, "tolist"):
                pos = pos.tolist()
            if not isinstance(pos, (list, tuple)) or len(pos) < 2:
                continue
            lef_w = float(lef_instance.get_width())
            lef_h = float(lef_instance.get_height())
            width = lef_w * def_scale
            height = lef_h * def_scale
            if width <= 0.0 or height <= 0.0:
                continue

            orient = cell_instance.get_orientation()
            rotate_angle, _flip = self._orient_to_rotate_flip(orient)
            xl = float(pos[0])
            yl = float(pos[1])

            if rotate_angle in (90, 270):
                width, height = height, width

            obs_by_layer = (
                lef_instance.get_obs_by_layer()
                if hasattr(lef_instance, "get_obs_by_layer")
                else {}
            )
            if obs_by_layer:
                layer_geometries = []
                for layer_name, primitives in obs_by_layer.items():
                    primitive_geoms = []
                    for prim in primitives:
                        prim_type = prim.get("type")
                        coords = prim.get("coords", ())
                        if prim_type == "rect" and len(coords) == 4:
                            rx0, ry0, rx1, ry1 = coords
                            corners = [
                                (rx0 * def_scale, ry0 * def_scale),
                                (rx1 * def_scale, ry0 * def_scale),
                                (rx1 * def_scale, ry1 * def_scale),
                                (rx0 * def_scale, ry1 * def_scale),
                            ]
                        elif prim_type == "polygon" and len(coords) >= 6:
                            corners = [
                                (coords[i] * def_scale, coords[i + 1] * def_scale)
                                for i in range(0, len(coords), 2)
                            ]
                        else:
                            continue
                        transformed = []
                        for cx, cy in corners:
                            tx, ty = self._lef_pin_offset_after_orient(
                                cx, cy, lef_w * def_scale, lef_h * def_scale, orient
                            )
                            transformed.append((xl + tx, yl + ty))
                        geom = Polygon(transformed)
                        if geom.is_valid and not geom.is_empty:
                            primitive_geoms.append(geom)
                    if primitive_geoms:
                        layer_geom = unary_union(primitive_geoms).intersection(die_rect)
                        if not layer_geom.is_empty:
                            layer_geometries.append((str(layer_name), layer_geom))

                if layer_geometries:
                    counts = {}
                    geometries = {}
                    for layer_name, geom in layer_geometries:
                        key = geom.wkb
                        counts[key] = counts.get(key, 0) + 1
                        geometries[key] = (layer_name, geom)

                    best_key = None
                    best_score = None
                    for key, count in counts.items():
                        layer_name, geom = geometries[key]
                        # Prefer the footprint repeated on many OBS layers.
                        # If tied, prefer larger area; OVERLAP is only a final tiebreak.
                        score = (
                            int(count),
                            float(geom.area),
                            1 if layer_name.upper() == "OVERLAP" else 0,
                        )
                        if best_score is None or score > best_score:
                            best_score = score
                            best_key = key

                    chosen_layer, chosen_geom = geometries[best_key]
                    aabb_area = max(width * height, 1.0)
                    if chosen_geom.area < 0.999 * aabb_area:
                        print(
                            f"[LEF_DEF_analysis] non-rect fixed macro {name}: "
                            f"using OBS layer {chosen_layer} footprint "
                            f"(repeated={counts[best_key]}, area_ratio={chosen_geom.area / aabb_area:.4f})"
                        )
                        yield name, chosen_geom
                        continue

            cell_box = box(xl, yl, xl + width, yl + height).intersection(die_rect)
            if not cell_box.is_empty:
                yield name, cell_box

    def _collect_fixed_cell_boxes(self, die_rect, cell_types):
        """Axis-aligned boxes for FIXED cells whose LEF type is in cell_types (DEF units).

        When a macro has OBS geometry stored on its LEF object, the actual OBS
        rectangles (transformed to DEF coordinates) are used instead of the
        simple bounding box.  This gives an accurate footprint for non-
        rectangular fixed macros.
        """
        return [geom for _, geom in self._iter_fixed_cell_footprints(die_rect, cell_types)]

    def _collect_fixed_macro_boxes(self, die_rect):
        """FIXED BLOCK/MACRO boxes from original DEF (not affected by port conversion)."""
        return self._collect_fixed_cell_boxes(die_rect, {"BLOCK", "MACRO"})

    def _collect_fixed_std_cell_boxes(self, die_rect):
        """FIXED CORE (standard cell) boxes."""
        return self._collect_fixed_cell_boxes(die_rect, {"CORE"})

    def build_region_info(self):
        die_rect = box(
            self.die_info['xl'],
            self.die_info['yl'],
            self.die_info['xh'],
            self.die_info['yh'],
        )
        layout = self.die_info.get('layout', [])
        polygon_geometry = None
        placeable_base = die_rect

        if len(layout) > 4:
            polygon_geometry = self._layout_to_polygon(layout)
            if not polygon_geometry.is_empty:
                placeable_base = polygon_geometry.intersection(die_rect)

        blockage_boxes = self._collect_placement_blockage_boxes(die_rect)
        # Always use original DEF fixed macros here (conversion has not run yet,
        # and lef_def.cellInfo is never cleared by convert_fixed_macros_to_ports).
        # Region partition: rectangular macros use AABB (simple holes).
        # Only true non-rect OBS footprints keep exact polygon geometry so
        # placeable area follows recess / L-shapes correctly.
        fixed_macro_boxes = []
        for geom in self._collect_fixed_macro_boxes(die_rect):
            if Analysis._is_nearly_rectangle(geom):
                fixed_macro_boxes.append(box(*geom.bounds))
            else:
                fixed_macro_boxes.append(geom)
        # Fixed std-cell OBS holes are not needed for recess snapping; AABB only.
        fixed_std_boxes = [box(*g.bounds) for g in self._collect_fixed_std_cell_boxes(die_rect)]
        blocked_parts = []
        if polygon_geometry is not None and not polygon_geometry.is_empty:
            outside_die = die_rect.difference(polygon_geometry)
            if not outside_die.is_empty:
                blocked_parts.append(outside_die)
        if blockage_boxes:
            blocked_parts.append(unary_union(blockage_boxes))
        if fixed_macro_boxes:
            blocked_parts.append(unary_union(fixed_macro_boxes))
        if fixed_std_boxes:
            blocked_parts.append(unary_union(fixed_std_boxes))

        blocked_area = unary_union(blocked_parts) if blocked_parts else GeometryCollection()
        # placeable = die(/layout) - PLACEMENT blockage - fixed macros - fixed std cells
        non_placeable_parts = []
        if blockage_boxes:
            non_placeable_parts.append(unary_union(blockage_boxes))
        if fixed_macro_boxes:
            non_placeable_parts.append(unary_union(fixed_macro_boxes))
        if fixed_std_boxes:
            non_placeable_parts.append(unary_union(fixed_std_boxes))
        if non_placeable_parts:
            placeable_area = placeable_base.difference(unary_union(non_placeable_parts))
        else:
            placeable_area = placeable_base

        vr_regions = (
            self.partition_geometry(blocked_area, "virtual placement blockage")
            if not blocked_area.is_empty
            else []
        )

        def_region_geometries = {}
        used_def_region_geometry = GeometryCollection()
        for region_name, region_rectangles in self.die_info.get("region", {}).items():
            if region_name in {"vr", "evr"}:
                continue
            # DEF rects are authoritative rectangles: die clip only, no layout punch.
            clipped_boxes, region_geometry = self.def_rectangles_to_boxes(
                region_rectangles, die_rect, f"DEF region {region_name}"
            )
            if not clipped_boxes:
                continue
            def_region_geometries[region_name] = region_geometry
            used_def_region_geometry = unary_union(
                [used_def_region_geometry, region_geometry]
            )
            self.region_info[region_name] = {"area": clipped_boxes}

        if def_region_geometries:
            def_region_union = unary_union(list(def_region_geometries.values()))
            evr_geometry = placeable_area.difference(def_region_union)
        else:
            evr_geometry = placeable_area

        # Clip EVR to Y-band on the polygon once, then partition once.
        slice_bounds = self.parse_evr_slice_y_bounds(getattr(self, "params", None))
        if slice_bounds is not None:
            min_y, max_y = slice_bounds
            slice_band = box(
                float(self.die_info["xl"]),
                float(min_y),
                float(self.die_info["xh"]),
                float(max_y),
            )
            evr_geometry = evr_geometry.intersection(slice_band)
            print(
                "[LEF_DEF_analysis] EVR Y-slice applied before partition "
                "[{}, {}]".format(min_y, max_y)
            )

        self.region_info["vr"] = vr_regions
        self.region_info["evr"] = (
            self.partition_geometry_compact(evr_geometry, "evr placeable area")
            if not evr_geometry.is_empty
            else []
        )
        # Exact adjacent merge only. No union / preferred-width retile / re-slice.
        self.maybe_repartition_evr()
        self.maybe_split_evr_from_boundary_file()
        self.maybe_slice_evr()
        self.maybe_compute_evr_islands()
        self.set_virtualRegion_to_cell()
        self.apply_group_regions_to_cells(def_region_geometries.keys())
        print(
            "[LEF_DEF_analysis] placeable = die/layout - placement blockage "
            "- fixed macros - fixed std cells "
            "(blockages={}, fixed_macros={}, fixed_std_cells={})".format(
                len(blockage_boxes), len(fixed_macro_boxes), len(fixed_std_boxes)
            )
        )
        print(f"[LEF_DEF_analysis] {len(self.region_info['vr'])} virtual placement blockages (vr)")
        if "evr" in self.region_info:
            print(f"[LEF_DEF_analysis] {len(self.region_info['evr'])} placeable regions (evr)")
        if "evr_boundary" in self.region_info:
            print(
                f"[LEF_DEF_analysis] {len(self.region_info['evr_boundary'])} boxes (evr_boundary), "
                f"{len(self.region_info.get('evr_center', []))} boxes (evr_center)"
            )
        print(f"[LEF_DEF_analysis] {len(def_region_geometries)} DEF regions added")
        self.build_fixed_macro_footprint_map(die_rect)

    def build_fixed_macro_footprint_map(self, die_rect=None):
        """OBS polygon rings for non-rect fixed macros, keyed by DEF instance name."""
        if die_rect is None:
            die_rect = box(
                self.die_info["xl"],
                self.die_info["yl"],
                self.die_info["xh"],
                self.die_info["yh"],
            )
        footprint_map = {}
        for name, geom in self._iter_fixed_cell_footprints(die_rect, {"BLOCK", "MACRO"}):
            rings = self._footprint_exterior_rings(geom)
            if rings:
                footprint_map[str(name)] = rings
        self.fixed_macro_footprint_map = footprint_map
        save_path = getattr(self.params, "save_path", None)
        if save_path:
            out_path = os.path.join(save_path, "fixed_macro_footprint_map.json")
            with open(out_path, "w") as w:
                json.dump(footprint_map, w)
            print(
                f"[LEF_DEF_analysis] fixed_macro_footprint_map: "
                f"{len(footprint_map)} non-rect macros -> {out_path}"
            )
        return footprint_map

    def _max_movable_macro_size(self):
        """Max width/height (DEF units) among movable BLOCK/MACRO cells."""
        def_scale = float(self.die_info.get("def_scale", 1.0))
        max_width = 0.0
        max_height = 0.0
        count = 0
        widest_name = None
        for cell_key in self.lef_def.cellInfo.keys():
            cell_instance = self.lef_def.cellInfo[cell_key]
            name = cell_instance.get_name()
            if name in {"virtual0", "virtual1"}:
                continue
            lef_instance = cell_instance.get_lef_info()
            cell_type = str(lef_instance.get_cell_type()).upper()
            if cell_type not in {"BLOCK", "MACRO"}:
                continue
            if cell_instance.get_placed_state() == "FIXED":
                continue
            width = float(lef_instance.get_width()) * def_scale
            height = float(lef_instance.get_height()) * def_scale
            count += 1
            if width > max_width:
                max_width = width
                widest_name = name
            if height > max_height:
                max_height = height
        return max_width, max_height, count, widest_name

    def _max_evr_movable_macro_size(self):
        """Deprecated alias: max size among all movable macros."""
        return self._max_movable_macro_size()

    def _max_evr_movable_macro_width(self):
        max_width, _max_height, count, widest_name = self._max_movable_macro_size()
        return max_width, count, widest_name

    @staticmethod
    def _partition_by_y_only(geometry, label):
        """
        Partition a (preferably vertical-slab) rectilinear geometry into rectangles
        using only horizontal (Y) cuts. Does not introduce extra vertical cuts.
        """
        if geometry is None or geometry.is_empty:
            return []
        regions = []
        for polygon in Analysis._iter_polygons(geometry):
            minx, miny, maxx, maxy = map(float, polygon.bounds)
            _xs, ys_edge = Analysis._polygon_xy_coords(polygon)
            ys = Analysis._snap_sorted_coords(set(ys_edge) | {miny, maxy}, abs_tol=1.0)
            if len(ys) < 2:
                continue
            for yl, yh in zip(ys[:-1], ys[1:]):
                if yh <= yl:
                    continue
                band = box(minx, yl, maxx, yh).intersection(polygon)
                if band.is_empty or band.area <= 0.0:
                    continue
                for piece in Analysis._iter_polygons(band):
                    # Skip holed leftovers so island macros are not filled back in.
                    if piece.interiors:
                        continue
                    if Analysis._is_nearly_rectangle(piece):
                        rect = Analysis._finite_rect(piece.bounds)
                        if rect is not None:
                            regions.append(rect)
                    else:
                        # Local X cuts only (no recursive full partition).
                        xs, _ = Analysis._polygon_xy_coords(piece)
                        xs = Analysis._snap_sorted_coords(xs, abs_tol=1.0)
                        pminx, pminy, pmaxx, pmaxy = map(float, piece.bounds)
                        xs = Analysis._snap_sorted_coords(
                            set(xs) | {pminx, pmaxx}, abs_tol=1.0
                        )
                        for xa, xb in zip(xs[:-1], xs[1:]):
                            if xb <= xa:
                                continue
                            frag = piece.intersection(box(xa, pminy, xb, pmaxy))
                            for p2 in Analysis._iter_polygons(frag):
                                if p2.interiors:
                                    continue
                                if Analysis._is_nearly_rectangle(p2):
                                    r = Analysis._finite_rect(p2.bounds)
                                    if r is not None:
                                        regions.append(r)
        return regions

    @staticmethod
    def partition_geometry_preferred_width(geometry, pref_w, label):
        """
        Partition into rectangles preferring a constant vertical-strip width pref_w.
        Global X cuts every pref_w; within each strip only Y cuts are used so the
        preferred width is not shredded by interior edge X coordinates.
        """
        if geometry is None or geometry.is_empty:
            return []
        pref_w = float(pref_w) if pref_w is not None else 0.0
        if pref_w <= 0.0:
            return Analysis.partition_geometry(geometry, label)

        gminx, gminy, gmaxx, gmaxy = map(float, geometry.bounds)
        xs = Analysis._append_preferred_cuts(set(), gminx, gmaxx, pref_w)
        if len(xs) < 2:
            return Analysis.partition_geometry(geometry, label)

        pad = max(1.0, 1e-6 * max(abs(gmaxy - gminy), 1.0))
        regions = []
        area_tol_rel = 1e-9
        area_tol_abs = 1e-3
        for xl, xh in zip(xs[:-1], xs[1:]):
            if xh <= xl:
                continue
            slab = box(xl, gminy - pad, xh, gmaxy + pad).intersection(geometry)
            if slab.is_empty or slab.area <= 0.0:
                continue
            regions.extend(Analysis._partition_by_y_only(slab, f"{label} slab"))

        regions = sorted(set(regions))
        partition_area = sum((xh - xl) * (yh - yl) for xl, yl, xh, yh in regions)
        area_diff = abs(partition_area - geometry.area)
        if area_diff > max(area_tol_abs, area_tol_rel * max(abs(geometry.area), 1.0)):
            print(
                f"[LEF_DEF_analysis] {label} preferred-width partition area mismatch "
                f"({partition_area} vs {geometry.area}); fallback to strip partition"
            )
            return Analysis.partition_geometry(geometry, label)
        return [list(region) for region in regions]

    def maybe_repartition_evr(self):
        """
        Compact every multi-box region with exact adjacent-rectangle merge only.
        Area must match before/after; otherwise the region is left unchanged.
        """
        for key in list(self.region_info.keys()):
            raw = self.region_info[key]
            as_dict = isinstance(raw, dict)
            if as_dict:
                boxes = raw.get("area")
                if not isinstance(boxes, list) or len(boxes) <= 1:
                    continue
            elif isinstance(raw, list):
                boxes = raw
                if len(boxes) <= 1:
                    continue
            else:
                continue

            before = [list(map(float, r)) for r in boxes]
            area_before = sum((r[2] - r[0]) * (r[3] - r[1]) for r in before)
            after = [
                list(r)
                for r in Analysis._merge_adjacent_rectangles(before, abs_tol=1.0)
            ]
            area_after = sum((r[2] - r[0]) * (r[3] - r[1]) for r in after)
            area_diff = abs(area_after - area_before)
            if area_diff > max(1e-3, 1e-12 * max(abs(area_before), 1.0)):
                print(
                    "[LEF_DEF_analysis] skip merge for {}: area would change "
                    "({:.6g} vs {:.6g})".format(key, area_after, area_before)
                )
                continue
            if len(after) == len(before):
                continue
            if as_dict:
                new_raw = dict(raw)
                new_raw["area"] = after
                self.region_info[key] = new_raw
            else:
                self.region_info[key] = after
            print(
                "[LEF_DEF_analysis] compacted {} boxes {} -> {} "
                "(exact adjacent merge, area preserved)".format(
                    key, len(before), len(after)
                )
            )

    @staticmethod
    def _read_evr_boundary_boxes_file(path):
        """Read xl yl xh yh boxes from a text file (one rectangle per line)."""
        boxes = []
        with open(path, "r") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.replace(",", " ").split()
                if len(parts) != 4:
                    raise ValueError(
                        "[LEF_DEF_analysis] evr_boundary_boxes_path line {} "
                        "must have 4 numbers (xl yl xh yh), got: {!r}".format(
                            line_no, line
                        )
                    )
                xl, yl, xh, yh = [float(v) for v in parts]
                if xh <= xl or yh <= yl:
                    raise ValueError(
                        "[LEF_DEF_analysis] invalid box on line {}: [{}, {}, {}, {}]".format(
                            line_no, xl, yl, xh, yh
                        )
                    )
                boxes.append([xl, yl, xh, yh])
        return boxes

    def maybe_split_evr_from_boundary_file(self):
        """
        Split EVR using an external boundary-box file.

        When params.evr_boundary_boxes_path is set (db_option=def):
          1) read boxes (xl yl xh yh per line)
          2) union boxes (and clip to EVR) to reduce fragment count
          3) evr_boundary = partitioned (union ∩ EVR)
          4) evr_center = partitioned (EVR − boundary)
        MakeDB later assigns EVR movable macros -> evr_boundary,
        other EVR-tagged cells -> evr_center.
        """
        params = getattr(self, "params", None)
        if params is None:
            return
        if not self._is_def_like_db_option():
            return
        path = getattr(params, "evr_boundary_boxes_path", "") or ""
        path = str(path).strip()
        if not path:
            return
        if not os.path.exists(path):
            raise ValueError(
                "evr_boundary_boxes_path does not exist: {}".format(path)
            )
        if "evr" not in self.region_info or not self.region_info["evr"]:
            print(
                "[LEF_DEF_analysis] evr_boundary_boxes_path set but EVR is empty; skip"
            )
            return

        file_boxes = self._read_evr_boundary_boxes_file(path)
        if not file_boxes:
            raise ValueError(
                "evr_boundary_boxes_path is empty: {}".format(path)
            )
        # Analysis is in original DEF units; PlaceDB applies square_stretch later.

        evr_boxes = [list(map(float, rect)) for rect in self.region_info["evr"]]
        evr_union = self.rectangles_to_unclipped_geometry(evr_boxes, "EVR union")
        boundary_raw = self.rectangles_to_unclipped_geometry(
            file_boxes, "evr_boundary file boxes"
        )
        if boundary_raw.is_empty:
            raise ValueError(
                "[LEF_DEF_analysis] no valid boxes in {}".format(path)
            )

        # Union(+clip to EVR) so overlapping/file-adjacent boxes collapse.
        boundary_geom = boundary_raw.intersection(evr_union)
        if boundary_geom.is_empty:
            raise ValueError(
                "[LEF_DEF_analysis] evr_boundary file boxes do not overlap EVR"
            )
        center_geom = evr_union.difference(boundary_geom)
        if center_geom.is_empty:
            raise ValueError(
                "[LEF_DEF_analysis] EVR center empty after applying boundary file; "
                "check {}".format(path)
            )

        boundary_boxes = self.partition_geometry(
            boundary_geom, "evr_boundary from file (unioned)"
        )
        use_pref_center = bool(int(getattr(params, "evr_center_use_preferred_partition", 0)))
        if use_pref_center:
            max_macro_width, max_macro_height, macro_count, _name = (
                self._max_movable_macro_size()
            )
            ratio_w = float(
                getattr(
                    params,
                    "evr_partition_width_ratio",
                    getattr(params, "evr_partition_block_width_ratio", 1.5),
                )
            )
            ratio_h = float(getattr(params, "evr_partition_block_height_ratio", 1.0))
            pref_w = max(max_macro_width * ratio_w, 1.0) if macro_count else 0.0
            pref_h = (
                max(max_macro_height * ratio_h, 1.0)
                if macro_count and max_macro_height > 0
                else pref_w
            )
            center_boxes = self.partition_geometry_preferred(
                center_geom, pref_w, pref_h, "evr_center preferred"
            )
        else:
            center_boxes = self.partition_geometry(center_geom, "evr_center strips")

        if not boundary_boxes:
            raise ValueError("[LEF_DEF_analysis] partitioned evr_boundary is empty")
        if not center_boxes:
            raise ValueError("[LEF_DEF_analysis] partitioned evr_center is empty")

        del self.region_info["evr"]
        self.region_info["evr_boundary"] = boundary_boxes
        self.region_info["evr_center"] = center_boxes
        print(
            "[LEF_DEF_analysis] split EVR from file {} -> "
            "evr_boundary/evr_center "
            "(file_boxes={}, boundary_boxes_after_union_partition={}, "
            "center_boxes={}, boundary_area={:.6g}, center_area={:.6g}, "
            "center_preferred={})".format(
                path,
                len(file_boxes),
                len(boundary_boxes),
                len(center_boxes),
                float(boundary_geom.area),
                float(center_geom.area),
                int(use_pref_center),
            )
        )

    @staticmethod
    def parse_evr_slice_y_bounds(params):
        """
        Return (min_y, max_y) in database units, or None if slicing is disabled.
        Both evr_slice_min_y and evr_slice_max_y must be set with max_y > min_y.
        """
        if params is None:
            return None
        min_raw = getattr(params, "evr_slice_min_y", None)
        max_raw = getattr(params, "evr_slice_max_y", None)
        if min_raw is None or max_raw is None:
            return None
        if isinstance(min_raw, str) and not min_raw.strip():
            return None
        if isinstance(max_raw, str) and not max_raw.strip():
            return None
        min_y = float(min_raw)
        max_y = float(max_raw)
        if not (max_y > min_y):
            raise ValueError(
                "evr_slice_max_y ({}) must be > evr_slice_min_y ({})".format(
                    max_y, min_y
                )
            )
        return min_y, max_y

    @staticmethod
    def _normalize_region_box_list(raw):
        """Normalize region_info entry to a list of [xl, yl, xh, yh]."""
        if raw is None:
            return []
        if isinstance(raw, dict):
            raw = raw.get("area", [])
        if not raw:
            return []
        if all(isinstance(v, (int, float)) for v in raw):
            if len(raw) % 4 != 0:
                raise ValueError(
                    "flat region coordinates length must be multiple of 4, got {}".format(
                        len(raw)
                    )
                )
            return [list(map(float, raw[i : i + 4])) for i in range(0, len(raw), 4)]
        boxes = []
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 4:
                boxes.append([float(v) for v in item])
        return boxes

    @staticmethod
    def apply_evr_y_slice_to_region_info(region_info, min_y, max_y, xl, xh):
        """
        Clip evr / evr_boundary / evr_center boxes to y in [min_y, max_y].
        Mutates region_info in place. Returns summary dict.
        """
        if region_info is None:
            return {"changed": False, "keys": {}}
        slice_rect = box(float(xl), float(min_y), float(xh), float(max_y))
        if slice_rect.is_empty or float(xh) <= float(xl):
            raise ValueError(
                "invalid EVR slice window xl={}, xh={}, min_y={}, max_y={}".format(
                    xl, xh, min_y, max_y
                )
            )

        summary = {"changed": False, "keys": {}}
        for key in ("evr", "evr_boundary", "evr_center"):
            if key not in region_info:
                continue
            raw = region_info[key]
            as_dict = isinstance(raw, dict)
            boxes_before = Analysis._normalize_region_box_list(raw)
            if not boxes_before:
                continue
            boxes_after = []
            for bx in boxes_before:
                bxl, byl, bxh, byh = map(float, bx)
                cyl = max(byl, float(min_y))
                cyh = min(byh, float(max_y))
                if cyh > cyl and bxh > bxl:
                    boxes_after.append([bxl, cyl, bxh, cyh])
            if not boxes_after:
                raise ValueError(
                    "[LEF_DEF_analysis] {} empty after evr_slice_y [{}, {}]; "
                    "check evr_slice_min_y / evr_slice_max_y".format(key, min_y, max_y)
                )
            # Exact merge after clip; reject if area would change.
            clipped_area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes_after)
            merged = [
                list(r)
                for r in Analysis._merge_adjacent_rectangles(boxes_after, abs_tol=1.0)
            ]
            merged_area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in merged)
            if abs(merged_area - clipped_area) <= max(
                1e-3, 1e-12 * max(abs(clipped_area), 1.0)
            ):
                boxes_after = merged
            if as_dict:
                region_info[key] = dict(raw)
                region_info[key]["area"] = boxes_after
            else:
                region_info[key] = boxes_after
            y0 = min(b[1] for b in boxes_after)
            y1 = max(b[3] for b in boxes_after)
            summary["keys"][key] = {
                "boxes_before": len(boxes_before),
                "boxes_after": len(boxes_after),
                "y_after": [y0, y1],
            }
            summary["changed"] = True
        return summary

    def maybe_slice_evr(self):
        """
        Clip EVR fence geometry to params.evr_slice_min_y / evr_slice_max_y.

        Runs for db_option=def / def_convert_macro2port after repartition/split.
        Named DEF fence regions outside the band are left unchanged.
        """
        params = getattr(self, "params", None)
        if params is None or not self._is_def_like_db_option():
            return
        bounds = self.parse_evr_slice_y_bounds(params)
        if bounds is None:
            return
        min_y, max_y = bounds
        xl = float(self.die_info["xl"])
        xh = float(self.die_info["xh"])
        summary = self.apply_evr_y_slice_to_region_info(
            self.region_info, min_y, max_y, xl, xh
        )
        if not summary.get("changed"):
            print(
                "[LEF_DEF_analysis] evr_slice_y [{}, {}] set but no evr* regions to clip".format(
                    min_y, max_y
                )
            )
            return
        detail = ", ".join(
            "{}: {}->{} boxes y=[{:.0f},{:.0f}]".format(
                key,
                info["boxes_before"],
                info["boxes_after"],
                info["y_after"][0],
                info["y_after"][1],
            )
            for key, info in summary["keys"].items()
        )
        print(
            "[LEF_DEF_analysis] applied evr_slice_y [{}, {}] ({})".format(
                min_y, max_y, detail
            )
        )

    @staticmethod
    def collect_evr_boxes(region_info):
        boxes = []
        if not region_info:
            return boxes
        for key in ("evr", "evr_boundary", "evr_center"):
            raw = region_info.get(key)
            if raw is None:
                continue
            if isinstance(raw, dict):
                raw = raw.get("area") or []
            for rect in raw or []:
                if rect is None or len(rect) < 4:
                    continue
                xl, yl, xh, yh = [float(v) for v in rect[:4]]
                if xh > xl and yh > yl:
                    boxes.append([xl, yl, xh, yh])
        return boxes

    @staticmethod
    def compute_evr_islands_from_boxes(
        boxes,
        neck_ratio=0.05,
        neck_width=0.0,
        n_samples=400,
        min_height_ratio=0.02,
    ):
        """
        Split EVR rectangles into large Y-stacked islands.

        Fully disconnected components are always split. Thin horizontal necks
        (width < neck_width, or < neck_ratio * median width) are also cuts,
        so a weakly connected EVR still becomes a few large islands.
        """
        boxes = [
            [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
            for b in (boxes or [])
            if b is not None and len(b) >= 4 and float(b[2]) > float(b[0]) and float(b[3]) > float(b[1])
        ]
        empty = {
            "neck_ratio": float(neck_ratio or 0.0),
            "neck_width": float(neck_width or 0.0),
            "median_width": 0.0,
            "evr_bounds": None,
            "islands": [],
        }
        if not boxes:
            return empty
        geoms = [box(b[0], b[1], b[2], b[3]) for b in boxes]
        geom = unary_union(geoms)
        if geom is None or geom.is_empty:
            return empty
        if isinstance(geom, Polygon):
            components = [geom]
        elif isinstance(geom, MultiPolygon):
            components = [g for g in geom.geoms if g is not None and not g.is_empty]
        else:
            components = [g for g in getattr(geom, "geoms", []) or [] if g is not None and not g.is_empty]
            if not components:
                components = [geom]
        minx, miny, maxx, maxy = [float(v) for v in geom.bounds]
        all_widths = []
        bands = []
        for comp in components:
            cminx, cminy, cmaxx, cmaxy = [float(v) for v in comp.bounds]
            cheight = cmaxy - cminy
            if cheight <= 0.0:
                continue
            # Width-neck scan inside this component. True Y-gaps are already
            # separate components from unary_union.
            n_local = max(int(n_samples), 80)
            ys = np.linspace(cminy, cmaxy, n_local)
            dy = cheight / float(max(n_local - 1, 1))
            half = max(dy * 0.49, 1.0)
            widths = np.zeros(n_local, dtype=np.float64)
            for i, y in enumerate(ys):
                sl = box(cminx - 1.0, float(y) - half, cmaxx + 1.0, float(y) + half)
                inter = comp.intersection(sl)
                if inter is None or inter.is_empty:
                    widths[i] = 0.0
                else:
                    widths[i] = float(inter.area) / max(2.0 * half, 1e-9)
            all_widths.append(widths)
            positive = widths[widths > 1.0]
            median_w = float(np.median(positive)) if positive.size else 0.0
            neck = float(neck_width or 0.0)
            if neck <= 0.0:
                neck = max(0.0, float(neck_ratio or 0.0) * median_w)
            inside = widths >= max(neck, 1.0)
            start = None
            for i, ok in enumerate(inside):
                if ok and start is None:
                    start = i
                elif (not ok) and start is not None:
                    bands.append((float(ys[start]) - half, float(ys[i - 1]) + half, neck, median_w))
                    start = None
            if start is not None:
                bands.append((float(ys[start]) - half, float(ys[-1]) + half, neck, median_w))

        if not bands:
            return empty
        used_neck = max(b[2] for b in bands)
        used_median = max(b[3] for b in bands)
        min_h = max(float(min_height_ratio or 0.0) * (maxy - miny), 1.0)
        islands = []
        for yl0, yh0, _neck, _med in bands:
            yl = max(miny, yl0)
            yh = min(maxy, yh0)
            if yh <= yl:
                continue
            clipped = []
            area = 0.0
            for xl, y0, xh, y1 in boxes:
                ny0 = max(y0, yl)
                ny1 = min(y1, yh)
                if ny1 <= ny0:
                    continue
                clipped.append([xl, ny0, xh, ny1])
                area += (xh - xl) * (ny1 - ny0)
            if not clipped or area <= 0.0:
                continue
            ixl = min(r[0] for r in clipped)
            ixh = max(r[2] for r in clipped)
            iyl = min(r[1] for r in clipped)
            iyh = max(r[3] for r in clipped)
            if (iyh - iyl) < min_h and area < 0.01 * float(geom.area):
                continue
            islands.append(
                {
                    "id": len(islands),
                    "xl": float(ixl),
                    "yl": float(iyl),
                    "xh": float(ixh),
                    "yh": float(iyh),
                    "area": float(area),
                    "boxes": clipped,
                }
            )
        islands.sort(key=lambda rec: (rec["yl"], rec["xl"]))
        for i, rec in enumerate(islands):
            rec["id"] = i
        return {
            "neck_ratio": float(neck_ratio or 0.0),
            "neck_width": float(used_neck),
            "median_width": float(used_median),
            "evr_bounds": [minx, miny, maxx, maxy],
            "islands": islands,
        }

    def maybe_compute_evr_islands(self):
        """Build EVR Y-islands during def / def_convert_macro2port and keep on self."""
        params = getattr(self, "params", None)
        if params is None or not self._is_def_like_db_option():
            self.evr_islands = {"islands": []}
            return
        boxes = self.collect_evr_boxes(self.region_info)
        payload = self.compute_evr_islands_from_boxes(
            boxes,
            neck_ratio=float(getattr(params, "evr_island_neck_ratio", 0.05) or 0.0),
            neck_width=float(getattr(params, "evr_island_neck_width", 0.0) or 0.0),
        )
        self.evr_islands = payload
        n = len(payload.get("islands") or [])
        print(
            "[LEF_DEF_analysis] EVR islands={} neck_width={:.6g} median_width={:.6g}".format(
                n,
                float(payload.get("neck_width") or 0.0),
                float(payload.get("median_width") or 0.0),
            )
        )
        for rec in payload.get("islands") or []:
            print(
                "[LEF_DEF_analysis]   island{} y=[{:.0f},{:.0f}] x=[{:.0f},{:.0f}] "
                "boxes={} area={:.6g}".format(
                    rec["id"],
                    rec["yl"],
                    rec["yh"],
                    rec["xl"],
                    rec["xh"],
                    len(rec.get("boxes") or []),
                    rec["area"],
                )
            )

    @staticmethod
    def save_evr_islands(payload, folder):
        if not folder:
            return
        path = os.path.join(folder, "evr_islands.json")
        with open(path, "w") as w:
            json.dump(payload or {"islands": []}, w, indent=2)
        n = len((payload or {}).get("islands") or [])
        print("[LEF_DEF_analysis] wrote {} ({} islands)".format(path, n))

    @staticmethod
    def load_evr_islands(folder):
        if not folder:
            return None
        path = os.path.join(folder, "evr_islands.json")
        if not os.path.exists(path):
            return None
        with open(path, "r") as f:
            return json.load(f)

    def get_virtual_region(self, extPinInfo=None):
        self.build_region_info()
        #self.virtualDieBlockage.plot(self.params.save_path + '/die_layout.png', extPinInfo)
    
    
    def get_cell_info(self):
        cell_info = {}
        for cell_name in self.cell_list_in_nets:
            cell = self.node_name2index_map[cell_name]
            cell_instance = self.lef_def.cellInfo[cell]
            lef_instance = self.lef_def.cellInfo[cell].get_lef_info()
            cell_type = lef_instance.get_cell_type()
            if cell_type == 'PIN':
                continue
            if self.node_name2index_map[cell_instance.get_name()] != cell_name:
                raise ValueError(f'Inconsistent cell name: {self.node_name2index_map[cell_instance.get_name()]} vs {cell_name}')
            cell_info[cell_name] = dict()
            cell_info[cell_name]['macro_id'] = self.lef_macro_name2index_map[lef_instance.get_name()]
            cell_info[cell_name]['cell_type'] = lef_instance.get_cell_type()
            cell_info[cell_name]['placed_state'] = cell_instance.get_placed_state()        
            cell_info[cell_name]['position'] = cell_instance.get_pos() if type(cell_instance.get_pos()) == list else cell_instance.get_pos().tolist()
            cell_info[cell_name]['orientation'] = cell_instance.get_orientation()
            cell_info[cell_name]['region'] = cell_instance.get_region()
        return cell_info      
            
    def get_die_info(self):
        dieInfo = {}
        self.lef_def.dieInfo.make_row_list()
        dieInfo['xl'] = self.lef_def.dieInfo.get_xl()
        dieInfo['yl'] = self.lef_def.dieInfo.get_yl()
        dieInfo['xh'] = self.lef_def.dieInfo.get_xh()
        dieInfo['yh'] = self.lef_def.dieInfo.get_yh()
        dieInfo['site_width'] = float(self.lef_def.dieInfo.get_site_width())
        dieInfo['row_height'] = float(self.lef_def.dieInfo.get_row_height())
        dieInfo['def_scale'] = float(self.lef_def.dieInfo.get_def_scale())
        dieInfo['lef_scale'] = float(self.lef_def.dieInfo.get_def_scale())
        dieInfo['layout'] = self.lef_def.dieInfo.get_layout().flatten().tolist()
        dieInfo['layout_xl'] = float(self.lef_def.dieInfo.get_layout_xl())
        dieInfo['layout_yl'] = float(self.lef_def.dieInfo.get_layout_yl())
        dieInfo['layout_xh'] = float(self.lef_def.dieInfo.get_layout_xh())
        dieInfo['layout_yh'] = float(self.lef_def.dieInfo.get_layout_yh())
        dieInfo['blockage'] = self.lef_def.dieInfo.get_blockage()
        dieInfo['region'] = self.lef_def.dieInfo.get_region()
        dieInfo['group'] = self.lef_def.dieInfo.get_group()
        return dieInfo
    
    
    
    def add_virtual_cell(self, cell_name:str, lef_name:str, net_name:str, net_use:str):
        """_summary_
        1. creates LEF instance by copying an instance of LEF for cell type = 'CORE'
        2. LEF instance for copy should not be a clustered cell due to their large size
        """
        
        # Create or get LEF macro index
        lef_idx = self.lef_macro_name_to_id.get_or_create(lef_name)

        # Create or get LEF instance
        if lef_idx in self.lef_def.cellInfo4LEF.keys():
            new_lef_instance = self.lef_def.cellInfo4LEF[lef_idx]
        else:
            new_lef_instance = LEF(lef_idx)
            new_lef_instance.set_width(0.1)
            new_lef_instance.set_height(0.1)
            new_lef_instance.set_symmetry('X Y ')
            new_lef_instance.set_cell_type('CORE')

        # Create or get pin index for virtual pin A
        pin_idx = self.pin_name_to_id.get_or_create('A')

        # Ensure virtual LEF has pin A
        pin_names = [pin.get_name() for pin in new_lef_instance.get_pins()]
        if pin_idx not in pin_names:
            new_pin_instance = Pin(pin_idx, new_lef_instance)
            new_pin_instance.set_layer_rectangle('C4', [[0, 50, 70, 80]])
            new_pin_instance.set_direction('INPUT')
            new_pin_instance.set_use('C4', net_use)
            new_lef_instance.add_pin(new_pin_instance)

        # Create new cell instance
        cell_idx = self.node_name_to_id.get_or_create(cell_name)

        new_cell_instance = Cell(cell_idx)
        new_cell_instance.set_lef_info(new_lef_instance)

        # Create or get the net instance
        net_idx = self.net_name_to_id.get_or_create(net_name)

        if net_idx not in self.lef_def.netInfo.keys():
            new_net_instance = Net(net_idx)
        else:
            new_net_instance = self.lef_def.netInfo[net_idx]

        # Add pins to the net
        for pin in new_lef_instance.get_pins():
            if pin.get_name() != pin_idx:
                continue
            new_net_instance.add_cell_pin(new_cell_instance, pin.get_name())
            new_net_instance.set_use(net_use)
            pin.add_net(new_cell_instance, net_idx)

        # Update the data structures
        self.lef_def.cellInfo4LEF[lef_idx] = new_lef_instance
        self.lef_def.cellInfo[cell_idx] = new_cell_instance
        self.lef_def.netInfo[net_idx] = new_net_instance
        
    
    def get_lef_info(self):
        lef_info = dict()
        for idx in self.lef_def.cellInfo4LEF.keys():
            name = self.lef_macro_name2index_map[idx] 
            lef_instance = self.lef_def.cellInfo4LEF[idx]
            cell_type = lef_instance.get_cell_type()
            if cell_type == 'PIN':
                continue
            width = lef_instance.get_width()
            height = lef_instance.get_height()
            symmetry = lef_instance.get_symmetry()
            cell_type = lef_instance.get_cell_type()
            lef_info[name] = {"width": float(width),
                              "height": float(height),
                              "symmetry": symmetry,
                              "class": cell_type,
                              "pin": dict()
                              }
            pins = lef_instance.get_pins()
            for pin in pins:
                pin_name = self.pin_name2index_map[pin.get_name()]
                direction = pin.get_direction()
                
                lef_info[name]['pin'][pin_name] = {"direction": direction,
                                                    "LAYER": dict()
                                                    }
                layers = pin.get_layers()
                for layer_name in layers:
                    try:
                        use = pin.get_use(layer_name)
                    except:
                        use = 'SIGNAL'
                        print(f"[WARNING] PIN {pin} of {name} IN LEF HAS NO USE DEFINITION FOR LAYER {layer_name}. FORCE TO SET USE TO SIGNAL")
                    rectangles = layers[layer_name]['rectangles'].tolist()
                    lef_info[name]['pin'][pin_name]['LAYER'][layer_name] = rectangles
                    lef_info[name]['pin'][pin_name]['use'] = use

        return lef_info
    
    
    
    @staticmethod
    def _resolve_useless_layer_list(params):
        """Return useless-layer names; missing key => empty list (no filtering)."""
        if params is None:
            return []
        if isinstance(params, dict):
            raw = params.get("useless_layer_list", None)
        else:
            raw = getattr(params, "useless_layer_list", None)
        if raw is None:
            return []
        if isinstance(raw, str):
            text = raw.strip()
            return [text] if text else []
        try:
            return [str(x) for x in list(raw) if str(x).strip()]
        except TypeError:
            return []

    def _pin_has_only_useless_layers(self, pin_instance, useless=None):
        """
        True when the pin has at least one layer and every layer is in
        useless_layer_list. Missing/empty useless_layer_list => False.
        """
        if useless is None:
            useless = set(self.useless_layer_list or [])
        if not useless:
            return False
        layers = pin_instance.get_layers() if pin_instance is not None else None
        if not layers:
            return False
        return all(layer_name in useless for layer_name in layers.keys())
    
    def get_netlist_info(self, filter_net_use='SIGNAL'):
        netInfo = {}
        net_cell_info = {}
        cell_net_info = {}
        cell_list_in_nets = set()
        std_cell_list_in_nets = set()
        macro_list_in_nets = set()
        ext_pin_list_in_nets = set()
        net_keys = list(self.lef_def.netInfo.keys())
        print("[LEF_DEF_analysis] building netlist_info from {} nets".format(len(net_keys)))
        useless_layers = set(self.useless_layer_list or [])
        ext_pins_useless = set(self.ext_pins_useless or [])
        skipped_useless_pins = 0
        nets_no_use = 0
        for net_i, net in enumerate(net_keys):
            if net_i and net_i % 50000 == 0:
                print(
                    "[LEF_DEF_analysis] netlist_info progress {}/{} nets "
                    "(kept={}, skipped_useless_pins={})".format(
                        net_i, len(net_keys), len(netInfo), skipped_useless_pins
                    )
                )
            if net == 'BLOCKAGE_BLOCKAGE':
                if net not in self.net_name2index_map:
                    continue

            netName = self.net_name2index_map[net] 

            #get net instance
            net_instance = self.lef_def.netInfo[net]
            
            # get net use
            net_use = net_instance.get_use()
            if net_use == None:
                net_use = "SIGNAL"
                nets_no_use += 1
                if nets_no_use <= 10:
                    print(f'[WARNING ] net {netName} has no use: use is redefined as SIGNAL')
                
            if net_use != filter_net_use:
                continue
            
            # get components
            net_components = net_instance.get_components() # {'cell pin name': {'pin': pinInstance, 'cell': cellINstance}}
                
            #get std cell list
            stdCellList = [inst.get_name() for inst in net_instance.get_stdCell_list()]
            stdCellNameList = [self.node_name2index_map[inst] for inst in stdCellList]
            stdCellNameSet = set(stdCellNameList)
            
            #get macro list
            macroList = [inst.get_name() for inst in net_instance.get_macro_list()]
            macroNameList = [self.node_name2index_map[inst] for inst in macroList]
            macroNameSet = set(macroNameList)
            
            #get extPin list
            #exclude pins included in ext_pin_useless
            extPinList = [
                inst.get_name()
                for inst in net_instance.get_extPin_list()
                if self.node_name2index_map[inst.get_name()] not in ext_pins_useless
            ]
            extPinNameList = [self.node_name2index_map[inst] for inst in extPinList]
            extPinNameSet = set(extPinNameList)
            allowed_cell_names = stdCellNameSet | macroNameSet | extPinNameSet

            #get not-used-cell list
            notUsedCellList = [inst.get_name() for inst in net_instance.get_not_used_cell_list()]
            notUsedCellNameSet = {
                self.node_name2index_map[inst] for inst in notUsedCellList
            }

            filtered_components = []
            for cell_pin_name in net_components.keys():
                if isinstance(cell_pin_name, tuple) and len(cell_pin_name) == 2:
                    cell_idx, pin_idx = cell_pin_name
                else:
                    cell_idx, pin_idx = cell_pin_name.split(' ')
                cell_name = self.node_name2index_map[int(cell_idx)]
                try:
                    pin_name = self.pin_name2index_map[int(pin_idx)]
                except Exception:
                    pin_name = cell_name

                is_virtual_cell = isinstance(cell_name, str) and cell_name.startswith('virtual')
                if pin_name == 'SD':
                    continue

                if int(cell_idx) in ext_pins_useless:
                    skipped_useless_pins += 1
                    continue

                pin_instance = net_components[cell_pin_name].get('pin')
                if self._pin_has_only_useless_layers(pin_instance, useless=useless_layers):
                    skipped_useless_pins += 1
                    continue

                #cell should be defined in the sum of stdCellList + macroList + extPinList
                #Virtual cells are internal fence-region anchors and must not be filtered out here.
                if (not is_virtual_cell) and (cell_name not in allowed_cell_names):
                    if cell_name not in notUsedCellNameSet:
                        raise ValueError(f'Cell {cell_name} not found in stdCellList + macroList + extPinList for net {netName}')
                    else:
                        print(f'[WARNING ] {cell_name} in Net {netName} is not considered due to its cell type')
                        continue

                is_ext_pin = net_components[cell_pin_name]['pin'].get_parent_LEF().get_cell_type() == 'PIN'
                filtered_components.append((cell_name, pin_name, is_ext_pin, is_virtual_cell))

            has_virtual_cell = any(is_virtual_cell for _, _, _, is_virtual_cell in filtered_components)
            if not has_virtual_cell:
                # Skip trivial nets after removing SD / useless-layer pins.
                if len(filtered_components) <= 1:
                    continue

            filtered_std_cells = [cell_name for cell_name, _, is_ext_pin, _ in filtered_components if (not is_ext_pin and cell_name in stdCellNameSet)]
            filtered_macros = [cell_name for cell_name, _, is_ext_pin, _ in filtered_components if (not is_ext_pin and cell_name in macroNameSet)]
            filtered_ext_pins = [cell_name for cell_name, _, is_ext_pin, _ in filtered_components if is_ext_pin]
            occupied = set(filtered_std_cells) | set(filtered_macros) | set(filtered_ext_pins)
            filtered_virtual_cells = [
                cell_name
                for cell_name, _, _, is_virtual_cell in filtered_components
                if is_virtual_cell and cell_name not in occupied
            ]

            #add cell to self.cell_list_in_nets
            for cell_ in filtered_std_cells:
                std_cell_list_in_nets.add(cell_)
                cell_list_in_nets.add(cell_)
            for cell_ in filtered_macros:
                macro_list_in_nets.add(cell_)
                cell_list_in_nets.add(cell_)
            for cell_ in filtered_virtual_cells:
                std_cell_list_in_nets.add(cell_)
                cell_list_in_nets.add(cell_)
            for ext_pin_ in filtered_ext_pins:
                ext_pin_list_in_nets.add(ext_pin_)

            
            #if net is empty after filtering, then skip it
            if len(filtered_std_cells + filtered_macros + filtered_ext_pins + filtered_virtual_cells) == 0:
                continue
            if len(filtered_components) == 0:
                continue
            
            #creates netInfo['cell_list']
            netInfo[netName] = {'cell_list': list(), 'use': None}
            netInfo[netName]['cell_list'] = []
            for cell_name, pin_name, is_ext_pin, _ in filtered_components:
                #add cell_to cell_net_info
                if cell_name not in cell_net_info:
                    cell_net_info[cell_name] = [netName]
                elif netName not in cell_net_info[cell_name]:
                    cell_net_info[cell_name].append(netName)
                
                #add net to netInfo       
                if is_ext_pin:
                    netInfo[netName]['cell_list'].append('PIN' + ' ' + pin_name)
                else:
                    netInfo[netName]['cell_list'].append(cell_name + ' ' + pin_name)
                
                #add cell to cell_list_in_nets
            
            #creates netInfo['use']
            netInfo[netName]['use'] = 'USE' + ' ' + net_use 
            
            #creates net_cell_info
            net_cell_info[netName] = {'std_cell': filtered_std_cells, 'macro': filtered_macros, 'ext_pin': filtered_ext_pins}
        print(
            "[LEF_DEF_analysis] netlist_info done: kept {}/{} SIGNAL nets "
            "(no-use-as-SIGNAL={}, skipped_useless_pins={})".format(
                len(netInfo),
                len(net_keys),
                nets_no_use,
                skipped_useless_pins,
            )
        )
        return netInfo, net_cell_info, cell_net_info, cell_list_in_nets, std_cell_list_in_nets, macro_list_in_nets, ext_pin_list_in_nets
    
    
    
    
    def get_extPin_info_use(self, filter_use='SIGNAL'):
        """_summary_
        get external pins of which use is 'SIGNAL'
        """
        #lef_def.extPinInfo[pinName] = {'pin_info': pin_pin, 'cell_info': pin_cell}
        ext_pin_info = {}
        useless = set(self.useless_layer_list or [])
        for ext_pin_idx in self.lef_def.extPinInfo.keys():
            ext_pin_name = self.node_name2index_map[ext_pin_idx]
            pin_instance = self.lef_def.extPinInfo[ext_pin_idx]['pin_info']
            cell_instance = self.lef_def.extPinInfo[ext_pin_idx]['cell_info']
            lef_instance = pin_instance.get_parent_LEF()
            
            
            layers = pin_instance.get_layers()
            layer_signal = []
            for layer_name in layers.keys():
                if layer_name in useless: #if layer is useless, then continue
                    continue
                use = pin_instance.get_use(layer_name)
                if use == filter_use: #get layer only for use == 'SIGNAL'
                    layer_signal.append(layer_name)
            
            if len(layer_signal) == 0:
                continue
            
            ext_pin_info[ext_pin_name] = dict()
            for layer_num, layer_name in enumerate(layer_signal):
                pos = cell_instance.get_pos().tolist()
                width = pin_instance.get_width() 
                height = pin_instance.get_height()
                direction = pin_instance.get_direction()
                ext_pin_info[ext_pin_name]['layer'+str(layer_num)] = {'layerName': layer_name, 
                                                         'position': pos, 
                                                         'orientation': 'N',
                                                         'width': float(width),
                                                         'height': float(height)
                                                         }
            ext_pin_info[ext_pin_name]['use'] = 'SIGNAL'
            ext_pin_info[ext_pin_name]['direction'] = direction
        return ext_pin_info

    def _db_option(self):
        return str(getattr(self.params, "db_option", "")).strip().lower()

    def _is_def_like_db_option(self):
        return self._db_option() in ("def", "def_convert_macro2port")

    def _force_square_stretch_enabled(self):
        """True when params.force_square_stretch is 1/True."""
        raw = getattr(self.params, "force_square_stretch", 0)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return int(raw) == 1
        text = str(raw).strip().lower()
        return text in ("1", "true", "yes", "on")

    def _convert_macro2port_enabled(self):
        """True when db_option=def_convert_macro2port or mode contains convert_macro2port."""
        if self._db_option() == "def_convert_macro2port":
            return True
        mode = str(getattr(self.params, "mode", "") or "")
        return "convert_macro2port" in mode

    def _active_stretch_scales(self):
        """Return (sx, sy) from the last force_square_stretch; default (1, 1)."""
        meta = getattr(self, "square_stretch", None) or {}
        return float(meta.get("sx", 1.0)), float(meta.get("sy", 1.0))

    @staticmethod
    def _parse_square_stretch_scale(raw):
        if raw is None:
            return "auto"
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw).strip().lower()
        if text in ("", "auto", "none"):
            return "auto"
        return float(text)

    def _resolve_square_stretch_factors(self):
        """
        Compute (sx, sy) with origin (xl, yl) fixed.
        Stretch only expands under scale=auto (never shrink to square).
        """
        xl = float(self.die_info["xl"])
        yl = float(self.die_info["yl"])
        xh = float(self.die_info["xh"])
        yh = float(self.die_info["yh"])
        width = xh - xl
        height = yh - yl
        if width <= 0.0 or height <= 0.0:
            raise ValueError(
                "force_square_stretch requires positive die size, got W={}, H={}".format(
                    width, height
                )
            )

        axis = str(getattr(self.params, "square_stretch_axis", "auto")).strip().lower()
        if axis not in ("x", "y", "auto"):
            raise ValueError(
                "square_stretch_axis must be x, y, or auto, got {}".format(axis)
            )
        scale_spec = self._parse_square_stretch_scale(
            getattr(self.params, "square_stretch_scale", "auto")
        )

        if axis == "auto":
            if abs(width - height) <= 1e-9 * max(width, height):
                chosen_axis = "none"
            elif width < height:
                chosen_axis = "x"
            else:
                chosen_axis = "y"
        else:
            chosen_axis = axis

        sx, sy = 1.0, 1.0
        if chosen_axis == "x":
            if scale_spec == "auto":
                sx = max(height / width, 1.0)
            else:
                if scale_spec <= 0.0:
                    raise ValueError("square_stretch_scale must be > 0")
                sx = float(scale_spec)
        elif chosen_axis == "y":
            if scale_spec == "auto":
                sy = max(width / height, 1.0)
            else:
                if scale_spec <= 0.0:
                    raise ValueError("square_stretch_scale must be > 0")
                sy = float(scale_spec)

        meta = {
            "enabled": True,
            "origin_xl": xl,
            "origin_yl": yl,
            "die_before": [xl, yl, xh, yh],
            "die_width_before": width,
            "die_height_before": height,
            "axis_param": axis,
            "axis_applied": chosen_axis,
            "scale_param": scale_spec if scale_spec == "auto" else float(scale_spec),
            "sx": float(sx),
            "sy": float(sy),
            "die_after": [xl, yl, xl + width * sx, yl + height * sy],
        }
        return sx, sy, meta

    @staticmethod
    def _stretch_abs_xy(x, y, ox, oy, sx, sy):
        return ox + (float(x) - ox) * sx, oy + (float(y) - oy) * sy

    @classmethod
    def _stretch_abs_box(cls, box4, ox, oy, sx, sy):
        xl, yl, xh, yh = [float(v) for v in box4[:4]]
        xl2, yl2 = cls._stretch_abs_xy(xl, yl, ox, oy, sx, sy)
        xh2, yh2 = cls._stretch_abs_xy(xh, yh, ox, oy, sx, sy)
        return [xl2, yl2, xh2, yh2]

    @classmethod
    def _stretch_local_box(cls, box4, sx, sy):
        a, b, c, d = [float(v) for v in box4[:4]]
        return [a * sx, b * sy, c * sx, d * sy]

    def _stretch_box_list(self, boxes, ox, oy, sx, sy, absolute=True):
        if boxes is None:
            return boxes
        out = []
        for box4 in boxes:
            if absolute:
                out.append(self._stretch_abs_box(box4, ox, oy, sx, sy))
            else:
                out.append(self._stretch_local_box(box4, sx, sy))
        return out

    def _stretch_region_value(self, value, ox, oy, sx, sy):
        """
        Stretch DEF region rectangles.
        Supported formats (same as rectangles_to_geometry):
          - nested: [[xl,yl,xh,yh], ...]
          - flat:   [xl,yl,xh,yh, xl,yl,xh,yh, ...]
          - one box:[xl,yl,xh,yh]
          - dict with 'area' / list values
        """
        if isinstance(value, np.ndarray):
            value = value.tolist()
        if isinstance(value, list):
            if not value:
                return value
            if isinstance(value[0], (list, tuple, np.ndarray)):
                return self._stretch_box_list(value, ox, oy, sx, sy, absolute=True)
            # Flat numeric list: one or more [xl,yl,xh,yh] packed contiguously.
            if all(isinstance(v, (int, float, np.integer, np.floating)) for v in value):
                if len(value) % 4 != 0:
                    raise ValueError(
                        "region rectangle coordinates should be multiple of 4, got len={}".format(
                            len(value)
                        )
                    )
                stretched = []
                for i in range(0, len(value), 4):
                    stretched.extend(
                        self._stretch_abs_box(value[i : i + 4], ox, oy, sx, sy)
                    )
                return stretched
            return value
        if isinstance(value, dict):
            out = {}
            for key, raw in value.items():
                if key == "area" or isinstance(raw, (list, tuple, np.ndarray)):
                    out[key] = self._stretch_region_value(raw, ox, oy, sx, sy)
                else:
                    out[key] = raw
            return out
        return value

    def _stretch_blockage_dict(self, blockage_dict, ox, oy, sx, sy):
        if not isinstance(blockage_dict, dict):
            return blockage_dict
        out = {}
        for name, blockage in blockage_dict.items():
            item = dict(blockage)
            coords = item.get("coords")
            if coords is not None and len(coords) == 4:
                item["coords"] = self._stretch_abs_box(coords, ox, oy, sx, sy)
            elif coords is not None and len(coords) >= 2 and len(coords) % 2 == 0:
                stretched = []
                for i in range(0, len(coords), 2):
                    x2, y2 = self._stretch_abs_xy(coords[i], coords[i + 1], ox, oy, sx, sy)
                    stretched.extend([x2, y2])
                item["coords"] = stretched
            out[name] = item
        return out

    def apply_square_stretch(self):
        """
        Deprecated: square stretch is applied in PlaceDB after MakeDB.

        Kept only so older call sites / docs do not break; Analysis builds stay
        in original DEF units.
        """
        print(
            "[LEF_DEF_analysis] apply_square_stretch is a no-op; "
            "stretch runs in PlaceDB after MakeDB (force_square_stretch)."
        )
        return

    @staticmethod
    def _virtual_fixed_macro_port_name(macro_name, pin_name):
        return "virtual_fixed_macro__port_{}_{}".format(macro_name, pin_name)

    @staticmethod
    def _orient_to_rotate_flip(orient):
        orient = str(orient or "N").strip().upper()
        if orient in ("UNKNOWN", "N", ""):
            return 0, "none"
        if orient == "S":
            return 180, "none"
        if orient == "W":
            return 90, "none"
        if orient == "E":
            return 270, "none"
        if orient == "FN":
            return 0, "vertical"
        if orient == "FS":
            return 180, "vertical"
        if orient == "FW":
            return 90, "vertical"
        if orient == "FE":
            return 270, "vertical"
        return 0, "none"

    @staticmethod
    def _lef_pin_offset_after_orient(px, py, width, height, orient):
        """
        Map a LEF-local pin offset (relative to macro LEF origin / lower-left)
        to the offset from the DEF COMPONENT origin after orientation.

        abs_pin = macro_def_origin + returned_offset
        """
        o = str(orient or "N").strip().upper()
        if o in ("N", "UNKNOWN", ""):
            return float(px), float(py)
        if o == "S":
            return float(width - px), float(height - py)
        if o == "W":
            return float(py), float(width - px)
        if o == "E":
            return float(height - py), float(px)
        if o == "FN":  # MY
            return float(width - px), float(py)
        if o == "FS":  # MX
            return float(px), float(height - py)
        if o == "FW":  # MX90
            return float(height - py), float(width - px)
        if o == "FE":  # MY90
            return float(py), float(px)
        return float(px), float(py)

    def _macro_def_origin(self, macro_name):
        """DEF COMPONENT origin (lower-left / LEF origin placement) of a macro."""
        cell_idx = self.node_name2index_map[macro_name]
        cell_instance = self.lef_def.cellInfo[cell_idx]
        pos = cell_instance.get_pos()
        if hasattr(pos, "tolist"):
            pos = pos.tolist()
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            raise ValueError(
                "[LEF_DEF_analysis] fixed macro {} has invalid DEF position: {!r}".format(
                    macro_name, pos
                )
            )
        return float(pos[0]), float(pos[1]), str(cell_instance.get_orientation() or "N")

    def _absolute_fixed_macro_pin_info(self, macro_name, pin_name):
        """
        Absolute die location of a fixed-macro pin:
          LEF pin center (relative to macro LL) * def_scale
          -> apply macro orientation
          -> add macro DEF origin (ll / placed origin)
        """
        if pin_name not in self.lef_info[self.cell_info[macro_name]["macro_id"]]["pin"]:
            raise ValueError(
                "[LEF_DEF_analysis] pin {} not found on fixed macro {} during port conversion".format(
                    pin_name, macro_name
                )
            )

        macro_id = self.cell_info[macro_name]["macro_id"]
        def_scale = float(self.die_info["def_scale"])
        pin_info = self.lef_info[macro_id]["pin"][pin_name]
        metal_layers = list(pin_info["LAYER"].keys())
        if not metal_layers:
            raise ValueError(
                "[LEF_DEF_analysis] pin {} on {} has no metal layers".format(pin_name, macro_name)
            )

        # LEF coordinates are relative to the macro's lower-left / LEF origin.
        pin_rect = pin_info["LAYER"][metal_layers[0]][0]
        lef_rel_x = (float(pin_rect[0]) + float(pin_rect[2])) / 2.0 * def_scale
        lef_rel_y = (float(pin_rect[1]) + float(pin_rect[3])) / 2.0 * def_scale
        cell_width = float(self.lef_info[macro_id]["width"]) * def_scale
        cell_height = float(self.lef_info[macro_id]["height"]) * def_scale

        macro_xl, macro_yl, orient = self._macro_def_origin(macro_name)
        placed_rel_x, placed_rel_y = self._lef_pin_offset_after_orient(
            lef_rel_x, lef_rel_y, cell_width, cell_height, orient
        )
        abs_x = macro_xl + placed_rel_x
        abs_y = macro_yl + placed_rel_y
        return {
            "layer_name": metal_layers[0],
            "position": [abs_x, abs_y],
            "direction": pin_info["direction"],
            "macro_origin": [macro_xl, macro_yl],
            "lef_relative": [lef_rel_x, lef_rel_y],
            "placed_relative": [placed_rel_x, placed_rel_y],
            "orientation": orient,
        }

    def _fixed_macro_names(self):
        fixed_macros = []
        for macro in self.macro_list_in_nets:
            if str(macro).startswith("BLOCKAGE"):
                continue
            cell_idx = self.node_name2index_map[macro]
            if self.lef_def.cellInfo[cell_idx].get_placed_state() == "FIXED":
                fixed_macros.append(macro)
        return fixed_macros

    def convert_fixed_macros_to_ports(self):
        """
        mode contains convert_macro2port:
          1) Convert every netlisted fixed-macro pin into a virtual port
             named virtual_fixed_macro__port_<macro>_<pin>, keeping pin
             absolute location/direction.
          2) Rewrite netlist PIN references and register ports in ext_pin_info.
          3) Delete fixed macros from components / cell maps, then rebuild
             total_cell_info so MakeDB reorders nodes from the new set.

        Does NOT change region_info / VR / EVR / lef_def.cellInfo:
        placeable regions are computed earlier from the original fixed macros.
        """
        if not self.region_info:
            print(
                "[LEF_DEF_analysis] WARNING: convert_macro2port with empty "
                "region_info (no placeable/VR built); fixed macros still removed "
                "from components only"
            )
        else:
            print(
                "[LEF_DEF_analysis] convert_macro2port: keeping precomputed "
                "region_info (keys={}) that already subtracted original fixed macros".format(
                    sorted(self.region_info.keys())
                )
            )

        fixed_macros = set(self._fixed_macro_names())
        if not fixed_macros:
            print("[LEF_DEF_analysis] convert_macro2port: no fixed macros to convert")
            return

        port_to_macro_pin = {}
        converted_pins = 0
        for net_name, net_data in self.netlist_info.items():
            new_cell_list = []
            for cell_pin in net_data.get("cell_list", []):
                parts = cell_pin.split(" ")
                if len(parts) != 2:
                    new_cell_list.append(cell_pin)
                    continue
                cell_name, pin_name = parts
                if cell_name == "PIN" or cell_name not in fixed_macros:
                    new_cell_list.append(cell_pin)
                    continue
                port_name = self._virtual_fixed_macro_port_name(cell_name, pin_name)
                if port_name not in self.ext_pin_info:
                    pin_abs = self._absolute_fixed_macro_pin_info(cell_name, pin_name)
                    self.ext_pin_info[port_name] = {
                        "layer0": {
                            "layerName": pin_abs["layer_name"],
                            # Absolute die center = macro DEF origin + oriented LEF-relative offset.
                            "position": pin_abs["position"],
                            "orientation": "N",
                            "width": 0.0,
                            "height": 0.0,
                        },
                        "use": "SIGNAL",
                        "direction": pin_abs["direction"],
                        "macro_origin": pin_abs["macro_origin"],
                        "lef_relative": pin_abs["lef_relative"],
                        "placed_relative": pin_abs["placed_relative"],
                        "macro_orientation": pin_abs["orientation"],
                    }
                    if converted_pins < 5:
                        print(
                            "[LEF_DEF_analysis] virtual port {}: "
                            "macro_ll=({}, {}) orient={} lef_rel=({:.3f},{:.3f}) "
                            "abs=({:.3f},{:.3f})".format(
                                port_name,
                                pin_abs["macro_origin"][0],
                                pin_abs["macro_origin"][1],
                                pin_abs["orientation"],
                                pin_abs["lef_relative"][0],
                                pin_abs["lef_relative"][1],
                                pin_abs["position"][0],
                                pin_abs["position"][1],
                            )
                        )
                    port_to_macro_pin[port_name] = (cell_name, pin_name)
                    converted_pins += 1
                new_cell_list.append("PIN " + port_name)
            self.netlist_info[net_name]["cell_list"] = new_cell_list

        # Rebuild connectivity maps after pin->port rewrite.
        new_net_cell_info = {}
        new_cell_net_info = {}
        new_cell_list = set()
        new_std_cells = set()
        new_macros = set()
        new_ext_pins = set()
        for net_name, net_data in self.netlist_info.items():
            std_cells = []
            macros = []
            ext_pins = []
            for cell_pin in net_data.get("cell_list", []):
                cell_name, pin_name = cell_pin.split(" ")
                if cell_name == "PIN":
                    port_name = pin_name
                    ext_pins.append(port_name)
                    new_ext_pins.add(port_name)
                    if port_name not in new_cell_net_info:
                        new_cell_net_info[port_name] = []
                    if net_name not in new_cell_net_info[port_name]:
                        new_cell_net_info[port_name].append(net_name)
                    continue

                if cell_name in self.std_cell_list_in_nets or (
                    isinstance(cell_name, str) and cell_name.startswith("virtual")
                ):
                    std_cells.append(cell_name)
                    new_std_cells.add(cell_name)
                    new_cell_list.add(cell_name)
                elif cell_name in self.macro_list_in_nets and cell_name not in fixed_macros:
                    macros.append(cell_name)
                    new_macros.add(cell_name)
                    new_cell_list.add(cell_name)
                else:
                    raise ValueError(
                        "[LEF_DEF_analysis] unexpected cell {} left in net {} after fixed-macro port conversion".format(
                            cell_name, net_name
                        )
                    )

                if cell_name not in new_cell_net_info:
                    new_cell_net_info[cell_name] = []
                if net_name not in new_cell_net_info[cell_name]:
                    new_cell_net_info[cell_name].append(net_name)

            new_net_cell_info[net_name] = {
                "std_cell": std_cells,
                "macro": macros,
                "ext_pin": ext_pins,
            }

        self.net_cell_info = new_net_cell_info
        self.cell_net_info = new_cell_net_info
        self.cell_list_in_nets = new_cell_list
        self.std_cell_list_in_nets = new_std_cells
        self.macro_list_in_nets = new_macros
        self.ext_pin_list_in_nets = new_ext_pins

        # Keep fixed-macro geometry for iteration / region plots even after removal from DB.
        def_scale = float(self.die_info["def_scale"])
        plot_boxes = []
        plot_names = []
        plot_footprints = []
        die_rect = box(
            self.die_info["xl"],
            self.die_info["yl"],
            self.die_info["xh"],
            self.die_info["yh"],
        )
        footprint_by_name = {
            name: geom
            for name, geom in self._iter_fixed_cell_footprints(die_rect, {"BLOCK", "MACRO"})
        }
        for macro_name in sorted(fixed_macros):
            if macro_name not in self.cell_info:
                continue
            macro_id = self.cell_info[macro_name]["macro_id"]
            width = round(float(round(float(self.lef_info[macro_id]["width"]) * def_scale)), 1)
            height = round(float(round(float(self.lef_info[macro_id]["height"]) * def_scale)), 1)
            rotate_angle, _flip = self._orient_to_rotate_flip(
                self.cell_info[macro_name].get("orientation", "N")
            )
            if rotate_angle in (90, 270):
                width, height = height, width
            pos = self.cell_info[macro_name]["position"]
            plot_boxes.append([float(pos[0]), float(pos[1]), float(width), float(height)])
            plot_names.append(macro_name)
            plot_footprints.append(
                self._footprint_exterior_rings(footprint_by_name.get(macro_name))
            )
        self.plot_fixed_macros = (
            np.asarray(plot_boxes, dtype=np.float64)
            if plot_boxes
            else np.zeros((0, 4), dtype=np.float64)
        )
        self.plot_fixed_macro_names = plot_names
        self.plot_fixed_macro_footprints = plot_footprints

        # Drop fixed macros from cell maps / node components.
        for macro_name in fixed_macros:
            self.cell_info.pop(macro_name, None)
            self.cell_net_info.pop(macro_name, None)

        self.total_cell_info = self.get_total_cell_info()

        report = {
            "num_fixed_macros_removed": len(fixed_macros),
            "num_virtual_ports_created": converted_pins,
            "fixed_macros": sorted(fixed_macros),
            "virtual_ports": sorted(port_to_macro_pin.keys()),
            "plot_fixed_macros": [
                {
                    "name": name,
                    "x": float(box[0]),
                    "y": float(box[1]),
                    "width": float(box[2]),
                    "height": float(box[3]),
                }
                for name, box in zip(plot_names, plot_boxes)
            ],
            "port_to_macro_pin": {
                port: {"macro": macro, "pin": pin}
                for port, (macro, pin) in port_to_macro_pin.items()
            },
        }
        report_path = os.path.join(
            self.params.save_path, "fixed_macro_to_port_conversion.json"
        )
        with open(report_path, "w") as w:
            json.dump(report, w, indent=4)
        print(
            "[LEF_DEF_analysis] convert_macro2port: removed {} fixed macros, "
            "created {} virtual ports -> {}".format(
                len(fixed_macros), converted_pins, report_path
            )
        )
            
    
    def get_total_cell_info(self):

        stdCellList = list(self.std_cell_list_in_nets)
        print(f'STD CELL LIST: {len(stdCellList)}')
        print(f'MACRO LIST: {len(self.macro_list_in_nets)}')


        movableMacroList = []
        fixedMacroList = []
        # get fixed and movable macros
        for macro in self.macro_list_in_nets:
            cellIdx = self.node_name2index_map[macro]
            cell_instance = self.lef_def.cellInfo[cellIdx].get_placed_state()
            if cell_instance == 'FIXED':
                fixedMacroList.append(macro)
            else:
                movableMacroList.append(macro)
        
        #get external pins of which use is 'SIGNAL'
        extPinList = list(self.ext_pin_list_in_nets)
        
        #creates total cell 
        total_cell_info = stdCellList + movableMacroList + fixedMacroList + extPinList
        print(f'CHECK TOTAL CELL INFO:{len(total_cell_info)}')
        #creats data frame for total cell
        data = []
        for cell_name in total_cell_info:
            if cell_name in self.ext_pin_info and (
                cell_name not in self.node_name2index_map
                or str(cell_name).startswith("virtual_fixed_macro__port_")
            ):
                pos = self.ext_pin_info[cell_name]["layer0"]["position"]
                data.append(
                    [
                        cell_name,
                        "",
                        "PIN",
                        "FIXED",
                        pos,
                        "N",
                        "",
                        "SIGNAL",
                    ]
                )
                continue
            cell = self.node_name2index_map[cell_name]
            try:
                cellId4LEF = self.lef_macro_name2index_map[self.lef_def.cellInfo[cell].get_lef_info().get_name()]
            except: 
                print(f"[ERROR] PIN {cell_name} NOT FOUND IN LEF PIN NAME TO INDEX MAP")
                print(f"[ERROR] PIN {cell_name} and {cell}")    
                print(f"[ERROR] LEF NAME {self.lef_def.cellInfo[cell].get_lef_info().get_name()}")
                raise ValueError('PIN NOT FOUND IN LEF PIN NAME TO INDEX MAP')
            cell_type = self.lef_def.cellInfo[cell].get_lef_info().get_cell_type()
            if cell_type == 'CORE':
                cell_type = 'STD_CELL'
            elif cell_type == "BLOCK":
                cell_type = 'MACRO'
            elif cell_type == 'PIN':
                cell_type = 'PIN'
            else:
                raise ValueError('cell type should be CORE, BLOCK, or PIN')
            placed_state = self.lef_def.cellInfo[cell].get_placed_state()
            pos = self.lef_def.cellInfo[cell].get_pos()
            orient = self.lef_def.cellInfo[cell].get_orientation()
            if cell_type == 'STD_CELL':
                orient = 'UNKNOWN'
            elif cell_type == 'PIN':
                orient = 'N'
            use = '' if cell_type != 'PIN' else 'SIGNAL'
            region = self.lef_def.cellInfo[cell].get_region()
            if region == None:
                region = ''
            data.append([cell_name, cellId4LEF, cell_type, placed_state, pos, orient, region, use])
        df_total_cell = pd.DataFrame(data)
        print(df_total_cell)
        df_total_cell.columns = ["cell_name", "cell_ID", "cell_type", "placed_state", "position", "orientation", "region", "use"]
        
        
        return df_total_cell
    
    
    
    def get_fixed_default_macros(self):
        df_macros = self.total_cell_info[self.total_cell_info['cell_type'] == 'MACRO']
        df_fixed_macros = df_macros[df_macros['placed_state'] == 'FIXED']
        df_fixed_default_macros = df_fixed_macros[df_fixed_macros['cell_name'].isin([i for i in df_fixed_macros['cell_name'] if not i.startswith('BLOCKAGE_BLOCKAGE')])]
        print('[MAKEDB ] FIXED DEFAULT MACKRS')
        print(df_fixed_default_macros)
        if len(df_fixed_default_macros):
            return df_fixed_default_macros['cell_name'].tolist()
        else:
            return []
    
    def createCriticalPathNet(self):
        for path in self.critical_path_info.keys():
            netName= 'CriticalPathNet_' + path
            cellInfo4path = self.critical_path_info[path]['cell_list']
            cell_list_path = set()
            for cell_pin in cellInfo4path:
                cell, pin = cell_pin[0], cell_pin[1]
                if cell not in self.convert_name2supercell.keys(): #check if cell is added as buffer cell or not
                    continue
                supercellName = "supercell"+str(self.convert_name2supercell[cell])
                cell_list_path.add(supercellName)
            print(f"[LEF_DEF_analysis] {netName} : {len(cell_list_path)} CELLS")
            if len(cell_list_path) == 0:
                continue
            else: 
                new_net_instance = Net(netName)
                new_net_instance.set_use('SIGNAL')
                for cellName in cell_list_path:
                    #creates Pin instance for pin 'VP' and add it to Cell instance
                    virtual_pin = Pin('VP', self.lef_def.cellInfo[cellName].get_lef_info())
                    cell_width  = self.lef_def.cellInfo[cellName].get_lef_info().get_width()
                    cell_height = self.lef_def.cellInfo[cellName].get_lef_info().get_height()
                    virtual_pin_rects = [cell_width/4, cell_height/4, cell_width*3/4, cell_height*3/4]
                    virtual_pin.set_layer_rectangle('M2', virtual_pin_rects)
                    virtual_pin.set_direction('INPUT')
                    virtual_pin.set_use('M2', 'SIGNAL')
                    virtual_pin.add_net(self.lef_def.cellInfo[cellName], netName)
                    #update LEF instance due to the addition of pin 'VP'
                    self.lef_def.cellInfo4LEF[self.lef_def.cellInfo[cellName].get_lef_info().get_name()].add_pin(virtual_pin)
                    #add pin to net
                    new_net_instance.add_cell_pin(self.lef_def.cellInfo[cellName], 'VP')
                print(f"[LEF_DEF_analysis] {netName} CREATED")
                self.lef_def.netInfo[netName] = new_net_instance

    
    # def createCriticalPathNet(self):
    #     critical_path_net = dict()
    #     for i in range(len(self.df_path_slack)):
    #         netName= 'CriticalPathNet' + str(i+1)
    #         path = self.df_path_slack.loc[i, 'path']
    #         cellInfo4path = self.critical_path_info[path]['cell_list']
    #         cell_list_path = set()
    #         for cell in cellInfo4path:
    #             if not cell[0] in self.convert_name2supercell.keys(): #check if cell is added as buffer cell or not
    #                 continue
    #             supercellName = self.convert_name2supercell[cell[0]]
    #             cell_list_path.add(supercellName)
    #         cell_start_point = self.critical_path_info[path]['start_point'].split('(')[0].strip()
    #         if cell_start_point in self.convert_name2supercell.keys():
    #              cell_list_path.add(self.convert_name2supercell[cell_start_point])
    #         cell_end_point = self.critical_path_info[path]['end_point'].split('(')[0].strip()
    #         if cell_end_point in self.convert_name2supercell[cell_end_point]:
    #              cell_list_path.add(self.convert_name2supercell[cell_start_point])
    #         cell_list_path = cell_list_path.intersection(set(self.lef_def.cellInfo.keys()))
    #         if not len(critical_path_net):
    #             critical_path_net[netName] = cell_list_path
    #         else:
    #             overlap = False
    #             for net in critical_path_net.keys():
    #                 if cell_list_path == critical_path_net[net]:
    #                     overlap = True
    #                     print(netName+' HAS SAME CELL LIST WITH '+net)
    #                     break
    #             if overlap:
    #                 continue
    #         if len(cell_list_path) ==0:
    #             continue
    #         new_net_instance = Net(netName)
    #         new_net_instance.set_use('SIGNAL')
    #         for cellName in cell_list_path:
    #             #creates Pin instance for pin 'VP' and add it to Cell instance
    #             virtual_pin = Pin('VP', self.lef_def.cellInfo[cellName].get_lef_info())
    #             cell_width  = self.lef_def.cellInfo[cellName].get_lef_info().get_width()
    #             cell_height = self.lef_def.cellInfo[cellName].get_lef_info().get_height()
    #             virtual_pin_rects = [cell_width/4, cell_height/4, cell_width*3/4, cell_height*3/4]
    #             virtual_pin.set_layer_rectangle('M2', virtual_pin_rects)
    #             virtual_pin.set_direction('INPUT')
    #             virtual_pin.set_use('M2', 'SIGNAL')
    #             virtual_pin.add_net(self.lef_def.cellInfo[cellName], netName)
    #             #update LEF instance due to the addition of pin 'VP'
    #             self.lef_def.cellInfo4LEF[self.lef_def.cellInfo[cellName].get_lef_info().get_name()].add_pin(virtual_pin)
    #             #add pin to net
    #             new_net_instance.add_cell_pin(self.lef_def.cellInfo[cellName], 'VP')
    #         print(f"[LEF_DEF_analysis] {netName} CREATED")
    #         self.lef_def.netInfo[netName] = new_net_instance
    
    
    
