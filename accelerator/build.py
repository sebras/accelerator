############################################################################
#                                                                          #
# Copyright (c) 2017 eBay Inc.                                             #
# Modifications copyright (c) 2019 Anders Berkeman                         #
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

import time
import sys
import os
import json
from operator import itemgetter
from collections import defaultdict
from base64 import b64encode
from importlib import import_module
from argparse import ArgumentParser, RawTextHelpFormatter

from accelerator.compat import unicode, str_types, PY3
from accelerator.compat import urlencode, urlopen, Request, URLError, HTTPError
from accelerator.compat import getarglist

from accelerator import setupfile
from accelerator.extras import json_encode, json_decode, DotDict, job_post,_ListTypePreserver
from accelerator.job import Job
from accelerator.status import print_status_stacks
from accelerator import unixhttp; unixhttp # for unixhttp:// URLs, as used to talk to the daemon


class DaemonError(Exception):
	pass

class UrdError(Exception):
	pass

class UrdPermissionError(UrdError):
	pass

class UrdConflictError(UrdError):
	pass

class JobError(Exception):
	def __init__(self, jobid, method, status):
		Exception.__init__(self, "Failed to build %s (%s)" % (jobid, method,))
		self.jobid = jobid
		self.method = method
		self.status = status

	def format_msg(self):
		res = ["%s (%s):" % (self.jobid, self.method,)]
		for component, msg in self.status.items():
			res.append("  %s:" % (component,))
			res.append("   %s" % (msg.replace("\n", "\n    "),))
		return "\n".join(res)


class Automata:
	"""
	Launch jobs, wait for completion.
	Don't use this directly, use the Urd object.
	"""

	method = '?' # fall-through case when we resume waiting for something

	def __init__(self, server_url, verbose=False, flags=None, subjob_cookie=None, infoprints=False, print_full_jobpath=False):
		self.url = server_url
		self.subjob_cookie = subjob_cookie
		self.history = []
		self.verbose = verbose
		self.monitor = None
		self.flags = flags or []
		self.job_method = None
		# Workspaces should be per Automata
		from accelerator.job import WORKDIRS
		WORKDIRS.update(self.list_workdirs())
		self.update_method_deps()
		self.clear_record()
		# Only do this when run from shell.
		if infoprints:
			from accelerator.workarounds import SignalWrapper
			siginfo = SignalWrapper(['SIGINFO', 'SIGUSR1'])
			self.siginfo_check = siginfo.check
		else:
			self.siginfo_check = lambda: False
		self.print_full_jobpath = print_full_jobpath

	def clear_record(self):
		self.record = defaultdict(JobList)
		self.jobs = self.record[None]

	def validate_response(self, response):
		# replace with homemade function,
		# this is run on bigdata response
		pass

	def _url_get(self, *path, **kw):
		nest = kw.pop('nest', 0)
		url = self.url + os.path.join('/', *path)
		req = urlopen(url, **kw)
		try:
			resp = req.read()
			if req.getcode() == 503:
				print('503:', resp, file=sys.stderr)
				if nest < 3:
					print('Retrying (%d/3).' % (nest + 1,), file=sys.stderr)
					time.sleep(nest / 5 + 0.1)
					return self._url_get(*path, nest=nest + 1, **kw)
				else:
					print('Giving up.', file=sys.stderr)
					raise DaemonError('Daemon says 503: %s' % (resp,))
		finally:
			req.close()
		if PY3:
			resp = resp.decode('utf-8')
		return resp

	def _url_json(self, *path, **kw):
		return json_decode(self._url_get(*path, **kw))

	def abort(self):
		return self._url_json('abort')

	def info(self):
		return self._url_json('workspace_info')

	def config(self):
		return self._url_json('config')

	def _submit(self, method, options, datasets, jobids, caption=None, wait=True, why_build=False, workdir=None):
		"""
		Submit job to server and conditionaly wait for completion.
		"""
		self.job_method = method
		if not why_build and 'why_build' in self.flags:
			why_build = 'on_build'
		if self.monitor and not why_build:
			self.monitor.submit(method)
		if not caption:
			caption = 'fsm_' + method
		params = {method: dict(options=options, datasets=datasets, jobids=jobids,)}
		data = setupfile.generate(caption, method, params, why_build=why_build)
		if self.subjob_cookie:
			data.subjob_cookie = self.subjob_cookie
			data.parent_pid = os.getpid()
		if workdir:
			data.workdir = workdir
		t0 = time.time()
		self.job_retur = self._server_submit(data)
		self.history.append((data, self.job_retur))
		#
		if wait and not self.job_retur.done:
			self.wait(t0)
		if self.monitor and not why_build:
			self.monitor.done()
		return self.jobid(method), self.job_retur

	def wait(self, t0=None, ignore_old_errors=False):
		idle, status_stacks, current, last_time = self._server_idle(0, ignore_errors=ignore_old_errors)
		if idle:
			return
		if t0 is None:
			if current:
				t0 = current[0]
			else:
				t0 = time.time()
		waited = int(round(time.time() - t0)) - 1
		if self.verbose == 'dots':
			print('[' + '.' * waited, end=' ')
		while not idle:
			if self.siginfo_check():
				print()
				print_status_stacks(status_stacks)
			waited += 1
			if waited % 60 == 0 and self.monitor:
				self.monitor.ping()
			if self.verbose:
				now = time.time()
				if current:
					current = (now - t0, current[1], now - current[2],)
				else:
					current = (now - t0, self.job_method, 0,)
				if self.verbose == 'dots':
					if waited % 60 == 0:
						sys.stdout.write('[%d]\n ' % (now - t0,))
					else:
						sys.stdout.write('.')
				elif self.verbose == 'log':
					if waited % 60 == 0:
						print('%d seconds, still waiting for %s (%d seconds)' % current)
				else:
					current_display = (
						fmttime(current[0], True),
						current[1],
						fmttime(current[2], True),
					)
					sys.stdout.write('\r\033[K           %s %s %s' % current_display)
			idle, status_stacks, current, last_time = self._server_idle(1)
		if self.verbose == 'dots':
			print('(%d)]' % (last_time,))
		else:
			print('\r\033[K              %s' % (fmttime(last_time),))

	def jobid(self, method):
		"""
		Return jobid of "method"
		"""
		if 'jobs' in self.job_retur:
			return self.job_retur.jobs[method].link

	def dump_history(self):
		return self.history

	def _server_idle(self, timeout=0, ignore_errors=False):
		"""ask server if it is idle, return (idle, status_stacks)"""
		path = ['status']
		if self.verbose:
			path.append('full')
		path.append('?subjob_cookie=%s&timeout=%d' % (self.subjob_cookie or '', timeout,))
		resp = self._url_json(*path)
		if 'last_error' in resp and not ignore_errors:
			print("\nFailed to build jobs:", file=sys.stderr)
			for jobid, method, status in resp.last_error:
				e = JobError(jobid, method, status)
				print(e.format_msg(), file=sys.stderr)
			raise e
		return resp.idle, resp.get('status_stacks'), resp.get('current'), resp.get('last_time')

	def _server_submit(self, json):
		# submit json to server
		postdata = urlencode({'json': setupfile.encode_setup(json)})
		res = self._url_json('submit', data=postdata)
		if 'error' in res:
			raise DaemonError('Submit failed: ' + res.error)
		if 'why_build' not in res:
			if not self.subjob_cookie:
				self._printlist(res.jobs)
			self.validate_response(res.jobs)
		return res

	def _printlist(self, returndict):
		# print (return list) in neat format
		for method, item in sorted(returndict.items(), key=lambda x: x[1].link):
			if item.make == True:
				make_msg = 'MAKE'
			else:
				make_msg = item.make or 'link'
			print('        -  %44s' % method.ljust(44), end=' ')
			print(' %s' % (make_msg,), end=' ')
			if self.print_full_jobpath:
				print(' %s' % Job(item.link).path, end=' ')
			else:
				print(' %s' % item.link, end=' ')
			if item.make != True and 'total_time' in item:
				print(' %s' % fmttime(item.total_time), end=' ')
			print()

	def method_info(self, method):
		return self._url_json('method_info', method)

	def methods_info(self):
		return self._url_json('methods')

	def update_methods(self):
		resp = self._url_get('update_methods')
		self.update_method_deps()
		return resp

	def update_method_deps(self):
		info = self.methods_info()
		self.dep_methods = {str(name): set(map(str, data.get('dep', ()))) for name, data in info.items()}

	def list_workdirs(self):
		return self._url_json('list_workdirs')

	def call_method(self, method, options={}, datasets={}, jobids={}, record_in=None, record_as=None, why_build=False, caption=None, workdir=None):
		jid, res = self._submit(method, options, datasets, jobids, caption, why_build=why_build, workdir=workdir)
		if why_build: # specified by caller
			return res.why_build
		if 'why_build' in res: # done by server anyway (because --flags why_build)
			print("Would have built from:")
			print("======================")
			print(setupfile.encode_setup(self.history[-1][0], as_str=True))
			print("Could have avoided build if:")
			print("============================")
			print(json_encode(res.why_build, as_str=True))
			print()
			from inspect import stack
			stk = stack()[2]
			print("Called from %s line %d" % (stk[1], stk[2],))
			exit()
		jid = Job(jid, record_as or method)
		self.record[record_in].append(jid)
		return jid


def fmttime(t, short=False):
	if short:
		units = ['h', 'm', 's']
		fmts = ['%.2f', '%.1f', '%.0f']
	else:
		units = ['hours', 'minutes', 'seconds']
		fmts = ['%.2f ', '%.1f ', '%.1f ']
	unit = units.pop()
	fmt = fmts.pop()
	while t > 60 * 3 and units:
		unit = units.pop()
		fmt = fmts.pop()
		t /= 60
	return fmt % (t,) + unit


class JobList(_ListTypePreserver):
	"""
	A list of Jobs with some convenience methods.
	.find(method) a new JobList with only jobs with that method in it.
	.get(method, default=None) latest Job with that method.
	[method] Same as .get but error if no job with that method is in the list.
	.as_tuples The same list but as (method, jid) tuples.
	.pretty a pretty-printed version (string).
	"""

	def __getitem__(self, item):
		if isinstance(item, slice):
			return self.__class__(list.__getitem__(self, item))
		elif isinstance(item, str_types):
			return self.find(item)[-1] # last matching or IndexError
		else:
			return list.__getitem__(self, item)

	@property
	def pretty(self):
		"""Formated for printing"""
		if not self: return 'JobList([])'
		template = '   [%%3d] %%%ds : %%s' % (max(len(i.method) for i in self),)
		return 'JobList(\n' + \
			'\n'.join(template % (i, j.method, j) for i, j in enumerate(self)) + \
			'\n)'

	@property
	def as_tuples(self):
		return [(e.method, e) for e in self]

	def find(self, method):
		"""Matching elements returned as new Joblist."""
		return self.__class__(e for e in self if e.method == method)

	def get(self, method, default=None):
		l = self.find(method)
		return l[-1] if l else default

	@property
	def profile(self):
		total = 0
		seen = set()
		per_method = defaultdict(int)
		for jid in self:
			if jid not in seen:
				seen.add(jid)
				t = jid.post.profile.total
				total += t
				per_method[jid.method] += t
		return total, per_method

	def print_profile(self, verbose=True):
		total, per_method = self.profile
		if verbose and per_method:
			print("Time per method:")
			tmpl = "   %%-%ds  %%s  (%%d%%%%)" % (max(len(method) for method in per_method),)
			total_time = sum(per_method.values())
			for method, t in sorted(per_method.items(), key=itemgetter(1), reverse=True):
				print(tmpl % (method, fmttime(t), round(100 * t / total_time) if total_time else 0.0))
		print("Total time", fmttime(total))

def profile_jobs(jobs):
	if isinstance(jobs, str):
		jobs = [jobs]
	total = 0
	seen = set()
	for j in jobs:
		if isinstance(j, tuple):
			j = j[1]
		if j not in seen:
			total += job_post(j).profile.total
			seen.add(j)
	return total


class UrdResponse(dict):
	def __new__(cls, d):
		assert cls is UrdResponse, "Always make these through UrdResponse"
		obj = dict.__new__(UrdResponse if d else EmptyUrdResponse)
		return obj

	def __init__(self, d):
		d = dict(d or ())
		d.setdefault('caption', '')
		d.setdefault('timestamp', '0')
		d.setdefault('joblist', JobList())
		d.setdefault('deps', {})
		dict.__init__(self, d)

	__setitem__ = dict.__setitem__
	__delattr__ = dict.__delitem__
	def __getattr__(self, name):
		if name.startswith('_') or name not in self:
			raise AttributeError(name)
		return self[name]

	@property
	def as_dep(self):
		return DotDict(timestamp=self.timestamp, joblist=self.joblist.as_tuples, caption=self.caption)

class EmptyUrdResponse(UrdResponse):
	# so you can do "if urd.latest('foo'):" and similar.
	# python2 version
	def __nonzero__(self):
		return False
	# python3 version
	def __bool__(self):
		return False

def _urd_typeify(d):
	if isinstance(d, str):
		d = json.loads(d)
		if not d or isinstance(d, unicode):
			return d
	res = DotDict()
	for k, v in d.items():
		if k == 'joblist':
			v = JobList(Job(e[1], e[0]) for e in v)
		elif isinstance(v, dict):
			v = _urd_typeify(v)
		res[k] = v
	return res

class Urd(object):
	def __init__(self, a, info, user, password, horizon=None):
		self._a = a
		if info.urd:
			assert '://' in str(info.urd), 'Bad urd URL: %s' % (info.urd,)
		self._url = info.urd or ''
		self._user = user
		self._current = None
		self.info = info
		self.flags = set(a.flags)
		self.horizon = horizon
		self.joblist = a.jobs
		self.workdir = None
		auth = '%s:%s' % (user, password,)
		if PY3:
			auth = b64encode(auth.encode('utf-8')).decode('ascii')
		else:
			auth = b64encode(auth)
		self._headers = {'Content-Type': 'application/json', 'Authorization': 'Basic ' + auth}
		self._auth_tested = False
		self._warnings = []

	def _path(self, path):
		if '/' not in path:
			path = '%s/%s' % (self._user, path,)
		return path

	def _call(self, url, data=None, fmt=_urd_typeify):
		assert self._url, "No urd configured for this daemon"
		url = url.replace(' ', '%20')
		if data is not None:
			req = Request(url, json_encode(data), self._headers)
		else:
			req = Request(url)
		tries_left = 3
		while True:
			try:
				r = urlopen(req)
				try:
					code = r.getcode()
					if code == 401:
						raise UrdPermissionError()
					if code == 409:
						raise UrdConflictError()
					d = r.read()
					if PY3:
						d = d.decode('utf-8')
					return fmt(d)
				finally:
					try:
						r.close()
					except Exception:
						pass
			except HTTPError as e:
				# It seems inconsistent if we get HTTPError or not for 4xx codes.
				if e.code == 401:
					raise UrdPermissionError()
				if e.code == 409:
					raise UrdConflictError()
				tries_left -= 1
				if not tries_left:
					raise UrdError('Error %d from urd' % (e.code,))
				print('Error %d from urd, %d tries left' % (e.code, tries_left,), file=sys.stderr)
			except ValueError as e:
				tries_left -= 1
				msg = 'Bad data from urd, %s: %s' % (type(e).__name__, e,)
				if not tries_left:
					raise UrdError(msg)
				print('%s, %d tries left' % (msg, tries_left,), file=sys.stderr)
			except URLError:
				print('Error contacting urd', file=sys.stderr)
				raise UrdError('Error contacting urd')
			time.sleep(4)

	def _get(self, path, *a):
		assert self._current, "Can't record dependency with nothing running"
		path = self._path(path)
		assert path not in self._deps, 'Duplicate ' + path
		url = '/'.join((self._url, path,) + a)
		res = UrdResponse(self._call(url))
		if res:
			self._deps[path] = res.as_dep
		self._latest_joblist = res.joblist
		return res

	def _latest_str(self):
		if self.horizon:
			return '<=' + self.horizon
		else:
			return 'latest'

	def get(self, path, timestamp):
		return self._get(path, timestamp)

	def latest(self, path):
		return self.get(path, self._latest_str())

	def first(self, path):
		return self.get(path, 'first')

	def peek(self, path, timestamp):
		path = self._path(path)
		url = '/'.join((self._url, path, timestamp,))
		return UrdResponse(self._call(url))

	def peek_latest(self, path):
		return self.peek(path, self._latest_str())

	def peek_first(self, path):
		return self.peek(path, 'first')

	def since(self, path, timestamp):
		path = self._path(path)
		url = '%s/%s/since/%s' % (self._url, path, timestamp,)
		return self._call(url, fmt=json.loads)

	def list(self):
		url = '/'.join((self._url, 'list'))
		return self._call(url, fmt=json.loads)

	def begin(self, path, timestamp=None, caption=None, update=False):
		assert not self._current, 'Tried to begin %s while running %s' % (path, self._current,)
		if not self._auth_tested:
			try:
				self._call('%s/test/%s' % (self._url, self._user,), True)
			except UrdPermissionError:
				raise Exception('Urd says permission denied, did you forget to set URD_AUTH?')
			self._auth_tested = True
		self._current = self._path(path)
		self._current_timestamp = timestamp
		self._current_caption = caption
		self._update = update
		self._deps = {}
		self._a.clear_record()
		self.joblist = self._a.jobs
		self._latest_joblist = None

	def abort(self):
		self._current = None

	def finish(self, path, timestamp=None, caption=None):
		path = self._path(path)
		assert self._current, 'Tried to finish %s with nothing running' % (path,)
		assert path == self._current, 'Tried to finish %s while running %s' % (path, self._current,)
		user, build = path.split('/')
		self._current = None
		caption = caption or self._current_caption or ''
		timestamp = timestamp or self._current_timestamp
		assert timestamp, 'No timestamp specified in begin or finish for %s' % (path,)
		data = DotDict(
			user=user,
			build=build,
			joblist=self.joblist.as_tuples,
			deps=self._deps,
			caption=caption,
			timestamp=timestamp,
		)
		if self._update:
			data.flags = ['update']
		url = self._url + '/add'
		return self._call(url, data)

	def truncate(self, path, timestamp):
		url = '%s/truncate/%s/%s' % (self._url, self._path(path), timestamp,)
		return self._call(url, '')

	def set_workdir(self, workdir):
		"""Build jobs in this workdir, None to restore default"""
		self.workdir = workdir

	def build(self, method, options={}, datasets={}, jobids={}, name=None, caption=None, why_build=False, workdir=None):
		return self._a.call_method(method, options=options, datasets=datasets, jobids=jobids, record_as=name, caption=caption, why_build=why_build, workdir=workdir or self.workdir)

	def build_chained(self, method, options={}, datasets={}, jobids={}, name=None, caption=None, why_build=False, workdir=None):
		datasets = dict(datasets or {})
		assert 'previous' not in datasets, "Don't specify previous dataset to build_chained"
		assert name, "build_chained must have 'name'"
		assert self._latest_joblist is not None, "Can't build_chained without a dependency to chain from"
		datasets['previous'] = self._latest_joblist.get(name)
		return self.build(method, options, datasets, jobids, name, caption, why_build, workdir)

	def warn(self, line=''):
		"""Add a warning message to be displayed at the end of the build"""
		self._warnings.extend(l.rstrip() for l in line.expandtabs().split('\n'))

	def _show_warnings(self):
		if self._warnings:
			from itertools import chain
			from accelerator.compat import terminal_size
			max_width = max(34, terminal_size().columns - 6)
			def reflow(line):
				indent = ''
				for c in line:
					if c.isspace():
						indent += c
					else:
						break
				width = max(max_width - len(indent), 25)
				current = ''
				between = ''
				for word in line[len(indent):].split(' '):
					if len(current + word) >= width:
						if len(word) > width / 2 and word.startswith('"/') and (word.endswith('"') or word.endswith('",')):
							for pe in word.split('/'):
								if len(current + pe) >= width:
									if current:
										yield indent + current
										current = ('/' if between == '/' else '') + pe
									else:
										yield indent + between + pe
								else:
									current += between + pe
								between = '/'
						else:
							if current:
								yield indent + current
								current = word
							else:
								yield indent + word
					else:
						current += between + word
					between = ' '
				if current:
					yield indent + current
			warnings = list(chain.from_iterable(reflow(w) for w in self._warnings))
			print()
			width = max(len(line) for line in warnings)
			print('\x1b[35m' + ('#' * (width + 6)) + '\x1b[m')
			for line in warnings:
				print('\x1b[35m##\x1b[m', line.ljust(width), '\x1b[35m##\x1b[m')
			print('\x1b[35m' + ('#' * (width + 6)) + '\x1b[m')
			self._warnings = []


def find_automata(a, package, script):
	all_packages = sorted(a.config()['method_directories'])
	if package:
		if package in all_packages:
			package = [package]
		else:
			for cand in all_packages:
				if cand.endswith('.' + package):
					package = [cand]
					break
			else:
				raise Exception('No method directory found for %r in %r' % (package, all_packages))
	else:
		package = all_packages
	if not script.startswith('build'):
		script = 'build_' + script
	for p in package:
		module_name = p + '.' + script
		try:
			module_ref = import_module(module_name)
			print(module_name)
			return module_ref
		except ImportError as e:
			if PY3:
				if not e.msg[:-1].endswith(script):
					raise
			else:
				if not e.message.endswith(script):
					raise
	raise Exception('No build script "%s" found in {%s}' % (script, ', '.join(package)))

def run_automata(options, cfg):
	a = Automata(cfg.url, verbose=options.verbose, flags=options.flags.split(','), infoprints=True, print_full_jobpath=options.fullpath)

	if options.abort:
		a.abort()
		return

	try:
		a.wait(ignore_old_errors=not options.just_wait)
	except JobError:
		# An error occured in a job we didn't start, which is not our problem.
		pass

	if options.just_wait:
		return

	module_ref = find_automata(a, options.package, options.script)

	assert getarglist(module_ref.main) == ['urd'], "Only urd-enabled automatas are supported"
	if 'URD_AUTH' in os.environ:
		user, password = os.environ['URD_AUTH'].split(':', 1)
	else:
		user, password = os.environ['USER'], ''
	info = a.info()
	urd = Urd(a, info, user, password, options.horizon)
	if options.quick:
		a.update_method_deps()
	else:
		a.update_methods()
	module_ref.main(urd)
	urd._show_warnings()


def main(argv, cfg):
	parser = ArgumentParser(
		prog=argv.pop(0),
		usage="%(prog)s [options] [script]",
		formatter_class=RawTextHelpFormatter,
	)
	parser.add_argument('-f', '--flags',    default='',          help="comma separated list of flags", )
	parser.add_argument('-A', '--abort',    action='store_true', help="abort (fail) currently running job(s)", )
	parser.add_argument('-q', '--quick',    action='store_true', help="skip method updates and checking workdirs for new jobs", )
	parser.add_argument('-w', '--just_wait',action='store_true', help="just wait for running job, don't run any build script", )
	parser.add_argument('-F', '--fullpath', action='store_true', help="print full path to jobdirs")
	parser.add_argument('--verbose',        default='status',    help="verbosity style {no, status, dots, log}")
	parser.add_argument('--quiet',          action='store_true', help="same as --verbose=no")
	parser.add_argument('--horizon',        default=None,        help="time horizon - dates after this are not visible in\nurd.latest")
	parser.add_argument('script',           default='build'   ,  help="build script to run. default \"build\".\nsearches under all method directories in alphabetical\norder if it does not contain a dot.\nprefixes build_ to last element unless specified.\npackage name suffixes are ok.\nso for example \"test_methods.tests\" expands to\n\"accelerator.test_methods.build_tests\".", nargs='?')

	options = parser.parse_args(argv)

	if '.' in options.script:
		options.package, options.script = options.script.rsplit('.', 1)
	else:
		options.package = None

	options.verbose = {'no': False, 'status': True, 'dots': 'dots', 'log': 'log'}[options.verbose]
	if options.quiet: options.verbose = False

	try:
		run_automata(options, cfg)
		return 0
	except (JobError, DaemonError):
		# If it's a JobError we don't care about the local traceback,
		# we want to see the job traceback, and maybe know what line
		# we built the job on.
		# If it's a DaemonError we just want the line and message.
		print_minimal_traceback()
	return 1


def print_minimal_traceback():
	build_fn = __file__
	if build_fn[-4:] in ('.pyc', '.pyo',):
		# stupid python2
		build_fn = build_fn[:-1]
	blacklist_fns = {build_fn}
	last_interesting = None
	_, e, tb = sys.exc_info()
	while tb is not None:
		code = tb.tb_frame.f_code
		if code.co_filename not in blacklist_fns:
			last_interesting = tb
		tb = tb.tb_next
	lineno = last_interesting.tb_lineno
	filename = last_interesting.tb_frame.f_code.co_filename
	if isinstance(e, JobError):
		print("Failed to build job %s on %s line %d" % (e.jobid, filename, lineno,))
	else:
		print("Server returned error on %s line %d:\n%s" % (filename, lineno, e.args[0]))
