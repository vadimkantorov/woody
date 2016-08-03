import os
import re
import sys
import math
import json
import time
import shutil
import hashlib
import argparse
import traceback
import itertools
import subprocess
import xml.dom.minidom

class config:
	__tool_name__ = 'woody'
	__items__ = staticmethod(lambda: [(k, v) for k, v in vars(config).items() if '__' not in k])

	root = '.' + __tool_name__
	html_root = []
	html_root_alias = None
	notification_command_on_error = None
	notification_command_on_success = None
	strftime = '%d/%m/%Y %H:%M:%S'
	max_stdout_size = 2048
	sleep_between_queue_checks = 2.0

	path = []
	ld_library_path = []
	source = []
	env = {}

	queue = None
	mem_lo_gb = 10.0
	mem_hi_gb = 64.0
	parallel_jobs = 4
	batch_size = 1

class P:
	bugreport_page = 'http://github.com/vadimkantorov/%s/issues' % config.__tool_name__ 
	jobdir = staticmethod(lambda stage_name: os.path.join(P.job, stage_name))
	logdir = staticmethod(lambda stage_name: os.path.join(P.log, stage_name))
	sgejobdir = staticmethod(lambda stage_name: os.path.join(P.sgejob, stage_name))
	jobfile = staticmethod(lambda stage_name, job_idx: os.path.join(P.jobdir(stage_name), 'j%06d.sh' % job_idx))
	joblogfiles = staticmethod(lambda stage_name, job_idx: (os.path.join(P.logdir(stage_name), 'stdout_j%06d.txt' % job_idx), os.path.join(P.logdir(stage_name), 'stderr_j%06d.txt' % job_idx)))
	sgejobfile = staticmethod(lambda stage_name, sgejob_idx: os.path.join(P.sgejobdir(stage_name), 's%06d.sh' % sgejob_idx))
	sgejoblogfiles = staticmethod(lambda stage_name, sgejob_idx: (os.path.join(P.logdir(stage_name), 'stdout_s%06d.txt' % sgejob_idx), os.path.join(P.logdir(stage_name), 'stderr_s%06d.txt' % sgejob_idx)))
	explogfiles = staticmethod(lambda: (os.path.join(P.log, 'stdout_experiment.txt'), os.path.join(P.log, 'stderr_experiment.txt')))

	@staticmethod
	def read_or_empty(file_path):
		subprocess.check_call(['touch', file_path]) # workaround for NFS caching
		if os.path.exists(file_path):
			with open(file_path, 'r') as f:
				return f.read()
		return ''

	@staticmethod
	def init(exp_py, rcfile):
		P.exp_py = exp_py
		P.rcfile = os.path.abspath(rcfile)
		P.locally_generated_script = os.path.abspath(os.path.basename(exp_py) + '.generated.sh')
		P.experiment_name = os.path.basename(P.exp_py)
		P.experiment_name_code = P.experiment_name + '_' + hashlib.md5(os.path.abspath(P.exp_py)).hexdigest()[:3].upper()
		
		P.root = os.path.abspath(config.root)
		P.html_root = config.html_root or [os.path.join(P.root, 'html')]
		P.html_root_alias = config.html_root_alias
		P.html_report_file_name = P.experiment_name_code + '.html'
		P.html_report_url = os.path.join(P.html_root_alias or P.html_root[0], P.html_report_file_name)

		P.experiment_root = os.path.join(P.root, P.experiment_name_code)
		P.log = os.path.join(P.experiment_root, 'log')
		P.job = os.path.join(P.experiment_root, 'job')
		P.sgejob = os.path.join(P.experiment_root, 'sge')
		P.all_dirs = [P.root, P.experiment_root, P.log, P.job, P.sgejob] + P.html_root

class Q:
	@staticmethod
	def retry(f):
		def safe_f(*args, **kwargs):
			while True:
				try:
					return f(*args, **kwargs)
				except subprocess.CalledProcessError, err:
					print >> sys.stderr, '\nRetrying. Got CalledProcessError while calling %s:\nreturncode: %d\ncmd: %s\noutput: %s\n\n' % (f, err.returncode, err.cmd, err.output)
					time.sleep(config.sleep_between_queue_checks)
					continue
		return safe_f

	@staticmethod
	def get_jobs(job_name_prefix, state = '', stderr = None):
		return [int(elem.getElementsByTagName('JB_job_number')[0].firstChild.data) for elem in xml.dom.minidom.parseString(Q.retry(subprocess.check_output)(['qstat', '-xml'], stderr = stderr)).documentElement.getElementsByTagName('job_list') if elem.getElementsByTagName('JB_name')[0].firstChild.data.startswith(job_name_prefix) and elem.getElementsByTagName('state')[0].firstChild.data.startswith(state)]
	
	@staticmethod
	def submit_job(sgejob_file, sgejob_name, stderr = None):
		while True:
			try:
				return int(subprocess.check_output(['qsub', '-N', sgejob_name, '-terse', sgejob_file], stderr = stderr))
			except subprocess.CalledProcessError, err:
				jobs = Q.get_jobs(sgejob_name, stderr = stderr)
				if len(jobs) == 1:
					return jobs[0]

	@staticmethod
	def delete_jobs(jobs, stderr = None):
		if jobs:
			Q.retry(subprocess.check_call)(['qdel'] + map(str, jobs), stdout = stderr, stderr = stderr)

class Path:
	def __init__(self, path_parts, domakedirs = False, isoutput = False):
		path_parts = path_parts if isinstance(path_parts, tuple) else (path_parts, )
		assert all([part != None for part in path_parts])
	
		self.string = os.path.join(*path_parts)
		self.domakedirs = domakedirs
		self.isoutput = isoutput

	def join(self, *path_parts):
		assert all([part != None for part in path_parts])

		return Path(os.path.join(self.string, *map(str, path_parts)))

	def makedirs(self):
		return Path(self.string, domakedirs = True, isoutput = self.isoutput)

	def output(self):
		return Path(self.string, domakedirs = self.domakedirs, isoutput = True)

	def __str__(self):
		return self.string

class Experiment:
	def __init__(self, name, name_code):
		self.name = name
		self.name_code = name_code
		self.stages = []
	
	def stage(self, name, queue = None, parallel_jobs = None, batch_size = None, mem_lo_gb = None, mem_hi_gb = None, source = [], path = [], ld_library_path = [], env = {}):
		self.stages.append(Experiment.Stage(name, queue or config.queue, parallel_jobs or config.parallel_jobs, batch_size or config.batch_size, mem_lo_gb or config.mem_lo_gb, mem_hi_gb or config.mem_hi_gb, source or config.source, config.path + path, config.ld_library_path + ld_library_path, dict(config.env.items() + env.items())))
		return self.stages[-1]

	def run(self, executable, name = None, env = {}, cwd = Path(os.getcwd()), stage = None):
		effective_stage = self.stages[-1] if stage == None else ([s for s in self.stages if s.name == stage] or [self.stage(stage)])[0]
		name = '_'.join(map(str, name if isinstance(name, tuple) else (name,))) if name != None else str(len(effective_stage.jobs))
		effective_stage.jobs.append(Experiment.Job(name, executable, dict(effective_stage.env.items() + env.items()), cwd))
		return effective_stage.jobs[-1]
	
	def has_failed_stages(self):
		return any([stage.calculate_aggregate_status() == Experiment.ExecutionStatus.error for stage in self.stages])

	def bash(self, script_path, script_args = '', switches = ''):
		return Executable('bash', switches, script_path, script_args)

	def experiment_name(self):
		return self.name

	def path(self, *path_parts):
		return Path(path_parts)

	class ExecutionStatus:
		waiting = 'waiting'
		submitted = 'submitted'
		running = 'running'
		success = 'success'
		error = 'error'
		killed = 'killed'
		canceled = 'canceled'

	class Job:
		def __init__(self, name, executable, env, cwd):
			self.name = name
			self.executable = executable
			self.env = env
			self.cwd = cwd
			self.status = Experiment.ExecutionStatus.waiting

		def get_used_paths(self):
			return [v for k, v in sorted(self.env.items()) if isinstance(v, Path)] + [self.cwd] + self.executable.get_used_paths()

		def has_failed(self):
			return self.status == Experiment.ExecutionStatus.error or self.status == Experiment.ExecutionStatus.killed
	
	class Stage:
		def __init__(self, name, queue, parallel_jobs, batch_size, mem_lo_gb, mem_hi_gb, source, path, ld_library_path, env):
			self.name = name
			self.queue = queue
			self.parallel_jobs = parallel_jobs
			self.batch_size = batch_size
			self.mem_lo_gb = mem_lo_gb
			self.mem_hi_gb = mem_hi_gb
			self.source = source
			self.path = path
			self.ld_library_path = ld_library_path
			self.env = env
			self.jobs = []

		def calculate_aggregate_status(self):
			conditions = {
				(Experiment.ExecutionStatus.waiting, ) : (),
				(Experiment.ExecutionStatus.submitted, ) : (Experiment.ExecutionStatus.waiting, Experiment.ExecutionStatus.success),
				(Experiment.ExecutionStatus.running, ) : (Experiment.ExecutionStatus.waiting, Experiment.ExecutionStatus.submitted, Experiment.ExecutionStatus.success),
				(Experiment.ExecutionStatus.success, ) : (),
				(Experiment.ExecutionStatus.error, Experiment.ExecutionStatus.killed) : None,
				(Experiment.ExecutionStatus.canceled, ): (Experiment.ExecutionStatus.waiting, )
			}

			return [status[0] for status, extra_statuses in conditions.items() if any([job.status in status for job in self.jobs]) and (extra_statuses == None or all([job.status in status + extra_statuses for job in self.jobs]))][0]

		def job_batch_count(self):
			return int(math.ceil(float(len(self.jobs)) / self.batch_size))

		def calculate_job_range(self, batch_idx):
			return range(batch_idx * self.batch_size, min(len(self.jobs), (batch_idx + 1) * self.batch_size))

class Executable:
	def __init__(self, executor, switches, script_path, script_args):
		self.executor = executor
		self.switches = switches
		self.script_path = script_path
		self.script_args = script_args

	def get_used_paths(self):
		return [Path(str(self.script_path))]

	def generate_bash_script_lines(self):
		return ['%s %s "%s" %s' % (self.executor, self.switches, self.script_path, self.script_args)]

class Magic:
	prefix = '%' + config.__tool_name__
	class Action:
		stats = 'stats'
		environ = 'environ'
		results = 'results'
		status = 'status'

	def __init__(self, stderr):
		self.stderr = stderr
	
	def findall_and_load_arg(self, action):
		def safe_json_loads(s):
			try:
				return json.loads(s)
			except:
				print >> sys.stderr, 'Error parsing json. Action: %s. Stderr:\n%s' % (action, self.stderr)
				return {}
		return map(safe_json_loads, re.findall('%s %s (.+)$' % (Magic.prefix, action), self.stderr, re.MULTILINE))

	def stats(self):
		return dict(itertools.chain(*map(dict.items, self.findall_and_load_arg(Magic.Action.stats) or [{}])))

	def environ(self):
		return (self.findall_and_load_arg(Magic.Action.environ) or [{}])[0]

	def results(self):
		return self.findall_and_load_arg(Magic.Action.results)

	def status(self):
		return (self.findall_and_load_arg(Magic.Action.status) or [None])[-1]

	@staticmethod
	def echo(action, arg):
		return '%s %s %s' % (Magic.prefix, action, json.dumps(arg))
	
def html(e = None):
	HTML_PATTERN = '''
<!DOCTYPE html>

<html lang="en">
	<head>
		<title>%s</title>
		<meta charset="utf-8" />
		<meta http-equiv="cache-control" content="no-cache" />
		<meta name="viewport" content="width=device-width, initial-scale=1" />
		<link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.6/css/bootstrap.min.css" integrity="sha384-1q8mTJOASx8j1Au+a5WDVnPi2lkFfwwEAa8hDDdjZlpLegxhjVME1fgjWPGmkzs7" crossorigin="anonymous">
		<link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.6/css/bootstrap-theme.min.css" integrity="sha384-fLW2N01lMqjakBkx3l/M9EahuwpSfeNvV63J5ezn3uZzapT0u7EYsXMjQV+0En5r" crossorigin="anonymous">
		<script type="text/javascript" src="https://code.jquery.com/jquery-2.2.3.min.js"></script>
		<script src="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.6/js/bootstrap.min.js" integrity="sha384-0mSbJDEHialfmuBBQP6A4Qrprq5OVfW37PRR3j5ELqxss1yVqOtnepnHVP9aJ7xS" crossorigin="anonymous"></script>
		<script type="text/javascript" src="https://cdnjs.cloudflare.com/ajax/libs/jsviews/0.9.75/jsrender.min.js"></script>
		
		<style>
			.job-status-waiting {background-color: white}
			.job-status-submitted {background-color: gray}
			.job-status-running {background-color: lightgreen}
			.job-status-success {background-color: green}
			.job-status-error {background-color: red}
			.job-status-killed {background-color: orange}
			.job-status-canceled {background-color: salmon}

			.experiment-pane {overflow: auto}
			a {cursor: pointer;}

			.modal-dialog, .modal-content {height: 90%%;}
			.modal-body { height:calc(100%% - 100px); }
			.full-screen {height:calc(100%% - 120px); width: 100%%}
		</style>
	</head>
	<body>
		<script type="text/javascript">
			var report = %s;

			$(function() {
				$.views.helpers({
					sortedkeys : function(obj, exclude) {
						return $.grep(Object.keys(obj).sort(), function(x) {return $.inArray(x, exclude || []) == -1;})
					},
					format : function(name, value) {
						var return_name = arguments.length == 1;
						if(!return_name && value == undefined)
							return '';

						if(name.indexOf('seconds') >= 0)
						{
							name = name + ' (h:m:s)'
							if(return_name)
								return name;

							var seconds = Math.round(value);
							var hours = Math.floor(seconds / (60 * 60));
							var divisor_for_minutes = seconds %% (60 * 60);
							return hours + ":" + Math.floor(divisor_for_minutes / 60) + ":" + Math.ceil(divisor_for_minutes %% 60);
						}
						else if(name.indexOf('kbytes') >= 0)
						{
							name = name + ' (Gb)'
							if(return_name)
								return name;

							return (value / 1024 / 1024).toFixed(1);
						}
						return String(return_name ? name : value);
					}
				});

				$(window).on('hashchange', function() {
					var re = /\#(\/[^\/]+)?(\/.+)?/;
					var groups = re.exec(window.location.hash) || [];
					var stage_name = groups[1], job_name = groups[2];

					var stats_keys_reduced_experiment = ['name_code', 'time_started', 'time_finished'];
					var stats_keys_reduced_stage = ['time_wall_clock_avg_seconds'];
					var stats_keys_reduced_job = ['exit_code', 'time_wall_clock_seconds'];
					var environ_keys_reduced = ['USER', 'PWD', 'HOME', 'HOSTNAME', 'CUDA_VISIBLE_DEVICES', 'JOB_ID', 'PATH', 'LD_LIBRARY_PATH'];

					var render_details = function(obj, ctx) {
						$('#divDetails').html($('#tmplDetails').render(obj, ctx));
						$('pre.log-output').each(function() {$(this).scrollTop(this.scrollHeight);});
						$('#lnkJobName').html(ctx.header || '&nbsp;');
					};
			
					$('#divExp').html($('#tmplExp').render(report));
					$('#lnkExpName').html(report.name);
					for(var i = 0; i < report.stages.length; i++)
					{
						if('/' + report.stages[i].name == stage_name)
						{
							$('#divJobs').html($('#tmplJobs').render(report.stages[i]));
							for(var j = 0; j < report.stages[i].jobs.length; j++)
							{
								if('/' + report.stages[i].jobs[j].name == job_name)
								{
									render_details(report.stages[i].jobs[j], {
										header : stage_name + job_name,
										stats_keys_reduced : stats_keys_reduced_job, 
										environ_keys_reduced : environ_keys_reduced
									});
									return;
								}
							}

							render_details(report.stages[i], {
								header : stage_name, 
								stats_keys_reduced : stats_keys_reduced_stage, 
								environ_keys_reduced : environ_keys_reduced
							});
							return;
						}
					}
					$('#divJobs').html('');
					render_details(report, {
						stats_keys_reduced : stats_keys_reduced_experiment,
						environ_keys_reduced : environ_keys_reduced
					});
				}).trigger('hashchange');
			});

		</script>
		<div class="container">
			<div class="row">
				<div class="col-sm-12">
					<h1><a href="#" id="lnkExpName"></a></h1>
					<h1><a href="#" id="lnkJobName"></a></h1>
				</div>
			</div>
			<div class="row">
				<div class="col-sm-4 experiment-pane" id="divExp"></div>
				<script type="text/x-jsrender" id="tmplExp">
					<h3>stages</h3>
					<table class="table table-bordered">
						<thead>
							<th>name</th>
							<th>status</th>
						</thead>
						<tbody>
							{{for stages}}
							<tr>
								<td><a href="#/{{>name}}">{{>name}}</a></td>
								<td title="{{>status}}" class="job-status-{{>status}}"></td>
							</tr>
							{{/for}}
						</tbody>
					</table>
				</script>

				<div class="col-sm-4 experiment-pane" id="divJobs"></div>
				<script type="text/x-jsrender" id="tmplJobs">
					<h3>jobs</h3>
					<table class="table table-bordered">
						<thead>
							<th>name</th>
							<th>status</th>
						</thead>
						<tbody>
							{{for jobs}}
							<tr>
								<td><a href="#/{{>#parent.parent.data.name}}/{{>name}}">{{>name}}</a></td>
								<td title="{{>status}}" class="job-status-{{>status}}"></td>
							</tr>
							{{/for}}
						</tbody>
					</table>
				</script>

				<div class="col-sm-4 experiment-pane" id="divDetails"></div>
				<script type="text/x-jsrender" id="tmplDetails">
					<h3><a data-toggle="collapse" data-target=".extended-stats">stats &amp; config</a></h3>
					<table class="table table-striped">
						{{for ~stats_keys_reduced ~stats=stats tmpl="#tmplStats" /}}
						{{for ~sortedkeys(stats, ~stats_keys_reduced) ~stats=stats tmpl="#tmplStats" ~row_class="collapse extended-stats" /}}
					</table>

					{{if results}}
					{{for results}}
						{{include tmpl="#tmplModal" ~type=type ~path=path ~name="results: " + name ~value=value id="results-" + #index  /}}
					{{else}}
						<h3>results</h3>
						<pre>no results provided</pre>
					{{/for}}
					{{/if}}

					{{include tmpl="#tmplModal" ~type="text" ~path=stdout_path ~name="stdout" ~value=stdout ~id="stdout" ~preview_class="log-output" /}}
					
					{{include tmpl="#tmplModal" ~type="text" ~path=stderr_path  ~name="stderr" ~value=stderr ~id="stderr" ~preview_class="log-output" /}}

					{{if env}}
					<h3>user env</h3>
					<table class="table table-striped">
						{{for ~sortedkeys(env) ~env=env tmpl="#tmplEnv"}}
						{{else}}
						<tr><td>no variables were passed</td></tr>
						{{/for}}
					</table>
					{{/if}}
					
					{{if environ}}
					<h3><a data-toggle="collapse" data-target=".extended-environ">effective env</a></h3>
					<div class="collapse extended-environ">
						<table class="table table-striped">
							{{for ~environ_keys_reduced ~env=environ tmpl="#tmplEnv" /}}
							{{for ~sortedkeys(environ, ~environ_keys_reduced) ~env=environ tmpl="#tmplEnv" /}}
						</table>
					</div>
					{{/if}}

					{{if script}}
					{{include tmpl="#tmplModal" ~type="text" ~path=script_path ~name="script" ~value=script ~id="sciprt" ~preview_class="hidden" /}}
					{{/if}}
					{{if rcfile}}
					{{include tmpl="#tmplModal" ~type="text" ~path=rcfile_path ~name="rcfile" ~value=rcfile ~id="sciprt" ~preview_class="hidden" /}}
					{{/if}}
				</script>
				
				<script type="text/x-jsrender" id="tmplModal">
					<h3><a data-toggle="modal" data-target="#full-screen-{{:~id}}">{{>~name}}</a></h3>
					{{if ~type == 'text'}}
					<pre class="pre-scrollable {{:~preview_class}}">{{if ~value}}{{>~value}}{{else}}empty so far{{/if}}</pre>
					{{else ~type == 'iframe'}}
					<div class="embed-responsive embed-responsive-16by9 {{:~preview_class}}">
						<iframe src="{{:~path}}"></iframe>
					</div>
					{{/if}}

					<div id="full-screen-{{:~id}}" class="modal" tabindex="-1">
						<div class="modal-dialog modal-content">
							<div class="modal-header">
								<button type="button" class="close" data-dismiss="modal"><span>&times;</span></button>
								<h4 class="modal-title">{{>~name}}</h4>
							</div>
							<div class="modal-body">
								{{if ~path}}
								<h5>path</h5>
								<pre class="pre-scrollable">{{>~path}}</pre>
								<br />
								{{/if}}
								<h5>content</h5>
								{{if ~type == 'text'}}
								<pre class="full-screen">{{>~value}}</pre>
								{{else ~type == 'iframe'}}
								<iframe class="full-screen" src="{{:~path}}"></iframe>
								{{/if}}
							</div>
						</div>
					</div>
				</script>

				<script type="text/x-jsrender" id="tmplEnv">
					<tr class="{{>~row_class}}">
						<th>{{>#data}}</th>
						<td>{{if ~env[#data] != null}}{{>~env[#data]}}{{else}}N/A{{/if}}</td>
					</tr>
				</script>
				
				<script type="text/x-jsrender" id="tmplStats">
					<tr class="{{>~row_class}}">
						<th>{{>~format(#data)}}</th>
						<td>{{>~format(#data, ~stats[#data]) || "N/A"}}</td>
					</tr>
				</script>
			</div>
		</div>
		<nav class="navbar navbar-default navbar-fixed-bottom" role="navigation">
			<div class="container">
				<div class="row">
					<div class="col-sm-12">
						<h4>this is a <a href="https://github.com/vadimkantorov/%s">%s</a> dashboard generated at %s</h4>
					</div>
				</div>
			</div>
		</nav>
	</body>
</html>
'''

	if e == None:
		print 'You are in debug mode, the report will not be 100% complete and accurate.'
		e = init()
		for stage in e.stages:
			for job_idx, job in enumerate(stage.jobs):
				job.status = Magic(P.read_or_empty(P.joblogfiles(stage.name, job_idx)[1])).status() or job.status
		print '%-30s %s' % ('Report will be at:', P.html_report_url)

	sgejoblog_paths = lambda stage, k: [P.sgejoblogfiles(stage.name, sgejob_idx)[k] for sgejob_idx in range(stage.job_batch_count())]
	sgejoblog = lambda stage, k: '\n'.join(['#BATCH #%d (%s)\n%s\n\n' % (sgejob_idx, log_file_path, P.read_or_empty(log_file_path)) for sgejob_idx, log_file_path in enumerate(sgejoblog_paths(stage, k))])
	sgejobscript = lambda stage: '\n'.join(['#BATCH #%d (%s)\n%s\n\n' % (sgejob_idx, sgejob_path, P.read_or_empty(sgejob_path)) for sgejob_path in [P.sgejobfile(stage.name, sgejob_idx) for sgejob_idx in range(stage.job_batch_count())]])
	truncate_stdout = lambda stdout: stdout[:config.max_stdout_size / 2] + '\n\n[%d characters skipped]\n\n' % (len(stdout) - 2 * (config.max_stdout_size / 2)) + stdout[-(config.max_stdout_size / 2):] if stdout != None and len(stdout) > config.max_stdout_size else stdout

	merge_dicts = lambda dicts: reduce(lambda x, y: dict(x.items() + y.items()), dicts)
	
	exp_job_logs = {obj : (P.read_or_empty(log_paths[0]), Magic(P.read_or_empty(log_paths[1]))) for obj, log_paths in [(e, P.explogfiles())] + [(job, P.joblogfiles(stage.name, job_idx)) for stage in e.stages for job_idx, job in enumerate(stage.jobs)]}

	def put_extra_job_stats(report_job):
		if report_job['status'] == Experiment.ExecutionStatus.running and 'time_started_unix' in report_job['stats']:
			report_job['stats']['time_wall_clock_seconds'] = int(time.time()) - int(report_job['stats']['time_started_unix'])
		return report_job

	def put_extra_stage_stats(report_stage):
		wall_clock_seconds = filter(lambda x: x != None, [report_job['stats'].get('time_wall_clock_seconds') for report_job in report_stage['jobs'] if report_job['status'] != Experiment.ExecutionStatus.running])
		report_stage['stats']['time_wall_clock_avg_seconds'] = float(sum(wall_clock_seconds)) / len(wall_clock_seconds) if wall_clock_seconds else None
		return report_stage

	def process_results(results):
		processed_results = []
		for i, r in enumerate(results):
			if not isinstance(r, dict):
				r = {'type' : 'text', 'path' : r}
			if r.get('name') == None and r.get('path') != None:
				r['name'] = os.path.basename(r['path'])
			if r['type'] == 'text' and r.get('value') == None and r.get('path') != None:
				r['value'] = P.read_or_empty(r['path'])
			if r.get('name') == None:
				r['name'] = '#' + i
			processed_results = filter(lambda rr: rr['name'] != r['name'], processed_results) + [r]
		return sorted(processed_results, key = lambda item: item['name'])

	report = {
		'name' : e.name, 
		'stdout' : exp_job_logs[e][0], 
		'stdout_path' : P.explogfiles()[0],
		'stderr' : exp_job_logs[e][1].stderr, 
		'stderr_path' : P.explogfiles()[1],
		'script' : P.read_or_empty(P.exp_py), 
		'script_path' : os.path.abspath(P.exp_py),
		'rcfile' : P.read_or_empty(P.rcfile) if P.rcfile != None else None,
		'rcfile_path' : P.rcfile,
		'environ' : exp_job_logs[e][1].environ(),
		'env' : config.env,
		'stats' : merge_dicts([{
			'experiment_root' : P.experiment_root,
			'exp_py' : os.path.abspath(P.exp_py),
			'rcfile' : P.rcfile,
			'name_code' : e.name_code, 
			'html_root' : P.html_root, 
			'argv_joined' : ' '.join(['"%s"' % arg if ' ' in arg else arg for arg in sys.argv])}, 
			{'config.' + k : v for k, v in config.__items__()},
			exp_job_logs[e][1].stats()
		]),
		'stages' : [put_extra_stage_stats({
			'name' : stage.name, 
			'stdout' : sgejoblog(stage, 0), 
			'stdout_path' : '\n'.join(sgejoblog_paths(stage, 0)),
			'stderr' : sgejoblog(stage, 1), 
			'stderr_path' : '\n'.join(sgejoblog_paths(stage, 1)),
			'env' : stage.env,
			'script' : sgejobscript(stage),
			'status' : stage.calculate_aggregate_status(), 
			'stats' : {
				'mem_lo_gb' : stage.mem_lo_gb, 
				'mem_hi_gb' : stage.mem_hi_gb,
			},
			'jobs' : [put_extra_job_stats({
				'name' : job.name, 
				'stdout' : truncate_stdout(exp_job_logs[job][0]),
				'stdout_path' : P.joblogfiles(stage.name, job_idx)[0],
				'stderr' : exp_job_logs[job][1].stderr, 
				'stderr_path' : P.joblogfiles(stage.name, job_idx)[1],
				'script' : P.read_or_empty(P.jobfile(stage.name, job_idx)),
				'script_path' : P.jobfile(stage.name, job_idx),
				'status' : job.status, 
				'environ' : exp_job_logs[job][1].environ(),
				'env' : job.env,
				'results' : process_results(exp_job_logs[job][1].results()),
				'stats' : exp_job_logs[job][1].stats()
			}) for job_idx, job in enumerate(stage.jobs)] 
		}) for stage in e.stages]
	}

	report_json = json.dumps(report, default = str)
	for html_dir in P.html_root:
		with open(os.path.join(html_dir, P.html_report_file_name), 'w') as f:
			f.write(HTML_PATTERN % (e.name_code, report_json, config.__tool_name__, config.__tool_name__, time.strftime(config.strftime)))

def clean():
	if os.path.exists(P.experiment_root):
		shutil.rmtree(P.experiment_root)

def stop(stderr = None):
	print 'Stopping the experiment "%s"...' % P.experiment_name_code
	Q.delete_jobs(Q.get_jobs(P.experiment_name_code, stderr = stderr), stderr = stderr)
	while len(Q.get_jobs(P.experiment_name_code), stderr = stderr) > 0:
		print '%d jobs are still not deleted. Sleeping...' % len(Q.get_jobs(P.experiment_name_code, stderr = stderr))
		time.sleep(config.sleep_between_queue_checks)
	print 'Done.\n'
	
def init():
	globals_mod = globals().copy()
	e = Experiment(os.path.basename(P.exp_py), P.experiment_name_code)
	globals_mod.update({m : getattr(e, m) for m in dir(e)})
	exec open(P.exp_py, 'r').read() in globals_mod, globals_mod

	def makedirs_if_does_not_exist(d):
		if not os.path.exists(d):
			os.makedirs(d)
		
	for d in P.all_dirs:
		makedirs_if_does_not_exist(d)
	
	for stage in e.stages:
		makedirs_if_does_not_exist(P.logdir(stage.name))
		makedirs_if_does_not_exist(P.jobdir(stage.name))
		makedirs_if_does_not_exist(P.sgejobdir(stage.name))
	
	return e

def gen(force, locally):
	if not locally and len(Q.get_jobs(P.experiment_name_code)) > 0:
		if force == False:
			print 'Please stop existing jobs for this experiment first. Add --force to the previous command or type:'
			print ''
			print '%s stop "%s"' % (config.__tool_name__, P.exp_py)
			print ''
			sys.exit(1)
		else:
			stop()

	if not locally:
		clean()
	
	e = init()

	print '%-30s %s' % ('Generating the experiment to:', P.locally_generated_script if locally else P.experiment_root)
	for p in [p for stage in e.stages for job in stage.jobs for p in job.get_used_paths() if p.domakedirs == True and not os.path.exists(str(p))]:
		os.makedirs(str(p))
	
	generate_job_bash_script_lines = lambda stage, job, job_idx: ['# stage.name = "%s", job.name = "%s", job_idx = %d' % (stage.name, job.name, job_idx )] + map(lambda file_path: '''if [ ! -e "%s" ]; then echo 'File "%s" does not exist'; exit 1; fi''' % (file_path, file_path), job.get_used_paths()) + list(itertools.starmap('export {0}="{1}"'.format, sorted(job.env.items()))) + ['\n'.join(['source "%s"' % source for source in reversed(stage.source)]), 'export PATH="%s:$PATH"' % ':'.join(reversed(stage.path)) if stage.path else '', 'export LD_LIBRARY_PATH="%s:$LD_LIBRARY_PATH"' % ':'.join(reversed(stage.ld_library_path)) if stage.ld_library_path else '', 'cd "%s"' % job.cwd] + job.executable.generate_bash_script_lines()
	
	if locally:
		with open(P.locally_generated_script, 'w') as f:
			f.write('#! /bin/bash\n')
			f.write('#  this is a stand-alone script generated from "%s"\n\n' % P.exp_py)
			for stage in e.stages:
				for job_idx, job in enumerate(stage.jobs):
					f.write('\n'.join(['('] + map(lambda l: '\t' + l, generate_job_bash_script_lines(stage, job, job_idx)) + [')', '', '']))
		return

	for stage in e.stages:
		for job_idx, job in enumerate(stage.jobs):
			with open(P.jobfile(stage.name, job_idx), 'w') as f:
				f.write('\n'.join(['#! /bin/bash'] + generate_job_bash_script_lines(stage, job, job_idx)))

	qq = lambda s: s.replace('"', '\\"')
	for stage in e.stages:
		for sgejob_idx in range(stage.job_batch_count()):
			with open(P.sgejobfile(stage.name, sgejob_idx), 'w') as f:
				f.write('\n'.join([
					'#$ -S /bin/bash',
					'#$ -l mem_req=%.2fG' % stage.mem_lo_gb,
					'#$ -l h_vmem=%.2fG' % stage.mem_hi_gb,
					'#$ -o %s -e %s\n' % P.sgejoblogfiles(stage.name, sgejob_idx),
					'#$ -q %s' % stage.queue if stage.queue else '',
					''
				]))

				for job_idx in stage.calculate_job_range(sgejob_idx):
					job_stderr_path = P.joblogfiles(stage.name, job_idx)[1]
					f.write('\n'.join([
						'# stage.name = "%s", job.name = "%s", job_idx = %d' % (stage.name, stage.jobs[job_idx].name, job_idx),
						'echo "' + qq(Magic.echo(Magic.Action.status, Experiment.ExecutionStatus.running)) + '" > "%s"' % job_stderr_path,
						'echo "' + qq(Magic.echo(Magic.Action.stats, {'time_started' : "$(date +'%s')" % config.strftime})) + '" >> "%s"' % job_stderr_path,
						'echo "' + qq(Magic.echo(Magic.Action.stats, {'time_started_unix' : "$(date +'%s')"})) + '" >> "%s"' % job_stderr_path,
						'echo "' + qq(Magic.echo(Magic.Action.stats, {'hostname' : '$(hostname)'})) + '" >> "%s"' % job_stderr_path,
						'echo "' + qq(Magic.echo(Magic.Action.stats, {'qstat_job_id' : '$JOB_ID'})) + '" >> "%s"' % job_stderr_path,
						'echo "' + qq(Magic.echo(Magic.Action.stats, {'cuda_visible_devices' : '$CUDA_VISIBLE_DEVICES'})) + '" >> "%s"' % job_stderr_path,
						'''python -c "import json, os; print('%s %s ' + json.dumps(dict(os.environ)))" >> "%s"''' % (Magic.prefix, Magic.Action.environ, job_stderr_path),
						'''/usr/bin/time -f '%s %s {"exit_code" : %%x, "time_user_seconds" : %%U, "time_system_seconds" : %%S, "time_wall_clock_seconds" : %%e, "rss_max_kbytes" : %%M, "rss_avg_kbytes" : %%t, "page_faults_major" : %%F, "page_faults_minor" : %%R, "io_inputs" : %%I, "io_outputs" : %%O, "context_switches_voluntary" : %%w, "context_switches_involuntary" : %%c, "cpu_percentage" : "%%P", "signals_received" : %%k}' bash -e "%s" > "%s" 2>> "%s"''' % ((Magic.prefix.replace('%', '%%'), Magic.Action.stats, P.jobfile(stage.name, job_idx)) + P.joblogfiles(stage.name, job_idx)),
						'''([ "$?" == "0" ] && (echo "%s") || (echo "%s")) >> "%s"''' % (qq(Magic.echo(Magic.Action.status, Experiment.ExecutionStatus.success)), qq(Magic.echo(Magic.Action.status, Experiment.ExecutionStatus.error)), job_stderr_path),
						'echo "' + qq(Magic.echo(Magic.Action.stats, {'time_finished' : "$(date +'%s')" % config.strftime})) + '" >> "%s"' % job_stderr_path,
						'# end',
						''
					]))
	return e

def run(force, dry, verbose, notify):
	e = gen(force, False)

	print '%-30s %s' % ('Report will be at:', P.html_report_url)
	print ''

	html(e)

	if dry:
		print 'Dry run. Quitting.'
		return

	class Tee:
		def __init__(self, diskfile, dup):
			self.diskfile = diskfile
			self.dup = dup

		def write(self, message):
			self.diskfile.write(message)
			self.diskfile.flush()
			for stream in self.dup:
				stream.write(message)
				stream.flush()

		def flush(self):
			self.diskfile.flush()
			for stream in self.dup:
				stream.flush()
		
		def fp(self):
			return self.diskfile

		def nodup(self):
			return Tee(self.diskfile, [])

		def verbose(self):
			return Tee(self.diskfile, self.dup if verbose else [])

	sys.stderr = Tee(open(P.explogfiles()[1], 'w'), [sys.__stderr__])
	sys.stdout = Tee(open(P.explogfiles()[0], 'w'), [sys.__stdout__, sys.stderr.nodup()])

	sgejob2job = {}

	def put_status(stage, job, status):
		with open(P.joblogfiles(stage.name, stage.jobs.index(job))[1], 'a') as f:
			print >> f, Magic.echo(Magic.Action.status, status)
		job.status = status
		
	def update_status(stage, new_status = None):
		active_jobs = [job for sgejob in Q.get_jobs(e.name_code, stderr = sys.stderr.fp()) for job in sgejob2job[sgejob]]
		for job_idx, job in enumerate(stage.jobs):
			if new_status:
				put_status(stage, job, new_status)
			else:
				job.status = Magic(P.read_or_empty(P.joblogfiles(stage.name, job_idx)[1])).status() or job.status
				if job.status == Experiment.ExecutionStatus.running and job not in active_jobs:
					put_status(stage, job, Experiment.ExecutionStatus.killed)

	def wait_if_more_jobs_than(stage, num_jobs):
		prev_msg = None
		while len(Q.get_jobs(e.name_code, stderr = sys.stderr.fp())) > num_jobs:
			msg = 'Running %d jobs, waiting %d jobs.' % (len(Q.get_jobs(e.name_code, 'r', stderr = sys.stderr.fp())), len(Q.get_jobs(e.name_code, 'qw', stderr = sys.stderr.fp())))
			if msg != prev_msg:
				print >> sys.stderr.verbose(), msg
				prev_msg = msg
			time.sleep(config.sleep_between_queue_checks)
			update_status(stage)
			html(e)
		
		update_status(stage)
		html(e)

	print >> sys.stderr.nodup(), Magic.echo(Magic.Action.stats, {'time_started' : time.strftime(config.strftime)})
	print >> sys.stderr.nodup(), Magic.echo(Magic.Action.environ, dict(os.environ))

	for stage_idx, stage in enumerate(e.stages):
		unhandled_exception_hook.notification_hook = lambda formatted_exception_message : subprocess.call(config.notification_command_on_error.format(NAME_CODE = e.name_code, HTML_REPORT_URL = P.html_report_url, FAILED_STAGE = stage.name, FAILED_JOB = [job.name for job in stage.jobs if job.has_failed()][0], EXCEPTION_MESSAGE = formatted_exception_message), shell = True)

		time_started = time.time()
		sys.stdout.write('%-30s ' % ('%s (%d jobs)' % (stage.name, len(stage.jobs))))
		for sgejob_idx in range(stage.job_batch_count()):
			wait_if_more_jobs_than(stage, stage.parallel_jobs - 1)
			sgejob = Q.submit_job(P.sgejobfile(stage.name, sgejob_idx), '%s_%s_%d' % (e.name_code, stage.name, sgejob_idx), stderr = sys.stderr.fp())
			sgejob2job[sgejob] = [stage.jobs[job_idx] for job_idx in stage.calculate_job_range(sgejob_idx)]

			for job_idx in stage.calculate_job_range(sgejob_idx):
				stage.jobs[job_idx].status = Experiment.ExecutionStatus.submitted

		wait_if_more_jobs_than(stage, 0)
		elapsed = int(time.time() - time_started)
		elapsed = '%dh%dm' % (elapsed / 3600, math.ceil(float(elapsed % 3600) / 60))

		if e.has_failed_stages():
			for stage_to_cancel in e.stages[1 + stage_idx:]:
				update_status(stage_to_cancel, Experiment.ExecutionStatus.canceled)

			print '[error, elapsed %s]' % elapsed
			if notify and config.notification_command_on_error:
				sys.stdout.write('Executing custom notification_command_on_error. ')
				cmd = config.notification_command_on_error.format(NAME_CODE = e.name_code, HTML_REPORT_URL = P.html_report_url, FAILED_STAGE = stage.name, FAILED_JOB = [job.name for job in stage.jobs if job.has_failed()][0])
				print >> sys.stderr.verbose(), '\nCommand: %s' % cmd
				print 'Exit code: %d' % subprocess.call(cmd, shell = True, stdout = sys.stderr.fp(), stderr = sys.stderr.fp())
			print '\nStopping the experiment. Skipped stages: %s' % ', '.join([e.stages[si].name for si in range(stage_idx + 1, len(e.stages))])
			break
		else:
			print '[ok, elapsed %s]' % elapsed
	
	print >> sys.stderr.nodup(), Magic.echo(Magic.Action.stats, {'time_finished' : time.strftime(config.strftime)})

	if not e.has_failed_stages():
		if notify and config.notification_command_on_success:
			sys.stdout.write('Executing custom notification_command_on_success. ')
			cmd = config.notification_command_on_success.format(NAME_CODE = e.name_code, HTML_REPORT_URL = P.html_report_url)
			print >> sys.stderr.verbose(), '\nCommand: %s' % cmd
			print 'Exit code: %d' % subprocess.call(cmd, shell = True, stdout = sys.stderr.fp(), stderr = sys.stderr.fp())
		print '\nALL OK. KTHXBAI!'
	
	html(e)

def log(xpath, stdout = True, stderr = True):
	e = init()

	log_slice = slice(0 if stdout else 1, 2 if stderr else 1)
	log_paths = []

	if xpath == '/':
		log_paths += P.explogfiles()[log_slice]

	for stage in e.stages:
		if '/%s' % stage.name == xpath:
			for sgejob_idx in range(stage.job_batch_count()):
				log_paths += P.sgejoblogfiles(stage.name, sgejob_idx)[log_slice]

		for job_idx in range(len(stage.jobs)):
			if '/%s/%s' % (stage.name, job.name) == xpath:
				log_paths += P.joblogfiles(stage.name, job_idx)[log_slice]

	subprocess.call('cat "%s" | less' % '" "'.join(log_paths), shell = True)

def info(xpath):
	e = init()

	for stage in e.stages:
		for job_idx in range(len(stage.jobs)):
			if '/%s/%s' % (stage.name, job.name) == xpath:
				print 'JOB "/%s/%s"' % (stage.name, job.name)
				print '--'
				print 'ENV:'
				print '\n'.join(map('\t{0:10}: {1}'.format, sorted(job.env.items())))
				print 'SCRIPT:'
				print P.read_or_empty(P.jobfile(stage.name, job_idx))


def unhandled_exception_hook(exc_type, exc_value, exc_traceback):
	formatted_exception_message = '\n'.join([
		'Unhandled exception occured!',
		'',
		'If it is not a "No space left on device", please consider filing a bug report at %s' % P.bugreport_page,
		'Please paste the stack trace below into the issue.',
		'',
		'==STACK_TRACE_BEGIN==',
		'',
		''.join(traceback.format_exception(exc_type, exc_value, exc_traceback)),
		'===STACK_TRACE_END==='
	])

	print >> sys.__stderr__, formatted_exception_message
	
	if unhandled_exception_hook.notification_hook_on_error:
		unhandled_exception_hook.notification_hook_on_error(formatted_exception_message)

	sys.exit(1)

if __name__ == '__main__':
	unhandled_exception_hook.notification_hook_on_error = None 
	sys.excepthook = unhandled_exception_hook
	def add_config_fields(parser, config_fields):
		for k in config_fields:
			parser.add_argument('--' + k, type = type(getattr(config, k) or ''))

	common_parent = argparse.ArgumentParser(add_help = False)
	common_parent.add_argument('exp_py')

	gen_parent = argparse.ArgumentParser(add_help = False)
	add_config_fields(gen_parent, ['queue', 'mem_lo_gb', 'mem_hi_gb', 'parallel_jobs', 'batch_size'])
	gen_parent.add_argument('--source', action = 'append', default = [])
	gen_parent.add_argument('--path', action = 'append', default = [])
	gen_parent.add_argument('--ld_library_path', action = 'append', default = [])
	
	run_parent = argparse.ArgumentParser(add_help = False)
	add_config_fields(run_parent, ['notification_command_on_error', 'notification_command_on_success', 'strftime', 'max_stdout_size', 'sleep_between_queue_checks'])
	
	gen_run_parent = argparse.ArgumentParser(add_help = False)
	gen_run_parent.add_argument('-v', dest = 'env', action = 'append', default = [])
	gen_run_parent.add_argument('--force', action = 'store_true')

	parser = argparse.ArgumentParser(parents = [run_parent, gen_parent])
	parser.add_argument('--rcfile', default = os.path.expanduser('~/.%src' % config.__tool_name__))
	parser.add_argument('--root')
	parser.add_argument('--html_root', action = 'append', default = [])
	parser.add_argument('--html_root_alias')

	subparsers = parser.add_subparsers()
	subparsers.add_parser('stop', parents = [common_parent]).set_defaults(func = stop)
	subparsers.add_parser('clean', parents = [common_parent]).set_defaults(func = clean)
	subparsers.add_parser('html', parents = [common_parent]).set_defaults(func = html)

	cmd = subparsers.add_parser('log', parents = [common_parent])
	cmd.add_argument('--xpath', required = True)
	cmd.add_argument('--stdout', action = 'store_false', dest = 'stderr')
	cmd.add_argument('--stderr', action = 'store_false', dest = 'stdout')
	cmd.set_defaults(func = log)

	cmd = subparsers.add_parser('info', parents = [common_parent])
	cmd.add_argument('--xpath', required = True)
	cmd.set_defaults(func = info)
	
	cmd = subparsers.add_parser('gen', parents = [common_parent, gen_parent, gen_run_parent])
	cmd.set_defaults(func = gen)
	cmd.add_argument('--locally', action = 'store_true')
	
	cmd = subparsers.add_parser('run', parents = [common_parent, gen_parent, run_parent, gen_run_parent])
	cmd.set_defaults(func = run)
	cmd.add_argument('--dry', action = 'store_true')
	cmd.add_argument('--verbose', action = 'store_true')
	cmd.add_argument('--notify', action = 'store_true')
	
	args = vars(parser.parse_args())
	rcfile, cmd = args.pop('rcfile'), args.pop('func')

	if os.path.exists(rcfile):
		exec open(rcfile).read() in globals(), globals()

	args['env'] = dict([k_eq_v.split('=') for k_eq_v in args.pop('env', {})])
	for k, v in config.__items__():
		arg = args.pop(k)
		if arg != None:
			if isinstance(arg, list):
				setattr(config, k, getattr(config, k) + arg)
			elif isinstance(arg, dict):
				setattr(config, k, dict(getattr(config, k).items() + arg.items()))
			else:
				setattr(config, k, arg)

	P.init(args.pop('exp_py'), rcfile)
	try:
		cmd(**args)
	except KeyboardInterrupt:
		print 'Quitting (Ctrl+C pressed). To stop jobs:'
		print ''
		print '%s stop "%s"' % (config.__tool_name__, P.exp_py)
		print ''
