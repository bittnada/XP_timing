import numpy as np
import numpy as np
class Die:
    def __init__(self):
        self.die_layout = None 
        self.layout_xl, self.layout_yl, self.layout_xh, self.layout_yh = None, None, None, None 
        self.width, self.height = None, None
        self.rowInfo = dict()
        self.row_xl_list = list()
        self.row_yl_list = list()
        self.row_xh_list = list()
        self.row_yh_list = list()
        self.scale = None
        self.row_height = None
        self.rows = None
        self.region = dict()
        self.blockage = dict()
        self.group = dict()
    
    def set_layout(self, layout: list):
        layout = np.array(layout).reshape(-1,2)
        self.die_layout = layout
        self.layout_xl = np.min(self.die_layout[:,0])
        self.layout_xh = np.max(self.die_layout[:,0])
        self.layout_yl = np.min(self.die_layout[:,1])
        self.layout_yh = np.max(self.die_layout[:,1])
        self.width = self.layout_xh - self.layout_xl 
        self.height = self.layout_yh - self.layout_yl
    
    def get_layout(self):
        return self.die_layout
    
    def get_layout_xl(self):
        return self.layout_xl
    
    def get_layout_yl(self):
        return self.layout_yl
    
    def get_layout_xh(self):
        return self.layout_xh
    
    def get_layout_yh(self):
        return self.layout_yh
    
    def set_width(self, width):
        self.width = width
    
    def get_width(self):
        return self.width
    
    def set_height(self, height):
        self.height = height
    
    def get_height(self):
        return self.height
    
    def set_site_name(self, site_name:str):
        self.site = site_name 
    
    def get_site_name(self):
        return self.site
    
    def set_row_height(self, row_height:float):
        self.row_height = row_height
    
    def get_row_height(self):
        return self.row_height
    
    def set_site_width(self, site_width:float):
        self.site_width = site_width 
        
    def get_site_width(self):
        return self.site_width
    
    def get_blockage(self):
        return self.blockage
    
    def get_region(self):
        return self.region
    
    def get_group(self):
        return self.group
    
    def add_region(self, regionName: str, region_coords: np.ndarray):
        if regionName not in self.region:
            self.region[regionName] = region_coords
        else:
            print(f"Region {regionName} already exists. Overwriting the existing region.")

    def add_blockage(self, blockageNum: int, blockage_coords: np.ndarray):
        if blockageNum not in self.blockage:
            self.blockage[blockageNum] = blockage_coords
        else:
            print(f"Blockage {blockageNum} already exists. Overwriting the existing blockage.")
    
    def add_group(self, groupName: str, group_coords: np.ndarray):
        if groupName not in self.group:
            self.group[groupName] = group_coords
        else:
            print(f"Group {groupName} already exists. Overwriting the existing group.")

    def add_row(self, row: list):
        row_idx, row_name, row_xl, row_yl, orient, step_num, step_size = row
        row_xl, row_yl, step_size = [float(i) for i in [row_xl, row_yl, step_size]]
        step_num = int(step_num)
        self.set_site_name(row_name)
        self.set_site_width(float(step_size))
        if len(row) == 0:
            raise ValueError('row should be a list: [row_idx, row name, row_xl, row_yl, orient, step_size]')
        self.rowInfo[row_idx] = {
            'row_name': row_name,
            'row_xl': row_xl,
            'row_yl': row_yl,
            'orient': orient,
            'step_num': step_num,
            'step_size': step_size
        }
        self.row_xl_list.append(row_xl)
        self.row_yl_list.append(row_yl)
        self.row_xh_list.append(row_xl + step_num*step_size)
        if len(self.row_yl_list) ==2:
            self.set_row_height(abs(self.row_yl_list[1] - self.row_yl_list[0]))
            self.row_height = self.get_row_height()
            self.row_yh_list.append(self.row_yl_list[0] + self.row_height)
            self.row_yh_list.append(row_yl + self.row_height)
        elif len(self.row_yl_list) > 2:
            self.row_yh_list.append(row_yl + self.row_height)
    
    
    
    def make_row_list(self):
        rows = np.array([self.get_row_xl_list(), self.get_row_yl_list(), self.get_row_xh_list(), self.get_row_yh_list()])
        rows = rows.T
        self.rows = rows
    
    def get_row_list(self):
        return self.rows
    
    def get_xl(self):
        return np.min(self.get_row_list()[:,0])
    
    def get_yl(self):
        return np.min(self.get_row_list()[:,1])
    
    def get_xh(self):
        return np.max(self.get_row_list()[:,2])
    
    def get_yh(self):
        return np.max(self.get_row_list()[:,3])
    
    def get_row_info(self):
        return self.rowInfo
    
    def get_row_xl_list(self):
        return self.row_xl_list
    
    def get_row_yl_list(self):
        return self.row_yl_list
    
    def get_row_xh_list(self):
        return self.row_xh_list
    
    def get_row_yh_list(self):
        return self.row_yh_list
    
    def get_def_scale(self):
        return self.scale 
    
    def set_def_scale(self, scale):
        self.scale = scale 
        
    
        
