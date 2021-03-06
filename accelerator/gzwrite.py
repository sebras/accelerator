############################################################################
#                                                                          #
# Copyright (c) 2017 eBay Inc.                                             #
# Modifications copyright (c) 2018-2019 Carl Drougge                       #
#                                                                          #
# Licensed under the Apache License, Version 2.0 (the "License");          #
# you may not use this file except in compliance with the License.         #
# You may obtain a copy of the License at                                  #
#                                                                          #
#  http://www.apache.org/licenses/LICENSE-2.0                              #
#                                                                          #
# Unless required by applicable law or agreed to in writing, software      #
# distributed under the License is distributed on an "AS IS" BASIS,        #
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. #
# See the License for the specific language governing permissions and      #
# limitations under the License.                                           #
#                                                                          #
############################################################################

from __future__ import print_function
from __future__ import division

from accelerator import gzutil
from accelerator.compat import str_types, PY3

GzWrite = gzutil.GzWrite

_convfuncs = {
	'number'   : gzutil.GzWriteNumber,
	'float64'  : gzutil.GzWriteFloat64,
	'float32'  : gzutil.GzWriteFloat32,
	'int64'    : gzutil.GzWriteInt64,
	'int32'    : gzutil.GzWriteInt32,
	'bits64'   : gzutil.GzWriteBits64,
	'bits32'   : gzutil.GzWriteBits32,
	'bool'     : gzutil.GzWriteBool,
	'datetime' : gzutil.GzWriteDateTime,
	'date'     : gzutil.GzWriteDate,
	'time'     : gzutil.GzWriteTime,
	'bytes'    : gzutil.GzWriteBytes,
	'ascii'    : gzutil.GzWriteAscii,
	'unicode'  : gzutil.GzWriteUnicode,
	'parsed:number'   : gzutil.GzWriteParsedNumber,
	'parsed:float64'  : gzutil.GzWriteParsedFloat64,
	'parsed:float32'  : gzutil.GzWriteParsedFloat32,
	'parsed:int64'    : gzutil.GzWriteParsedInt64,
	'parsed:int32'    : gzutil.GzWriteParsedInt32,
	'parsed:bits64'   : gzutil.GzWriteParsedBits64,
	'parsed:bits32'   : gzutil.GzWriteParsedBits32,
}

def typed_writer(typename):
	if typename not in _convfuncs:
		raise ValueError("Unknown writer for type %s" % (typename,))
	return _convfuncs[typename]

def typed_reader(typename):
	from accelerator.sourcedata import type2iter
	if typename not in type2iter:
		raise ValueError("Unknown reader for type %s" % (typename,))
	return type2iter[typename]

from ujson import dumps, loads
class GzWriteJson(object):
	min = max = None
	def __init__(self, *a, **kw):
		assert 'default' not in kw, "default not supported for Json, sorry"
		if PY3:
			self.fh = gzutil.GzWriteUnicode(*a, **kw)
		else:
			self.fh = gzutil.GzWriteBytes(*a, **kw)
		self.count = 0
	def write(self, o):
		self.count += 1
		self.fh.write(dumps(o, ensure_ascii=False))
	def close(self):
		self.fh.close()
	def __enter__(self):
		return self
	def __exit__(self, type, value, traceback):
		self.close()
_convfuncs['json'] = GzWriteJson

class GzWriteParsedJson(GzWriteJson):
	"""This assumes strings are the object you wanted and parse them as json.
	If they are unparseable you get an error."""
	def write(self, o):
		if isinstance(o, str_types):
			o = loads(o)
		self.count += 1
		self.fh.write(dumps(o, ensure_ascii=False))
_convfuncs['parsed:json'] = GzWriteParsedJson
