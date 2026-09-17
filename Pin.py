import numpy as np
from Cell import LEF
from Cell import Cell
class Pin(LEF):
    def __init__(self, name: int, parent_LEF=None):
        super().__init__(name)
        self.name = name
        """_summary_
        self.layers = {
            "rectnalges": renctanle arrays defined in LEF,
            "position" minimum x and y of the rectangle arrays
        }
        """
        self.layers = {}  # Dictionary to store layer information
        """_summary_
        self.width = differebce between  the minimum x and maximum x of the rectangle arrays
        self.height = differebce between  the minimum y and maximum y of the rectangle arrays
        """
        self.width = None
        self.height = None 
        """_summary_
        self.netList[netName] = [cellName pinName, ...]
        here cellName is a name defined in components not LEF 
        """
        self.netList = {}
        # Only large fanout lists get a membership index. Keep public list
        # order and avoid allocating a set for every tiny net/pin association.
        self._large_net_members = {}
        self.use = {}   # Pin usage type
        self.direction = None #INPUT, OUTPUT
        
        # Store the parent cell reference
        self.parent_LEF = parent_LEF
        self.pos=None
        self.parent_LEF = parent_LEF
        # If parent cell is provided, add this pin to the parent
        if parent_LEF:
            self.parent_LEF.add_pin(self)
    
    def set_parent_LEF(self, lef:LEF):
        self.parent_LEF = lef 
    
    def get_parent_LEF(self):
        return self.parent_LEF
    
    def set_name(self, name):
        self.name = name 
    
    def get_name(self):
        return self.name
    
    def reset_net(self):
        self.netList = {}
        self._large_net_members = {}
    
    def add_net(self, cell:Cell, net:int):
        """Adds a cell to a net in the netList.

        Args:
            cell: The cell to add.
            net: The net to add the cell to.
        """
        if not isinstance(cell, Cell):
            raise TypeError('cell should be an instance of Cell')
        
        
        cell_pin_name = f"{cell.get_name()} {self.get_name()}"
        if net not in self.netList:
            self.netList[net] = [cell_pin_name]
        else:
            members = self.netList[net]
            if len(members) < 32:
                if cell_pin_name not in members:
                    members.append(cell_pin_name)
            else:
                # Also accept Pin objects restored from old pickles.
                if not hasattr(self, '_large_net_members'):
                    self._large_net_members = {}
                index = self._large_net_members.get(net)
                if index is None or len(index) != len(members):
                    index = self._large_net_members[net] = set(members)
                if cell_pin_name not in index:
                    members.append(cell_pin_name)
                    index.add(cell_pin_name)
        
    def get_net_list(self):
        return self.netList 
    
    def set_direction(self, direction):
        self.direction = direction
    
    def get_direction(self):
        return self.direction
    
    def get_layer_pos(self, layer_name):
        """Get position for a specific layer."""
        if layer_name in self.layers and 'position' in self.layers[layer_name]:
            return self.layers[layer_name]['position']
        return None
    
    def get_width(self):
        if not self.layers:
            return None
            
        rectangleList = np.array([])
        for layer_name in self.layers:
            if 'rectangles' in self.layers[layer_name]:
                rectangles = self.layers[layer_name]['rectangles']
                if len(rectangleList) == 0:
                    rectangleList = rectangles
                else:
                    rectangleList = np.vstack((rectangleList, rectangles))
        
        if len(rectangleList) == 0:
            raise ValueError('Pin has no information for shape')
        
        if len(rectangleList) > 1:   
            xl = np.min(rectangleList[0,:])
            xh = np.max(rectangleList[2,:])
        elif len(rectangleList) == 1:
            xl = rectangleList[0][0]
            xh = rectangleList[0][2]
        return xh - xl 
    
    def get_height(self):
        if not self.layers:
            return None
            
        rectangleList = np.array([])
        for layer_name in self.layers:
            if 'rectangles' in self.layers[layer_name]:
                rectangles = self.layers[layer_name]['rectangles']
                if len(rectangleList) == 0:
                    rectangleList = rectangles
                else:
                    rectangleList = np.vstack((rectangleList, rectangles))
        
        if len(rectangleList) == 0:
            raise ValueError('Pin has no information for shape')
            
        if len(rectangleList) > 1:   
            yl = np.min(rectangleList[1,:])
            yh = np.max(rectangleList[3,:])
        elif len(rectangleList) == 1:
            yl = rectangleList[0][1]
            yh = rectangleList[0][3]
        return yh - yl
    
    def set_layer_rectangle(self, metalLayer, rectangleList):  # Changed parameter name to match implementation
        """Set position for a specific layer.
        
        Args:
            metalLayer: name of the layer
            rectangleList: list of rectangles defined as [[xl, yl, xh, yh], ...]
        """
        if not isinstance(rectangleList, list) and not isinstance(rectangleList, np.ndarray):
            print(type(rectangleList))
            raise TypeError(f"rectangleList must be a list or numpy array: {rectangleList}")

        
        # Initialize the layer dict if it doesn't exist
        
        if metalLayer not in self.layers:
            self.layers[metalLayer] = {}
        
        # Convert to numpy array for easier manipulation
        pos_array = np.array(rectangleList)
    
        # # Reshape into a matrix where each row is [xl, yl, xh, yh]
        rectangles = pos_array.reshape(-1, 4)
        # Store all rectangles
        self.layers[metalLayer]['rectangles'] = rectangles
    
        # Calculate the bounding box for all rectangles
        xl = np.min(rectangles[:, 0])  # smallest xl
        yl = np.min(rectangles[:, 1])  # smallest yl
        xh = np.max(rectangles[:, 2])  # largest xh
        yh = np.max(rectangles[:, 3])  # largest yh
    
        # Store the overall bounding box as the position
        self.layers[metalLayer]['position'] = np.array([xl, yl])
    
    def get_pos(self):
        xl_list, yl_list = np.array([]), np.array([])
        for layer in self.layers:
            x_layer, y_layer = self.layers[layer]['position']
            xl_list = np.append(xl_list, x_layer)
            yl_list = np.append(yl_list, y_layer)
        return np.min(xl_list), np.min(yl_list)
                
        
    def set_use(self, layer_num, use):
        """Set the usage type of this pin."""
        self.use[layer_num] = use
        
    def get_use(self, layer_num):
        """Get the usage type of this pin."""
        return self.use[layer_num]
    
    
    def get_layers(self):
        return self.layers
    
    def set_orientation(self, orientation):
        self.orientation = orientation 
    
    def get_orientation(self):
        return self.orientation
