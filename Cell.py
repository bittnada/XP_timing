import numpy as np


class LEF:
    def __init__(self, name: int, symmetry=None):
        self.name = name
        self.width = None
        self.height = None
        self.symmetry = symmetry
        self.pins = []
        self._pin_by_name = {}
        self.cell_type = None
        self.site = None
        self._obs_by_layer = {}
    
    def get_name(self):
        return self.name
        
    def set_name(self, name):
        self.name = name
        
    def get_height(self):
        return self.height
        
    def set_height(self, height: float):
        self.height = height
        
    def get_width(self):
        return self.width
        
    def set_width(self, width: float):
        self.width = width
        
    def get_symmetry(self):
        return self.symmetry
        
    def set_symmetry(self, symmetry):
        self.symmetry = symmetry
        
    def add_pin(self, pin: 'Pin'):
        if not isinstance(pin, Pin):
            raise TypeError('pin should be an instance of Pin class')
        pin_name = pin.get_name()
        if pin_name not in self._pin_by_name: #first, check if the same pin name is already included
            if pin.get_parent_LEF().get_name() == self.get_name(): #second, check if the pin's parent LEF is self
                self.pins.append(pin)
                self._pin_by_name[pin_name] = pin

    def get_pin(self, pin_name, default=None):
        pin = self._pin_by_name.get(pin_name, default)
        if pin is None and self.pins and not self._pin_by_name:
            self._pin_by_name = {p.get_name(): p for p in self.pins}
            pin = self._pin_by_name.get(pin_name, default)
        return pin

    def has_pin(self, pin_name):
        return self.get_pin(pin_name) is not None
            
    def get_pins(self):
        return self.pins
    
    def get_cell_type(self):
        return self.cell_type 
    
    def set_cell_type(self, cell_type_name: str):
        if not isinstance(cell_type_name, str):
            raise TypeError('cell type name should be string type')
        if cell_type_name not in [
            'CORE',
            'BLOCK',
            'CORE ANTENNACELL',
            'PIN',
            'PAD',
            'PAD AREAIO',
            'BUMP',
            'PAD SPACER',
            'ENDCAP BOTTOMLEFT',
            'CORE WELLTAP',
            'CORE SPACER',
            'RING',
            'CORE TIELOW',
            'CORE TIEHIGH',
        ]:
            if cell_type_name == 'RING':
                cell_type_name == 'BLOCK'
                print(f'[ReadLEF warning] cell type name RING is automatically converted to BLOCK')
                
            raise ValueError(f'cell type name "{cell_type_name}" should be BLOCK or CORE')
        self.cell_type = cell_type_name
    
    def get_site(self):
        return self.site
        
    def set_site(self, site):
        self.site = site

    def add_obs_rect(self, layer, xl, yl, xh, yh):
        """Store one OBS RECT in LEF coordinates for a given layer."""
        if layer not in self._obs_by_layer:
            self._obs_by_layer[layer] = []
        self._obs_by_layer[layer].append(
            {"type": "rect", "coords": (float(xl), float(yl), float(xh), float(yh))}
        )

    def add_obs_polygon(self, layer, coords):
        """Store one OBS POLYGON in LEF coordinates for a given layer."""
        if layer not in self._obs_by_layer:
            self._obs_by_layer[layer] = []
        self._obs_by_layer[layer].append(
            {"type": "polygon", "coords": tuple(float(c) for c in coords)}
        )

    def get_obs_by_layer(self):
        return self._obs_by_layer

    def has_obs_geometry(self):
        return bool(self._obs_by_layer)


class Cell:
    def __init__(self, name):
        self.name = name
        self.placed_state = 'UNPLACED'
        self.position = []
        self.orientation = 'UNKNOWN'
        # Store the parent LEF reference
        self.lef_info = None
        self.region = None
        self.halo = None
        
    def set_lef_info(self, lef_info: 'LEF'):
        if not isinstance(lef_info, LEF):
            raise TypeError('lef_info should be an instance of LEF')
        self.lef_info = lef_info 
    
    def get_lef_info(self):
        return self.lef_info
        
    def set_name(self, name):
        self.name = name
        
    def get_name(self):
        return self.name
                
    def get_all_nets_from_pins(self):
        """Collect all netlists from the cell's pins"""
        all_nets = set()  # Using a set to avoid duplicates
        
        # Get nets from all pins
        for pin in self.get_lef_info().get_pins():
            netList = pin.get_net_list()
            for net in netList.keys():
                for cell_pin_name in netList[net]:
                    cellName, pinName = cell_pin_name.split(' ')
                    cell_type = self.get_lef_info().get_cell_type()
                    if cell_type != 'PIN':
                        if cellName == self.get_name():
                            all_nets.add(net)
                    else:
                        if pinName == self.get_name():
                            all_nets.add(net)
        
        return list(all_nets)  # Convert back to list

        
    def set_placed_state(self, placed_state):
        if placed_state in ['UNPLACED', 'PLACED', 'FIXED']:
            self.placed_state = placed_state  # Fixed typo: placed_stae -> placed_state
        else:
            raise TypeError('IMPROPER PLACED STATE: ' + str(placed_state))
            
    def get_placed_state(self):  # Fixed parameter that shouldn't be there
        return self.placed_state
        
    
    def get_pos(self):
        """Get position for a specific layer."""
        return self.position
        
    def set_pos(self, position):
        self.position = np.array(position, dtype=np.float32)  # Fixed: was setting position to np.array(self.position)
    
    
    def get_orientation(self):
        return self.orientation
    
    def set_orientation(self, orientation):
        self.orientation = orientation 
        
    def set_region(self, region):
        self.region = region 
    
    def get_region(self):
        return self.region
    
    def set_halo(self, halo):
        self.halo = halo 
    
    def get_halo(self):
        return self.halo

from Pin import Pin
