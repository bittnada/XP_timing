import json
import os, sys
import numpy as np
import logging
from Cell import LEF
from Pin import Pin
from NameIdMap import NameIdMap


def polygon_coords_to_bbox_rect(coords):
    """Convert a LEF POLYGON vertex list [x0, y0, x1, y1, ...] to one RECT [xl, yl, xh, yh]."""
    if len(coords) < 6 or len(coords) % 2 != 0:
        raise ValueError(
            "LEF POLYGON needs at least 3 vertices as flat xy pairs, got {}".format(coords)
        )
    xs = coords[0::2]
    ys = coords[1::2]
    return [min(xs), min(ys), max(xs), max(ys)]


class ReadLEFinfo:
     
    def __init__(self, lef_file_directory):
        #self.params = params
        self.direcotry_path_group = lef_file_directory
        #self.lef_scale = 1000
        self.total_lef_info = {}
        self.lef_macro_name_to_id = NameIdMap("LEF macro")
        self.lef_pin_name_to_id = NameIdMap("LEF pin")
        # Backward-compatible aliases. Internally these are no longer
        # bidirectional dicts; they store name_to_id and id_to_name separately.
        self.lef_macro_name2index_map = self.lef_macro_name_to_id
        self.lef_pin_name2index_map = self.lef_pin_name_to_id
        
    def get_total_macro_info(self):
        paths = self.direcotry_path_group
        if isinstance(paths, (str, os.PathLike)):
            paths = [paths]
        fileList = []
        for path in paths:
            if os.path.isdir(path):
                fileList.extend(os.path.join(path, f) for f in sorted(os.listdir(path))
                                if f.lower().endswith('.lef'))
            else:
                fileList.append(os.fspath(path))
        if not fileList:
            raise ValueError('No LEF files found in lef_dir_path/lef_input')
        for f in dict.fromkeys(fileList):
            lefInfo = self.getMacroInfo(f)
            for k, v in lefInfo.items():
                self.total_lef_info[k] = v
        return self.total_lef_info
        
        
        
    def save_as_json(self, save_name):
        dict_lef = {}
        for macro in self.total_lef_info.keys():
            dict_lef[macro] = {'class':None, 'width': None, 'height': None, 'symmetry': None, 'pin': None}
            cell_type = self.total_lef_info[macro].get_cell_type()
            width = self.total_lef_info[macro].get_width()
            height = self.total_lef_info[macro].get_height()
            symmetry = self.total_lef_info[macro].get_symmetry()
            dict_lef[macro]['class'] = cell_type
            dict_lef[macro]['width'] = width
            dict_lef[macro]['height'] = height 
            dict_lef[macro]['symmetry'] = symmetry
            dict_lef[macro]['pin'] = dict()
            pinList = self.total_lef_info[macro].get_pins()
            for pin_ in pinList:
                pinName = pin_.get_name()
                direction = pin_.get_direction()
                use = pin_.get_use()
                dict_lef[macro]['pin'][pinName] = {'direction': direction, 'use': use, 'LAYER': dict()}
                layers = pin_.get_layers()
                for layerName in layers.keys():
                    rectangles = layers[layerName]['rectangles']
                    dict_lef[macro]['pin'][pinName]['LAYER'][layerName] = [list(r) for r in rectangles]
            
        with open(save_name, "w") as w: json.dump(dict_lef, w, indent=4)
                  
            
    
            
    def getMacroInfo(self, fileAddress):
        file = open(fileAddress, 'r')
        macroID = None
        macroLinebegin = False
        pinbegin = False
        layerbegin = False
        rectbegin = False
        currentPinID = ""
        macro_lef = None
        LEFinfo ={}
        
        for idx, line in enumerate(file):
            line = line.strip()
            if  macroLinebegin == False and line.startswith("MACRO"):
                if line.endswith(";"):
                    continue
                macroLinebegin = True
                macroID = line.replace("MACRO", "").replace("\n", "").strip()
                macroIdx = self.lef_macro_name_to_id.get_or_create(macroID)
                macro_lef = LEF(macroIdx)
                rectangleList ={}
    
            if macroLinebegin == True and line.startswith("END " + macroID):
                macroLinebegin = False
                virtualPinIdx = self.lef_pin_name_to_id.get_or_create("VP")
                virtual_pin = Pin(virtualPinIdx, macro_lef)
                cell_width  = macro_lef.get_width()
                cell_height = macro_lef.get_height()
                virtual_pin_rects = [cell_width/4, cell_height/4, cell_width*3/4, cell_height*3/4]
                virtual_pin.set_layer_rectangle('M2', virtual_pin_rects)
                virtual_pin.set_direction('INPUT')
                virtual_pin.set_use('M2', 'SIGNAL')
                # Update LEF instance due to the addition of pin 'VP'
                macro_lef.add_pin(virtual_pin)
                
                LEFinfo[macroIdx] = macro_lef
        
            if macroLinebegin == True and line.startswith("CLASS"):
                classMacro = line.replace("CLASS", "").replace("\n", "").replace(";","").strip()
                macro_lef.set_cell_type(classMacro)
            
            if macroLinebegin == True and line.startswith("SIZE"):
                size = line.replace("SIZE", "").replace("\n", "").replace(";","").strip()
                width, height = float(size.split("BY")[0].strip()), float(size.split("BY")[1].strip())
                macro_lef.set_width(width)
                macro_lef.set_height(height)
            
            if macroLinebegin == True and line.startswith("SYMMETRY"):
                symmetry = line.replace("SYMMETRY", "").replace("\n", "").replace(";","").strip()
                macro_lef.set_symmetry(symmetry)
            
            if macroLinebegin == True and line.startswith("SITE"):
                site = line.replace("SITE", "").replace("\n", "").replace(";","").strip()
                macro_lef.set_site(site)
                
            if macroLinebegin == True and (line.startswith("PIN") or line.startswith("OBS")):
                if line.startswith("PIN"):  
                    pinID = line.replace("PIN", "").replace("\n", "").replace(";","").strip()
                else:
                    pinID = 'OBS'

                currentPinID = pinID
                currentPinIdx = self.lef_pin_name_to_id.get_or_create(currentPinID)
                pin_ = Pin(currentPinIdx, macro_lef)
                pinbegin = True
                # USE is a pin attribute, never inherited from a preceding pin
                # or macro. Resolve it at END <pin>, after all PORTs/attributes.
                use = None
                
            
            if macroLinebegin == True and pinbegin == True and line == ("END " + currentPinID):
                if use is None:
                    use = 'SIGNAL'
                    logging.warning(
                        "[ReadLEF] %s:%d PIN '%s/%s' has no USE; forcing USE to SIGNAL",
                        fileAddress, idx + 1, macroID, currentPinID)
                for layer_name in pin_.get_layers():
                    pin_.set_use(layer_name, use)
                macro_lef.add_pin(pin_)
                pinbegin = False #initialization
                
            if macroLinebegin == True and pinbegin == True and line.startswith("DIRECTION"):
                direction = line.replace("DIRECTION", "").replace("\n", "").replace(";","").strip()
                pin_.set_direction(direction)
            
            if macroLinebegin == True and pinbegin == True and line.startswith("USE"):
                use = line.replace("USE", "").replace("\n", "").replace(";","").strip()
                
                
            if macroLinebegin == True and pinbegin == True and layerbegin == False and line.strip().startswith("LAYER"):
                metalLayer = line.replace("LAYER", "").replace("\n", "").replace(";","").strip()
                layerbegin = True
                rectangleList[metalLayer] = []
                
            if macroLinebegin == True and pinbegin == True and layerbegin == True and (line.startswith("RECT") or line.startswith('POLYGON')):
                is_polygon = line.startswith('POLYGON')
                if line.startswith('RECT'):
                    pinPosList = [float(i) for i in line.strip().replace("RECT","").replace(';','').strip().split(" ") if i != ""]
                elif is_polygon:
                    pinPosList = [float(i) for i in line.strip().replace("POLYGON","").replace(';','').strip().split(" ") if i != ""]
                else:
                    raise ValueError("[ERROR ] UNDEFINED LAYER TYPE {line}")

                if "" in pinPosList: 
                    pinPosList.remove("")
                if ";" in pinPosList:
                    pinPosList.remove(";")

                obs_polygon_coords = None
                if is_polygon:
                    obs_polygon_coords = list(pinPosList)
                    pinPosList = polygon_coords_to_bbox_rect(pinPosList)

                if pinID == 'OBS' and macro_lef is not None:
                    if is_polygon:
                        macro_lef.add_obs_polygon(metalLayer, obs_polygon_coords)
                    elif len(pinPosList) == 4:
                        macro_lef.add_obs_rect(metalLayer, *pinPosList)
                try:
                    rectangleList[metalLayer].extend(pinPosList)
                except:
                    raise ValueError(f"[ Warning] LEF parsing error at line {idx} in file {fileAddress}. line: {line}, Error info: pin {currentPinID} in macro {macroID} has invalid rectangle data for metal layer {metalLayer}.")
                rectbegin = True

            if macroLinebegin == True and pinbegin == True and layerbegin == True and rectbegin == True and line.strip().startswith("LAYER"):
                metalLayer = line.replace("LAYER", "").replace("\n", "").replace(";","").strip()
                layerbegin = True
                rectangleList[metalLayer] = []
                
            if macroLinebegin == True and pinbegin == True and layerbegin == True and line.strip() == "END":
                if len(rectangleList.get(metalLayer, [])) >= 4:
                    pin_.set_layer_rectangle(metalLayer, rectangleList[metalLayer])
                # OBS is not a signal pin. Ordinary pin USE is assigned only
                # at END <pin>, so explicit USE after a PORT also takes effect.
                if pinID == 'OBS':
                    pin_.set_use(metalLayer, 'OBS')
                layerbegin = False
        file.close()
        return LEFinfo
        
                        
        
        



if __name__ == "__main__":
    params_path = sys.argv[1]
    with open(params_path, "r") as f: params = json.load(f)
    lef = ReadLEFinfo(params["lef_dir_path"])

    
     
