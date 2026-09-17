import numpy as np
from Cell import Cell
from Pin import Pin

class Net:
    def __init__(self, name: int):
        self.name = name
        self.components = {}  # Dictionary to store components and their pins
        self.use = None
        self.macroList = [] # List to store cells
        self.stdCellList = [] # List to store cells
        self.extPinList = [] # List to store cells
        self.not_used_cell_list = [] 
    def set_name(self, name: str):
        self.name = name
        
    def get_name(self):
        return self.name
        
    def add_cell_pin(self, cell: Cell or Pin, pin_idx: int):
        """Add a cell and its pin to this net.
        
        Args:
            cell: Instance of Cell class
            pin_name: Name of the pin to add
        """
        if not isinstance(cell, Cell)and not isinstance(cell, Pin):
                raise TypeError('cell must be an instance of either Cell or Pin class')
        
            
        cell_idx = cell.get_name()
        lef = cell.get_lef_info()
        pinInstance = lef.get_pin(pin_idx)
        if pinInstance is None:
            raise ValueError(f'"{pin_idx}" is not defined in "{cell.get_name()}"')
        self.components[str(cell_idx) + ' ' + str(pin_idx)] = {'pin': pinInstance, 'cell': cell}
        
        cell_type = lef.get_cell_type()
        if cell_type == 'BLOCK':
            self.macroList.append(cell)
        elif cell_type == 'CORE':
            self.stdCellList.append(cell)
        elif cell_type == 'PIN':
            self.extPinList.append(cell)
        else:
            #raise ValueError(f'[ReadDEF] ERROR cell_type {cell_type} is unproper')
            print(f'[WARNING ] UNPROPER CELL TYPE {cell_type} DEFINED IN NET {self.name}')
            self.not_used_cell_list.append(cell)
   
    def get_not_used_cell_list(self):
        return self.not_used_cell_list

    def get_macro_list(self):
        """Get the list of macros in this net."""
        return self.macroList
    
    def get_stdCell_list(self):
        """Get the list of stdCells in this net."""
        return self.stdCellList
    
    def get_extPin_list(self):
        """Get the list of extPins in this net."""
        return self.extPinList
        
    def get_components(self):
        """Get all components in this net."""
        return self.components
    
    def set_use(self, use):
        """Set the usage type of this pin."""
        self.use = use
        
    def get_use(self):
        """Get the usage type of this pin."""
        return self.use
    
    def get_pinInstance_list(self):
        pinInstList = []
        for cell in self.components.keys():
            pinInstance = self.components[cell]['pin']
            pinInstList.append(pinInstance)
        return pinInstList
   
    
    
