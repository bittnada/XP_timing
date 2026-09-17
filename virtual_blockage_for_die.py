import time
from math import sqrt
import numpy as np
import torch
from shapely import affinity
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Polygon, box 
from shapely.ops import unary_union
import json
from shapely.geometry import Polygon, mapping
import sys 
import matplotlib.pyplot as plt 
import matplotlib.patches as patches
import virtual_blockage_for_ext_pins
import numpy as np

class VirtualDieRegion:

    def __init__(self, layout_vertices):
        self.layout_vertices = np.array(layout_vertices).flatten()
        
    def run(self):
        
        self.polygon4die, self.xList4die = self.die2polygon()
        self.xList4die = list(set(self.xList4die))
        self.xList4die.sort()    
        self.virtualBlockageRegionList = self.getVirtuaBlockagelRegion4Die()
        self.blockList4die = self.getBlock4Diepartition()
        

        if isinstance(self.polygon4die, Polygon) == False:
            print("DIE IS SPLIT INTO MULTIPLE POLYGONS!")
            exit()

        self.xList4die = list(set(self.xList4die))
        self.xList4die.sort()
        self.virtualBlockageRegionList = self.getVirtuaBlockagelRegion4Die()


        layout_vertices = self.layout_vertices.reshape(-1,2)
        rectDie = box(min(layout_vertices[:,0]), min(layout_vertices[:,1]), max(layout_vertices[:,0]), max(layout_vertices[:,1]))
        self.blockList4die = self.getRegionPartition(rectDie, self.polygon4die)
        self.blockList4die = list(set(self.blockList4die))

     





    def plot(self, name, extPos=None):
        layout_vertices = self.layout_vertices.reshape(-1,2)
        fig, ax = plt.subplots()
        die = patches.Rectangle((0, 0), np.max(layout_vertices[:,0]) - np.min(layout_vertices[:,0]), np.max(layout_vertices[:,1]) - np.min(layout_vertices[:,1]), facecolor="none", edgecolor="black")
        ax.add_patch(die)
        for blockage in self.virtualBlockageRegionList:
           block = patches.Rectangle((blockage[0], blockage[1]), (blockage[2]-blockage[0]),(blockage[3]-blockage[1]), facecolor="grey", edgecolor="black")
           ax.add_patch(block)

        for blockage in self.blockList4die:
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
                

        plt.xlim(-10, (np.max(layout_vertices) - np.min(layout_vertices))*1.1)
        plt.ylim(-10, (np.max(layout_vertices) - np.min(layout_vertices))*1.1)
        plt.savefig(name, dpi=300)


    def getVirtualBlockage4extPin(self, ext_pin_info_path, die_info_path):
         calc = virtual_blockage_for_ext_pins.virtualBlock4Pins(ext_pin_info_path, die_info_path)
         return calc.extPinPos, calc.x_cluster_list, calc.y_cluster_list


    def readJson(self, path):
        with open(path, "r") as f: data = json.load(f)
        return data


    def die2polygon(self):
        polygon4die, xList4die = list(), list()
        for i in range(int(len(self.layout_vertices)/2)):
            polygon4die.append((self.layout_vertices[i*2], self.layout_vertices[i*2+1]))
            xList4die.append(self.layout_vertices[i*2])
        polygon4die.append((self.layout_vertices[0], self.layout_vertices[1]))

        polygon4die = Polygon((polygon4die))
        xList4die = list(set(xList4die))
        xList4die.sort()
        return polygon4die, xList4die



    
    def getVirtuaBlockagelRegion4Die(self):
        layout_vertices = self.layout_vertices.reshape(-1,2)
        rectDie = box(np.min(layout_vertices[:,0]), np.min(layout_vertices[:,1]), np.max(layout_vertices[:,0]), np.max(layout_vertices[:,1]))
        virtualRegionList = list()
        difference_result = rectDie.difference(self.polygon4die)

        if not difference_result.is_empty:

            # Handle different types of geometries
            if isinstance(difference_result, (GeometryCollection, MultiPolygon)):
                virtualRegions = difference_result.geoms
            elif isinstance(difference_result, Polygon):
                virtualRegions = [difference_result]
            else:
                print("Unexpected geometry type")
                exit()

            for vr in virtualRegions:
                if isinstance(vr, Polygon) and len(vr.bounds) == 4:
                    xl_boundBox, yl_boundBox, xh_boundBox, yh_boundBox = vr.bounds
                    boundBox = box(xl_boundBox, yl_boundBox, xh_boundBox, yh_boundBox)
                    blockList = self.getRegionPartition(boundBox, vr)
                    virtualRegionList.extend(blockList)

                elif isinstance(vr, (GeometryCollection, MultiPolygon)):
                    virtualRegionList.extend([i.bounds for i in vr if isinstance(i, Polygon) and len(i.bounds) == 4])

                else:
                    print("ERROR@VirutalRegion")
                    exit()
        virtualRegionList = list(set(virtualRegionList))
        return virtualRegionList


    
    def getBlock4Diepartition(self):
        blockList = list()
        boundList = list()
        layout_vertices = self.layout_vertices.reshape(-1,2)
        for idx in range(len(self.xList4die)-1):
            bound = box(self.xList4die[idx], np.min(layout_vertices[:,1]), self.xList4die[idx+1], np.max(layout_vertices[:,1]))
            boundList.append(bound)
        for bound in boundList:
            overlap = self.polygon4die.intersection(bound)
            if isinstance(overlap, (GeometryCollection, MultiPolygon)):
                blockList.extend([j.bounds for j in overlap.geoms if isinstance(j, Polygon) and len(j.bounds) == 4])
            elif isinstance(overlap, Polygon):
                blockList.append(overlap.bounds)
            else:
                print("ERROR")
                exit()
        return blockList


    def getRegionPartition(self, boundBox:box, region:Polygon):
        boundList = list()
        blockList = list()
        yl4boundBox, yh4boundBox = boundBox.bounds[1], boundBox.bounds[3]
        xList4Region = [i[0] for i in mapping(region)["coordinates"][0]]
        xList4Region = list(set(xList4Region))
        xList4Region.sort()
        for idx in range(len(xList4Region)-1):
            bound = box(xList4Region[idx], yl4boundBox, xList4Region[idx+1], yh4boundBox)
            boundList.append(bound)
        bound = box(xList4Region[len(xList4Region)-2], yl4boundBox, xList4Region[len(xList4Region)-1], yh4boundBox)
        boundList.append(bound)
        for bound in boundList:
            overlap = region.intersection(bound)
            if isinstance(overlap, (GeometryCollection, MultiPolygon)):
                blockList.extend([j.bounds for j in overlap.geoms if isinstance(j, Polygon) and len(j.bounds) == 4])
            elif isinstance(overlap, Polygon):
                blockList.append(overlap.bounds)
            else:
                print("ERROR@RegionPartition")
                exit()
        return blockList



if __name__ == "__main__":

    params = sys.argv[1]
    vr = VirtualDieRegion(params)
    
