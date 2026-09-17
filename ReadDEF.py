import json, sys
from ReadLEF import ReadLEFinfo
import pandas as pd
import networkx as nx
import virtual_blockage_for_die
import copy
import numpy as np
from Cell import Cell
from Cell import LEF 
from Pin import Pin 
from Net import Net
from Die import Die 
from ReadLEF import ReadLEFinfo
from NameIdMap import NameIdMap
import re 
import pickle
import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor

class ReadDEFinfo:

    def __init__(self, params):
        self.params = params
        if type(self.params) == dict:
            self.fileAddress = self.params["def_path"]
            self.lef_dir_path = self.params["lef_dir_path"]
            self.save_path = self.params.get("save_path", "./tmp")
        elif isinstance(self.params, str):
            with open(params, 'r') as f: self.params = json.load(f)
            self.fileAddress = self.params['def_path']
            self.lef_dir_path = self.params['lef_dir_path']
            self.save_path = self.params.get("save_path", "./tmp")
        else:
            self.fileAddress = self.params.def_path
            self.lef_dir_path = self.params.lef_dir_path
            self.save_path = self.params.save_path 
            
        self.dieInfo = None
        self.cellInfo = dict()
        self.netInfo = dict()
        self.extPinInfo = dict()
        self.readLEFinfo = None
        self.cellInfo4LEF = None
        self.node_names = []
        self.num_net_pickle_files = 1
        self.num_cutoff_net_pickle_files = 1000000
        self.node_name_to_id = NameIdMap("node")
        self.net_name_to_id = NameIdMap("net")
        self.node_name2index_map = self.node_name_to_id
        self.net_name2index_map = self.net_name_to_id
    
    def save_with_pickle(self, output_path='./def_data.pkl'):
        """Save all ReadDEF data using pickle."""
        data = {
            'cellInfo': self.cellInfo,
            'cellInfo4LEF': self.cellInfo4LEF,
            'netInfo': self.netInfo,
            'extPinInfo': self.extPinInfo,
            'dieInfo': self.dieInfo
        }
        
        with open(output_path, 'wb') as f:
            pickle.dump(data, f)
        
        print(f"Data saved to {output_path}")

    def _add_cell_pin_to_net(self, net_instance, cell_name, pin_name):
        """Helper function to add cell pin to net instance."""
        if cell_name not in self.cellInfo:
            raise ValueError(f'Cell "{cell_name}" is not defined in components')
        cell = self.cellInfo[cell_name]
        lef_info = cell.get_lef_info()
        pin_instance = lef_info.get_pin(pin_name) if lef_info is not None else None
        if pin_instance is None:
            raise ValueError(f'Pin "{pin_name}" not found in cell "{cell_name}"')
        pin_instance.add_net(cell, net_instance.get_name())
        net_instance.add_cell_pin(cell, pin_name)
        
        # if pin_name != 'VP':
        #     pin_instance = next((i for i in self.cellInfo[cell_name].get_lef_info().get_pins() if i.get_name() == pin_name), None)
        #     if pin_instance is None:
        #         raise ValueError(f'Pin "{pin_name}" not found in cell "{cell_name}"')
        #     pin_instance.add_net(self.cellInfo[cell_name], net_instance.get_name())
        #     net_instance.add_cell_pin(self.cellInfo[cell_name], pin_name)
        # else:
        #     # Create Pin and LEF instances for VP pin if it doesn't exist
        #     pin_list_for_cell = [i.get_name() for i in self.cellInfo[cell_name].get_lef_info().get_pins()]
        #     if 'VP' not in pin_list_for_cell:
        #         # Create Pin instance for pin 'VP' and add it to Cell instance
        #         virtual_pin = Pin('VP', self.cellInfo[cell_name].get_lef_info())
        #         cell_width  = self.cellInfo[cell_name].get_lef_info().get_width()
        #         cell_height = self.cellInfo[cell_name].get_lef_info().get_height()
        #         virtual_pin_rects = [cell_width/4, cell_height/4, cell_width*3/4, cell_height*3/4]
        #         virtual_pin.set_layer_rectangle('M2', virtual_pin_rects)
        #         virtual_pin.set_direction('INPUT')
        #         virtual_pin.set_use('M2', 'SIGNAL')
        #         virtual_pin.add_net(self.cellInfo[cell_name], net_instance.get_name())
        #         # Update LEF instance due to the addition of pin 'VP'
        #         self.cellInfo4LEF[self.cellInfo[cell_name].get_lef_info().get_name()].add_pin(virtual_pin)
        #     else:
        #         virtual_pin = next((i for i in self.cellInfo[cell_name].get_lef_info().get_pins() if i.get_name() == 'VP'), None) # get the Pin instance for pin 'VP'
        #         virtual_pin.add_net(self.cellInfo[cell_name], net_instance.get_name())  
        #     net_instance.add_cell_pin(self.cellInfo[cell_name], pin_name)   
        

    def _param_get(self, key, default=None):
        params = getattr(self, "params", None)
        if params is None:
            return default
        if isinstance(params, dict):
            return params.get(key, default)
        return getattr(params, key, default)

    def _def_parse_num_threads(self):
        raw = self._param_get("def_parse_num_threads", None)
        if raw is None:
            raw = self._param_get("num_threads", 8)
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = 8
        if n <= 0:
            try:
                n = int(self._param_get("num_threads", 8))
            except (TypeError, ValueError):
                n = 8
        return max(1, n)

    @staticmethod
    def _find_section_range(lines, section_name):
        """
        Return [body_start, body_end) line indices for SECTION ... END SECTION.
        body_start is the first line after the SECTION header.
        """
        header = section_name
        ender = "END " + section_name
        start = None
        for i, line in enumerate(lines):
            if start is None:
                if line.startswith(header) and not line.startswith("END"):
                    start = i + 1
            elif line.startswith(ender):
                return start, i
        return None, None

    @staticmethod
    def _align_to_dash_record(lines, idx, end):
        """Advance idx to the next line that starts with '- ' / '-', or end."""
        while idx < end:
            s = lines[idx]
            if s.startswith("-"):
                return idx
            idx += 1
        return end

    def _split_aligned_chunks(self, lines, start, end, n_threads):
        """
        Split [start, end) into ~equal chunks; each chunk start is aligned to a
        '-' record line. Chunk i owns records that begin in
        [aligned_i, aligned_{i+1}).
        """
        if start is None or end is None or end <= start:
            return []
        n_threads = max(1, int(n_threads))
        span = end - start
        n_threads = min(n_threads, max(1, span))
        chunk_size = max(1, (span + n_threads - 1) // n_threads)
        aligned = []
        for t in range(n_threads):
            lo = start + t * chunk_size
            if lo >= end:
                break
            lo = self._align_to_dash_record(lines, lo, end)
            if lo >= end:
                break
            if aligned and lo <= aligned[-1]:
                continue
            aligned.append(lo)
        ranges = []
        for i, lo in enumerate(aligned):
            hi = aligned[i + 1] if i + 1 < len(aligned) else end
            if lo < hi:
                ranges.append((lo, hi))
        return ranges

    def _extract_dash_record_groups(self, lines, lo, hi, hard_end):
        """
        Collect groups of lines for each record whose starting '-' line is in
        [lo, hi). Reading may continue past hi (up to hard_end) to finish ';'.
        """
        groups = []
        i = lo
        while i < hi:
            if not lines[i].startswith("-"):
                i += 1
                continue
            group = [lines[i]]
            i += 1
            if ";" in group[0]:
                groups.append(group)
                continue
            while i < hard_end:
                group.append(lines[i])
                if ";" in lines[i]:
                    i += 1
                    break
                i += 1
            groups.append(group)
        return groups

    def _commit_component_record_text(self, lineInfoCollect):
        """Parse one COMPONENTS record text (leading '-' already optional)."""
        lineInfoCollect = lineInfoCollect.strip()
        if lineInfoCollect.startswith("-"):
            lineInfoCollect = lineInfoCollect.replace("-", "", 1).strip()
        cellName = lineInfoCollect.split(" ")[0].strip()
        cellName = cellName[:-1] if cellName[-1] == ";" else cellName
        cellIdx = self.node_name_to_id.get_or_create(cellName)
        lineInfoCollect = lineInfoCollect.replace(cellName, "", 1).strip()

        cellID = lineInfoCollect.split(" ")[0].strip()
        cellID = cellID[:-1] if cellID[-1] == ";" or cellID[-1] == "+" else cellID
        try:
            cellIdx4LEF = self.lef_macro_name2index_map[cellID]
        except KeyError:
            raise KeyError(
                "[ReadDEF] LEF macro {!r} not found for cell {!r}. "
                "Check lef_dir_path covers all macros used in DEF.".format(
                    cellID, cellName
                )
            )
        lefInfo4cell = self.cellInfo4LEF[cellIdx4LEF]

        lineInfoCollect = lineInfoCollect.replace(cellID, "", 1).strip()
        lineInfoCollect = [
            info.replace(";", "").strip() for info in lineInfoCollect.split("+")
        ]

        cell_ = Cell(cellIdx)
        cell_.set_lef_info(lefInfo4cell)
        for info in lineInfoCollect:
            if info.startswith("UNPLACED") or info.startswith("PLACED") or info.startswith(
                "FIXED"
            ):
                placed_state = info.split(" ")[0].strip()
                cell_.set_placed_state(placed_state)
                if placed_state == "PLACED" or placed_state == "FIXED":
                    pos = list(info.split("(")[1].split(")")[0].strip().split(" "))
                    orientation = info.split(")")[1].strip()
                    cell_.set_pos(pos)
                    cell_.set_orientation(orientation)
                else:
                    cell_.set_orientation("N")
            elif info.startswith("HALO"):
                halo = [float(i) for i in info.replace("HALO", "").strip().split(" ")]
                cell_.set_halo(halo)
            elif info.startswith("REGION"):
                region = info.replace("REGION", "").strip()
                cell_.set_region(region)
        self.cellInfo[cellIdx] = cell_

    def _commit_pin_record_text(self, lineInfoCollect):
        """Parse one PINS record text."""
        lineInfoCollect = lineInfoCollect.replace(";", "").strip()
        if lineInfoCollect.startswith("-"):
            lineInfoCollect = lineInfoCollect[1:].strip()
        pinName = lineInfoCollect.split(" ")[0].strip()
        if pinName not in self.node_name2index_map:
            pinIdx = self.node_name_to_id.get_or_create(pinName)
            lef_macro_idx = self.lef_macro_name_to_id.get_or_create(pinName)
            lef_pin_idx = self.lef_pin_name_to_id.get_or_create(pinName)
            pin_lef = LEF(lef_macro_idx)
            pin_lef.set_cell_type("PIN")
            pin_cell = Cell(pinIdx)
            pin_cell.set_lef_info(pin_lef)
            pin_pin = Pin(lef_pin_idx, pin_lef)
            pin_lef.add_pin(pin_pin)
        else:
            raise ValueError(
                'Duplicate external pin name "{}" found in PINS section.'.format(
                    pinName
                )
            )

        pattern = r"\+\s*(.*?)(?=\s*\+|\s*$)"
        matches = re.findall(pattern, lineInfoCollect)
        netIdx = None
        direction = None
        pos = None
        use = None
        for info in matches:
            if info.startswith("NET"):
                netName = info.replace("NET", "").strip()
                if netName not in self.net_name2index_map:
                    netIdx = self.net_name_to_id.get_or_create(netName)
                netIdx = int(self.net_name2index_map[netName])
            elif info.startswith("DIRECTION"):
                direction = info.replace("DIRECTION", "").strip()
            elif info.startswith("PLACED") or info.startswith("FIXED"):
                pattern = r"\(\s*(.*?)\s*\)"
                parsing_result = re.findall(pattern, info)
                pos = np.array(
                    [i.split(" ") for i in parsing_result], dtype=np.float32
                ).flatten()
            elif info.startswith("USE"):
                pattern = r"USE\s+(\w+)"
                use = re.findall(pattern, info)[0]

        if use is None:
            logging.warning(
                "[ReadDEF] PINS '%s' has no USE; forcing USE to SIGNAL",
                pinName,
            )
            use = "SIGNAL"

        pin_pin.add_net(pin_cell, netIdx)
        for info in matches:
            if not info.startswith("LAYER"):
                continue
            layer_name = info.replace("LAYER", "").strip().split(" ")[0]
            rect_match = re.findall(r"\(\s*(.*?)\s*\)", info)
            if not rect_match:
                logging.warning(
                    "[ReadDEF] PINS '%s' LAYER %s has no rectangles; skipped",
                    pinName,
                    layer_name,
                )
                continue
            layer_rects = np.array(
                [i.split() for i in rect_match], dtype=np.float32
            ).flatten()
            if layer_rects.size < 4:
                logging.warning(
                    "[ReadDEF] PINS '%s' LAYER %s has invalid rectangles; skipped",
                    pinName,
                    layer_name,
                )
                continue
            pin_pin.set_layer_rectangle(layer_name, layer_rects)
            pin_pin.set_use(layer_name, use)

        if pin_pin.get_layers():
            pin_lef.set_width(pin_pin.get_width())
            pin_lef.set_height(pin_pin.get_height())
        else:
            logging.warning(
                "[ReadDEF] PINS '%s' has no usable LAYER geometry; "
                "kept for NET connectivity (filtered later if useless/non-SIGNAL)",
                pinName,
            )
            pin_lef.set_width(20.0)
            pin_lef.set_height(20.0)

        pin_pin.set_direction(direction)
        pin_cell.set_pos(pos)
        pin_cell.set_placed_state("FIXED")

        self.extPinInfo[pinIdx] = {"pin_info": pin_pin, "cell_info": pin_cell}
        self.cellInfo[pinIdx] = pin_cell
        self.cellInfo4LEF[lef_macro_idx] = pin_lef

    def _commit_net_record_lines(self, group_lines):
        """Parse one NETS record from its stripped lines (same semantics as legacy loop)."""
        netName = None
        netIdx = None
        net_ = None
        for line in group_lines:
            if line.startswith("-"):
                netName = line.split(" ")[1].strip()
                if netName not in self.net_name2index_map:
                    netIdx = self.net_name_to_id.get_or_create(netName)
                netIdx = int(self.net_name2index_map[netName])
                net_ = Net(netIdx)
                line = " ".join(line.split(" ")[2:]).strip()
                if line == "":
                    continue

            if line.startswith("END"):
                continue

            if line.startswith("+"):
                use = line.replace("+", "").strip().replace("USE", "").strip().split(" ")[
                    0
                ]
                net_.set_use(use)
                self.netInfo[netIdx] = net_
                continue

            cell_pin_list = []
            line_parsed = line.split(" ")
            for idx, e in enumerate(line_parsed):
                if e == "USE":
                    use = line_parsed[idx + 1]
                    net_.set_use(use)
                    break
                else:
                    if e != "" and e != "(" and e != ")" and e != ";" and e != "+":
                        cell_pin_list.append(e)
            if len(cell_pin_list) % 2 != 0:
                raise ValueError(
                    'Net "{}" has an incomplete cell-pin pair.'.format(netName)
                )
            for idx in range(int(len(cell_pin_list) // 2)):
                cellName, pinName = cell_pin_list[2 * idx], cell_pin_list[2 * idx + 1]
                if cellName != "PIN":
                    cellIdx = int(self.node_name2index_map[cellName])
                    if cellIdx not in self.cellInfo:
                        raise ValueError(
                            'cell "{}" is not defined in components in the line {}'.format(
                                cellName, line
                            )
                        )
                    if pinName not in self.lef_pin_name2index_map:
                        raise ValueError(
                            'pin "{}" is not defined in LEF for cell "{}" in the line {}'.format(
                                pinName, cellName, line
                            )
                        )
                    try:
                        lef_info = self.cellInfo[cellIdx].get_lef_info()
                        macroIdx = lef_info.get_name()
                    except AttributeError:
                        raise ValueError(
                            'cell "{}" does not have LEF info.'.format(cellName)
                        )
                    pinIdx = int(self.lef_pin_name2index_map[pinName])
                    if not lef_info.has_pin(pinIdx):
                        lef_macroName = self.lef_macro_name2index_map[macroIdx]
                        raise ValueError(
                            'pin "{}" is not defined in LEF for cell "{}" in the line {}'.format(
                                pinName, lef_macroName, line
                            )
                        )
                    self._add_cell_pin_to_net(net_, cellIdx, pinIdx)
                else:
                    cellIdx = int(self.node_name2index_map[pinName])
                    try:
                        ext_pin_instance = self.extPinInfo[cellIdx]["pin_info"]
                        ext_pin_instance.add_net(self.cellInfo[cellIdx], netIdx)
                        net_.add_cell_pin(
                            self.cellInfo[cellIdx], ext_pin_instance.get_name()
                        )
                    except KeyError:
                        raise KeyError(
                            'External pin "{}" is not defined in PINS section in the net {}'.format(
                                pinName, netName
                            )
                        )
            if line_parsed and line_parsed[-1].endswith(";"):
                self.netInfo[netIdx] = net_

    def _parallel_extract_record_groups(self, lines, start, end, n_threads, section):
        chunks = self._split_aligned_chunks(lines, start, end, n_threads)
        if not chunks:
            return []
        print(
            "[ReadDEF] {} parallel extract: lines=[{}, {}), chunks={}, threads={}".format(
                section, start, end, len(chunks), len(chunks)
            )
        )
        groups = []
        if len(chunks) == 1:
            lo, hi = chunks[0]
            return self._extract_dash_record_groups(lines, lo, hi, end)
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            futures = [
                pool.submit(self._extract_dash_record_groups, lines, lo, hi, end)
                for lo, hi in chunks
            ]
            # Merge in DEF order, not worker completion order. Name/net/pin IDs
            # must not change with scheduling or def_parse_num_threads.
            for fut in futures:
                groups.extend(fut.result())
        return groups

    def _parallel_parse_components(self, lines, start, end, n_threads):
        t0 = time.time()
        groups = self._parallel_extract_record_groups(
            lines, start, end, n_threads, "COMPONENTS"
        )
        extracted = time.time()
        # COMPONENTS concat style: no spaces between stripped lines.
        for group in groups:
            self._commit_component_record_text("".join(group))
        print(
            "[ReadDEF] COMPONENTS FINISHED ({} cells, extract={:.2f}s, serial parse/build={:.2f}s)".format(
                len(self.cellInfo), extracted - t0, time.time() - extracted
            )
        )

    def _parallel_parse_pins(self, lines, start, end, n_threads):
        t0 = time.time()
        groups = self._parallel_extract_record_groups(
            lines, start, end, n_threads, "PINS"
        )
        extracted = time.time()
        for group in groups:
            self._commit_pin_record_text(" ".join(group))
        print(
            "[ReadDEF] PINS FINISHED ({} pins, extract={:.2f}s, serial parse/build={:.2f}s)".format(
                len(self.extPinInfo), extracted - t0, time.time() - extracted
            )
        )

    def _parallel_parse_nets(self, lines, start, end, n_threads):
        t0 = time.time()
        groups = self._parallel_extract_record_groups(
            lines, start, end, n_threads, "NETS"
        )
        extracted = time.time()
        for group in groups:
            self._commit_net_record_lines(group)
        print(
            "[ReadDEF] NETS FINISHED ({} nets, extract={:.2f}s, serial parse/build={:.2f}s)".format(
                len(self.netInfo), extracted - t0, time.time() - extracted
            )
        )

    def read(self):

        started = time.time()
        if "lef_dir_path" in self.__dict__:
            self.readLEFinfo = ReadLEFinfo(self.lef_dir_path)
            self.cellInfo4LEF = self.readLEFinfo.get_total_macro_info()
            self.lef_macro_name_to_id = self.readLEFinfo.lef_macro_name_to_id
            self.lef_pin_name_to_id = self.readLEFinfo.lef_pin_name_to_id
            self.lef_macro_name2index_map = self.lef_macro_name_to_id
            self.lef_pin_name2index_map = self.lef_pin_name_to_id

            lef_info = dict()
            for idx in self.cellInfo4LEF.keys():
                name = self.lef_macro_name2index_map[idx]
                lef_instance = self.cellInfo4LEF[idx]
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
                    pin_name = self.lef_pin_name2index_map[pin.get_name()]
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
            # Analysis.save2data stores the same information under save_path.


            self.lef_macro_name2index_map = self.lef_macro_name_to_id
            self.lef_pin_name2index_map = self.lef_pin_name_to_id

        logging.info("[ReadDEF] LEF read/preparation %.3fs", time.time() - started)
        started = time.time()
        with open(self.fileAddress, "r") as f:
            lines = [ln.strip() for ln in f]
        logging.info("[ReadDEF] DEF read/strip %.3fs (%d lines)", time.time() - started, len(lines))

        comp_lo, comp_hi = self._find_section_range(lines, "COMPONENTS")
        pins_lo, pins_hi = self._find_section_range(lines, "PINS")
        nets_lo, nets_hi = self._find_section_range(lines, "NETS")
        n_threads = self._def_parse_num_threads()
        print("[ReadDEF] parallel parse threads = {}".format(n_threads))
        skip_bodies = []
        if comp_lo is not None:
            skip_bodies.append((comp_lo, comp_hi))
        if pins_lo is not None:
            skip_bodies.append((pins_lo, pins_hi))
        if nets_lo is not None:
            skip_bodies.append((nets_lo, nets_hi))

        # COMPONENTS -> PINS -> NETS (dependency order), each section multi-threaded.
        if comp_lo is not None:
            self._parallel_parse_components(lines, comp_lo, comp_hi, n_threads)
        if pins_lo is not None:
            self._parallel_parse_pins(lines, pins_lo, pins_hi, n_threads)
        if nets_lo is not None:
            self._parallel_parse_nets(lines, nets_lo, nets_hi, n_threads)

        readRowStart, readComponentsStart, readNetsStart, readExtPinsStart, readRegionStart, readBlockageBlockageStart, readBlockageStart, readGroupStart, die_analysis = False, False, False, False, False, False, False, False, False
        for line_idx, line in enumerate(lines):
            if any(lo <= line_idx < hi for lo, hi in skip_bodies):
                continue
            # Headers/footers of parallel sections (bodies already handled).
            if line.startswith("COMPONENTS") or line.startswith("END COMPONENTS"):
                continue
            if line.startswith("PINS") or line.startswith("END PINS"):
                continue
            if line.startswith("NETS") or line.startswith("END NETS"):
                continue

            if line.startswith("UNITS"):
                pattern = r'\d+'
                def_scale = int(re.search(pattern, line).group())
                self.dieInfo = Die()
                self.dieInfo.set_def_scale(def_scale)
                
            elif line.startswith("DIEAREA"):
               line = line.replace("DIEAREA","").replace("(","").replace(")","").replace(";","").strip().split(" ")
               die_layout = [float(i) for i in line if i != ""]
               self.dieInfo.set_layout(die_layout)

            elif line.startswith("ROW"):
                readRowStart = True

            elif line.startswith("REGIONS"):
                readRegionStart = True
                continue

            elif line.startswith("BLOCKAGE_BLOCKAGE"):
                readBlockageBlockageStart = True
                continue

            elif line.startswith("BLOCKAGE") and not line.startswith("BLOCKAGE_BLOCKAGE"):
                readBlockageStart = True
                blockage_result = dict()
                blockageNum = 0
                blockageInfo = False
                continue
            elif line.startswith("GROUP"):
                readGroupStart = True
                currentGroupName = None
                current_group = None
                elements = []
                groupInfo = False
                continue

            if readRowStart and line.startswith("ROW"):
                row, row_idx, row_name = line.split(" ")[0], line.split(" ")[1], line.split(" ")[2]
                row_xl, row_yl, orient = line.split(" ")[3], line.split(" ")[4], line.split(" ")[5]
                do, step_num, by, onestep, step, step_size = line.split(" ")[6], line.split(" ")[7], line.split(" ")[8], line.split(" ")[9], line.split(" ")[10], line.split(" ")[11]
                self.dieInfo.add_row([row_idx, row_name, row_xl, row_yl, orient, step_num, step_size])
                
            if readRowStart and line.startswith("ROW") == False:
                readRowStart = False
                die_analysis =True
            
            if readRegionStart:
                line = line.strip()
                if line.startswith("REGIONS"):
                    continue
                if line.startswith("END REGIONS"):
                    readRegionStart = False
                    continue
                if line.startswith("-"):
                    line_region = line.strip()
                else:
                    line_region = line_region + " " + line.strip()
                
                if line.endswith(";"):
                    line_region = line_region.strip()
                    regionName = line_region.split("-")[1].strip().split(" ")[0].strip()
                    pattern = r'\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)'
                    matches = re.findall(pattern, line_region)
                    coords = [int(x) for pair in matches for x in pair]
                    region_rectangles = np.array(coords, dtype=np.float32).tolist()
                    self.dieInfo.add_region(regionName, region_rectangles)

            
            if readBlockageStart:
                line=line.strip()

                if line.startswith("BLOCKAGES"):
                    continue

                if line.startswith("END BLOCKAGES"):
                    readBlockageStart = False
                    blockageInfo = False
                    continue

                if line.startswith("-"):
                    line_blockages = line.strip()
                    blockageInfo = True
                    if not line.endswith(";"): #asuume that a blockage information should be a single line
                        raise ValueError(f"[ReadDEF ERROR] Blockage information {line} is not considered")
                
                if blockageInfo:
                    if line.endswith(";"):
                        line_blockages = line_blockages.strip()
                        blockageNum += 1
                        blockage_result = {
                                "type": None,
                                "layer": None,
                                "options": [],
                                "shape": None,
                                "coords": []
                        }
                        # 좌표 추출
                        blockage_result["coords"] = [
                            float(v)
                            for pair in re.findall(r'\(\s*(-?\d+)\s+(-?\d+)\s*\)', line_blockages)
                            for v in pair
                        ]
                        # PLACEMENT
                        if "PLACEMENT" in line_blockages:
                            blockage_result["type"] = "PLACEMENT"

                            if "+ SOFT" in line_blockages:
                                blockage_result["options"].append("SOFT")

                            if "POLYGON" in line:
                                blockage_result["shape"] = "POLYGON"
                            elif "RECT" in line:
                                blockage_result["shape"] = "RECT"
                            else:
                                raise ValueError(f"Unknown shape type in line: {line_blockages}")

                        # LAYER
                        elif "LAYER" in line_blockages:
                            blockage_result["type"] = "LAYER"

                            m = re.search(r'LAYER\s+(\S+)', line_blockages)
                            if m:
                                blockage_result["layer"] = m.group(1)
                                option_keywords = [
                                "EXCEPTPGNET",
                                "SPACING",
                                "DESIGNRULEWIDTH",
                                "MASK"
                                ]

                                for opt in option_keywords:
                                    if opt in line_blockages:
                                        blockage_result["options"].append(opt)

                                if "POLYGON" in line_blockages:
                                    blockage_result["shape"] = "POLYGON"
                                elif "RECT" in line:
                                    blockage_result["shape"] = "RECT"
                                else:
                                    raise ValueError(f"Unknown shape type in line: {line_blockages}")
                        self.dieInfo.add_blockage(blockageNum, blockage_result)
                        blockageInfo = False

                    else:
                        line_blockages = line_blockages + " " +line.strip()
            
            if readGroupStart:
                line = line.strip()
                if line.startswith("GROUPS"):
                    continue

                if line.startswith("END GROUPS"):
                    readGroupStart = False
                    currentGroupName = None
                    current_group = None
                    elements = []
                    groupInfo = False
                    continue

                if line.startswith("-"):
                    currentGroupName = line.split('-')[1].strip().split(" ")[0].strip()
                    current_group = {
                        "name": currentGroupName,
                        "elements": [],
                        "region": None,
                    }
                    groupInfo = True

                    if line.endswith(';'):
                        clean_line = line.replace("- " + currentGroupName, "").replace(";", "").strip()
                        elements.extend([e for e in clean_line.split(" ") if e != ""]) 
                        current_group["elements"] = elements
                        self.dieInfo.add_group(currentGroupName, current_group)
                        currentGroupName = None
                        current_group = None
                        elements = []
                        groupInfo = False
                    continue

                if groupInfo:
                    if line.startswith("+ REGION") and line.endswith(';'):
                        current_group["region"] = line.split("+ REGION")[1].strip().replace(";", "").strip()
                        current_group["elements"] = elements
                        self.dieInfo.add_group(currentGroupName, current_group)
                        currentGroupName = None
                        current_group = None
                        elements = []
                        groupInfo = False
                    elif not line.startswith("+ REGION") and not line.endswith(';'):
                        elements.extend([e for e in line.split(" ") if e != ""]) 
                    elif not line.startswith("+ REGION") and line != ";" and line.endswith(";"):
                        clean_line = line[:-1]
                        elements.extend([e for e in clean_line.split(" ") if e != ""])
                        current_group["region"] = None
                        current_group["elements"] = elements
                        self.dieInfo.add_group(currentGroupName, current_group)
                        currentGroupName = None
                        current_group = None
                        elements = []
                        groupInfo = False
                    elif line == ";":
                        current_group["region"] = None
                        current_group["elements"] = elements
                        self.dieInfo.add_group(currentGroupName, current_group)
                        currentGroupName = None
                        current_group = None
                        elements = []
                        groupInfo = False
                    else:
                        raise ValueError(f"([ReadDEF ] ERROR with {line} parsing grammer not considered")

            if readBlockageBlockageStart:
                line = line.strip()
                if 'num_blockage' not in self.__dict__:
                    self.num_blockage = 1
                    blockage_net = Net('BLOCKAGE_BLOCKAGE')
                if line.startswith('-'):
                   line_blockages = line.strip()
                elif line.startswith('END'):
                   readBlockageBlockageStart = False
                   continue
                else:
                   line_blockages = line_blockages+ line
                if ';' not in line_blockages:
                   continue
                else:
                    pattern = r'\(\s*(.*?)\s*\)'
                    rect_match = re.findall(pattern, line_blockages)
                    blockage_rectangles = np.array([i.split(' ') for i in rect_match], dtype=np.float32).flatten()
                    #creates an instance of Cell for blockage
                    cellName = 'BLOCKAGE_BLOCKAGE'+str(self.num_blockage)
                    cellIdx = self.node_name_to_id.get_or_create(cellName)
                    blockage_cell = Cell(cellIdx)
                    blockage_cell.set_placed_state('FIXED')
                    blockage_cell.set_pos(blockage_rectangles[:2])
                    blockage_cell.set_orientation('N')
                    
                    #creates an instance of LEF for blockage
                    cellName4LEF = 'BLOCKAGE_BLOCKAGE'+str(self.num_blockage)
                    cell_idx4LEF = self.lef_macro_name_to_id.get_or_create(cellName4LEF)
                    blockage_lef = LEF(cell_idx4LEF)
                    blockage_lef.set_width((blockage_rectangles[2] - blockage_rectangles[0])/self.dieInfo.get_def_scale())
                    blockage_lef.set_height((blockage_rectangles[3] - blockage_rectangles[1])/self.dieInfo.get_def_scale())
                    blockage_lef.set_symmetry = 'X Y'
                    blockage_lef.set_cell_type('BLOCK')
                    blockage_cell.set_lef_info(blockage_lef)
                    
                    #creates blockage pin 
                    blockage_pin = Pin('A', blockage_lef)
                    cell_width  = blockage_lef.get_width()
                    cell_height = blockage_lef.get_height()
                    blockage_pin_rects = [cell_width/4, cell_height/4, cell_width*3/4, cell_height*3/4]
                    blockage_pin.set_layer_rectangle('M2', blockage_pin_rects)
                    blockage_pin.set_direction('INPUT')
                    blockage_pin.set_use('M2', 'SIGNAL')
                    blockage_pin.add_net(blockage_cell, blockage_net.get_name())
                    blockage_net.add_cell_pin(blockage_cell, 'A')
                    blockage_net.set_use('SIGNAL')                 
                    
                    self.cellInfo['BLOCKAGE_BLOCKAGE'+str(self.num_blockage)] = blockage_cell
                    self.cellInfo4LEF['BLOCKAGE_BLOCKAGE'+str(self.num_blockage)] = blockage_lef
                    self.netInfo['BLOCKAGE_BLOCKAGE'] = blockage_net
                    self.num_blockage += 1
                
        
    
    

if __name__ == "__main__":

    params_path = sys.argv[1]

    with open(params_path, "r") as f: params = json.load(f)

    def_info = readDEFinfo(params)

    def_info.read()
