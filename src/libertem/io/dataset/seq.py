# Based on the SEQ reader of the PIMS project, with the following license:
#
#    Copyright (c) 2013-2014 PIMS contributors
#    https://github.com/soft-matter/pims
#    All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the soft-matter organization nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL <COPYRIGHT HOLDER> BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import os
import struct
from typing import Tuple

import numpy as np
from ncempy.io.mrc import mrcReader

from libertem.common import Shape
from libertem.web.messages import MessageConverter
from libertem.io.dataset.base import (
    FileSet, DataSet, BasePartition, DataSetMeta, DataSetException,
    LocalFile,
)
from libertem.corrections import CorrectionSet


DWORD = 'L'
LONG = 'l'
DOUBLE = 'd'
USHORT = 'H'


HEADER_FIELDS = [
    ('magic', DWORD),
    ('name', '24s'),
    ('version', LONG),
    ('header_size', LONG),
    ('description', '512s'),
    ('width', DWORD),
    ('height', DWORD),
    ('bit_depth', DWORD),
    ('bit_depth_real', DWORD),
    ('image_size_bytes', DWORD),
    ('image_format', DWORD),
    ('allocated_frames', DWORD),
    ('origin', DWORD),
    ('true_image_size', DWORD),
    ('suggested_frame_rate', DOUBLE),
    ('description_format', LONG),
    ('reference_frame', DWORD),
    ('fixed_size', DWORD),
    ('flags', DWORD),
    ('bayer_pattern', LONG),
    ('time_offset_us', LONG),
    ('extended_header_size', LONG),
    ('compression_format', DWORD),
    ('reference_time_s', LONG),
    ('reference_time_ms', USHORT),
    ('reference_time_us', USHORT)
    # More header values not implemented
]

HEADER_SIZE = sum([
    struct.Struct('<' + field[1]).size
    for field in HEADER_FIELDS
])


def _read_header(path, fields):
    with open(path, "rb") as fh:
        fh.seek(0)
        return _unpack_header(fh.read(HEADER_SIZE), fields)


def _unpack_header(header_bytes, fields):
    str_fields = {"name", "description"}
    tmp = dict()
    pos = 0
    for name, fmt in fields:
        header_part = header_bytes[pos:]
        val, size = _unpack_header_field(header_part, fmt)
        if name in str_fields:
            val = _decode_str(val)
        tmp[name] = val
        pos += size

    return tmp


def _unpack_header_field(header_part, fs):
    s = struct.Struct('<' + fs)
    vals = s.unpack(header_part[:s.size])
    if len(vals) == 1:
        return vals[0], s.size
    else:
        return vals, s.size


def _decode_str(str_in):
    end_pos = len(str_in)
    end_mark = b'\x00\x00'
    if end_mark in str_in:
        end_pos = str_in.index(end_mark) + 1
    return str_in[:end_pos].decode("utf16")


def _get_image_offset(header):
    if header['version'] >= 5:
        return 8192
    else:
        return 1024


class SEQDatasetParams(MessageConverter):
    SCHEMA = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "http://libertem.org/SEQDatasetParams.schema.json",
        "title": "SEQDatasetParams",
        "type": "object",
        "properties": {
            "type": {"const": "SEQ"},
            "path": {"type": "string"},
            "scan_size": {
                "type": "array",
                "items": {"type": "number", "minimum": 1},
                "minItems": 2,
                "maxItems": 2,
            },
        },
        "required": ["type", "path", "scan_size"],
    }

    def convert_to_python(self, raw_data):
        data = {
            "path": raw_data["path"],
        }
        if "scan_size" in raw_data:
            data["scan_size"] = tuple(raw_data["scan_size"])
        return data


class SEQFileSet(FileSet):
    pass


class SEQDataSet(DataSet):
    """
    Read data from Norpix SEQ files.

    Parameters
    ----------
    path
        Path to the .seq file

    scan_size
        A tuple that specifies the size of the scanned region/line/...
    """
    def __init__(self, path: str, scan_size: Tuple[int]):
        self._path = path
        self._scan_size = scan_size
        self._header = None
        self._meta = None
        self._footer_size = None
        self._filesize = None
        self._image_count = None
        self._dark = None
        self._gain = None

    def _do_initialize(self):
        header = self._header = _read_header(self._path, HEADER_FIELDS)
        self._image_offset = _get_image_offset(header)
        if header['version'] >= 5:  # StreamPix version 6
            # Timestamp = 4-byte unsigned long + 2-byte unsigned short (ms)
            #   + 2-byte unsigned short (us)
            self._timestamp_micro = True
        else:  # Older versions
            self._timestamp_micro = False
        try:
            dtype = np.dtype('uint%i' % header['bit_depth'])
        except TypeError:
            raise DataSetException("unsupported bit depth: %s" % header['bit_depth'])
        frame_size_bytes = header['width'] * header['height'] * dtype.itemsize
        self._footer_size = header['true_image_size'] - frame_size_bytes
        self._filesize = os.stat(self._path).st_size
        self._image_count = int(
            (self._filesize - self._image_offset) / header['true_image_size']
        )
        if self._image_count != np.prod(self._scan_size):
            raise DataSetException("scan_size doesn't match number of frames")
        shape = Shape(
            self._scan_size + (header['height'], header['width']),
            sig_dims=2,
        )
        self._meta = DataSetMeta(
            shape=shape,
            raw_dtype=dtype,
            dtype=dtype,
            metadata=header,
        )
        self._maybe_load_dark_gain()
        return self

    def _maybe_load_mrc(self, path):
        if not os.path.exists(path):
            return None
        data_dict = mrcReader(path)
        return np.squeeze(data_dict['data'])

    def _maybe_load_dark_gain(self):
        self._dark = self._maybe_load_mrc(self._path + ".dark.mrc")
        self._gain = self._maybe_load_mrc(self._path + ".gain.mrc")

    def get_correction_data(self):
        return CorrectionSet(
            dark=self._dark,
            gain=self._gain,
        )

    def initialize(self, executor):
        return executor.run_function(self._do_initialize)

    def get_diagnostics(self):
        header = self._header
        return [
            {"name": k, "value": str(v)}
            for k, v in header.items()
        ] + [
            {"name": "Footer size",
             "value": str(self._footer_size)},

            {"name": "Dark frame included",
             "value": str(self._dark is not None)},

            {"name": "Gain map included",
             "value": str(self._gain is not None)},
        ]

    @property
    def meta(self):
        return self._meta

    @classmethod
    def get_msg_converter(cls):
        return SEQDatasetParams

    @classmethod
    def detect_params(cls, path, executor):
        try:
            header = _read_header(path, HEADER_FIELDS)
            if header['magic'] != 0xFEED:
                return False
            image_offset = _get_image_offset(header)
            filesize = os.stat(path).st_size
            image_count = int(
                (filesize - image_offset) / header['true_image_size']
            )
            return {
                "parameters": {
                    "path": path,
                    "scan_size": str(image_count),
                }
            }
        except (OSError, UnicodeDecodeError):
            return False

    @classmethod
    def get_supported_extensions(cls):
        return set(["seq"])

    @property
    def dtype(self):
        return self._meta.raw_dtype

    @property
    def shape(self):
        return self._meta.shape

    def _get_fileset(self):
        return SEQFileSet(files=[
            LocalFile(
                path=self._path,
                start_idx=0,
                end_idx=self._image_count,
                native_dtype=self._meta.raw_dtype,
                sig_shape=self._meta.shape.sig,
                frame_footer=self._footer_size,
                frame_header=0,
                file_header=self._image_offset,
            )
        ], frame_footer_bytes=self._footer_size)

    def check_valid(self):
        if self._header['magic'] != 0xFEED:
            raise DataSetException('The format of this .seq file is unrecognized')
        if self._header['compression_format'] != 0:
            raise DataSetException('Only uncompressed images are supported in .seq files')
        if self._header['image_format'] != 100:
            raise DataSetException('Non-monochrome images are not supported')

    def get_cache_key(self):
        return {
            "path": self._path,
            "shape": self.shape,
        }

    def _get_num_partitions(self):
        """
        returns the number of partitions the dataset should be split into
        """
        res = max(self._cores, self._filesize // (512*1024*1024))
        return res

    def get_partitions(self):
        slices = BasePartition.make_slices(
            shape=self.shape,
            num_partitions=self._get_num_partitions(),
        )
        fileset = self._get_fileset()
        for part_slice, start, stop in slices:
            yield BasePartition(
                meta=self._meta,
                fileset=fileset.get_for_range(start, stop),
                partition_slice=part_slice,
                start_frame=start,
                num_frames=stop - start,
            )

    def __repr__(self):
        return "<SEQDataSet of %s shape=%s>" % (self.dtype, self.shape)
