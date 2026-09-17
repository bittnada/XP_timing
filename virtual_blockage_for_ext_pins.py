import time
from math import sqrt
import numpy as np
import torch
from shapely import affinity
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Polygon, box
from shapely.ops import unary_union
import torch
import json
import sys
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from sklearn.cluster import KMeans
class virtualBlock4Pins:

    def __init__(self, ext_pin_path, die_info_path):

        self.ext_pin_path = ext_pin_path
        self.die_info_path = die_info_path
        if type(self.die_info_path) == str:
            self.dieData = self.readJson(self.die_info_path)
        else:
            self.dieData = self.die_info_path

        self.extPinPos = np.array([])
        if type(self.ext_pin_path) == str:
            self.extPinData = self.readJson(self.ext_pin_path)
        else:
            self.extPinData = self.ext_pin_path
        self.x_cluster_list, self.y_cluster_list = list(), list()
        self.getExtPinCluster(self.extPinData)


        # self.pinCluster_y = self.makePinGroup("y", self.y_cluster_list) # pins are aligned along x-axis
        #self.blockageList = self.makeBlockage("y", self.pinCluster_y, 1)
        #self.blockageList = self.makeBlockage()
        #self.blockageList.extend([i for i in self.makeBlockage("x", self.pinCluster_y, 1) if i != []] )
        #print("BLOCKAGE LIST 4 EXT PIN")
        #print(self.blockageList)
        #self.plot()

    def makeBlockage(self):
        blockageList = list()
        for x_ in self.x_cluster_list:
            mask = self.extPinPos[:,0] == x_
            extPinGroup = self.extPinPos[mask]
            yList4group = extPinGroup[:,1]
            ymin, ymax = np.min(yList4group) - self.dieData["row_height"] * 1000 , np.max(yList4group)+ self.dieData["row_height"] * 1000
            blockage_yl = max(self.dieData["layout_yl"], ymin)
            blockage_yh = min(self.dieData["layout_yh"], ymax)
            blockage_xl = max(self.dieData["layout_xl"], x_ - self.dieData["site_width"] * 1000)
            blockage_xh = min(self.dieData["layout_xh"], x_ + self.dieData["site_width"] * 1000)
            blockage = [blockage_xl, blockage_yl, blockage_xh, blockage_yh]
            print(blockage)
            blockageList.append(blockage)

        for y_ in self.y_cluster_list:
            mask = self.extPinPos[:,1] == y_
            extPinGroup = self.extPinPos[mask]
            xList4group = extPinGroup[:,0]
            xmin, xmax = np.min(xList4group) - self.dieData["site_width"] * 1000 , np.max(xList4group)+ self.dieData["site_width"] * 1000
            blockage_xl = max(self.dieData["layout_xl"], xmin)
            blockage_xh = min(self.dieData["layout_xh"], xmax)
            blockage_yl = max(self.dieData["layout_yl"], y_ - self.dieData["row_height"] * 1000)
            blockage_yh = min(self.dieData["layout_yh"], y_ + self.dieData["row_height"] * 1000)
            blockage = [blockage_xl, blockage_yl, blockage_xh, blockage_yh]
            blockageList.append(blockage)
        return blockageList

    def kMeansCluster(self, ext_pin_array, number_clusters):
        kmeans = KMeans(init="k-means++", n_clusters = number_clusters, n_init=10)
        kmeans.fit(ext_pin_array)
        cluster_labels = kmeans.labels_
        return cluster_labels


    def readJson(self, path):
        with open(path) as f: data = json.load(f)
        return data


    def getExtPinCluster(self, extPinData):
        xList, yList = list(), list()
        for pin in extPinData.keys():
            if "layer0" not in extPinData[pin].keys():
                continue
            if "position" not in extPinData[pin]["layer0"].keys():
                continue
            if "use" not in extPinData[pin].keys():
                continue
            if extPinData[pin]["use"] != "SIGNAL":
                continue

            pos = extPinData[pin]["layer0"]["position"]
            xList.append(float(pos[0]))
            yList.append(float(pos[1]))
        extPinPos = list()
        for i in range(len(xList)):
            x, y = xList[i], yList[i]
            extPinPos.append([x,y])
        self.extPinPos = np.array(extPinPos)
        set_xList = list(set(xList))
        set_yList = list(set(yList))
        xList = np.array(xList)
        yList = np.array(yList)

        for x in set_xList:
            count = np.count_nonzero(xList==x)
            if count > 5:
                self.x_cluster_list.append(x)

        for y in set_yList:
            count = np.count_nonzero(yList==y)
            if count > 5:
                self.y_cluster_list.append(y)


    def plot(self):
        fig, ax = plt.subplots()
        die = patches.Rectangle((0, 0), self.dieData["layout_xh"] - self.dieData["layout_xl"], self.dieData["layout_yh"]-self.dieData["layout_yl"], facecolor="none", edgecolor="black")
        ax.add_patch(die)
        for pin in self.extPinPos:
            x, y = pin[0], pin[1]
            plt.plot(x,y,'ro')

        for blockage in self.blockageList:
            block = patches.Rectangle((blockage[0], blockage[1]), (blockage[2]-blockage[0]),(blockage[3]-blockage[1]) )
            ax.add_patch(block)

        plt.xlim(-10, (self.dieData["xh"] - self.dieData["xl"])*1.1)
        plt.ylim(-10, (self.dieData["yh"] - self.dieData["yl"])*1.1)
        plt.savefig("extPininDie.png")

        plt.show()


if __name__ == "__main__":
    ext_pin_path = sys.argv[1]
    die_info_path = sys.argv[2]
    diePartition = virtualBlock4Pins(ext_pin_path, die_info_path)
