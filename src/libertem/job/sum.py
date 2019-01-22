import numpy as np

from .base import Job, Task, ResultTile


class SumFramesJob(Job):
    def get_tasks(self):
        for partition in self.dataset.get_partitions():
            yield SumFramesTask(partition=partition)

    def get_result_shape(self):
        return self.dataset.shape.sig


class SumFramesTask(Task):
    def __call__(self):
        """
        sum frames over navigation axes
        """
        part = np.zeros(self.partition.meta.shape.sig, dtype="float32")
        for data_tile in self.partition.get_tiles():
            data = data_tile.data
            if data.dtype.kind in ('u', 'i'):
                data = data.astype("float32")
            # sum over all navigation axes; for 2d this would be (0, 1), for 1d (0,) etc.:
            axis = tuple(range(data_tile.tile_slice.shape.nav.dims))
            result = data.sum(axis=axis)
            part[data_tile.tile_slice.get(sig_only=True)] += result
        return [
            SumResultTile(
                data=part,
            )
        ]


class SumResultTile(ResultTile):
    def __init__(self, data):
        self.data = data

    @property
    def dtype(self):
        return self.data.dtype

    def reduce_into_result(self, result):
        result += self.data
        return result
