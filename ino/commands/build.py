# -*- coding: utf-8; -*-

import re
import os.path
import inspect
import subprocess
import platform
import jinja2
import shlex

from jinja2.runtime import StrictUndefined

import ino.filters

from ino.commands.base import Command
from ino.environment import Version
from ino.filters import colorize
from ino.utils import SpaceList, list_subdirs
from ino.exc import Abort


class Build(Command):
    """
    Build a project in the current directory and produce a ready-to-upload
    firmware file.

    The project is expected to have a `src' subdirectory where all its sources
    are located. This directory is scanned recursively to find
    *.[c|cpp|pde|ino] files. They are compiled and linked into resulting
    firmware hex-file.

    Also any external library dependencies are tracked automatically. If a
    source file includes any library found among standard Arduino libraries or
    a library placed in `lib' subdirectory of the project, the library gets
    built too.

    Build artifacts are placed in `.build' subdirectory of the project.
    """

    name = 'build'
    help_line = "Build firmware from the current directory project"

    default_make = 'make'
    default_cc = 'avr-gcc'
    default_cxx = 'avr-g++'
    default_ar = 'avr-ar'
    default_objcopy = 'avr-objcopy'

    default_cppflags = '-ffunction-sections -fdata-sections -g -Os -w'
    default_cflags = ''
    default_cxxflags = '-fno-exceptions'
    default_ldflags = '-Os --gc-sections'

    def setup_arg_parser(self, parser):
        super(Build, self).setup_arg_parser(parser)
        self.e.add_board_model_arg(parser)
        self.e.add_arduino_dist_arg(parser)

        parser.add_argument('--make', metavar='MAKE',
                            default='',
                            help='Specifies the make tool to use. If '
                            'a full path is not given, searches in Arduino '
                            'directories before PATH. Default: "%(default)s".')

        parser.add_argument('--cc', metavar='COMPILER',
                            default='',
                            help='Specifies the compiler used for C files. If '
                            'a full path is not given, searches in Arduino '
                            'directories before PATH. Default: "%(default)s".')

        parser.add_argument('--cxx', metavar='COMPILER',
                            default='',
                            help='Specifies the compiler used for C++ files. '
                            'If a full path is not given, searches in Arduino '
                            'directories before PATH. Default: "%(default)s".')

        parser.add_argument('--ar', metavar='AR',
                            default='',
                            help='Specifies the AR tool to use. If a full path '
                            'is not given, searches in Arduino directories '
                            'before PATH. Default: "%(default)s".')

        parser.add_argument('--objcopy', metavar='OBJCOPY',
                            default='',
                            help='Specifies the OBJCOPY to use. If a full path '
                            'is not given, searches in Arduino directories '
                            'before PATH. Default: "%(default)s".')

        parser.add_argument('-f', '--cppflags', metavar='FLAGS',
                            default=self.default_cppflags,
                            help='Flags that will be passed to the compiler. '
                            'Note that multiple (space-separated) flags must '
                            'be surrounded by quotes, e.g. '
                            '`--cppflags="-DC1 -DC2"\' specifies flags to define '
                            'the constants C1 and C2. Default: "%(default)s".')

        parser.add_argument('--cflags', metavar='FLAGS',
                            default=self.default_cflags,
                            help='Like --cppflags, but the flags specified are '
                            'only passed to compilations of C source files. '
                            'Default: "%(default)s".')

        parser.add_argument('--cxxflags', metavar='FLAGS',
                            default=self.default_cxxflags,
                            help='Like --cppflags, but the flags specified '
                            'are only passed to compilations of C++ source '
                            'files. Default: "%(default)s".')

        parser.add_argument('--ldflags', metavar='FLAGS',
                            default=self.default_ldflags,
                            help='Like --cppflags, but the flags specified '
                            'are only passed during the linking stage. Note '
                            'these flags should be specified as if `ld\' were '
                            'being invoked directly (i.e. the `-Wl,\' prefix '
                            'should be omitted). Default: "%(default)s".')

        parser.add_argument('--menu', metavar='OPTIONS', default='',
                            help='"key:val,key:val" formatted string of '
                            'build menu items and their desired values')

        parser.add_argument('-v', '--verbose', default=False, action='store_true',
                            help='Verbose make output')

    # Merges one dictionary into another, overwriting non-dict entries and
    # recursively merging dictionaries mapped to the same key.
    def _mergeDicts(self,dest,src):
        for key,val in src.iteritems():
            if not key in dest:
                dest[key] = val
            elif type(val) == dict and type(dest[key]) == dict:
                self._mergeDicts(dest[key],val)
            else:
                dest[key] = val

    # Attempts to determine selections in boars['menu'] based on args.menu.
    # args.menu is assumed to be formatted as "key0:val0,key1:val1,...".
    # Selected options are then merged into board as though the settings 
    # were there all along.
    def _parseMenu(self,args,board):
        if not 'menu' in board:
            return
        choices = {}
        # Menu args specified in ino.ini will already be split into a list
        splitargs = args.menu if isinstance(args.menu, list) else args.menu.split(",")
        for option in splitargs: 
            pair = option.split(":")
            if len(pair) < 2:
                continue
            choices[pair[0]] = pair[1]

        selectedOptions = {}

        menu = board['menu']
        failed = 0
        for item,options in menu.iteritems():
            if item in choices:
                if not choices[item] in menu[item]:
                    print '\'%s\' is not a valid choice for %s (valid choices are: %s).' \
                            % (choices[item],item,
                               ",".join(["'%s'" % s for s in options.keys()]))
                    failed += 1
                else:
                    self._mergeDicts(selectedOptions,options[choices[item]])
            else:
                if len(options) == 0:
                    continue
                print 'No option specified for %s. Defaulting to \'%s\'.' % \
                        (item,options.keys()[0])
                self._mergeDicts(selectedOptions,options[options.keys()[0]])
        if failed > 0:
            raise KeyError(str(failed) + " invalid menu choices")
        del selectedOptions['name']
        self._mergeDicts(board,selectedOptions)


    def discover(self, args):
        board = self.e.board_model(args.board_model)
        self._parseMenu(args,board)

        core_place = os.path.join(board['_coredir'], 'cores', board['build']['core'])
        core_header = 'Arduino.h' if self.e.arduino_lib_version.major else 'WProgram.h'
        self.e.find_dir('arduino_core_dir', [core_header], [core_place],
                        human_name='Arduino core library')

#        if not board['name'].lower().startswith('teensy') and self.e.arduino_lib_version.major:
        if board['name'].lower().startswith('arduino') and self.e.arduino_lib_version.major:
            variants_place = os.path.join(board['_coredir'], 'variants')
            self.e.find_dir('arduino_variants_dir', ['.'], [variants_place],
                            human_name='Arduino variants directory')

        self.e.find_arduino_dir('arduino_libraries_dir', ['libraries'],
                                human_name='Arduino standard libraries')

        if args.make == '':
            try:
                args.make = board['build']['command']['make']
            except KeyError as _:
                args.make = self.default_make
        if args.cc == '':
            try:
                args.cc = board['build']['command']['gcc']
            except KeyError as _:
                args.cc = self.default_cc
        if args.cxx == '':
            try:
                args.cxx = board['build']['command']['g++']
            except KeyError as _:
                args.cxx = self.default_cxx
        if args.ar == '':
            try:
                args.ar = board['build']['command']['ar']
            except KeyError as _:
                args.ar = self.default_ar
        if args.objcopy == '':
            try:
                args.objcopy = board['build']['command']['objcopy']
            except KeyError as _:
                args.objcopy = self.default_objcopy

        toolset = [
            ('make', args.make),
            ('cc', args.cc),
            ('cxx', args.cxx),
            ('ar', args.ar),
            ('objcopy', args.objcopy),
        ]

        for tool_key, tool_binary in toolset:
            self.e.find_arduino_tool(
                tool_key, ['hardware', 'tools', '*', 'bin'],
                items=[tool_binary], human_name=tool_binary)

    # Used to parse board options. Finds a sequence of entries in table with the
    # keys prefix0, prefix1, prefix2 (or beginning with prefix1 if start = 1),
    # and appends them to the list-like structure out which has a constructor wrap.
    # For example:
    #  >>> o = []
    #  >>> table = {'squirrel':3,'a1':1,'a2':1,'a3':2,'a4':3,'a5':5,'a7':13}
    #  >>> _appendNumberedEntries(o,table,'a',start=1,wrap=lambda x:[x])
    #  >>> o
    #  >>> [1,1,2,3,5]
    def _appendNumberedEntries(self, out, table, prefix, start=0, wrap=SpaceList):
        i = start
        while (prefix + str(i)) in table:
            out += wrap([table[prefix+str(i)]])
            i += 1

    def setup_flags(self, args):
        board = self.e.board_model(args.board_model)
        self.e['incflag'] = board['build']['incflag'] if 'incflag' in board['build'] else '-I'
        if board['name'].lower().startswith('spark'):
            self.e['cppflags'] = SpaceList([])
            self.e['cflags'] = SpaceList([])
            self.e['cxxflags'] = SpaceList([])
            self.e['ldflags'] = SpaceList([])
            self.e['names'] = {
                'obj': '%s.o',
                'lib': 'lib%s.a',
                'cpp': '%s.cpp',
                'deps': '%s.d',
            }
            return
        cpu,mcu = '',''
        if 'cpu' in board['build']:
            cpu = '-mcpu=' + board['build']['cpu']
        elif 'mcu' in board['build']:
            mcu = '-mmcu=' + board['build']['mcu']
        if 'f_cpu' in board['build']:
            f_cpu = board['build']['f_cpu']
        else:
            raise KeyError('No valid source of f_cpu option')

        # Hard-code the flags that are essential to building the sketch
        self.e['cppflags'] = SpaceList([
            cpu,
            mcu,
            '-DF_CPU=' + f_cpu,
            '-DARDUINO=' + str(self.e.arduino_lib_version.as_int()),
            self.e.incflag + self.e['arduino_core_dir'],
        ])
        # Add additional flags as specified
        self.e['cppflags'] += SpaceList(shlex.split(args.cppflags))
        self._appendNumberedEntries(self.e['cppflags'],board['build'],'option',start=1)
        self._appendNumberedEntries(self.e['cppflags'],board['build'],'define')

        if 'vid' in board['build']:
            self.e['cppflags'].append('-DUSB_VID=%s' % board['build']['vid'])
        if 'pid' in board['build']:
            self.e['cppflags'].append('-DUSB_PID=%s' % board['build']['pid'])

        if not board['name'].lower().startswith('arduino'):
            pass
        elif self.e.arduino_lib_version.major:
            variant_dir = os.path.join(self.e.arduino_variants_dir,
                                       board['build']['variant'])
            self.e.cppflags.append(self.e.incflag + variant_dir)

        self.e['cflags'] = SpaceList(shlex.split(args.cflags))
        self.e['cxxflags'] = SpaceList(shlex.split(args.cxxflags))
        self._appendNumberedEntries(self.e['cxxflags'],board['build'],
                                    'cppoption',start=1)

        # Again, hard-code the flags that are essential to building the sketch
        self.e['ldflags'] = SpaceList([cpu,mcu])
        self.e['ldflags'] += SpaceList([
            '-Wl,' + flag for flag in shlex.split(args.ldflags)
        ])
        self._appendNumberedEntries(self.e['ldflags'],board['build'],
                                    'linkoption',start=1)
        self._appendNumberedEntries(self.e['ldflags'],board['build'],
                                    'additionalobject',start=1)

        if 'linkscript' in board['build']:
            script = self.e.find_arduino_tool(board['build']['linkscript'],
                                              ['hardware','*','cores','*'],
                                              human_name='Link script')
            self.e['ldflags'] = SpaceList(['-T' + script]) + \
                                self.e['ldflags']

        self.e['names'] = {
            'obj': '%s.o',
            'lib': 'lib%s.a',
            'cpp': '%s.cpp',
            'deps': '%s.d',
        }

    def create_jinja(self, verbose):
        templates_dir = os.path.join(os.path.dirname(__file__), '..', 'make')
        self.jenv = jinja2.Environment(
            loader=jinja2.FileSystemLoader(templates_dir),
            undefined=StrictUndefined, # bark on Undefined render
            extensions=['jinja2.ext.do'])

        # inject @filters from ino.filters
        for name, f in inspect.getmembers(ino.filters, lambda x: getattr(x, 'filter', False)):
            self.jenv.filters[name] = f

        # inject globals
        self.jenv.globals['e'] = self.e
        self.jenv.globals['v'] = '' if verbose else '@'
        self.jenv.globals['slash'] = os.path.sep
        self.jenv.globals['SpaceList'] = SpaceList

    def render_template(self, source, target, **ctx):
        template = self.jenv.get_template(source)
        contents = template.render(**ctx)
        out_path = os.path.join(self.e.build_dir, target)
        with open(out_path, 'wt') as f:
            f.write(contents)

        return out_path

    def make(self, makefile, **kwargs):
        makefile = self.render_template(makefile + '.jinja', makefile, **kwargs)
        ret = subprocess.call([self.e.make, '-f', makefile, 'all'])
        if ret != 0:
            raise Abort("Make failed with code %s" % ret)

    def recursive_inc_lib_flags(self, dashcmd, libdirs):
        flags = SpaceList()
        for d in libdirs:
            flags.append(dashcmd + d)
            flags.extend(dashcmd + subd for subd in list_subdirs(d, recursive=True, exclude=['examples']))
        return flags

    def _scan_dependencies(self, dir, lib_dirs, inc_flags):
        output_filepath = os.path.join(self.e.build_dir, os.path.basename(dir), 'dependencies.d')
        self.make('Makefile.deps', inc_flags=inc_flags, src_dir=dir, output_filepath=output_filepath)
        self.e['deps'].append(output_filepath)

        # search for dependencies on libraries
        # for this scan dependency file generated by make
        # with regexes to find entries that start with
        # libraries dirname
        regexes = dict((lib, re.compile(r'\s' + lib + re.escape(os.path.sep))) for lib in lib_dirs)
        used_libs = set()
        with open(output_filepath) as f:
            for line in f:
                for lib, regex in regexes.iteritems():
                    if regex.search(line) and lib != dir:
                        used_libs.add(lib)
        return used_libs

    def scan_dependencies(self):
        self.e['deps'] = SpaceList()

        lib_dirs = [self.e.arduino_core_dir] + list_subdirs(self.e.lib_dir) + list_subdirs(self.e.arduino_libraries_dir)
        inc_flags = self.recursive_inc_lib_flags(self.e.incflag, lib_dirs)

        # If lib A depends on lib B it have to appear before B in final
        # list so that linker could link all together correctly
        # but order of `_scan_dependencies` is not defined, so...
        
        # 1. Get dependencies of sources in arbitrary order
        used_libs = list(self._scan_dependencies(self.e.src_dir, lib_dirs, inc_flags))

        # 2. Get dependencies of dependency libs themselves: existing dependencies
        # are moved to the end of list maintaining order, new dependencies are appended
        scanned_libs = set()
        while scanned_libs != set(used_libs):
            for lib in set(used_libs) - scanned_libs:
                dep_libs = self._scan_dependencies(lib, lib_dirs, inc_flags)

                i = 0
                for ulib in used_libs[:]:
                    if ulib in dep_libs:
                        # dependency lib used already, move it to the tail
                        used_libs.append(used_libs.pop(i))
                        dep_libs.remove(ulib)
                    else:
                        i += 1

                # append new dependencies to the tail
                used_libs.extend(dep_libs)
                scanned_libs.add(lib)

        self.e['used_libs'] = used_libs
        self.e['cppflags'].extend(self.recursive_inc_lib_flags(self.e.incflag, used_libs))

    def run(self, args):
        self.discover(args)
        self.setup_flags(args)
        self.create_jinja(verbose=args.verbose)
        self.make('Makefile.sketch')
        self.scan_dependencies()
        self.make('Makefile')
