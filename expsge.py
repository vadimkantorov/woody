#TODO: fix sgejob_idx to allow complex job <-> sgejob mapping

import os
import re
import sys
import time
import json
import shutil
import hashlib
import argparse
import itertools
import subprocess
import xml.dom.minidom

class config:
	maximum_simultaneously_submitted_jobs = 4
	sleep_between_queue_checks = 2
	mem_lo_gb = 10
	mem_hi_gb = 64
	max_stdout_characters = 1024

class P:
	html_report = os.getenv('EXPSGE_HTML_REPORT')
	root = os.getenv('EXPSGE_ROOT')

	jobdir = staticmethod(lambda stage_name: os.path.join(P.job, stage_name))
	logdir = staticmethod(lambda stage_name: os.path.join(P.log, stage_name))
	sgejobdir = staticmethod(lambda stage_name: os.path.join(P.sgejob, stage_name))
	jobfile = staticmethod(lambda stage_name, job_idx: os.path.join(P.jobdir(stage_name), 'j%06d.sh' % job_idx))
	joblogfiles = staticmethod(lambda stage_name, job_idx: (os.path.join(P.logdir(stage_name), 'stdout_j%06d.txt' % job_idx), os.path.join(P.logdir(stage_name), 'stderr_j%06d.txt' % job_idx)))
	sgejobfile = staticmethod(lambda stage_name, sgejob_idx: os.path.join(P.sgejobdir(stage_name), 's%06d.sh' % sgejob_idx))
	sgejoblogfiles = staticmethod(lambda stage_name, sgejob_idx: (os.path.join(P.logdir(stage_name), 'stdout_s%06d.txt' % sgejob_idx), os.path.join(P.logdir(stage_name), 'stderr_s%06d.txt' % sgejob_idx)))
	jsonfile = staticmethod(lambda : os.path.join(P.json, 'expsgejob.json'))

	@staticmethod
	def init(exp_py):
		exp_code = md5.new(os.path.abspath(exp_py)).hexdigest()[:3].upper()
		P.experiment_prefix = os.path.basename(exp_py) + '_' + exp_code
		experiment_root = os.path.join(P.root, experiment_prefix)
		P.log = os.path.join(experiment_root, 'log')
		P.job = os.path.join(experiment_root, 'job')
		P.sgejob = os.path.join(experiment_root, 'sgejob')
		P.all_dirs = [experiment_root, log, job, sgejob]

class Q:
	@staticmethod
	def get_jobs(job_name_prefix, state = ''):
		return [elem for elem in xml.dom.minidom.parseString(subprocess.check_output(['qstat', '-xml'])).documentElement.getElementsByTagName('job_list') if elem.getElementsByTagName('JB_name')[0].firstChild.data.startswith(job_name_prefix) and elem.getElementsByTagName('state')[0].firstChild.data.startswith(state)]
	
	@staticmethod
	def submit_job(sgejob_file):
		subprocess.check_call(['qsub', sgejob_file])

	@staticmethod
	def delete_jobs(jobs):
		subprocess.check_call(['qdel'] + [elem.getElementsByTagName('JB_job_number')[0].firstChild.data for elem in jobs])

class path:
	def __init__(self, string, mkdirs = False):
		self.string = string
		self.mkdirs = mkdirs

	def join(self, *args):
		return path(os.path.join(self.string, *map(str, args)))

	def makedirs(self):
		return path(self.string, True)

	@staticmethod
	def cwd():
		return path(os.getcwd())
	
	def __str__(self):
		return self.string

class Experiment:
	class ExecutionStatus:
		waiting = 'waiting'
		submitted = 'submitted'
		running = 'running'
		success = 'success'
		failure = 'failure'
		canceled = 'canceled'

	class Job:
		def __init__(self, name, executable, env, cwd):
			self.name = name
			self.executable = executable
			self.env = env
			self.cwd = cwd
			self.status = Experiment.ExecutionStatus.waiting

		def get_used_paths(self):
			return [v for k, v in sorted(self.env.items()) if isinstance(v, path)] + [self.cwd] + self.executable.get_used_paths()
	
	class Stage:
		def __init__(self, name, queue):
			self.name = name
			self.queue = queue
			self.mem_lo_gb = config.mem_lo_gb
			self.mem_hi_gb = config.mem_hi_gb
			self.jobs = []

		def calculate_aggregate_status(self):
			conditions = {
				Experiment.ExecutionStatus.waiting : [],
				Experiment.ExecutionStatus.submitted : [Experiment.ExecutionStatus.waiting],
				Experiment.ExecutionStatus.running : [Experiment.ExecutionStatus.waiting, Experiment.ExecutionStatus.submitted, Experiment.ExecutionStatus.success],
				Experiment.ExecutionStatus.success : [],
				Experiment.ExecutionStatus.failure : None,
				Experiment.ExecutionStatus.canceled: []
			}
			
			for status, extra_statuses in conditions.items():
				if any([job.status == status for job in self.jobs]) and (extra_statuses == None or all([job.status in [status] + extra_statuses for job in self.jobs])):
					return status
			raise Exception('Can not calculate_aggregate_status')

	def __init__(self, name):
		self.name = name
		self.stages = []

	def stage(self, name, queue = None):
		stage = Experiment.Stage(name, queue)
		self.stages.append(stage)

	def run(self, executable, name = None, env = {}, cwd = path.cwd()):
		name = name or str(len(self.stages[-1].jobs))
		job = Experiment.Job(name, executable, env, cwd)
		self.stages[-1].jobs.append(job)

	def has_failed_stages(self):
		return any([stage.calculate_aggregate_status() == Experiment.ExecutionStatus.failure for stage in self.stages])

	def cancel_stages_after(self, failed_stage):
		for stage in self.stages[1 + self.stages.index(failed_stage):]:
			for job in stage.jobs:
				job.status = Experiment.ExecutionStatus.canceled

class bash:
	def __init__(self, script_path, args = ''):
		self.script_path = script_path
		self.args = args

	def get_used_paths(self):
		return [path(str(self.script_path))]

	def generate_bash_script_lines(self):
		return [str(self.script_path) + ' ' + self.args]

class torch(bash):
	TORCH_ACTIVATE = os.getenv('EXPSGE_TORCH_ACTIVATE')

	def get_used_paths(self):
		return [path(torch.TORCH_ACTIVATE)] + shell.get_used_paths(self)

	def generate_bash_script_lines(self):
		return ['source "%s"' % torch.TORCH_ACTIVATE, 'th ' + str(self.script_path) + ' ' + self.args]

def init():
	globals_mod = globals().copy()
	e = Experiment(os.path.basename(P.exp_py))
	globals_mod.update({m : getattr(e, m) for m in dir(e)})
	exec open(exp_py, 'r').read() in globals_mod, globals_mod

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

def clean():
	if os.path.exists(P.root):
		shutil.rmtree(P.root)

def html(e):
	HTML_PATTERN = '''
<!DOCTYPE html>

<html>
	<head>
		<title>Report on %s</title>
		<meta charset="utf-8" />
		<meta http-equiv="cache-control" content="no-cache" />
		<link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.6/css/bootstrap.min.css" integrity="sha384-1q8mTJOASx8j1Au+a5WDVnPi2lkFfwwEAa8hDDdjZlpLegxhjVME1fgjWPGmkzs7" crossorigin="anonymous">
		<link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.6/css/bootstrap-theme.min.css" integrity="sha384-fLW2N01lMqjakBkx3l/M9EahuwpSfeNvV63J5ezn3uZzapT0u7EYsXMjQV+0En5r" crossorigin="anonymous">
		<script type="text/javascript" src="https://code.jquery.com/jquery-2.2.3.min.js"></script>
		<script src="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.6/js/bootstrap.min.js" integrity="sha384-0mSbJDEHialfmuBBQP6A4Qrprq5OVfW37PRR3j5ELqxss1yVqOtnepnHVP9aJ7xS" crossorigin="anonymous"></script>
		<script type="text/javascript" src="https://cdnjs.cloudflare.com/ajax/libs/jsviews/0.9.75/jsrender.min.js"></script>
		
		<style>
			.experiment-pane {overflow: auto}
			.job-status-waiting {background-color: white}
			.job-status-submitted {background-color: gray}
			.job-status-running {background-color: lightgreen}
			.job-status-success {background-color: green}
			.job-status-failure {background-color: red}
			.job-status-canceled {background-color: salmon}
		</style>
	</head>
	<body>
		<script type="text/javascript">
			var report = %s;

			function show_job(stage_name, job_name)
			{
				$('#divExp').html($('#tmplExp').render(report));
				for(var i = 0; i < report.stages.length; i++)
				{
					if(report.stages[i].name == stage_name)
					{
						var details_pane_object = report.stages[i];
						for(var j = 0; j < report.stages[i].jobs.length; j++)
							if(report.stages[i].jobs[j].name == job_name)
								details_pane_object = report.stages[i].jobs[j];

						$('#divJobs').html($('#tmplJobs').render(report.stages[i]));
						$('#divJob').html($('#tmplJob').render(details_pane_object));
						return;
					}
				}
				alert('Error. Could not find requested stage/job.');
			}

			$(function() {
				$.views.helpers({
					average_wall_clock_time_seconds : function(jobs) {
						var total = 0.0, cnt = 0;
						for(var i = 0; i < jobs.length; i++)
						{
							if(jobs[i].stats.wall_clock_time_seconds != null)
							{
								total += jobs[i].stats.wall_clock_time_seconds;
								cnt++;
							}
						}
						return cnt == 0 ? undefined : total / cnt;
					},
					format : function(name, value) {
						var undefined_formatted = 'N/A';
						var return_name = arguments.length == 1;
						var return_undefined_formatted = value == undefined;

						if(name.indexOf('seconds') > 0)
						{
							name = name + ' (h:m:s)'
							if(return_name)
								return name;
							if(return_undefined_formatted)
								return undefined_formatted;

							var seconds = Math.round(value);
							var hours = Math.floor(seconds / (60 * 60));
							var divisor_for_minutes = seconds %% (60 * 60);
							return hours + ":" + Math.floor(divisor_for_minutes / 60) + ":" + Math.ceil(divisor_for_minutes %% 60);
						}
						else if(name.indexOf('kbytes') > 0)
						{
							name = name + ' (Gb)'
							if(return_name)
								return name;
							if(return_undefined_formatted)
								return undefined_formatted;
							return (value / 1024 / 1024).toFixed(1);
						}
						return return_name ? name : return_undefined_formatted ? undefined_formatted : value;
					},
					stats_keys_reduced : ['exit_code', 'wall_clock_time_seconds'],
					stats_keys_extended : ['time_started', 'time_finished', 'user_time_seconds', 'system_time_seconds', 'max_rss_kbytes', 'avg_rss_kbytes', 'major_page_faults', 'minor_page_faults', 'voluntary_context_switches', 'involuntary_context_switches', 'inputs', 'outputs', 'signals_received', 'cpu_percentage', 'stdout_path', 'stderr_path']
				});

				$(window).on('hashchange', function() {
					var re = /(\#[^\/]+)?(\/.+)?/;
					var groups = re.exec(window.location.hash);
					show_job(groups[1].substring(1), groups[2] == null ? null : groups[2].substring(1));
				});

				if(window.location.hash == '')
					window.location.hash = '#' + report.stages[0].name + '/' + report.stages[0].jobs[0].name;
				else
					$(window).trigger('hashchange');
			});

		</script>
		
		<div class="container">
			<div class="row">
				<div class="col-sm-4 experiment-pane" id="divExp"></div>
				<script type="text/x-jsrender" id="tmplExp">
					<h1><a href="#{{:stages[0].name}}/{{:stages[0].jobs[0].name}}">{{:name}}</a></h1>
					<h3>stages</h3>
					<table class="table-bordered">
						<thead>
							<th>name</th>
							<th>duration (avg)</th>
							<th>status</th>
						</thead>
						<tbody>
							{{for stages}}
							<tr>
								<td><a href="#{{:name}}">{{:name}}</a></td>
								<td>{{:~format("average_wall_clock_time_seconds", ~average_wall_clock_time_seconds(jobs))}}</td>
								<td title="{{:status}} "class="job-status-{{:status}}"></td>
							</tr>
							{{/for}}
						</tbody>
					</table>
				</script>

				<div class="col-sm-4 experiment-pane" id="divJobs"></div>
				<script type="text/x-jsrender" id="tmplJobs">
					<h1>{{:name}}</h1>
					<h3>jobs</h3>
					<table class="table-bordered">
						<thead>
							<th>name</th>
							<th>status</th>
						</thead>
						<tbody>
							{{for jobs}}
							<tr>
								<td><a href="#{{:#parent.parent.data.name}}/{{:name}}">{{:name}}</a></td>
								<td title="{{:status}}" class="job-status-{{:status}}"></td>
							</tr>
							{{/for}}
						</tbody>
					</table>
				</script>

				<div class="col-sm-4 experiment-pane" id="divJob"></div>
				<script type="text/x-jsrender" id="tmplJob">
					<h1>{{:name}}</h1>
					<h3>stats</h3>
					<table class="table table-striped">
						{{for ~stats_keys_reduced ~stats=stats ~row_class="" tmpl="#tmplStats" /}}
						{{for ~stats_keys_extended ~stats=stats ~row_class="collapse table-stats-extended" tmpl="#tmplStats" /}}
					</table>
					<a class="btn btn-info" data-toggle="collapse" data-target=".table-stats-extended">Toggle all stats</a>
					<h3>stderr</h3>
					<pre>{{>stderr}}</pre>
					<h3>stdout</h3>
					<pre>{{>stdout}}
				</script>
				
				<script type="text/x-jsrender" id="tmplStats">
					<tr class="{{:~row_class}}">
						<th>{{:~format(#data)}}</th>
						<td>{{:~format(#data, ~stats[#data])}}</td>
					</tr>
				</script>
			</div>
		</div>
	</body>
</html>
	'''

	read_or_empty = lambda x: open(x).read() if os.path.exists(x) else ''
	sgejoblog = lambda stage, k: '\n'.join(['#SGEJOB #%d (%s)\n%s\n\n' % (sgejob_idx, log_file_path, read_or_empty(log_file_path)) for log_file_path in [P.sgejoblogfiles(stage.name, sgejob_idx)[k] for sgejob_idx in range(len(stage.jobs))]])

	j = {'name' : e.name, 'stages' : []}
	for stage in e.stages:
		jobs = []
		for job_idx, job in enumerate(stage.jobs):
			stdout_path, stderr_path = P.joblogfiles(stage.name, job_idx)
			stdout, stderr = map(read_or_empty, [stdout_path, stderr_path])
			stats = {'stdout_path' : stdout_path, 'stderr_path' : stderr_path}
			time_output = re.search('time_output = (.+)$', stderr, re.MULTILINE)
			time_started = re.search('expsge_job_started = (.+)$', stderr, re.MULTILINE)
			time_finished = re.search('expsge_job_finished = (.+)$', stderr, re.MULTILINE)
			if time_output:
				stats.update(json.loads(stats.group(1)))
			if time_started:
				stats['time_started'] = time_started.group(1)
			if time_finished:
				stats['time_finished'] = time_finished.group(1)

			if stdout != None and len(stdout) > config.max_stdout_characters:
				half = config.max_stdout_characters / 2
				stdout = stdout[:half] + '\n\n[%d characters skipped]\n\n' % (len(stdout) - 2 * half) + stdout[-half:]
			jobs.append({'name' : job.name, 'stdout' : stdout, 'stderr' : stderr, 'status' : job.status, 'stats' : stats})
		stdout, stderr = sgejoblog(stage, 0), sgejoblog(stage, 1)
		j['stages'].append({'name' : stage.name, 'jobs' : jobs, 'status' : stage.calculate_aggregate_status(), 'stdout' : stdout, 'stderr' : stderr})
			
	with open(P.html_report, 'w') as f:
		f.write(HTML_PATTERN % (e.name, json.dumps(j)))

def gen(e):
	for stage in e.stages:
		for job_idx, job in enumerate(stage.jobs):
			with open(P.jobfile(stage.name, job_idx), 'w') as f:
				f.write('\n'.join(
					['# stage.name = "%s", job.name = "%s", job_idx = %d' % (stage.name, job.name, job_idx )] +
					map(lambda path: '''if [ ! -e "%s" ]; then echo 'File "%s" does not exist'; exit 1; fi''' % (path, path), job.get_used_paths()) +
					list(itertools.starmap('export {0}="{1}"'.format, sorted(job.env.items()))) +
					['cd "%s"' % job.cwd] +
					job.executable.generate_shell_script_lines()
				))

			for p in job.get_used_paths():
				if p.mkdirs == True and not os.path.exists(str(p)):
					os.makedirs(str(p))

	for stage in e.stages:
		for job_idx, job in enumerate(stage.jobs):
			sgejob_idx = job_idx
			job_stderr_path = P.joblogfiles(stage.name, job_idx)[1]
			with open(P.sgejobfile(stage.name, sgejob_idx), 'w') as f:
				f.write('\n'.join([
					'#$ -N %s_%s' % (e.name, stage.name),
					'#$ -S /bin/bash',
					'#$ -l mem_req=%.2fG' % stage.mem_lo_gb,
					'#$ -l h_vmem=%.2fG' % stage.mem_hi_gb,
					'#$ -o %s -e %s\n' % P.sgejoblogfiles(stage.name, sgejob_idx),
					'#$ -q %s' % stage.queue if stage.queue else '',
					'',
					'# stage.name = "%s", job.name = "%s", job_idx = %d' % (stage.name, job.name, job_idx),
					'echo "expsge_job_started = $(date)" > "%s"' % job_stderr_path,
					'''/usr/bin/time -f 'time_output = {"exit_code" : %%x, "user_time_seconds" : %%U, "system_time_seconds" : %%S, "wall_clock_time_seconds" : %%e, "max_rss_kbytes" : %%M, "avg_rss_kbytes" : %%t, "major_page_faults" : %%F, "minor_page_faults" : %%R, "inputs" : %%I, "outputs" : %%O, "voluntary_context_switches" : %%w, "involuntary_context_switches" : %%c, "cpu_percentage" : "%%P", "signals_received" : %%k}' bash -e "%s" > "%s" 2>> "%s"''' % ((P.jobfile(stage.name, job_idx), ) + P.joblogfiles(stage.name, job_idx)),
					'echo "expsge_job_finished = $(date)" >> "%s"' % job_stderr_path,
					'# end',
					'']))

def run(dry, verbose):
	#clean()
	e = init()
	html(e)
	#gen(e)


	def update_status(stage):
		for job_idx, job in enumerate(stage.jobs):
			stderr_path = P.joblogfiles(stage.name, job_idx)[1]
			stderr = open(stderr_path).read() if os.path.exists(stderr_path) else ''

			if 'expsge_job_started' in stderr:
				job.status = Experiment.ExecutionStatus.running
			if 'Command exited with non-zero status' in stderr:
				job.status = Experiment.ExecutionStatus.failure
			if '"exit_code": 0' in stderr:
				job.status = Experiment.ExecutionStatus.success

	for stage in e.stages:
		update_status(stage)
		if stage.calculate_aggregate_status() == Experiment.ExecutionStatus.failure:
			e.cancel_stages_after(stage)
			break
	html(e)

	if dry:
		print 'Dry run. Quitting.'
		return
	
	def wait_if_more_jobs_than(stage, job_name_prefix, num_jobs):
		while len(Q.get_jobs(job_name_prefix)) > num_jobs:
			msg = 'Running %d jobs, waiting %d jobs.' % (len(Q.get_jobs(job_name_prefix, 'r')), len(Q.get_jobs(job_name_prefix, 'qw')))
			if verbose:
				print msg
			time.sleep(config.sleep_between_queue_checks)
			update_status(stage)
			html(e)

		update_status(stage)
		html(e)
	
	for stage_idx, stage in enumerate(e.stages):
		print 'Starting stage #%d [%s], with %d jobs.' % (stage_idx, stage.name, len(stage.jobs))
		for job_idx in range(len(stage.jobs)):
			#TODO: support multiple jobs per sge job
			sgejob_idx = job_idx
			wait_if_more_jobs_than(stage, P.experiment_prefix, config.maximum_simultaneously_submitted_jobs)
			Q.submit_job(P.sgejobfile(stage.name, sgejob_idx))
			stage.jobs[job_idx].status = Experiment.ExecutionStatus.submitted

		wait_if_more_jobs_than(stage, P.experiment_prefix, 0)

		update_status(stage)
		if e.has_failed_stages():
			e.cancel_stages(stage)
			print 'Stage [%s] failed. Stopping the experiment. Quitting.' % stage.name
			break

	print '\nDone.'

def stop():
	Q.delete_jobs(Q.get_jobs(P.experiment_prefix))

if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	subparsers = parser.add_subparsers()
	
	cmd = subparsers.add_parser('stop')
	cmd.set_defaults(func = stop)
	cmd.add_argument('exp_py')
	
	cmd = subparsers.add_parser('run')
	cmd.set_defaults(func = run)
	cmd.add_argument('exp_py')
	cmd.add_argument('--dry', action = 'store_true')
	cmd.add_argument('--verbose', action = 'store_true')
	
	args = vars(parser.parse_args())
	P.init(args.pop('exp_py'))
	
	cmd = args.pop('func')
	try:
		cmd(**args)
	except KeyboardInterrupt:
		print 'Quitting (Ctrl+C pressed). To stop jobs:'
		print ''
		print 'expsge stop "%s"' % args['exp_py']
