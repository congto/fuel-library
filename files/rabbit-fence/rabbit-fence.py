#!/usr/bin/env python

#  Licensed under the Apache License, Version 2.0 (the "License"); you may
#  not use this file except in compliance with the License. You may obtain
#  a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#  License for the specific language governing permissions and limitations
#  under the License.

from __future__ import print_function

usage = """Help: this daemon fences dead rabbitmq nodes"""

import daemon
import dbus
import dbus.decorators
import dbus.mainloop.glib
import fcntl
import gobject
import logging
import logging.handlers
import os
import pwd
import re
import signal
import socket
import subprocess
import sys
import time

USER = 'rabbitmq'
MAIL = '/var/spool/mail/rabbitmq'
PWD = '/var/lib/rabbitmq'
HOME = '/var/lib/rabbitmq'
LOGNAME = 'rabbitmq'


def catchall_signal_lh(*args, **kwargs):

    def bash_command(cmd):
        p = subprocess.Popen(cmd, env=env, shell=True,
                             stderr=subprocess.PIPE,
                             stdout=subprocess.PIPE)
        out, err = p.communicate()
        my_logger.debug('Command %s' % cmd)
        if out != '':
            my_logger.debug('  Stdout: %s' % out)
        if err != '':
            my_logger.debug('  Stderr: %s' % err)
        return out.strip()

    message = kwargs['message']
    action = args[3]
    if kwargs['type'] == 'NodeStateChange' and action == 'left':
        node = args[0]
        this_node = socket.gethostname().split('.')[0]
        node_name = node.split('.')[0]

        # We're looking for a line like "NODENAME=rabbit@messaging-node-6"
        with open('/etc/rabbitmq/rabbitmq-env.conf', 'r') as fl:
            node_to_remove = re.findall('^\s*NODENAME\s*=\s*(\S*)\s*$',
                                        fl.read(), re.MULTILINE)[0]

        my_logger.info("Got %s that left cluster" % node)
        my_logger.debug(kwargs)
        for arg in message.get_args_list():
            my_logger.debug("        " + str(arg))
        my_logger.info("Preparing to fence node %s from rabbit cluster"
                       % node_to_remove)

        if node == '' or re.search('\\b%s\\b' % this_node, node_name):
            my_logger.debug('Ignoring the node %s' % node_to_remove)
            return

        # NOTE(bogdando) when the rabbit node went down, its status
        # remains 'running' for a while, so few retries are required
        count = 0
        while True:
            cmd = ('rabbitmqctl eval '
                   '"mnesia:system_info(running_db_nodes)."'
                   '| grep -o %s') % node_to_remove
            results = bash_command(cmd)
            is_running = results != ''

            if not is_running or count >= 5:
                break

            count += 1
            time.sleep(10)

        if is_running:
            my_logger.warn('Ignoring alive node %s' % node_to_remove)
            return

        cmd = ('rabbitmqctl eval '
               '"mnesia:system_info(db_nodes)."'
               '| grep -o %s') % node_to_remove

        results = bash_command(cmd)
        in_cluster = results != ''

        if not in_cluster:
            my_logger.debug('Ignoring forgotten node %s' % node_to_remove)
            return

        my_logger.info('Disconnecting node %s' % node_to_remove)
        cmd = ('rabbitmqctl eval "disconnect_node'
               '(list_to_atom(\\"%s\\"))."') % node_to_remove
        bash_command(cmd)

        my_logger.info('Forgetting cluster node %s' % node_to_remove)
        cmd = 'rabbitmqctl forget_cluster_node %s' % node_to_remove
        bash_command(cmd)


def sigterm_handler(_signo, _stack_frame):
    my_logger.info("Caught SIGTERM, terminating...")
    sys.exit(0)


def acquire_lock(lock_file, logger=None):
    global lock_file_obj
    lock_file_obj = open(lock_file, "w")
    try:
        fcntl.lockf(lock_file_obj, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except IOError:
        msg = "Another copy of fuel-rabbit-fence is running!"
        if logger:
            my_logger.exception(msg)
        else:
            print(msg, file=sys.stderr)
        return False


def main():
    lock_file = '/var/run/rabbitmq/rabbit-fence.lock'
    if not acquire_lock(lock_file, logger=my_logger):
        sys.exit(1)

    my_logger.info('Starting rabbit fence script main loop')
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    bus = dbus.SystemBus()
    try:
        bus.get_object("org.freedesktop.DBus", "/org/corosync")
    except dbus.DBusException:
        my_logger.exception("Cannot get the DBus object")
        sys.exit(1)

    bus.add_signal_receiver(catchall_signal_lh,
                            member_keyword="type",
                            message_keyword="message",
                            dbus_interface="org.corosync")
    signal.signal(signal.SIGTERM, sigterm_handler)
    loop = gobject.MainLoop()
    loop.run()


if __name__ == '__main__':
    my_logger = logging.getLogger('rabbit-fence')
    my_logger.setLevel(logging.DEBUG)
    lh = logging.handlers.SysLogHandler(address='/dev/log',
                                        facility='daemon')
    formatter = logging.Formatter('%(name)-12s '
                                  '%(asctime)s '
                                  '%(levelname)-8s '
                                  '%(message)s')
    lh.setFormatter(formatter)
    my_logger.addHandler(lh)

    rabbit_pwd = pwd.getpwnam('rabbitmq')
    uid = rabbit_pwd.pw_uid
    gid = rabbit_pwd.pw_gid
    env = os.environ.copy()
    env['USER'] = USER
    env['MAIL'] = MAIL
    env['PWD'] = PWD
    env['HOME'] = HOME
    env['LOGNAME'] = LOGNAME

    try:
        with daemon.DaemonContext(files_preserve=[lh.socket.fileno()],
                                  uid=uid, gid=gid, umask=0o022,
                                  detach_process=True):
            main()
    except Exception:
        my_logger.exception("A generic exception caught!")
        sys.exit(1)
