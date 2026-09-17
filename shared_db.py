import numpy as np
from multiprocessing import shared_memory
import json


class SharedDB:

    def __init__(self, name="XP_DB"):
        self.name = name
        self.arrays = {}
        self.meta = {}

    def add_array(self, name, arr):
        arr = np.asarray(arr)
        
        if arr.ndim == 0:
            arr = arr.reshape(1)

        self.arrays[name] = arr

    def create(self, folder):

        # 전체 메모리 크기 계산
        total_bytes = sum(arr.nbytes for arr in self.arrays.values())

        print("Shared memory size (GB):", total_bytes / 1e9)

        shm = shared_memory.SharedMemory(
            name=self.name,
            create=True,
            size=total_bytes
        )

        offset = 0

        for name, arr in self.arrays.items():

            shm_arr = np.ndarray(
                arr.shape,
                dtype=arr.dtype,
                buffer=shm.buf,
                offset=offset
            )

            shm_arr[...] = arr

            self.meta[name] = {
                "shape": arr.shape,
                "dtype": str(arr.dtype),
                "offset": offset
            }

            offset += arr.nbytes

        # meta 정보 저장
        with open(folder + "/xp_db_meta.json", "w") as f:
            json.dump(self.meta, f, indent=4)

        print("Shared memory created:", self.name)

        return shm
