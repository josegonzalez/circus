import sys
import traceback
try:
    from queue import Queue, Empty  # NOQA
except ImportError:
    from Queue import Queue, Empty  # NOQA

import zmq
from zmq.utils.jsonapi import jsonmod as json
from zmq.eventloop import ioloop, zmqstream

from circus.commands import get_commands, ok, error, errors
from circus import logger
from circus.exc import MessageError
from circus.py3compat import string_types
from circus.sighandler import SysHandler


class BaseController(object):
    """The base controller.

    Listens the zmq endpoint and knows how to decode them.  this base
    implementation doesn't handle the command execution, one would need to
    subclass and implement the execute_command method.

    """

    def __init__(self, endpoint, context, loop, check_delay=1.0):
        self.endpoint = endpoint
        self.context = context
        self.loop = loop
        self.check_delay = check_delay * 1000
        self.started = False

        self.jobs = Queue()

        # initialize the sys handler
        self._init_syshandler()

        # get registered commands
        self.commands = get_commands()

    def _init_syshandler(self):
        self.sys_hdl = SysHandler(self)

    def _init_stream(self):
        self.stream = zmqstream.ZMQStream(self.ctrl_socket, self.loop)
        self.stream.on_recv(self.handle_message)

    def initialize(self):
        # initialize controller

        # Initialize ZMQ Sockets
        self.ctrl_socket = self.context.socket(zmq.ROUTER)
        self.ctrl_socket.bind(self.endpoint)
        self.ctrl_socket.linger = 0
        self._init_stream()

    def start(self):
        self.initialize()
        self.caller = ioloop.PeriodicCallback(self.wakeup, self.check_delay,
                                              self.loop)
        self.caller.start()
        self.started = True

    def stop(self):
        if self.started:
            self.caller.stop()
            try:
                self.stream.flush()
                self.stream.close()
            except (IOError, zmq.ZMQError):
                pass
            self.ctrl_socket.close()
        self.sys_hdl.stop()

    def wakeup(self):
        job = None
        try:
            job = self.jobs.get(block=False)
        except Empty:
            pass

        if job is not None:
            self.dispatch(job)

    def add_job(self, cid, msg):
        self.jobs.put((cid, msg), False)
        self.wakeup()

    def handle_message(self, raw_msg):
        cid, msg = raw_msg
        msg = msg.strip()

        if not msg:
            self.send_response(cid, msg, "error: empty command")
        else:
            logger.debug("got message %s", msg)
            self.add_job(cid, msg)

    def dispatch(self, job):
        cid, msg = job

        try:
            json_msg = json.loads(msg)
        except ValueError:
            return self.send_error(cid, msg, "json invalid",
                                   errno=errors.INVALID_JSON)

        cmd_name = json_msg.get('command')
        properties = json_msg.get('properties', {})
        cast = json_msg.get('msg_type') == "cast"

        resp = self.execute_command(cmd_name, properties, cid, msg, cast)

        if resp is None:
            resp = ok()

        if not isinstance(resp, (dict, list,)):
            msg = "msg %r tried to send a non-dict: %s" % (msg, str(resp))
            logger.error("msg %r tried to send a non-dict: %s", msg, str(resp))
            return self.send_error(cid, msg, "server error", cast=cast,
                                   errno=errors.BAD_MSG_DATA_ERROR)

        if isinstance(resp, list):
            resp = {"results": resp}

        self.send_ok(cid, msg, resp, cast=cast)

        if cmd_name.lower() == "quit":
            if cid is not None:
                self.stream.flush()

            self.arbiter.stop()

    def send_error(self, cid, msg, reason="unknown", tb=None, cast=False,
                   errno=errors.NOT_SPECIFIED):
        resp = error(reason=reason, tb=tb, errno=errno)
        self.send_response(cid, msg, resp, cast=cast)

    def send_ok(self, cid, msg, props=None, cast=False):
        resp = ok(props)
        self.send_response(cid, msg, resp, cast=cast)

    def send_response(self, cid, msg, resp, cast=False):
        if cast:
            return

        if cid is None:
            return

        if not isinstance(resp, string_types):
            resp = json.dumps(resp)

        if isinstance(resp, unicode):
            resp = resp.encode('utf8')

        try:
            self.stream.send(cid, zmq.SNDMORE)
            self.stream.send(resp)
        except zmq.ZMQError as e:
            logger.debug("Received %r - Could not send back %r - %s", msg,
                         resp, str(e))

    def execute_command(self, cmd_name, properties, cid, msg, cast):
        raise NotImplementedError()


class Controller(BaseController):
    """A controller able to execute the commands on the given arbiter.
    """

    def __init__(self, endpoint, context, loop, arbiter, check_delay=1.0):
        self.arbiter = arbiter
        super(Controller, self).__init__(endpoint, context, loop, check_delay)

    def wakeup(self):
        super(Controller, self).wakeup()
        self.arbiter.manage_watchers()

    def execute_command(self, cmd_name, properties, cid, msg, cast):
        try:
            cmd = self.commands[cmd_name.lower()]
        except KeyError:
            error_ = "unknown command: %r" % cmd_name
            return self.send_error(cid, msg, error_, cast=cast,
                                   errno=errors.UNKNOWN_COMMAND)

        try:
            cmd.validate(properties)
            return cmd.execute(self.arbiter, properties)
        except MessageError as e:
            return self.send_error(cid, msg, str(e), cast=cast,
                                   errno=errors.MESSAGE_ERROR)
        except OSError as e:
            return self.send_error(cid, msg, str(e), cast=cast,
                                   errno=errors.OS_ERROR)
        except:
            exctype, value = sys.exc_info()[:2]
            tb = traceback.format_exc()
            reason = "command %r: %s" % (msg, value)
            logger.debug("error: command %r: %s\n\n%s", msg, value, tb)
            return self.send_error(cid, msg, reason, tb, cast=cast,
                                   errno=errors.COMMAND_ERROR)
