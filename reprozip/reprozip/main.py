# Copyright (C) 2014-2016 New York University
# This file is part of ReproZip which is released under the Revised BSD License
# See file LICENSE for full license details.

"""Entry point for the reprozip utility.

This contains :func:`~reprozip.main.main`, which is the entry point declared to
setuptools. It is also callable directly.

It dispatchs to other routines, or handles the testrun command.
"""

from __future__ import division, print_function, unicode_literals

import argparse
import locale
import logging
import os
from rpaths import Path
import sqlite3
import string
import sys

from reprozip import __version__ as reprozip_version
from reprozip import _pytracer
from reprozip.common import setup_logging, \
    setup_usage_report, enable_usage_report, \
    submit_usage_report, record_usage
import reprozip.pack
import reprozip.tracer.trace
import reprozip.traceutils
from reprozip.utils import PY3, unicode_, stderr
import httplib2
import json
import requests
import os


def shell_escape(s):
    """Given bl"a, returns "bl\\"a".
    """
    if isinstance(s, bytes):
        s = s.decode('utf-8')
    if any(c in s for c in string.whitespace + '*$\\"\''):
        return '"%s"' % (s.replace('\\', '\\\\')
                          .replace('"', '\\"')
                          .replace('$', '\\$'))
    else:
        return s


def print_db(database):
    """Prints out database content.
    """
    if PY3:
        # On PY3, connect() only accepts unicode
        conn = sqlite3.connect(str(database))
    else:
        conn = sqlite3.connect(database.path)
    conn.row_factory = sqlite3.Row
    conn.text_factory = lambda x: unicode_(x, 'utf-8', 'replace')

    cur = conn.cursor()
    processes = cur.execute(
        '''
        SELECT id, parent, timestamp, exit_timestamp, exitcode, cpu_time
        FROM processes;
        ''')
    print("\nProcesses:")
    header = ("+------+--------+-------+------------------+------------------+"
              "----------+")
    print(header)
    print("|  id  | parent |  exit |     timestamp    |  exit timestamp  |"
          " cpu time |")
    print(header)
    for (r_id, r_parent, r_timestamp, r_endtime,
            r_exit, r_cpu_time) in processes:
        f_id = "{0: 5d} ".format(r_id)
        if r_parent is not None:
            f_parent = "{0: 7d} ".format(r_parent)
        else:
            f_parent = "        "
        if r_exit & 0x0100:
            f_exit = " sig{0: <2d} ".format(r_exit)
        else:
            f_exit = "    {0: <2d} ".format(r_exit)
        f_timestamp = "{0: 17d} ".format(r_timestamp)
        f_endtime = "{0: 17d} ".format(r_endtime)
        if r_cpu_time >= 0:
            f_cputime = "{0: 9.2f} ".format(r_cpu_time * 0.001)
        else:
            f_cputime = "          "
        print('|'.join(('', f_id, f_parent, f_exit,
                        f_timestamp, f_endtime, f_cputime, '')))
        print(header)
    cur.close()

    cur = conn.cursor()
    processes = cur.execute(
        '''
        SELECT id, name, timestamp, process, argv
        FROM executed_files;
        ''')
    print("\nExecuted files:")
    header = ("+--------+------------------+---------+------------------------"
              "---------------+")
    print(header)
    print("|   id   |     timestamp    | process | name and argv              "
          "           |")
    print(header)
    for r_id, r_name, r_timestamp, r_process, r_argv in processes:
        f_id = "{0: 7d} ".format(r_id)
        f_timestamp = "{0: 17d} ".format(r_timestamp)
        f_proc = "{0: 8d} ".format(r_process)
        argv = r_argv.split('\0')
        if not argv[-1]:
            argv = argv[:-1]
        cmdline = ' '.join(shell_escape(a) for a in argv)
        if argv[0] not in (r_name, os.path.basename(r_name)):
            cmdline = "(%s) %s" % (shell_escape(r_name), cmdline)
        f_cmdline = " {0: <37s} ".format(cmdline)
        print('|'.join(('', f_id, f_timestamp, f_proc, f_cmdline, '')))
        print(header)
    cur.close()

    cur = conn.cursor()
    processes = cur.execute(
        '''
        SELECT id, name, timestamp, mode, process
        FROM opened_files;
        ''')
    print("\nFiles:")
    header = ("+--------+------------------+---------+------+-----------------"
              "---------------+")
    print(header)
    print("|   id   |     timestamp    | process | mode | name                "
          "           |")
    print(header)
    for r_id, r_name, r_timestamp, r_mode, r_process in processes:
        f_id = "{0: 7d} ".format(r_id)
        f_timestamp = "{0: 17d} ".format(r_timestamp)
        f_proc = "{0: 8d} ".format(r_process)
        f_mode = "{0: 5d} ".format(r_mode)
        f_name = " {0: <30s} ".format(r_name)
        print('|'.join(('', f_id, f_timestamp, f_proc, f_mode, f_name, '')))
        print(header)
    cur.close()

    cur = conn.cursor()
    connections = cur.execute(
        '''
        SELECT DISTINCT inbound, family, protocol, address
        FROM connections
        ORDER BY inbound, family, timestamp;
        ''')
    header_shown = -1
    current_family = current_protocol = None
    for r_inbound, r_family, r_protocol, r_address in connections:
        if header_shown < r_inbound:
            if r_inbound:
                print("\nIncoming connections:")
            else:
                print("\nRemote connections:")
            header_shown = r_inbound
            current_family = None
        if current_family != r_family:
            print("    %s" % r_family)
            current_family = r_family
            current_protocol = None
        if current_protocol != r_protocol:
            print("      %s" % r_protocol)
            current_protocol = r_protocol
        print("        %s" % r_address)

    conn.close()


def testrun(args):
    """testrun subcommand.

    Runs the command with the tracer using a temporary sqlite3 database, then
    reads it and dumps it out.

    Not really useful, except for debugging.
    """
    fd, database = Path.tempfile(prefix='reprozip_', suffix='.sqlite3')
    os.close(fd)
    try:
        if args.arg0 is not None:
            argv = [args.arg0] + args.cmdline[1:]
        else:
            argv = args.cmdline
        logging.debug("Starting tracer, binary=%r, argv=%r",
                      args.cmdline[0], argv)
        c = _pytracer.execute(args.cmdline[0], argv, database.path,
                              args.verbosity)
        print("\n\n-----------------------------------------------------------"
              "--------------------")
        print_db(database)
        if c != 0:
            if c & 0x0100:
                print("\nWarning: program appears to have been terminated by "
                      "signal %d" % (c & 0xFF))
            else:
                print("\nWarning: program exited with non-zero code %d" % c)
    finally:
        database.remove()


def trace(args):
    """trace subcommand.

    Simply calls reprozip.tracer.trace() with the arguments from argparse.
    """
    # print(args.project)
    if args.arg0 is not None:
        argv = [args.arg0] + args.cmdline[1:]
    else:
        argv = args.cmdline
    reprozip.tracer.trace.trace(args.cmdline[0],
                                argv,
                                Path(args.dir),
                                args.append,
                                args.verbosity)
    reprozip.tracer.trace.write_configuration(Path(args.dir),
                                              args.identify_packages,
                                              args.find_inputs_outputs,
                                              overwrite=False)

    if os.path.exists("bundle.rpz"):
        os.remove("bundle.rpz")

    args.target = 'bundle.rpz'
    pack(args)
    link_to_corr(config_path=args.config, project_name=args.project)

def link_to_corr(config_path=None, project_name=None):
    if config_path:
        config = {}
        with open(config_path, 'r') as config_file:
            config = json.loads(config_file.read())

        scope = config.get('default', {})
        api = scope.get('api',{})
        host = api.get('host','')
        port = api.get('port', 80)
        key = api.get('key', '')
        path = api.get('path', '')
        token = scope.get('app', '')
        client = httplib2.Http('.cache', disable_ssl_certificate_validation=True)
        server_url = "{0}:{1}{2}/private/{3}/{4}/".format(host, port, path, key, token)
        url = "%sprojects" % (server_url)
        project = None
        print(url)
        response, content = http_get(client, url)
        if response.status == 200:
            json_content = json.loads(content)['content']
            exist = False
            if json_content['total_projects'] > 0:
                for _project in json_content['projects']:
                    if _project['name'] == project_name:
                        exist = True
                        project = _project
                        break
            if not exist:
                response, content = put_project(client, server_url, project_name)
                project = json.loads(content)['content']

            push_record(client, server_url, project)
        else:
            print(content)
    else:
        print("Config file not provided.")

def http_get(client, url):
    headers = {'Accept': 'application/json'}
    response, content = client.request(url, headers=headers)
    return response, content

def put_project(client, url, project_name, long_name='No goals provided.', description='No description provided.'):
        url = "%sproject/create" % (url)
        content = {'name':project_name, 'goals':long_name, 'description':description}
        headers = {'Content-Type': 'application/json'}
        response, content = client.request(url, 'POST', json.dumps(content), headers=headers)
        return response, content

def push_record(client, server_url, project):
        record_id = None
        
        url = "%sproject/record/create/%s" % (server_url, project['id'])
        headers = {'Content-Type': 'application/json'}
        _content = {}
        _content['label'] = 'no label provided'
        _content['tags'] = []
        _content['system'] = {}
        _content['inputs'] = []
        _content['outputs'] = []
        _content['dependencies'] = []
        _content['execution'] = {}
        _content['status'] = 'finished'
        _content['timestamp'] = 'not captured'
        _content['reason'] = 'no reason provided'
        _content['duration'] ='not captured'
        _content['executable'] = {}
        _content['repository'] = {}
        _content['main_file'] = ''
        _content['version'] = 'not captured'
        _content['parameters'] = {}
        _content['script_arguments'] = 'not captured'
        _content['datastore'] = {}
        _content['input_datastore'] = {}
        _content['outcome'] = 'no outcome provided'
        _content['stdout_stderr'] = 'not captured'
        _content['diff'] = 'not captured'
        _content['user'] = 'not captured'
        response, content = client.request(url, 'POST', json.dumps(_content), headers=headers)

        if response.status == 200:
            record = json.loads(content)['content']
            record_id = record['head']['id']
            # print(record)
            response = upload_file(server_url, record_id, 'bundle.rpz', 'resource-record')
            # print(response)
        else:
            print(content)

def upload_file(server_url, record_id, file_path, group):
    url = "%sfile/upload/%s/%s" % (server_url, group, record_id)
    files = {'file':open(file_path)}
    response = requests.post(url, files=files, verify=False)
    return response

def reset(args):
    """reset subcommand.

    Just regenerates the configuration (config.yml) from the trace
    (trace.sqlite3).
    """
    reprozip.tracer.trace.write_configuration(Path(args.dir),
                                              args.identify_packages,
                                              args.find_inputs_outputs,
                                              overwrite=True)


def pack(args):
    """pack subcommand.

    Reads in the configuration file and writes out a tarball.
    """
    target = Path(args.target)
    if not target.unicodename.lower().endswith('.rpz'):
        target = Path(target.path + b'.rpz')
        logging.warning("Changing output filename to %s", target.unicodename)
    reprozip.pack.pack(target, Path(args.dir), args.identify_packages)


def combine(args):
    """combine subcommand.

    Reads in multiple trace databases and combines them into one.

    The runs from the original traces are appended ('run_id' field gets
    translated to avoid conflicts).
    """
    traces = []
    for tracepath in args.traces:
        if tracepath == '-':
            tracepath = Path(args.dir) / 'trace.sqlite3'
        else:
            tracepath = Path(tracepath)
            if tracepath.is_dir():
                tracepath = tracepath / 'trace.sqlite3'
        traces.append(tracepath)

    reprozip.traceutils.combine_traces(traces, Path(args.dir))
    reprozip.tracer.trace.write_configuration(Path(args.dir),
                                              args.identify_packages,
                                              args.find_inputs_outputs,
                                              overwrite=True)


def usage_report(args):
    if bool(args.enable) == bool(args.disable):
        logging.critical("What do you want to do?")
        sys.exit(2)
    enable_usage_report(args.enable)
    sys.exit(0)


def main():
    """Entry point when called on the command-line.
    """
    # Locale
    locale.setlocale(locale.LC_ALL, '')

    # http://bugs.python.org/issue13676
    # This prevents reprozip from reading argv and envp arrays from trace
    if sys.version_info < (2, 7, 3):
        stderr.write("Error: your version of Python, %s, is not supported\n"
                     "Versions before 2.7.3 are affected by bug 13676 and "
                     "will not work with ReproZip\n" %
                     sys.version.split(' ', 1)[0])
        sys.exit(1)

    # Parses command-line

    # General options
    def add_options(opt):
        opt.add_argument('--version', action='version',
                         version="reprozip version %s" % reprozip_version)
        opt.add_argument('-d', '--dir', default='.reprozip-trace',
                         help="where to store database and configuration file "
                         "(default: ./.reprozip-trace)")
        opt.add_argument(
            '--dont-identify-packages', action='store_false', default=True,
            dest='identify_packages',
            help="do not try identify which package each file comes from")
        opt.add_argument(
            '--dont-find-inputs-outputs', action='store_false',
            default=True, dest='find_inputs_outputs',
            help="do not try to identify input and output files")

    parser = argparse.ArgumentParser(
        description="reprozip is the ReproZip component responsible for "
                    "tracing and packing the execution of an experiment",
        epilog="Please report issues to reprozip-users@vgc.poly.edu")
    add_options(parser)
    parser.add_argument('-v', '--verbose', action='count', default=1,
                        dest='verbosity',
                        help="augments verbosity level")
    subparsers = parser.add_subparsers(title="commands", metavar='',
                                       dest='selected_command')

    # usage_report subcommand
    parser_stats = subparsers.add_parser(
        'usage_report',
        help="Enables or disables anonymous usage reports")
    add_options(parser_stats)
    parser_stats.add_argument('--enable', action='store_true')
    parser_stats.add_argument('--disable', action='store_true')
    parser_stats.set_defaults(func=usage_report)

    # trace command
    parser_trace = subparsers.add_parser(
        'trace',
        help="Runs the program and writes out database and configuration file")
    add_options(parser_trace)
    parser_trace.add_argument(
        '-a',
        dest='arg0',
        help="argument 0 to program, if different from program path")
    parser_trace.add_argument(
        '-config',
        dest='config',
        help="CoRR Backend config file.")
    parser_trace.add_argument(
        'project', help="the project name")
    parser_trace.add_argument(
        '-c', '--continue', action='store_true', dest='append',
        help="add to the previous run instead of replacing it")
    parser_trace.add_argument('cmdline', nargs=argparse.REMAINDER,
                              help="command-line to run under trace")
    parser_trace.set_defaults(func=trace)

    # testrun command
    parser_testrun = subparsers.add_parser(
        'testrun',
        help="Runs the program and writes out the database contents")
    add_options(parser_testrun)
    parser_testrun.add_argument(
        '-a',
        dest='arg0',
        help="argument 0 to program, if different from program path")
    parser_testrun.add_argument('cmdline', nargs=argparse.REMAINDER)
    parser_testrun.set_defaults(func=testrun)

    # reset command
    parser_reset = subparsers.add_parser(
        'reset',
        help="Resets the configuration file")
    add_options(parser_reset)
    parser_reset.set_defaults(func=reset)

    # pack command
    parser_pack = subparsers.add_parser(
        'pack',
        help="Packs the experiment according to the current configuration")
    add_options(parser_pack)
    parser_pack.add_argument('target', nargs=argparse.OPTIONAL,
                             default='experiment.rpz',
                             help="Destination file")
    parser_pack.set_defaults(func=pack)

    # combine command
    parser_combine = subparsers.add_parser(
        'combine',
        help="Combine multiple traces into one (possibly as subsequent runs)")
    add_options(parser_combine)
    parser_combine.add_argument('traces', nargs=argparse.ONE_OR_MORE)
    parser_combine.set_defaults(func=combine)

    args = parser.parse_args()
    setup_logging('REPROZIP', args.verbosity)
    if getattr(args, 'func', None) is None:
        parser.print_help(sys.stderr)
        sys.exit(2)
    setup_usage_report('reprozip', reprozip_version)
    if 'cmdline' in args and not args.cmdline:
        parser.error("missing command-line")
    record_usage(command=args.selected_command)
    try:
        args.func(args)
    except Exception as e:
        submit_usage_report(result=type(e).__name__)
        raise
    else:
        submit_usage_report(result='success')
    sys.exit(0)


if __name__ == '__main__':
    main()
