# -*- coding: utf-8; -*-

from __future__ import absolute_import

import os.path
import subprocess
import platform

from time import sleep
from serial import Serial
from serial.serialutil import SerialException

from ino.commands.base import Command
from ino.exc import Abort


class Upload(Command):
    """
    Upload built firmware to the device.

    The firmware must be already explicitly built with `ino build'. If current
    device firmare reads/writes serial port extensively, upload may fail. In
    that case try to retry few times or upload just after pushing Reset button
    on Arduino board.
    """

    name = 'upload'
    help_line = "Upload built firmware to the device"

    def setup_arg_parser(self, parser):
        super(Upload, self).setup_arg_parser(parser)
        parser.add_argument('-p', '--serial-port', metavar='PORT',
                            help='Serial port to upload firmware to\nTry to guess if not specified')

        self.e.add_board_model_arg(parser)
        self.e.add_arduino_dist_arg(parser)

    def discover(self,model):
        board = self.e.board_model(model)
        self.e.find_tool('stty', ['stty'])
        if model.startswith('teensy'):
            self.e.find_arduino_tool(board['build']['post_compile_script'], ['hardware', 'tools'])
            self.e.find_arduino_tool(board['upload']['avrdude_wrapper'], ['hardware','tools'])
        elif platform.system() == 'Linux':
            self.e.find_arduino_tool('avrdude', ['hardware', 'tools'])

            conf_places = self.e.arduino_dist_places(['hardware', 'tools'])
            conf_places.append('/etc/avrdude') # fallback to system-wide conf on Fedora
            self.e.find_file('avrdude.conf', places=conf_places)
        else:
            self.e.find_arduino_tool('avrdude', ['hardware', 'tools', 'avr', 'bin'])
            self.e.find_arduino_file('avrdude.conf', ['hardware', 'tools', 'avr', 'etc'])
    
    def run(self, args):
        self.discover(args.board_model)
        board = self.e.board_model(args.board_model)

        if args.board_model.startswith('teensy'):
            post_compile = self.e[board['build']['post_compile_script']]
            reboot = self.e[board['upload']['avrdude_wrapper']]
            # post_compile script requires:
            #  .hex filename w/o the extension
            filename = os.path.splitext(self.e.hex_filename)[0]
            #  full path to directory with the compiled .hex file 
            fullpath = os.path.realpath(self.e.build_dir)
            #  full path to the tools directory
            tooldir = self.e.find_arduino_dir('', ['hardware', 'tools'])
            subprocess.call([
                post_compile,
                '-file=' + filename,
                '-path=' + fullpath, 
                '-tools=' + tooldir
            ])
            # reboot to complete the upload
            # NOTE: this will warn the user if they need to press the reset button
            subprocess.call([
                reboot
            ])
            exit(0)
        protocol = board['upload']['protocol']
        if protocol == 'usbtiny':
            # something maybe?
            i = 0
            port = None
        else:
            port = args.serial_port or self.e.guess_serial_port()
            if protocol == 'stk500':
                # if v1 is not specifid explicitly avrdude will
                # try v2 first and fail
                protocol = 'stk500v1'

            if not os.path.exists(port):
                raise Abort("%s doesn't exist. Is Arduino connected?" % port)

            # send a hangup signal when the last process closes the tty
            file_switch = '-f' if platform.system() == 'Darwin' else '-F'
            ret = subprocess.call([self.e['stty'], file_switch, port, 'hupcl'])
            if ret:
                raise Abort("stty failed")

            # pulse on DTR
            try:
                s = Serial(port, 115200)
            except SerialException as e:
                raise Abort(str(e))
            s.setDTR(False)
            sleep(0.1)
            s.setDTR(True)
            s.close()

        # Need to do a little dance for Leonardo and derivatives:
        # open then close the port at the magic baudrate (usually 1200 bps) first
        # to signal to the sketch that it should reset into bootloader. after doing
        # this wait a moment for the bootloader to enumerate. On Windows, also must
        # deal with the fact that the COM port number changes from bootloader to
        # sketch.
        touch_port = \
                board['upload'].get('use_1200bps_touch') == 'true' or \
                board['upload']['protocol'] == 'avr109'

        if touch_port:
            new_port = None
            before = self.e.list_serial_ports()
            if port in before:
                ser = Serial()
                ser.port = port
                ser.baudrate = 1200
                ser.open()
                ser.close()

                # Scanning for available ports seems to open the port or
                # otherwise assert DTR, which would cancel the WDT reset if
                # it happened within 250 ms. So we wait until the reset should
                # have already occured before we start scanning.
                if platform.system() != 'Darwin':
                    sleep(0.3)

            elapsed = 0
            enum_delay = 0.25
            while elapsed < 10:
                now = self.e.list_serial_ports()
                diff = list(set(now) - set(before))
                if diff:
                    new_port = diff[0]
                    break

                before = now
                sleep(enum_delay)
                elapsed += enum_delay

            if not new_port:
                raise Abort("Couldn’t find a board on the selected port. "
                            "Check that you have the correct port selected. "
                            "If it is correct, try pressing the board's reset "
                            "button after initiating the upload.")

            port = new_port

        # call avrdude to upload .hex
        if port:
            subprocess.call([
                self.e['avrdude'],
                '-C', self.e['avrdude.conf'],
                '-p', board['build']['mcu'],
                '-P', port,
                '-c', protocol,
                '-b', board['upload']['speed'],
                '-D',
                '-U', 'flash:w:%s:i' % self.e['hex_path'],
            ])
        else:
            subprocess.call([
                self.e['avrdude'],
                '-C', self.e['avrdude.conf'],
                '-p', board['build']['mcu'],
            #    '-P', port,
                '-c', protocol,
                '-b', board['upload']['speed'],
                '-D',
                '-U', 'flash:w:%s:i' % self.e['hex_path'],
            ])

