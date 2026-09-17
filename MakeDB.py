import json
import numpy as np
import time
import pandas as pd
import copy
from random import uniform
import os
import re
import sys
import ast
import time
import math
from ReadDEF import ReadDEFinfo
from LEF_DEF_analysis import Analysis
from shared_db import SharedDB
import logging

class readDB:

    def __init__(self, params):
        self.params = params
        self.num_physical_nodes = 0 # number of real nodes, including movable nodes, terminals, and terminal_NIs
        self.num_terminals = 0 # number of terminals, essentially fixed macros
        self.num_terminal_NIs = 0 # number of terminal_NIs that can be overlapped, essentially IO pins
        self.num_fake_macro = 0 #macro_move��~@ True인 경�~Z� ��~P��~X macro��~X ��~\��~X를 ��~@장�~U~X기 ��~D해 ��~L든 ��~@��~X
        self.num_macro_blockage = 0 #verilog blockage info��~P ��~H��~T macro��~X ��~\��~X를 ��~@장�~^~H��~@ ��~D해 ��~L든 ��~@��~X
        self.num_movable_std_cell = 0
        self.num_movable_macro = 0
        self.num_fixed_std_cell = 0
        self.num_fixed_macro = 0


        self.node_name2id_map = dict() # node name to id map, cell name
        self.node_names = [] # 1D array, cell name
        self.node_x = [] # 1D array, cell position x
        self.node_y = [] # 1D array, cell position y
        self.node_orient = [] # 1D array, cell orientation
        self.node_size_x = [] # 1D array, cell width
        self.node_size_y = [] # 1D array, cell height

        self.node2orig_node_map = list() # some fixed cells may have non-rectangular shapes; we flatten them and create new nodes
                                        # this map maps the current multiple node ids into the original one

        self.pin_direct = list() # 1D array, pin direction IO
        self.pin_names = list() # 1D array, pin naems (new variable after DREAMPlace4.0)
        self.pin_offset_x = list() # 1D array, pin offset x to its node
        self.pin_offset_y = list() # 1D array, pin offset y to its node

        self.net_name2id_map = dict() # net name to id map
        self.net_names = list() # net name
        self.net_weights = list() # weights for each net
        self.net_weight_deltas = list() #new variable defiend in dreamplace4.0
        self.net_criticality = list() #new variable defiend in dreamplace4.0
        self.net_criticality_deltas = list() #new variable defiend in dreamplace4.0

        self.net2pin_map = list() # array of 1D array, each row stores pin id
        self.flat_net2pin_map = list() # flatten version of net2pin_map
        self.flat_net2pin_start_map = list() #starting index of each net in flat_net2pin_map

        self.node2pin_map = list() # array of 1D array, contains pin id of each node
        self.flat_node2pin_map = list() # flatten version of node2pin_map
        self.flat_node2pin_start_map = list() # starting index of each node in flat_node2pin_map

        self.pin2node_map = list() # 1D array, contain parent node id of each pin
        self.pin2net_map = list() # 1D array, contain parent net id of each pin

        self.rows = list() # NumRows x 4 array, stores xl, yl, xh, yh of each row

        self.regions = list() # array of 1D array, placement regions like FENCE and GUIDE
        self.flat_region_boxes = [] # flat version of regions
        self.flat_region_boxes_start = [] # start indices of regions, length of num regions + 1
        self.node2fence_region_map = list() # map cell to a region, maximum integer if no fence region

        self.xl = 0
        self.yl = 0
        self.xh = 0
        self.yh = 0

        self.row_height = 0
        self.site_width = 0

        # self.bin_size_x = list()
        # self.bin_size_y = list()
        # self.num_bins_x = list()
        # self.num_bins_y = list()

        self.num_movable_pins = 0

        self.total_movable_node_area = 0 # totaltotalPins = list()
        self.total_fixed_node_area = 0 # total fixed cell area
        self.total_space_area = 0 # total placeable space area excluding fixed cells
        #total_space_area = db.rowBbox().area() - std::min(total_fixed_node_overlap_area, total_fixed_node_area)\ (quoted from PyPlaceDb.cpp)
        # enable filler cells
        # the Idea from e-place and RePlace
        # self.total_filler_node_area = None
        # self.num_filler_nodes = None

        self.routing_grid_xl = None
        self.routing_grid_yl = None
        self.routing_grid_xh = None
        self.routing_grid_yh = None
        self.num_routing_grids_x = None
        self.num_routing_grids_y = None
        self.num_routing_layers = None
        self.unit_horizontal_capacity = None # per unit distance, projected to one layer
        self.unit_vertical_capacity = None # per unit distance, projected to one layer
        self.unit_horizontal_capacities = None # per unit distance, layer by layer
        self.unit_vertical_capacities = None # per unit distance, layer by layer
        self.initial_horizontal_demand_map = None # routing demand map from fixed cells, indexed by (grid x, grid y), projected to one layer
        self.initial_vertical_demand_map = None # routing demand map from fixed cells, indexed by (grid x, grid y), projected to one layer

        self.node_size_x_LEF = list()
        self.node_size_y_LEF = list()

        self.dtype = None

        #created by Yoon
        self.node_filler_x = None
        self.node_filler_y = None
        self.virtual_cells = None







    def __call__(self):

        print("Reading DB with option: ", self.params.db_option)
        if self.params.db_option == "binary" or self.params.db_option == "binary_wo_pos":
            folder = self.params.save_path
            num_node_info = np.load(folder +"/num_node_info.npy")
            self.num_physical_nodes = num_node_info[0]
            self.num_terminals = num_node_info[1]
            self.num_terminal_NIs = num_node_info[2] #��~X��~\��~\ ��~A��~X
            self.num_movable_pins = num_node_info[3]
            self.num_nodes = num_node_info[4]
            self.num_fake_macro = num_node_info[5]
            self.num_blockage = num_node_info[6]
            self.num_std_cells = self.num_physical_nodes - self.num_terminal_NIs - self.num_terminals - self.num_fake_macro
            if len(num_node_info) < 12:
                raise ValueError("num_node_info.npy must be regenerated with 12 entries; got {}".format(len(num_node_info)))
            self.num_movable_std_cell = int(num_node_info[8])
            self.num_movable_macro = int(num_node_info[9])
            self.num_fixed_std_cell = int(num_node_info[10])
            self.num_fixed_macro = int(num_node_info[11])
            if 'force_weight_center_region' in self.params.__dict__:
                self.force_weight_center_region = float(self.params.force_weight_center_region)
            else:
                self.force_weight_center_region=1
            if self.params.db_option == "binary":
                self.txt2list(self.params.read_node_names, self.node_names, "string")
            else:
                self.node_names = [i.strip() for i in open(self.params.save_path + "/node_names.txt").readlines()]
            node_name2id_list = list()
            self.txt2list(folder+"/node_name2id_list.txt", node_name2id_list, "int")
            self.node_name2id_map = dict()
            for idx, name in enumerate(self.node_names):
                self.node_name2id_map[name] = node_name2id_list[idx]
            with open('node_name2id.json', 'w') as w: json.dump(self.node_name2id_map, w, indent=4)

            #node_x��~P��~T blockage ��~D��~X ��~U보가 ��~F��~L (std_cell +  macro + external_pin + filler 들�~]~X ��~D��~X ��~U보만 ��~H��~L)
            if self.params.db_option == "binary":
                self.node_x, self.node_y,  self.node_orient = list(), list(), list()
                f = open(self.params.read_posX, "r")
                for i in f.readlines():
                    self.node_x.append(float(i))
                f.close()

                f = open(self.params.read_posY, "r")
                for i in f.readlines():
                    self.node_y.append(float(i))
                f.close()

                f = open(self.params.read_orient, "r")
                for idx, i in enumerate(f.readlines()):
                    if i.isdigit():
                        continue
                    self.node_orient.append(i.replace("\n", ""))
                f.close()
                assert len(self.node_x) == len(self.node_y), print("[makeDB error] size of node_x is different from that of node_y")


            def read_orient_for_binary_position_mode():
                self.node_orient = list()
                f = open(self.params.read_orient, "r")
                for idx, i in enumerate(f.readlines()):
                    if i.isdigit():
                        continue
                    self.node_orient.append(i.replace("\n", ""))
                f.close()
                for _ in range(len(self.node_x) - len(self.node_orient)):
                    self.node_orient.append("N")
                if len(self.node_orient) > len(self.node_x):
                    self.node_orient = self.node_orient[:len(self.node_x)]
                assert len(self.node_orient) == len(self.node_x), "[makeDB error] size of node_orient is different from that of node_x"


            #node_size_x��~P��~T std_cell + macro + blockage_macro + external pin들�~]~X 사�~]�즈 ��~U보가 ��~H��~L
            self.txt2list(folder+"/node_size_x.txt", self.node_size_x, "float")
            self.txt2list(folder+"/node_size_y.txt", self.node_size_y, "float")
            self.txt2list(folder+"/node_size_x_LEF.txt", self.node_size_x_LEF, "float")
            self.txt2list(folder+"/node_size_y_LEF.txt", self.node_size_y_LEF, "float")

            print("[makeDB binary read] ##################### binary file read start ##################################")
            print("[maekDB binary read] node_names: {}".format(len(self.node_names)))
            if self.params.db_option == "binary":
                print("[maekDB binary read] node_x: {}".format(len(self.node_x)))
                print("[maekDB binary read] node_y: {}".format(len(self.node_y)))
                print("[maekDB binary read] num_node_orient: {}".format(len(self.node_orient)))
            print("[maekDB binary read] num_filler: {}".format(len(self.node_names) - len(self.node_x)))
            print("[maekDB binary read] num_std_cell: {}".format(self.num_std_cells))
            print('[maekDB binary read] num_physical_nodes: {}'.format(self.num_physical_nodes))
            print("[maekDB binary read] num of macros: {}".format(self.num_terminals +self.num_fake_macro))
            print("[maekDB binary read] num_node_size_x: {}".format(len(self.node_size_x)))
            print("[maekDB binary read] num_node_size_y: {}".format(len(self.node_size_y)))


     
     
     
            if (self.num_terminals +self.num_fake_macro) > 0: 
                if self.params.macro_move == "False":
                    if self.num_fake_macro > 0: 
                        print("[maekDB binary read] macro_move is changed from True to False.")
                        print("[maekDB binary read] num_terminals is changed: {} to {}".format(self.num_terminals, self.num_terminals+self.num_fake_macro))
                        print("[maekDB binary read] num_fake_macro is chagend: {} to 0".format(self.num_fake_macro))
                        self.num_terminals += self.num_fake_macro
                        self.num_fake_macro = 0
     
                else:
                    if self.num_fake_macro == 0:
                        print("[maekDB binary read] macro_move is changed from False to True.")
                        print("[maekDB binary read] num_terminals is changed: {} to {}".format(self.num_terminals, self.num_blockage))
                        print("[maekDB binary read] num_fake_macro is chagend: {} to 0".format(self.num_terminals - self.num_blockage))
                        self.num_fake_macro = self.num_terminals - self.num_blockage
                        self.num_terminals = self.num_blockage 
     

            print("[maekDB binary read] ##################### binary file read end ##################################")
     
            self.txt2list(folder+"/node2orig_node_map.txt", self.node2orig_node_map, "int")

            self.txt2list(folder+"/pin_direct.txt", self.pin_direct, "string")
            self.txt2list(folder+"/pin_names.txt", self.pin_names, "string")
            pin_name2id_map_list = list()
            self.txt2list(folder+"/pin_name2id_map_list.txt", pin_name2id_map_list, "int")
            self.pin_name2id_map = dict()
            for idx, name in enumerate(self.pin_names):
                self.pin_name2id_map[name] = pin_name2id_map_list[idx]

            self.txt2list(folder+"/pin_offset_x.txt", self.pin_offset_x, "float")
            self.txt2list(folder+"/pin_offset_y.txt", self.pin_offset_y, "float")

            self.txt2list(folder+"/net_names.txt", self.net_names, 'string')
            self.net_name2id_map = dict() # net name to id map
            net_name2id_list = list()
            self.txt2list(folder+"/net_name2id_list.txt", net_name2id_list, "int")
            for idx, netName in enumerate(self.net_names):
                self.net_name2id_map[netName] = net_name2id_list[idx]

            self.txt2list(folder+"/net_weights.txt", self.net_weights, "float")

            self.net2pin_map = list(np.load(folder+"/net2pin_map.npy", allow_pickle=True))
            self.txt2list(folder+"/flat_net2pin_map.txt", self.flat_net2pin_map, "int")
            self.txt2list(folder+"/flat_net2pin_start_map.txt", self.flat_net2pin_start_map, "int")

            self.node2pin_map = list(np.load(folder+"/node2pin_map.npy", allow_pickle=True))
            self.txt2list(folder+"/flat_node2pin_map.txt", self.flat_node2pin_map, "int")
            self.txt2list(folder+"/flat_node2pin_start_map.txt",self.flat_node2pin_start_map, "int")
            
            if self.params.db_option == "binary":
                self.getOrientParam()
                self.convertOrient()
            #self.test_orient()

            self.txt2list(folder+"/pin2node_map.txt",self.pin2node_map, "int")
            self.txt2list(folder+"/pin2net_map.txt", self.pin2net_map, "int")


            self.rows = list(np.load(folder+"/rows.npy", allow_pickle=True))

            self.regions = list(np.load(folder+"/regions.npy", allow_pickle=True))
            self.flat_region_boxes = list(np.load(folder+"/flat_region_boxes.npy", allow_pickle=True))
            self.txt2list(folder+"/flat_region_boxes_start.txt", self.flat_region_boxes_start, "float")
            self.node2fence_region_map = list(np.load(folder+"/node2fence_region_map.npy", allow_pickle=True))
            with open(folder+"/region_info.json", "r") as f: self.regionInfo = json.load(f)
            
            if os.path.exists(folder+"/specialMacroNet.json"):
                with open(folder+'/specialMacroNet.json', 'r') as f: self.specialMacroNet = json.load(f)
                with open(folder+'/macro4specialMacroNet.json', 'r') as f: self.macro4specialMacroNet = json.load(f)
                           
            
            #with open(folder+"/group_info.json", "r") as f: self.groupInfo = json.load(f)

            #self.regionInfo_keys = list(self.regionInfo.keys())


            die_info = np.load(folder+"/die_info.npy", allow_pickle=True)
            self.xl = die_info[0]
            self.yl = die_info[1]
            self.xh = die_info[2]
            self.yh = die_info[3]


            self.row_height = die_info[4]
            self.site_width = die_info[5]

            self.lef_scale = die_info[6]
            self.def_scale = die_info[7]

            area_info = np.load(folder+"/area_info.npy", allow_pickle=True)
            self.total_movable_node_area = area_info[0]
            self.total_fixed_node_area = area_info[1]
            self.total_space_area = area_info[2]
            print(f"[CHECK TOTAL SPACE AREA] total movable node area: {self.total_movable_node_area}, total fixed node area: {self.total_fixed_node_area}, total space area: {self.total_space_area}")

            self.netInfoCellIdxList = list(np.load(folder+"/netInfoCellIdxList.npy", allow_pickle=True))
            self.netInfoCellIdxList_startIdx = list(np.load(folder+"/netInfoCellIdxList_startIdx.npy", allow_pickle=True))

            
            if "center_region_auto" in self.params.__dict__:
                self.center_region = Analysis.make_center_region(vertices=die_info['layout'], area_ratio=self.params.center_region_auto)
            elif "center_region" in self.params.__dict__:
                self.center_region=list(np.load(self.params.save_path + '/center_region.npy'))
            else:
                self.center_region=[]
            
            #self.fixed_default_macros = list(np.load(folder+'/fixed_default_macros.npy', allow_pickle=True)) 

            if self.params.db_option == "binary_wo_pos":
                with open(folder + "/cells_info.json", "r") as f:
                    cell_info_for_pos = json.load(f)
                with open(folder + "/ext_pin_info.json", "r") as f:
                    ext_pin_info_for_pos = json.load(f)

                movable_end = self.num_physical_nodes - self.num_terminal_NIs - self.num_terminals
                ext_pin_start = self.num_physical_nodes - self.num_terminal_NIs
                restored_fixed_nodes = 0
                restored_ext_pins = 0
                for idx, node in enumerate(self.node_names):
                    node_key = node.decode() if isinstance(node, bytes) else str(node)
                    if idx < movable_end:
                        self.node_x.append(0.0)
                        self.node_y.append(0.0)
                    elif idx < ext_pin_start:
                        pos = cell_info_for_pos.get(node_key, {}).get("position", [])
                        if len(pos) >= 2:
                            self.node_x.append(float(pos[0]))
                            self.node_y.append(float(pos[1]))
                            restored_fixed_nodes += 1
                        else:
                            self.node_x.append(0.0)
                            self.node_y.append(0.0)
                    else:
                        pin_info = ext_pin_info_for_pos.get(node_key, {}).get("layer0", {})
                        pos = pin_info.get("position", [])
                        width = float(pin_info.get("width", 0.0))
                        height = float(pin_info.get("height", 0.0))
                        if len(pos) >= 2:
                            self.node_x.append(float(pos[0]) + width / 2)
                            self.node_y.append(float(pos[1]) + height / 2)
                            restored_ext_pins += 1
                        else:
                            self.node_x.append(0.0)
                            self.node_y.append(0.0)
                print("[maekDB binary read] restored fixed node positions: {}".format(restored_fixed_nodes))
                print("[maekDB binary read] restored external pin positions: {}".format(restored_ext_pins))
                if "read_orient" in self.params.__dict__:
                    read_orient_for_binary_position_mode()
                    self.getOrientParam()
                    self.convertOrient()
                    print("[maekDB binary read] restored node_orient from read_orient: {}".format(len(self.node_orient)))
            self.getRoutingGrid()
            self.getArea()
            self.num_routing_grids_x = 0
            self.num_routing_grids_y = 0
            self.num_routing_layers = 1
            self.route_num_bins_x = self.params.route_num_bins_x
            self.route_num_bins_y = self.params.route_num_bins_y
            self.unit_horizontal_capacity = self.params.unit_horizontal_capacity
            self.unit_vertical_capacity = self.params.unit_vertical_capacity
            self.unit_horizontal_capacities = None # per unit distance, layer by layer
            self.unit_vertical_capacities = None # per unit distance, layer by layer
            self.initial_horizontal_demand_map = None # routing demand map from fixed cells, indexed by (grid x, grid y), projected to one layer
            self.initial_vertical_demand_map = None # routing demand map from fixed cells, indexed by (grid x, grid y), projected to one layer




        elif self.params.db_option == "def":
            self.dtype = np.float32
            defInfo = ReadDEFinfo(self.params)
            started = time.time()
            defInfo.read()
            logging.info("[MakeDB] ReadDEF/LEF total %.3fs", time.time() - started)
            self.original_net_names = {defInfo.net_name2index_map[i] for i in defInfo.netInfo}
            started = time.time()
            logging.info("[MakeDB] Analysis start")
            if "critical_path_info" in self.params.__dict__ and "convert_name2supercell" in self.params.__dict__:
                print('[ makeDB ] critical_path_info and convert_name2supercell are loaded for LEF/DEF analysis')
                self.lef_def_analysis = Analysis(defInfo, self.params, self.params.critical_path_info, self.params.convert_name2supercell)
            else:
                self.lef_def_analysis = Analysis(defInfo, self.params)
            logging.info("[MakeDB] Analysis total %.3fs", time.time() - started)
            #self.die_layout = defInfo.die_layout
            self.cell_info = self.lef_def_analysis.cell_info
            self.cell_info_keys = self.cell_info.keys()
            self.net_info = self.lef_def_analysis.netlist_info
            self.net_info_cellName = self.lef_def_analysis.net_cell_info
            self.cell_net_info = self.lef_def_analysis.cell_net_info
            self.net_list = list(self.net_info.keys())
            self.extPinInfo = self.lef_def_analysis.ext_pin_info
            self.extPinInfo_keys = self.extPinInfo.keys()
            self.def_scale = self.lef_def_analysis.die_info['def_scale']
            self.lef_scale = self.lef_def_analysis.die_info['lef_scale']
            self.die_info = copy.deepcopy(self.lef_def_analysis.die_info)
            self.xl = self.lef_def_analysis.die_info['xl'] 
            self.yl = self.lef_def_analysis.die_info['yl']
            self.xh = self.lef_def_analysis.die_info['xh']
            self.yh = self.lef_def_analysis.die_info['yh']
            print("[makeDB ] xl: {}, xh: {}, yl: {}, yh: {}".format(self.xl, self.xh, self.yl, self.yh))
            self.rows =  defInfo.dieInfo.get_row_list().tolist()
            self.site_width, self.row_height = self.lef_def_analysis.die_info['site_width'], self.lef_def_analysis.die_info['row_height']
            self.regionInfo = self.lef_def_analysis.region_info
            self.blockageInfo = self.lef_def_analysis.die_info.get("blockage", {})
            self.regionNameList_order = list()
            if "vr" in self.regionInfo.keys():
                self.regionNameList_order.append("vr")
            for region in self.regionInfo.keys():
                if region in ("vr", "evr", "evr_boundary", "evr_center"):
                    continue
                self.regionNameList_order.append(region)
            if "evr_boundary" in self.regionInfo.keys():
                self.regionNameList_order.append("evr_boundary")
            if "evr_center" in self.regionInfo.keys():
                self.regionNameList_order.append("evr_center")
            elif "evr" in self.regionInfo.keys():
                self.regionNameList_order.append("evr")
            print('[makeDB ] REGION: ', self.regionNameList_order)
            self.virtual_cells = self.lef_def_analysis.virtual_cells

            #self.regionInfo_keys = list(self.regionInfo.keys())
            
            self.lefCellInfo = self.lef_def_analysis.lef_info
            self.lefCellInfo_keys = set(self.lefCellInfo.keys())
            self.df_totalCell = self.lef_def_analysis.total_cell_info
            self.node_names = []
            if "verilog_blockage_info" in self.params.__dict__:
                with open(self.params.verilog_blockage_info, "r") as f: self.blockageInfo = json.load(f)
            
            if "net_weight" in self.params.__dict__:
                with open(self.params.net_weight, "r") as f : self.netName2weight = json.load(f)
                
            self.fixed_default_macros = self.lef_def_analysis.get_fixed_default_macros()
            
            self.center_region = self.lef_def_analysis.center_region
            if 'force_weight_center_region' in self.params.__dict__:
                self.force_weight_center_region = float(self.params.force_weight_center_region)
            else:
                self.force_weight_center_region = 1
                
            # if 'critical_path_info' in self.params.__dict__:
            #     self.cellList4criticalPath = self.getCellList4criticalPath(self.params.critical_path_info)
            
            
            ### select nets whose USE is signal ###
            #self.net_info = self.cleanNetList()
            #self.netListOrderIndef = [i for i in self.netListOrderIndef if i in self.net_info.keys()]
            #######################################
            
            
            started = time.time()
            logging.info("[MakeDB] node arrays start")
            self.getNodeMap() ## self.node_names ��~]성 [std_cell] + [macro] +[blockageInfo] + [ext_pin]
            self.getNodeSize()
            self.getNodePos()
            self.getNode2Orig()
            self.getNodeOrient()
            self.getOrientParam()
            logging.info("[MakeDB] node arrays %.3fs", time.time() - started)
            started = time.time()
            logging.info("[MakeDB] pin/net arrays start")
            self.getPins4net()
            logging.info("[MakeDB] pin/net arrays incl. metadata %.3fs", time.time() - started)
            started = time.time()
            self.convertOrient()
            self.getFlat4NetPin()
            self.getFlat4NodePin()
            self.getTotalMovablPins()
            self.getRoutingGrid()
            self.getRegionInfo()
            print('[makeDB ] flat region box: ', self.flat_region_boxes)
            print('[makeDB ] flat region box start: ', self.flat_region_boxes_start)
            print('[makeDB ] regions: ', self.regions)
            self.getNodeGroupID()
            self.getNetInfoCellName()
            logging.info("[MakeDB] CSR/regions %.3fs", time.time() - started)
            

            if "specialMacroNet" not in self.__dict__:
                self.specialMacroNet = dict()
                self.macro4specialMacroNet = dict()
            
            if 'net_weight' not in self.params.__dict__:
                for n_ in self.net_info:
                    if n_.startswith('MODCSA'):
                        netIndex_ = self.net_name2id_map[n_]
                        netWeight = 1000 
                        self.net_weights[netIndex_] = netWeight
                        self.specialMacroNet[n_] = {"weight": netWeight, "macros": list()}
                        for comp in self.net_info[n_]["cell_list"]:
                            macroName = comp.strip().split(" ")[0].strip()
                            self.specialMacroNet[n_]["macros"].append(macroName)
                            if macroName not in self.macro4specialMacroNet.keys():
                                self.macro4specialMacroNet[macroName]=[n_]
                            else:
                                self.macro4specialMacroNet[macroName].append(n_)
                        print('[makeDB ] NET INDEX {} : {} netweight set to {}'.format(netIndex_, n_, self.net_weights[netIndex_]))
                    elif n_.startswith('CriticalPathNet'):
                        netIndex_ = self.net_name2id_map[n_]
                        netWeight = 1000
                        self.net_weights[netIndex_] = netWeight
                        self.specialMacroNet[n_] = {"weight": netWeight, "macros": list()}
                        for comp in self.net_info[n_]["cell_list"]:
                            macroName = comp.strip().split(" ")[0].strip()
                            self.specialMacroNet[n_]["macros"].append(macroName)
                            if macroName not in self.macro4specialMacroNet.keys():
                                self.macro4specialMacroNet[macroName]=[n_]
                            else:
                                self.macro4specialMacroNet[macroName].append(n_)
                        print('[makeDB ] NET INDEX {} : {} netweight set to {}'.format(netIndex_, n_, self.net_weights[netIndex_]))
                    


                
            else:
                with open(self.params.net_weight, 'r') as f: dict_net_weight = json.load(f)
                print('[makeDB ] NET WEIGHT INFO FROM ' + self.params.net_weight)
                for n_ in self.net_list:
                    if n_ not in dict_net_weight.keys():
                        continue
                    netIndex_ = self.net_name2id_map[n_]
                    netWeight = dict_net_weight[n_]
                    self.net_weights[netIndex_] = netWeight
                    self.specialMacroNet[n_] = {"weight": netWeight, "macros": list()}
                    for comp in self.net_info[n_]["cell_list"]:
                        macroName = comp.strip().split(" ")[0].strip()
                        self.specialMacroNet[n_]["macros"].append(macroName)
                        if macroName not in self.macro4specialMacroNet.keys():
                            self.macro4specialMacroNet[macroName]=[n_]
                        else:
                            self.macro4specialMacroNet[macroName].append(n_)
                    print('[makeDB ] NET INDEX {} : {} netweight set to {}'.format(netIndex_, n_, self.net_weights[netIndex_]))
            
                    
            

            
            # if "netWeightInfo" in self.__dict__:
            #     self.setNetWeight()


                #self.total_space_area = (self.xh-self.xl)*(self.yh-self.yl) - self.total_fixed_node_area
                #print("Total space area: {}".format(self.total_space_area))

                # self.topModule = self.getTopModule()
                #self.getMacroClusterInfo()

                # # enable filler cells
                # # the Idea from e-place and RePlace
                # # self.total_filler_node_area = None
                # # self.num_filler_nodes = None
            
                    #self.total_space_area = 875281000000
            self.getArea()
            self.num_routing_grids_x = 0
            self.num_routing_grids_y = 0
            self.num_routing_layers = 1
            self.route_num_bins_x = self.params.route_num_bins_x
            self.route_num_bins_y = self.params.route_num_bins_y
            self.unit_horizontal_capacity = self.params.unit_horizontal_capacity
            self.unit_vertical_capacity = self.params.unit_vertical_capacity
            self.unit_horizontal_capacities = None # per unit distance, layer by layer
            self.unit_vertical_capacities = None # per unit distance, layer by layer
            self.initial_horizontal_demand_map = None # routing demand map from fixed cells, indexed by (grid x, grid y), projected to one layer
            self.initial_vertical_demand_map = None # routing demand map from fixed cells, indexed by (grid x, grid y), projected to one layer

        elif self.params.db_option == "master":
            print("[makeDB master read] reading data from shared memory built by master process")


            #### sahred memory에 binary로 저장된 데이터 읽어서 등록 ###
            folder = self.params.save_path
            shm_builder = SharedDB("XP_DB")

            self.num_node_info = np.load(folder+"/num_node_info.npy")
            self.num_node_info = np.array(self.num_node_info, dtype=np.int32)
            

            # 기존 txt 읽기
            self.node_size_x_LEF = np.loadtxt(folder+"/node_size_x_LEF.txt", dtype=np.float32)
            self.node_size_y_LEF = np.loadtxt(folder+"/node_size_y_LEF.txt", dtype=np.float32)
            self.node2orig_node_map = np.loadtxt(folder+"/node2orig_node_map.txt", dtype=np.int32)
            self.pin_name2id_map_list = np.loadtxt(folder+"/pin_name2id_map_list.txt", dtype=np.int32)
            self.net_name2id_list = np.loadtxt(folder+"/net_name2id_list.txt", dtype=np.int32)
            self.net_weights = np.loadtxt(folder+"/net_weights.txt", dtype=np.float32) 
            self.net_weights = np.array(self.net_weights, dtype=np.float32)                                              
            self.flat_net2pin_map = np.loadtxt(folder+"/flat_net2pin_map.txt", dtype=np.int32)
            self.flat_net2pin_start_map = np.loadtxt(folder+"/flat_net2pin_start_map.txt", dtype=np.int32)
            self.flat_node2pin_map = np.loadtxt(folder+"/flat_node2pin_map.txt", dtype=np.int32)      
            self.flat_node2pin_start_map = np.loadtxt(folder+"/flat_node2pin_start_map.txt", dtype=np.int32)
            self.pin2node_map = np.loadtxt(folder+"/pin2node_map.txt", dtype=np.int32)
            self.pin2net_map = np.loadtxt(folder+"/pin2net_map.txt", dtype=np.int32)
            self.net_weight_deltas = np.array([0.0 for i in range(len(self.net_weights))], dtype=np.float32)
            self.net_criticality = np.array([1.0 for i in range(len(self.net_weights))], dtype=np.float32)
            self.net_criticality_deltas = np.array([1.0 for i in range(len(self.net_weights))], dtype=np.float32)
            self.rows = np.load(folder+"/rows.npy", allow_pickle=True)
            self.rows = self.rows.astype(np.float32)
            self.flat_region_boxes = np.load(folder+"/flat_region_boxes.npy", allow_pickle=True)
            self.flat_region_boxes_start = np.loadtxt(folder+"/flat_region_boxes_start.txt", dtype=np.float32)
            self.node2fence_region_map = np.load(folder+"/node2fence_region_map.npy", allow_pickle=True)
            self.node2fence_region_map = self.node2fence_region_map.astype(np.int32)
            self.netInfoCellIdxList = np.load(folder+"/netInfoCellIdxList.npy", allow_pickle=True)
            self.netInfoCellIdxList = self.netInfoCellIdxList.astype(np.int32)
            self.netInfoCellIdxList_startIdx = np.load(folder+"/netInfoCellIdxList_startIdx.npy", allow_pickle=True)
            self.netInfoCellIdxList_startIdx = self.netInfoCellIdxList_startIdx.astype(np.int32)
            self.die_info = np.load(folder+"/die_info.npy", allow_pickle=True)
            self.die_info = self.die_info.astype(np.float32)
            self.xl = self.die_info[0]
            self.yl = self.die_info[1]
            self.xh = self.die_info[2]
            self.yh = self.die_info[3]
            self.row_height = self.die_info[4]
            self.site_width = self.die_info[5]
            self.lef_scale = self.die_info[6]
            self.def_scale = self.die_info[7]

            self.node_size_x = [float(i) for i in open(folder+"/node_size_x.txt", "r").readlines()]
            self.node_size_y = [float(i) for i in open(folder+"/node_size_y.txt", "r").readlines()]
            with open(folder+"/region_info.json", "r") as f: self.regionInfo = json.load(f)
            self.getArea()
            self.getRoutingGrid()
            print(f'[DIE CHECK] {self.xl}, {self.yl}, {self.xh}, {self.yh}')

            self.num_routing_grids_x = self.params.route_num_bins_x
            self.num_routing_grids_y = self.params.route_num_bins_y
            self.num_routing_layers = 1
            self.unit_horizontal_capacity = self.params.unit_horizontal_capacity
            self.unit_vertical_capacity = self.params.unit_vertical_capacity
            self.routing_grid = np.array([self.routing_grid_xl, self.routing_grid_yl, self.routing_grid_xh, self.routing_grid_yh, self.num_routing_grids_x, self.num_routing_grids_y, self.num_routing_layers, self.unit_horizontal_capacity, self.unit_vertical_capacity], dtype=np.float32)
            self.area_info = np.load(folder+"/area_info.npy", allow_pickle=True)
            self.area_info = np.array(self.area_info, dtype=np.float32)
            self.net_name2id_list = np.loadtxt(folder+"/net_name2id_list.txt", dtype=np.int32)
            # pin_names is list of string, so we need to convert it to numpy array of string with fixed length, and then write it to shared memory
            self.pin_names = [i.strip() for i in open(folder + "/pin_names.txt", "r").readlines()]
            self.pin_names = np.array(self.pin_names, dtype='U' + str(max([len(s) for s in self.pin_names])))
            
    
            
            if len(self.flat_region_boxes) > 0:
                self.flat_region_boxes = self.flat_region_boxes.astype(np.int32)
                shm_builder.add_array("flat_region_boxes", self.flat_region_boxes) 
            

            
            #redefine area info, since the values should be updated after scaling transformation, and the values should be scaled before they are written to shared memory by master process.
            self.area_info[0] = self.total_movable_node_area
            self.area_info[1] = self.total_fixed_node_area
            self.area_info[2] = self.total_space_area 
            print(f"[CHECK TOTAL SPACE AREA] total movable node area: {self.total_movable_node_area}, total fixed node area: {self.total_fixed_node_area}, total space area: {self.total_space_area}")

            self.params.shift_factor[0] = self.xl
            self.params.shift_factor[1] = self.yl
            logging.info("set shift_factor = (%g, %g), as original row bbox = (%g, %g, %g, %g)"
                    % (self.params.shift_factor[0], self.params.shift_factor[1], self.xl, self.yl, self.xh, self.yh))
            if self.params.scale_factor == 0.0 or self.site_width != 1.0:
                self.params.scale_factor = 1.0 / self.site_width
            logging.info("set scale_factor = %g, as site_width = %g" % (self.params.scale_factor, self.site_width))
            
            ### scale transformation  ###
            #  scale transformation is originally performed at PlaceDB, but it is proper to perform the scale transformation here at MakeDB//
            # , since the values should  not be changed when they are read from shared memory and registered to PlaceDB, and the values should be scaled before they are written to shared memory by master process.
            #self.scale(self.params.shift_factor, self.params.scale_factor)

            # after scaling, update die_info and area_info
            self.die_info = np.array([self.xl, self.yl, self.xh, self.yh, self.row_height, self.site_width, self.lef_scale, self.def_scale], dtype=np.float32)
            self.area_info = np.array([self.total_movable_node_area, self.total_fixed_node_area, self.total_space_area], dtype=np.float32)
            print(f'[DIE CHECK] {self.die_info}')

            shm_builder.add_array('area_info', self.area_info)
            shm_builder.add_array("num_node_info", self.num_node_info)
            shm_builder.add_array("node_size_x_LEF", self.node_size_x_LEF)
            shm_builder.add_array("node_size_y_LEF", self.node_size_y_LEF)
            shm_builder.add_array("node2orig_node_map", self.node2orig_node_map)
            shm_builder.add_array("pin_name2id_map_list", self.pin_name2id_map_list)
            shm_builder.add_array("net_name2id_list", self.net_name2id_list)
            shm_builder.add_array("net_weights", self.net_weights)
            shm_builder.add_array("flat_net2pin_map", self.flat_net2pin_map)
            shm_builder.add_array("flat_net2pin_start_map", self.flat_net2pin_start_map)
            shm_builder.add_array("flat_node2pin_map", self.flat_node2pin_map)
            shm_builder.add_array("flat_node2pin_start_map", self.flat_node2pin_start_map)
            shm_builder.add_array("pin2node_map", self.pin2node_map)
            shm_builder.add_array("pin2net_map", self.pin2net_map)
            shm_builder.add_array("net_weights", self.net_weights)
            shm_builder.add_array("net_weight_deltas", self.net_weight_deltas)
            shm_builder.add_array("net_criticality", self.net_criticality)
            shm_builder.add_array("net_criticality_deltas", self.net_criticality_deltas)
            shm_builder.add_array("rows", self.rows)
            shm_builder.add_array("flat_region_boxes_start", self.flat_region_boxes_start)
            shm_builder.add_array("node2fence_region_map", self.node2fence_region_map)
            shm_builder.add_array("netInfoCellIdxList", self.netInfoCellIdxList)
            shm_builder.add_array("netInfoCellIdxList_startIdx", self.netInfoCellIdxList_startIdx)
            shm_builder.add_array("die_info", self.die_info)
            shm_builder.add_array("routing_grid", self.routing_grid)
            shm_builder.add_array("net_name2id_list", self.net_name2id_list)
            shm_builder.add_array("pin_names", self.pin_names)

      
            if "center_region_auto" in self.params.__dict__:
                self.center_region = Analysis.make_center_region(vertices=self.die_info['layout'], area_ratio=self.params.center_region_auto)
                self.center_region_array = np.array(self.center_region, dtype=np.float32)
                shm_builder.add_array("center_region", self.center_region_array)
            elif "center_region" in self.params.__dict__:
                self.center_region = np.load(self.params.save_path + '/center_region.npy')
                self.center_region_array = np.array(self.center_region, dtype=np.float32)
                shm_builder.add_array("center_region", self.center_region_array)
            
            # center region scale transformation
            if 'cetner_region' in self.__dict__:
                if len(self.center_region) > 0:
                    box_shift_factor = np.array([self.params.shift_factor, self.params.shift_factor]).reshape(1, -1)
                    self.center_region -=  box_shift_factor
                    self.center_region *= self.params.scale_factor
            
            if 'force_weight_center_region' in self.params.__dict__:
                self.force_weight_center_region = float(self.params.force_weight_center_region)
                shm_builder.add_array("force_weight_center_region", np.array([self.force_weight_center_region], dtype=np.float32))
            else:
                self.force_weight_center_region = 1
                shm_builder.add_array("force_weight_center_region", np.array([self.force_weight_center_region], dtype=np.float32))

            if os.path.exists(folder+"/fixed_default_macros.npy"):
                self.fixed_default_macros = np.load(folder+'/fixed_default_macros.npy', allow_pickle=True)
                self.fixed_default_macros = np.array(self.fixed_default_macros, dtype=np.float32)
                shm_builder.add_array("fixed_default_macros", self.fixed_default_macros)

            
            total_bytes = sum(a.nbytes for a in shm_builder.arrays.values())
            print("Shared DB size (GB):", total_bytes/1e9)
            
            self.shm = shm_builder.create(folder)
            print("Shared DB created")


            ### list of varianles not defined for master due to never being used in further steps ###
            # 1. node_name2id_map
            # 2. node_names
            # 3. pin_direct
            # 4. pin_names
            # 5. net_name2id_map
            # 6. pin_name2id_map
            # 7. net_info_cellName
            # 8. net_names
            # 9. net2pin_map
            # 10. rows
            # 11. regions
            # 12. regions_original
            # 13. node2pin_map

            
            # keep the master process alive until all the worker processes read the shared memory and create their local copy of data, and then the master process can exit safely without causing the shared memory to be destroyed before the worker processes read it.
            print("Master process is going to sleep to keep the shared memory alive until all worker processes read the shared memory and create their local copy of data.")
            while True:
                time.sleep(3600)


    
    def scale(self, shift_factor=1, scale_factor=1):
        """
        @brief shift and scale coordinates
        @param shift_factor shift factor to make the origin of the layout to (0, 0)
        @param scale_factor scale factor
        """
        self.xl -= shift_factor[0]
        self.xl *= scale_factor
        self.yl -= shift_factor[1]
        self.yl *= scale_factor
        self.xh -= shift_factor[0]
        self.xh *= scale_factor
        self.yh -= shift_factor[1]
        self.yh *= scale_factor
        self.routing_grid_xl -= shift_factor[0]
        self.routing_grid_xl *= scale_factor
        self.routing_grid_yl -= shift_factor[1]
        self.routing_grid_yl *= scale_factor
        self.routing_grid_xh -= shift_factor[0]
        self.routing_grid_xh *= scale_factor
        self.routing_grid_yh -= shift_factor[1]
        self.routing_grid_yh *= scale_factor
        self.row_height *= scale_factor
        self.site_width *= scale_factor

        # shift factor for rectangle 
        box_shift_factor = np.array([shift_factor, shift_factor]).reshape(1, -1)
        self.rows -= box_shift_factor
        self.rows *= scale_factor
        self.total_space_area *= scale_factor * scale_factor # this is area

        if len(self.flat_region_boxes):
            self.flat_region_boxes -= box_shift_factor
            self.flat_region_boxes *= scale_factor
        # may have performance issue
        # I assume there are not many boxes

        for i in range(len(self.regions)):
            self.regions[i] -= box_shift_factor
            self.regions[i] *= scale_factor
        

      
     
     
    def getNetInfoCellName(self):
        self.netInfoCellIdxList, self.netInfoCellIdxList_startIdx = [], []
        for netName in self.net_list:
            self.netInfoCellIdxList_startIdx.append(len(self.netInfoCellIdxList))
            if netName not in self.net_info_cellName:
                continue
            cellNameList = self.net_info_cellName[netName]["macro"] + self.net_info_cellName[netName]["std_cell"] + self.net_info_cellName[netName]["ext_pin"]
            cellIdxList = [] 
            for cellName in cellNameList:
                cellIdxList.append(self.node_name2id_map[cellName])
            cellIdxList.sort()
            for i in cellIdxList:
                self.netInfoCellIdxList.append(i)



    def cleanNetList(self):
        net_info_clean = dict()
        for net in self.net_list:
            if "use" in self.net_info[net].keys():
                net_info_clean[net] = {"cell_list": list(), "use": self.net_info[net]["use"]}
            else:
                net_info_clean[net] = {"cell_list": list(), "use": "USE SIGNAL"}
            for cellName in self.net_info[net]["cell_list"]:
                if cellName.startswith("boundary"):
                    continue
                net_info_clean[net]["cell_list"].append(cellName)
        return net_info_clean

    

    def txt2list(self, path, listName, data_type):
        f = open(path, "r")
        if data_type == "string":
            for i in f.readlines():
                listName.append(i.strip())
        elif data_type == "int":
            for i in f.readlines():
                listName.append(int(i))
        elif data_type == "float":
            for i in f.readlines():
                listName.append(float(i))
        f.close()
    
    def getNodeMap(self):    
     
        ####### node_names, node_name2id_map, num_physical_nodes ��~L들기 ######## 

        df_node_std_cell = self.df_totalCell[self.df_totalCell["cell_type"] == "STD_CELL"]
        node_movable_std_cell = df_node_std_cell[df_node_std_cell["placed_state"] != "FIXED"]["cell_name"].to_list()
        node_fixed_std_cell = df_node_std_cell[df_node_std_cell["placed_state"] == "FIXED"]["cell_name"].to_list()
        node_ext_pin = self.df_totalCell[self.df_totalCell["cell_type"] == "PIN"]["cell_name"].to_list()
        df_node_macro = self.df_totalCell[self.df_totalCell["cell_type"] == "MACRO"]

        node_movable_std_cell.sort()
        node_fixed_std_cell.sort()
        node_ext_pin.sort()
        #node_macro.sort(reverse=True)
        
        node_blockage = []
        for name, placed_state in zip(self.df_totalCell["cell_name"], self.df_totalCell["placed_state"]):
            cellName = str(name)
            if cellName.startswith("BLOCKAGE"):
                if placed_state == "FIXED":
                    node_blockage.append(cellName)
                else:
                    raise Exception("[make DB ] blockage placed_state is wrong (should be FIXED).")

        self.num_terminal_NIs = len(node_ext_pin)
        if self.params.macro_move == "True":
            node_movable_macro = df_node_macro[df_node_macro["placed_state"] != "FIXED"]["cell_name"].to_list()
            node_fixed_macro = df_node_macro[df_node_macro["placed_state"] == "FIXED"]["cell_name"].to_list()
            node_movable_macro = [name for name in node_movable_macro if name.startswith("BLOCKAGE") == False]
            node_fixed_macro_wo_blockage = [name for name in node_fixed_macro if name.startswith("BLOCKAGE") == False]
            self.num_terminals = len(node_fixed_std_cell) + len(node_fixed_macro_wo_blockage) + len(node_blockage)
            self.num_fake_macro = len(node_movable_macro)
            ### sorting in order to compare other parsing code resutls
            node_movable_macro.sort()
            node_fixed_macro_wo_blockage.sort()
            node_blockage.sort()
            self.node_names = node_movable_std_cell + node_movable_macro + node_fixed_std_cell + node_fixed_macro_wo_blockage + node_blockage + node_ext_pin
        else:
            node_movable_macro = df_node_macro[df_node_macro["placed_state"] != "FIXED"]["cell_name"].to_list()
            node_fixed_macro = df_node_macro[df_node_macro["placed_state"] == "FIXED"]["cell_name"].to_list()
            node_fixed_macro_wo_blockage = [i for i in node_fixed_macro if i.startswith("BLOCKAGE") == False]
            print("[CHECK NODE]", node_fixed_macro_wo_blockage)
            self.num_terminals = len(node_fixed_std_cell) + len(node_fixed_macro_wo_blockage) + len(node_blockage)
            self.num_fake_macro = len(node_movable_macro)
            ### sorting in order to compare other parsing code resutls
            node_movable_macro.sort() 
            node_fixed_macro_wo_blockage.sort()
            node_blockage.sort()
            self.node_names = node_movable_std_cell + node_movable_macro + node_fixed_std_cell + node_fixed_macro_wo_blockage + node_blockage + node_ext_pin


        self.num_movable_std_cell = len(node_movable_std_cell)
        self.num_movable_macro = len(node_movable_macro)
        self.num_fixed_std_cell = len(node_fixed_std_cell)
        self.num_fixed_macro = len(node_fixed_macro_wo_blockage)


        ### node_name2id_map ��~]성 ###
        self.virtualNodes = []
        for idx, name in enumerate(self.node_names):
            self.node_name2id_map[name] = idx
            if name.startswith('virtual') and name[-1].isdigit():
                self.virtualNodes.append(name)


        ### num_physical_nodes ��~]성 ###
        self.num_physical_nodes = len(self.node_name2id_map.keys())
        self.num_blockage = len(node_blockage)
        
        print("[makeDB ] num_node_names: {}".format(len(self.node_names)))
        print("[makeDB ] num_physical_node: {}".format(self.num_physical_nodes))
        print("[makeDB ] num_terminals: {}".format(self.num_terminals))
        print("[makeDB ] num_terminal_NIs: {}".format(self.num_terminal_NIs))
        print("[makeDB ] num_blockage: {}".format(self.num_blockage))
        print("[makeDB ] num_movable_std_cell: {}".format(self.num_movable_std_cell))
        print("[makeDB ] num_movable_macro: {}".format(self.num_movable_macro))
        print("[makeDB ] num_fixed_std_cell: {}".format(self.num_fixed_std_cell))
        print("[makeDB ] num_fixed_macro: {}".format(self.num_fixed_macro))
        if self.params.macro_move != "True":
            print("[makeDB ] num_fixed_macros_wo_blockage: {}".format(len(node_fixed_macro_wo_blockage)))
        print("[makeDB ] num_fake_macro: {}".format(self.num_fake_macro))
        if len(self.virtualNodes)> 0:
            print("[makeDB ] virtual node: ", self.virtualNodes)
        if getattr(self.params, '_makedb_write_metadata', True):
            print("[makeDB ] node_names saved in ", self.params.save_path +"/node_names.txt")
            with open(self.params.save_path +"/node_names.txt", "w") as f:
                for i in self.node_names:
                    f.write(i+'\n')
     



    ### node_size_x, node_size_y 얻기 ####
    def getNodeSize(self):
        for node in self.node_names:
            #node = node.replace(".DREAMPlace.Shape0", "").strip()
            if node in self.cell_info_keys:
                id = self.cell_info[node]["macro_id"]
                width, height = round(float(round(self.lefCellInfo[id]["width"]*self.def_scale)),1), round(float(round(self.lefCellInfo[id]["height"]*self.def_scale)),1)
                self.node_size_x.append(width)
                self.node_size_y.append(height)
            elif node in self.extPinInfo_keys:
                self.node_size_x.append(0)
                self.node_size_y.append(0)
            else:
                raise Exception(f"node {node} not defined in cell_info")

        self.node_size_x_LEF = self.node_size_x.copy()
        self.node_size_y_LEF = self.node_size_y.copy()

     
     
    def getNode2Orig(self):
        for idx, node in enumerate(self.node_names):
            self.node2orig_node_map.append(idx)
     
     
    #### node��~X ��~D��~X ��~H기�~Y~T #### 
    def getNodePos(self):
        ## pos file = [std_cell pos] + [macro_pos] + [external pin pos] + [filler pos] (blockage macro pos is not included.)
        if "read" not in self.params.mode and "read_node_names" not in self.params.__dict__: 
            ### standard cell��~X ��~D��~X ######
            for node in self.node_names[:self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]:
                #node = node.replace(".DREAMPlace.Shape0", "").strip()
                if node in self.cell_info_keys:
                    self.node_x.append(-1)
                    self.node_y.append(-1)
                else:
                    raise Exception("{} node not in cells_info".format(node))

            ### macro��~X ��~D��~X #########
            for node in self.node_names[self.num_physical_nodes-self.num_terminals-self.num_terminal_NIs-self.num_fake_macro: self.num_physical_nodes-self.num_terminal_NIs]:
                #node = node.replace(".DREAMPlace.Shape0", "").strip()
                if node in self.cell_info_keys:
                    if len(self.cell_info[node]["position"])>0:
                        x, y = self.cell_info[node]["position"][0], self.cell_info[node]["position"][1]
                        self.node_x.append(float(x))
                        self.node_y.append(float(y))
                    else:
                        self.node_x.append(-1)
                        self.node_y.append(-1)
                else:
                    raise Exception("{} node not in cells_info".format(node))


            ### external pin��~X ��~D��~X #########     
            for node in self.node_names[self.num_physical_nodes-self.num_terminal_NIs: self.num_physical_nodes]:
                if node in self.extPinInfo_keys:
                    x, y = self.extPinInfo[node]["layer0"]["position"][0], self.extPinInfo[node]["layer0"]["position"][1]
                    width, height =  self.extPinInfo[node]["layer0"]["width"],  self.extPinInfo[node]["layer0"]["height"]
                    self.node_x.append(float(x) + float(width)/2)
                    self.node_y.append(float(y) + float(height)/2)
                else:
                    raise Exception("{} node not in cells_info", node)

        elif "read" in self.params.mode and "read_node_names" in self.params.__dict__:
            fx = open(self.params.read_posX, "r")
            for line in fx.readlines():
                line = line.replace("\n", "").strip()
                self.node_x.append(float(line))
            fx.close()
            fx = open(self.params.read_posY, "r")
            for line in fx.readlines():
                line = line.replace("\n", "").strip()
                self.node_y.append(float(line))
            fx.close()




    ### node��~X orient 구�~U~X기 ####
    def getNodeOrient(self):
        ## orient ��~L일 = [standard cell orientation] + [macro orientation] + [external pin orientation ] + [hpwl, overflow, max_density]
        ## ��~H��~@��~I [hpwl, overflow, max_density]��~@ ��~H��~A��~@ ��~H��~D ��~X��~D ��~H��~L
        #if self.params.mode == "read" or self.params.mode == "read/write" or self.params.mode == "read/binary_write":
        if "read" not in self.params.mode and "read_orient" not in self.params.__dict__:
            #### standard cell��~X orient ####
            for node in self.node_x[:self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals - self.num_fake_macro ]:
                self.node_orient.append("UNKNOWN")
            #### macro��~@ external pin��~X orient��~@ filler ### 
            for node in self.node_x[self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals - self.num_fake_macro:]:
                self.node_orient.append("N")
        else:
            ## orient ��~L일�~W~P��~T std_cell, macro, ext_pin��~X 방�~V� ��~U보만 ��~H��~L (blockage ��~O filler��~X ��~D��~X ��~U보 ��~F��~L)
            file = self.params.read_orient
            f = open(file, "r")
            self.node_orient = []

            #std_cell, macro, blockage, external pin��~L��~@��~X ��~U보를 읽기
            for i, o in enumerate(f.readlines()):
                o = o.replace("\n", "").strip()
                self.node_orient.append(o)
            f.close()


            #filler��~X orient 삽�~^~E
            for i in range(len(self.node_x) - len(self.node_orient)):
                self.node_orient.append("N")



            assert len(self.node_orient) == len(self.node_x), "[WARNING] Node_X and Node_Orient have different number of elements."



    ### orient��~P 따른 pin offset ��~D산 ###
    def getOrientParam(self):
        self.orientRotateAngleList, self.orientFlipList = list(), list()
        for idx, orient in enumerate(self.node_orient):
            if orient == "UNKNOWN" or orient == "N":
                rotateAngle = 0
                flip = "none"
            elif orient == "S":
                rotateAngle = 180
                flip = "none"
            elif orient == "W":
                rotateAngle = 90
                flip = "none"
            elif orient == "E":
                rotateAngle = 270
                flip = "none"
            elif orient == "FN":
                rotateAngle = 0
                flip = "vertical"
            elif orient == "FS":
                rotateAngle = 180
                flip = "vertical"
            elif orient == "FW":
                rotateAngle = 90
                flip = "vertical"
            elif orient == "FE":
                rotateAngle = 270
                flip = "vertical"

            self.orientRotateAngleList.append(rotateAngle)
            self.orientFlipList.append(flip)
    
    
    
    ### net에 정의된 pin을 구하기
    ### net_names, net_name2id_map, pin2node_map, pin2net_map, pin_offset_x, pin_offset_y, pin_direct 정의 ###
    ### pin offset을 자기가 속한 cell의 중앙에 위치 
    def getPins4net(self):
        write_metadata = getattr(self.params, '_makedb_write_metadata', True)
        pinInfo_dict = dict()
        nodeIdx2pin_map = list()
        for idx, nodeName in enumerate(self.node_names):
            nodeIdx2pin_map.append([])
        pin_names = [] #variable for opentimer
        pin_name2id_map = dict() #variable for opentimer
        totalPinNum = 0
        macro_net_start_idx_list = [0]
        macro_pin_idx_list = []
        pins_dict = dict()
        for netName in self.net_list:
            if write_metadata:
                pins_dict[netName] = dict()
            #print('NetName: ', netName)
            self.net_name2id_map[netName] = len(self.net_name2id_map.keys())
            netIdx = self.net_name2id_map[netName]
            cell_pins = self.net_info[netName]["cell_list"]
            #print('cell pins: ', cell_pins)
            self.net_names.append(netName)
            pinList4net = list()
            for cellPinName in cell_pins:
                if write_metadata:
                    pins_dict[netName][cellPinName] = dict()
                cellName, pinName = cellPinName.split(" ")
                if cellName == 'PIN':
                    cellName = pinName
                pinIdx = totalPinNum
                totalPinNum += 1
                nodeIdx = self.node_name2id_map[cellName]
                nodeIdx2pin_map[nodeIdx].append(pinIdx)
                #print(f'cell: {cellName}, pin: {pinName}')
                if nodeIdx >= (self.num_physical_nodes - self.num_terminal_NIs): #외부핀인경우
                    offset_x, offset_y = 0, 0
                    direction = self.extPinInfo[pinName]["direction"]
                    pin_names.append(cellPinName)
                    pin_name2id_map[cellPinName] = pinIdx
                    
                    
                elif nodeIdx >= (self.num_physical_nodes - self.num_terminal_NIs - self.num_terminals - self.num_fake_macro) and nodeIdx < (self.num_physical_nodes - self.num_terminal_NIs): #macro의 경우
                    #macroID = self.cell_info[cellName.replace(".DREAMPlace.Shape0", "")]["macro_id"]
                    macroID = self.cell_info[cellName]["macro_id"]
                    try:
                        macroIdx_ = self.lef_def_analysis.lef_macro_name2index_map[macroID]
                    except:
                        raise ValueError(f'cell {cellName} , macroID: {macroID}')
                    #print(f'cell: {cellName}, pin: {pinName} , macroID: {macroID}, macroIdx_: {macroIdx_}')
                    metalLayerList = list(self.lefCellInfo[macroID]["pin"][pinName]["LAYER"].keys())
                    pinPosition = self.lefCellInfo[macroID]["pin"][pinName]["LAYER"][metalLayerList[0]][0]
                    offset_x = round(float(round((float(pinPosition[2]) + float(pinPosition[0]))/2* self.def_scale)),1)
                    offset_y = round(float(round((float(pinPosition[3]) + float(pinPosition[1]))/2* self.def_scale)),1)
                    direction = self.lefCellInfo[macroID]["pin"][pinName]["direction"]
                    pin_names.append(cellPinName)
                    pin_name2id_map[cellPinName] = pinIdx
                else: #standard cell의 경우
                    macroID = self.cell_info[cellName]["macro_id"]
                    macroIdx_ = self.lef_def_analysis.lef_macro_name2index_map[macroID]
                    cell_width, cell_height = self.lefCellInfo[macroID]["width"], self.lefCellInfo[macroID]["height"]
                    offset_x = round(cell_width/2*self.def_scale)
                    offset_y = round(cell_height/2*self.def_scale)
                    direction = self.lefCellInfo[macroID]["pin"][pinName]["direction"]
                    pin_names.append(cellPinName)
                    pin_name2id_map[cellPinName] = pinIdx
                if write_metadata:
                    pins_dict[netName][cellPinName]["x"] = offset_x
                    pins_dict[netName][cellPinName]["y"] = offset_y
                    pinInfo_dict[pinIdx] = {"node_idx": nodeIdx, "net":netIdx, "direction": direction, "offset_x": offset_x, "offset_y": offset_y}
                # pinIdx is assigned monotonically, so append directly in ID
                # order instead of building/sorting/rereading a per-pin dict.
                self.pin2net_map.append(netIdx)
                self.pin2node_map.append(int(nodeIdx))
                self.pin_offset_x.append(offset_x)
                self.pin_offset_y.append(offset_y)
                self.pin_direct.append(direction)
                pinList4net.append(pinIdx)
                
            
            
            self.net2pin_map.append(pinList4net)
            #self.net_weights.append(len(pinList4net))
            
        
                        
        self.pin_names = pin_names
        
        
        
        
        for idx, nodeName in enumerate(self.node_names):
            self.node2pin_map.append(nodeIdx2pin_map[idx])
        
            
        
        #### net_weights에서 모든 값을 1로 설정 #####
        self.net_weights = list()
        for idx in range(len(self.net_list)):
            self.net_weights.append(1.0)
            self.net_weight_deltas.append(0)
            self.net_criticality.append(1.0)
            self.net_criticality_deltas.append(0.0)
        
        
        ### macro가 포함된 net은 가중치를 더 주기 ###
        # for netIdx in self.macro_net_idx_list:
        #     self.net_weights[netIdx] = self.params.macro_net_weight
            
        self.nodeName2pin_map = nodeIdx2pin_map
        self.pin_names = pin_names
        self.pin_name2id_map = pin_name2id_map
        
        print('[makeDB ] num_pins: ', len(self.pin_names))
        print('[makeDB ] num_nets: ', len(self.net2pin_map))
        if write_metadata:
            started = time.time()
            with open(self.params.save_path + "/pins_dict.json", "w") as w: json.dump(pins_dict, w, indent=4)
            with open(self.params.save_path + "/pinInfo_dict.json", "w") as w: json.dump(pinInfo_dict, w, indent=4)
            logging.info("[MakeDB] pin metadata write %.3fs", time.time() - started)
   


    def setNetWeight(self):
        for netName in self.netWeightInfo.keys():
            netIndex = self.net_list.index(netName)
            netWeight = self.netWeightInfo[netName]
            if netName == 'MACRO_NET':
                netWeight = 10
                print('[makeDB ] {} netweight set to 10'.format(netName))
            self.net_weights[netIndex] = netWeight

    def rotate_point(self, x, y, angle_degrees, origin=(0, 0)):
        """
        Rotate a point around a given origin
        angle_degrees: rotation angle in degrees (positive = counterclockwise)
        origin: point around which to rotate
        """
        angle_rad = math.radians(angle_degrees)
        ox, oy = origin
        
        # Translate point to origin
        px = x - ox
        py = y - oy
        
        # Rotation matrix
        cos_theta = np.cos(angle_rad)
        sin_theta = np.sin(angle_rad)
        
        # Apply rotation
        x_rot = px * cos_theta - py * sin_theta
        y_rot = px * sin_theta + py * cos_theta
        
        # Translate back
        x_rot += ox
        y_rot += oy
        
        return [round(x_rot, 5), round(y_rot, 5)]

    def rotate_flip_point(self, point, cellWidth, cellHeight, angle, flipY):
        try:
            # Parse command line arguments
            rectangleWidth = cellWidth
            rectangleHeight = cellHeight
            
            origin = (0, 0)
            rectanglePoints = [
                (0, 0), 
                (rectangleWidth, 0), 
                (rectangleWidth, rectangleHeight), 
                (0, rectangleHeight)
            ]
            
            # Transform all points
            coordinateTransformed = []
            for point_coords in rectanglePoints:
                rotatedPoint = self.rotate_point(point_coords[0], point_coords[1], angle, origin)
                if flipY:
                    rotatedPoint[0] *= -1
                coordinateTransformed.append(rotatedPoint)
            
            
            # Find minimum x and y coordinates
            x_min = min(p[0] for p in coordinateTransformed)
            y_min = min(p[1] for p in coordinateTransformed)
            origin_transformed = [x_min, y_min]
            
            # Check if origin_transformed is in coordinateTransformed
            if origin_transformed not in coordinateTransformed:
                print('ERROR: Transformed origin not found in coordinates')
                sys.exit(1)
            
            # Transform the specified point
            
            rotatedPoint = self.rotate_point(
                                    point[0], 
                                    point[1], 
                                    angle, 
                                    origin)

            if flipY:
                rotatedPoint[0] *= -1
                
            rotatedPoint_transformed = [
                rotatedPoint[0] - origin_transformed[0],
                rotatedPoint[1] - origin_transformed[1]
            ]
            
        except IndexError:
            print("ERROR: Not enough command line arguments")
            sys.exit(1)
        except ValueError:
            print("ERROR: Invalid argument type")
            sys.exit(1)
        
        return rotatedPoint_transformed
    def test_orient(self):
        totalCellInfo = pd.read_csv('tmp/ariane136/save/total_cell_info.tsv', sep='\t')
        macroList = totalCellInfo[totalCellInfo['cell_type']=='MACRO'].reset_index(drop=True)['cell_name'].tolist()
        orientList = totalCellInfo[totalCellInfo['cell_type']=='MACRO'].reset_index(drop=True)['orientation'].tolist()
        for idx, nodeName in enumerate(macroList):
            orient = orientList[idx]
            if 'F' in orient:
                flip = 'vertical'
            else:
                flip = 'none'
            if 'N' in orient:
                rotateAngle = 0
            elif 'S' in orient:
                rotateAngle = 180
            elif 'W' in orient:
                rotateAngle = 90
            elif 'E' in orient:
                rotateAngle = 270
                
            pinIdxList = self.node2pin_map[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]

            cellWidth = self.node_size_x_LEF[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]
            cellHeight = self.node_size_y_LEF[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]
            
            for pinIdx in pinIdxList:
                offset_x = self.pin_offset_x[pinIdx]
                offset_y = self.pin_offset_y[pinIdx]
                if flip == "vertical":
                    rotated_pin_offset = self.rotate_flip_point([offset_x, offset_y], cellWidth, cellHeight, rotateAngle, True)
                else:
                    rotated_pin_offset = self.rotate_flip_point([offset_x, offset_y], cellWidth, cellHeight, rotateAngle, False)
                self.pin_offset_x[pinIdx]= rotated_pin_offset[0]
                self.pin_offset_y[pinIdx]= rotated_pin_offset[1]
                print(nodeName, orient, rotateAngle, flip, self.pin_names[pinIdx].split(' ')[1].strip(), offset_x, offset_y,rotated_pin_offset )


            if rotateAngle == 90 or rotateAngle == 270:
                #cellWidth = self.node_size_x[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]
                #cellHeight =  self.node_size_y[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]
                self.node_size_x[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro] = cellHeight
                self.node_size_y[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro] = cellWidth
                
                   
            elif rotateAngle == 0 or rotateAngle == 180:
                self.node_size_x[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro] = cellWidth
                self.node_size_y[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro] = cellHeight
                
 
       
    def convertOrient(self):
        for idx, nodeName in enumerate(self.node_names[self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro: self.num_physical_nodes-self.num_terminal_NIs]):
        #for idx in range(self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro, self.num_physical_nodes-self.num_terminal_NIs):
            rotateAngle = self.orientRotateAngleList[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]
            flip = self.orientFlipList[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]
            pinIdxList = self.node2pin_map[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]

            cellWidth = self.node_size_x_LEF[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]
            cellHeight = self.node_size_y_LEF[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]

            for pinIdx in pinIdxList:
                offset_x = self.pin_offset_x[pinIdx]
                offset_y = self.pin_offset_y[pinIdx]
                convert_offset_x = np.cos(np.pi/180*rotateAngle)*(offset_x-cellWidth/2) - np.sin(np.pi/180*rotateAngle)*(offset_y-cellHeight) + cellWidth/2
                convert_offset_y = np.sin(np.pi/180*rotateAngle)*(offset_x-cellWidth/2) + np.cos(np.pi/180*rotateAngle)*(offset_y-cellHeight) + cellHeight/2
                self.pin_offset_x[pinIdx] = convert_offset_x
                self.pin_offset_y[pinIdx] = convert_offset_y
            if flip == "vertical":
                for pinIdx in pinIdxList:
                    self.pin_offset_x[pinIdx] = -1*self.pin_offset_x[pinIdx]
            if rotateAngle == 90 or rotateAngle == 270:
                #cellWidth = self.node_size_x[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]
                #cellHeight =  self.node_size_y[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro]
                self.node_size_x[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro] = cellHeight
                self.node_size_y[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro] = cellWidth
            elif rotateAngle == 0 or rotateAngle == 180:
                self.node_size_x[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro] = cellWidth
                self.node_size_y[idx+self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals-self.num_fake_macro] = cellHeight
        

    
        
        
                
         


 

    def getFlat4NetPin(self):
        self.flat_net2pin_map = list()
        totalPinNum = 0
        for pinArray4net in self.net2pin_map:
            pinArray4net.sort()
            for pinIdx in pinArray4net:
                self.flat_net2pin_map.append(pinIdx)
            self.flat_net2pin_start_map.append(totalPinNum)
            totalPinNum += len(pinArray4net)

        self.flat_net2pin_start_map.append(len(self.pin2net_map))



    def getFlat4NodePin(self):
        self.flat_node2pin_map = list()
        for pinArray4node in self.node2pin_map:
            pinArray4node.sort()
            for pinIdx in pinArray4node:
                self.flat_node2pin_map.append(pinIdx)

        totalSize = 0
        for nodeIdx in range(len(self.node2pin_map)):
            pinSize = len(self.node2pin_map[nodeIdx])
            startIdx = totalSize
            self.flat_node2pin_start_map.append(startIdx)
            totalSize += pinSize
        self.flat_node2pin_start_map.append(totalSize)



    def getTotalMovablPins(self):
        num_stdCell = self.num_physical_nodes - self.num_terminals - self.num_terminal_NIs
        stdCellList = self.node_names[:num_stdCell]

        for node in stdCellList:
            nodeIdx = self.node_name2id_map[node]
            pinSize = len(self.node2pin_map[nodeIdx])
            self.num_movable_pins += pinSize

    def getTotalMovablCellArea(self):
        totalArea = 0
        for node in self.node_names[:self.num_physical_nodes-self.num_terminals-self.num_terminal_NIs]:
            id = self.cell_info[node]["macro_id"]
            width, height = float(self.lefCellInfo[id]["width"]), float(self.lefCellInfo[id]["height"])
            totalArea += width*height
        self.total_movable_node_area = totalArea


    def getArea(self):
        num_stdCell = self.num_physical_nodes - self.num_terminals - self.num_terminal_NIs - self.num_fake_macro
        self.num_movable_nodes = num_stdCell + self.num_fake_macro
        self.total_movable_node_area = float(np.sum(np.array(self.node_size_x[:self.num_movable_nodes], dtype=np.float32)*np.array(self.node_size_y[:self.num_movable_nodes], dtype=np.float32)))

        self.total_fixed_node_area = float(np.sum(
                np.maximum(
                    np.minimum(np.array(self.node_x[self.num_movable_nodes:self.num_physical_nodes - self.num_terminal_NIs], dtype=np.float32) + np.array(self.node_size_x[self.num_movable_nodes:self.num_physical_nodes - self.num_terminal_NIs], dtype=np.float32), self.xh)
                    - np.maximum(np.array(self.node_x[self.num_movable_nodes:self.num_physical_nodes - self.num_terminal_NIs], dtype=np.float32), self.xl),
                    0.0) * np.maximum(
                        np.minimum(np.array(self.node_y[self.num_movable_nodes:self.num_physical_nodes - self.num_terminal_NIs], dtype=np.float32) + np.array(self.node_size_y[self.num_movable_nodes:self.num_physical_nodes - self.num_terminal_NIs], dtype=np.float32), self.yh)
                        - np.maximum(np.array(self.node_y[self.num_movable_nodes:self.num_physical_nodes - self.num_terminal_NIs], dtype=np.float32), self.yl),
                        0.0)
                ))
        ## region��~P��~\ virtual region이 ��~H��~D 경�~Z� 실제 palceable area��~T virtual region area를 빼줘야 함 ##
        if "region_info" in self.__dict__:
            if len(self.regionInfo.keys()) > 0:
                if "vr" in self.regionInfo.keys():
                    virtual_region_area = 0
                    for area in self.regionInfo["vr"]:
                        xl, yl, xh, yh = float(area[0]), float(area[1]), float(area[2]), float(area[3])
                        virtual_region_area += (xh-xl)*(yh-yl)
                    self.total_space_area = (self.xh-self.xl)*(self.yh-self.yl) - self.total_fixed_node_area - virtual_region_area*(self.def_scale)**(-2)
            else:
                self.total_space_area = (self.xh-self.xl)*(self.yh-self.yl) - self.total_fixed_node_area
        else:
            self.total_space_area = (self.xh-self.xl)*(self.yh-self.yl) - self.total_fixed_node_area
        print("[makeDB ] Fixed node area: {}".format(self.total_fixed_node_area))
        print("[makeDB ] Total space area: {}".format(self.total_space_area))
        print("[makeDB ] xl: {}, xh: {}, yl: {}, yh:{}".format(self.xl, self.xh, self.yl, self.yh))


    def getRoutingGrid(self):
        self.routing_grid_xl, self.routing_grid_yl, self.routing_grid_xh, self.routing_grid_yh = self.rows[0][0], self.rows[0][1], self.rows[-1][2], self.rows[-1][3]
   

    def getTopModule(self):
        return "CORTEXA5INTEGRATIONCS"


    def getMacroClusterInfo(self):
        self.flat_macroID_cluster_map = [0 for _ in range(self.num_terminals + self.num_fake_macro)]
        self.flat_cluster_macroID_map = list() #[macroID_1 mcaroID_200, macroID_500, ... ]
        self.flat_cluster_macroID_start_map = list()#[0,10,30, ...]
        clusterID=0
        for i in range(len(self.macro_cluster_info)):
            macro_count = 0
            cellList4cluster = ast.literal_eval(self.macro_cluster_info.loc[i, "cell_list"])
            for cellName in cellList4cluster:
                if cellName.startswith("PIN"):
                    continue
                _cellName = cellName.split("/")[-1]
                moduleName = cellName.replace("/"+_cellName, "").strip()
                cellName = self.moduleNameConvert[moduleName] + "/" + _cellName
                if cellName.startswith(self.topModule+"/"):
                    cellName = cellName.replace(self.topModule +"/", "").strip()
                try:
                    cellId = self.node_name2id_map[cellName]
                except:
                    #cellName = cellName +".DREAMPlace.Shape0"
                    cellId = self.node_name2id_map[cellName]

                if cellId >= (self.num_physical_nodes-self.num_terminals-self.num_fake_macro-self.num_terminal_NIs) and \
                    cellId < (self.num_physical_nodes-self.num_terminal_NIs-self.num_terminals): #��~@��~A이�~J~T macro��~L count
                        self.flat_macroID_cluster_map[cellId-(self.num_physical_nodes-self.num_terminals-self.num_fake_macro-self.num_terminal_NIs)] = clusterID
                        macro_count += 1
            if macro_count >0:
                clusterID += 1
                print("[makeDB ] Cluster {} has number of macros {}".format(clusterID, macro_count))


        for clusterID in range(max(self.flat_macroID_cluster_map)+1):
            macroID_list = [macroID for macroID in range(len(self.flat_macroID_cluster_map)) if self.flat_macroID_cluster_map[macroID] == clusterID]
            macroID_list.sort()
            self.flat_cluster_macroID_start_map.append(len(self.flat_cluster_macroID_map))
            self.flat_cluster_macroID_map = self.flat_cluster_macroID_map + macroID_list





    def getRegionInfo(self):
        list_format_regions = {"vr", "evr", "evr_boundary", "evr_center"}
        for region in self.regionNameList_order:
            if region in list_format_regions:
                regionArea = self.regionInfo[region]
            else:
                regionArea = self.regionInfo[region]['area']
            self.regions.append(regionArea)
            
            
        for region in self.regionNameList_order:
            self.flat_region_boxes_start.append(len(self.flat_region_boxes))
            for area in self.regionInfo[region]:
                if region in list_format_regions:
                    self.flat_region_boxes.append(area)
                else:
                    if area != 'area':
                        continue
                    regionArea = self.regionInfo[region]['area']
                    for a_ in regionArea:
                        self.flat_region_boxes.append(a_)
                        
        self.flat_region_boxes_start.append(len(self.flat_region_boxes))

            
            


    def getNodeGroupID(self):
        self.node2fence_region_map = [2147483647 for i in range(len(self.node_names[:len(self.node_names)-self.num_terminal_NIs]))]
        if "vr" in self.regionNameList_order:
            if "evr_center" in self.regionNameList_order:
                default_region_id = self.regionNameList_order.index("evr_center")
            elif "evr" in self.regionNameList_order:
                default_region_id = self.regionNameList_order.index("evr")
            else:
                default_region_id = len(self.regionNameList_order)
            region_name2id = {
                region_name: region_id
                for region_id, region_name in enumerate(self.regionNameList_order)
            }
            has_evr_split = (
                "evr_boundary" in region_name2id and "evr_center" in region_name2id
            )
            self.node2fence_region_map = [default_region_id for i in range(len(self.node2fence_region_map))]
            movable_macro_lo = int(getattr(self, "num_movable_std_cell", 0))
            movable_macro_hi = movable_macro_lo + int(getattr(self, "num_movable_macro", 0))
            for node_idx, node_name in enumerate(self.node_names[:len(self.node_names)-self.num_terminal_NIs]):
                cell_meta = self.cell_info.get(node_name, {})
                cell_region = cell_meta.get("region")
                if has_evr_split and cell_region in {"evr", "evr_boundary", "evr_center"}:
                    lef_type = str(cell_meta.get("cell_type", "")).upper()
                    is_movable_macro = (
                        movable_macro_lo <= node_idx < movable_macro_hi
                        or (lef_type in {"MACRO", "BLOCK"} and cell_meta.get("placed_state") != "FIXED")
                    )
                    self.node2fence_region_map[node_idx] = region_name2id[
                        "evr_boundary" if is_movable_macro else "evr_center"
                    ]
                    continue
                if cell_region in region_name2id:
                    self.node2fence_region_map[node_idx] = region_name2id[cell_region]
            for virtualCell in self.virtual_cells: 
                if virtualCell == 'virtual0':
                    virtual0_node_idx = self.node_name2id_map["virtual0"]
                    self.node2fence_region_map[virtual0_node_idx] = 0
                elif virtualCell == 'virtual1':
                    virtual1_node_idx = self.node_name2id_map["virtual1"]
                    self.node2fence_region_map[virtual1_node_idx] = len(self.regionInfo.keys())
                else:
                    virtual_node_idx = self.node_name2id_map[virtualCell]
                    self.node2fence_region_map[virtual_node_idx] = self.virtual_cells.index(virtualCell) + 1

    
    def getCellList4criticalPath(self, path):
        path_cell = list()
        with open(path, 'r') as f: ciritical_path_info = json.load()
        for path in critical_path_info.keys():
            path_cell.append([])
            cell_info_list = critical_path_info[path]['cell_list']
            for info in cell_info_list:
                cellName = info[0]
                if cellName not in path_cell[path]:
                    if cellName in self.node_name2id_map.keys():
                       node_idx = cellName
                       if node_idx not in path_cell[-1]:
                           path_cell[-1].append(node_idx)
        return path_cell
                
            
            
        


if __name__ == "__main__":
    # with open("/mnt/work/DREAMPlace/bin/benchmarks/ispd2015/mgc_superblue19/lef_info.json", "r") as f: lef_info = json.load(f)
    # with open("/mnt/work/DREAMPlace/bin/benchmarks/ispd2015/mgc_superblue19/cells_info.json", "r") as f: self.cell_info = json.load(f)
    # with open("/mnt/work/DREAMPlace/bin/benchmarks/ispd2015/mgc_superblue19/netlist_info.json") as f: net_info = json.load(f)
    # with open("/mnt/work/DREAMPlace/bin/benchmarks/ispd2015/mgc_superblue19/ext_pin_info.json") as f: extPinInfo = json.load(f)
    with open(sys.argv[1], "r") as f: paramsFile = json.load(f)
    class Params:
        def __init__(self, paramsFile):
            for k in paramsFile.keys():
                super().__setattr__(k, paramsFile[k])

    params = Params(paramsFile)
    db = readDB(params)
    db()
