############################################################################
#                                                                          #
# Copyright (c) 2019 Carl Drougge                                          #
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
from __future__ import unicode_literals

description = r'''
Verify that various separators and both line endings work in csvimport,
with and without quoting.
'''

import subjobs
from dispatch import JobError
from extras import resolve_jobid_filename
from dataset import Dataset

# different types so verify_failure can tell them apart
class CSVImportException(Exception):
	pass

class WrongDataException(Exception):
	pass

class WrongLabelsException(Exception):
	pass

def verify_failure(params, sep, data, testing_what, want_exc, **kw):
	try:
		check_one(params, "\n", sep, data, prefix="failing", **kw)
	except want_exc:
		# The right exception, hooray! (Other exceptions will fail the method.)
		return
	raise Exception("Self test failure, check didn't fail for " + testing_what)

def check_one(params, line_sep, sep, data, want_res=None, prefix="", quotes=False, leave_bad=False):
	sep_c = chr(sep)
	# Can't have separator character in unquoted values
	if not quotes and not leave_bad:
		data = [[el.replace(sep_c, "") for el in line] for line in data]
	if not want_res:
		want_res = [tuple(s.encode("ascii") for s in line) for line in data[1:]]
	filename = "%s_csv.%d.%r.txt" % (prefix, sep, line_sep)
	with open(filename, "w") as fh:
		for line in data:
			if quotes:
				line = [quotes + el.replace(quotes, quotes + quotes) + quotes for el in line]
			fh.write(sep_c.join(line))
			fh.write(line_sep)
	try:
		jid = subjobs.build("csvimport", options=dict(
			filename=resolve_jobid_filename(params.jobid, filename),
			separator=sep_c,
			quote_support=bool(quotes),
		))
	except JobError as e:
		raise CSVImportException("Failed to csvimport for separator %d with line separator %r, csvimport error was:\n%s" % (sep, line_sep, e.format_msg()))
	ds = Dataset(jid)
	labels = sorted(ds.columns)
	if labels != data[0]:
		raise WrongLabelsException("csvimport gave wrong labels for separator %d with line separator %r: %r (expected %r)" % (sep, line_sep, labels, data[0],))
	res = list(ds.iterate(None, data[0]))
	if res != want_res:
		raise WrongDataException("csvimport gave wrong data for separator %d with line separator %r: %r (expected %r)" % (sep, line_sep, res, want_res,))

def synthesis(params):
	# Any ascii character except \r or \n is a valid separator, but let's
	# try only a few popular or likely problem-characters to save time.
	separators = (
#		0,  # NUL is often problematic (it doesn't work here for instance)
		1,  # popular in hadoop
		8,  # backspace is an odd choice
		9,  # tab
		30, # record separator
		32, # space
		34, # double quote
		39, # single quote
		44, # comma
		46, # period
		92, # backslash
		127,# delete
	)
	# Don't use more than two lines after labels - order might change then.
	data = [
		["a", "b", "c", "d"], # labels
		["a b", "", "c,d", ""],
		['a"b"', "'cd", "e\tf", ""],
	]

	# Sanity check, make sure the various checks actually work.
	# Make sure to use separate separators in all of them, to avoid
	# cross-contamination.
	verify_failure(params, 1, data + [["short", "line"]], "short line", CSVImportException)
	verify_failure(params, 2, data, "wrong data", WrongDataException, want_res=["wrong"])
	verify_failure(params, 44, [["a", "b,c"]], "wrong labels", WrongLabelsException, leave_bad=True)

	# check that all the combinations we expect to work do in fact work
	for line_sep in ("\n", "\r\n"):
		for sep in separators:
			check_one(params, line_sep, sep, data, prefix="unquoted", quotes=False)
			if sep != 34:
				check_one(params, line_sep, sep, data, prefix="doublequoted", quotes='"')
			if sep != 39:
				check_one(params, line_sep, sep, data, prefix="singlequoted", quotes="'")