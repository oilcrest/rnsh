from __future__ import annotations
import contextlib
import functools
import threading
import rnsh.exception as exception
import asyncio
import rnsh.process as process
import rnsh.helpers as helpers
import rnsh.protocol as protocol
import enum
from typing import TypeVar, Generic, Callable, List
from abc import abstractmethod, ABC
from multiprocessing import Manager
import os
import RNS

import logging as __logging

from rnsh.protocol import MessageOutletBase, _TReceipt, MessageState

module_logger = __logging.getLogger(__name__)

_TLink = TypeVar("_TLink")

class SEType(enum.IntEnum):
    SE_LINK_CLOSED = 0


class SessionException(Exception):
    def __init__(self, setype: SEType, msg: str, *args):
        super().__init__(msg, args)
        self.type = setype


class LSState(enum.IntEnum):
    LSSTATE_WAIT_IDENT = 1
    LSSTATE_WAIT_VERS  = 2
    LSSTATE_WAIT_CMD   = 3
    LSSTATE_RUNNING    = 4
    LSSTATE_ERROR      = 5
    LSSTATE_TEARDOWN   = 6


_TIdentity = TypeVar("_TIdentity")


class LSOutletBase(protocol.MessageOutletBase):
    @abstractmethod
    def set_initiator_identified_callback(self, cb: Callable[[LSOutletBase, _TIdentity], None]):
        raise NotImplemented()

    @abstractmethod
    def set_link_closed_callback(self, cb: Callable[[LSOutletBase], None]):
        raise NotImplemented()

    @abstractmethod
    def unset_link_closed_callback(self):
        raise NotImplemented()

    @abstractmethod
    def teardown(self):
        raise NotImplemented()

    @abstractmethod
    def __init__(self):
        raise NotImplemented()


class ListenerSession:
    sessions: List[ListenerSession] = []
    messenger: protocol.Messenger = protocol.Messenger(retry_delay_min=5)
    allowed_identity_hashes: [any] = []
    allow_all: bool = False
    allow_remote_command: bool = False
    default_command: [str] = []
    remote_cmd_as_args = False

    def __init__(self, outlet: LSOutletBase, loop: asyncio.AbstractEventLoop):
        self._log = module_logger.getChild(self.__class__.__name__)
        self._log.info(f"Session started for {outlet}")
        self.outlet = outlet
        self.outlet.set_initiator_identified_callback(self._initiator_identified)
        self.outlet.set_link_closed_callback(self._link_closed)
        self.loop = loop
        self.state: LSState = None
        self.remote_identity = None
        self.term: str | None = None
        self.stdin_is_pipe: bool = False
        self.stdout_is_pipe: bool = False
        self.stderr_is_pipe: bool = False
        self.tcflags: [any] = None
        self.cmdline: [str] = None
        self.rows: int = 0
        self.cols: int = 0
        self.hpix: int = 0
        self.vpix: int = 0
        self.stdout_buf = bytearray()
        self.stdout_eof_sent = False
        self.stderr_buf = bytearray()
        self.stderr_eof_sent = False
        self.return_code: int | None = None
        self.return_code_sent = False
        self.process: process.CallbackSubprocess | None = None
        self._set_state(LSState.LSSTATE_WAIT_IDENT)
        self.sessions.append(self)

    def _terminated(self, return_code: int):
        self.return_code = return_code

    def _set_state(self, state: LSState, timeout_factor: float = 10.0):
        timeout = max(self.outlet.rtt * timeout_factor, max(self.outlet.rtt * 2, 10)) if timeout_factor is not None else None
        self._log.debug(f"Set state: {state.name}, timeout {timeout}")
        orig_state = self.state
        self.state = state
        if timeout_factor is not None:
            self._call(functools.partial(self._check_protocol_timeout, lambda: self.state == orig_state, state.name), timeout)

    def _call(self, func: callable, delay: float = 0):
        def call_inner():
            # self._log.debug("call_inner")
            if delay == 0:
                func()
            else:
                self.loop.call_later(delay, func)
        self.loop.call_soon_threadsafe(call_inner)

    def send(self, message: protocol.Message):
        self.messenger.send(self.outlet, message)

    def _protocol_error(self, name: str):
        self.terminate(f"Protocol error ({name})")

    def _protocol_timeout_error(self, name: str):
        self.terminate(f"Protocol timeout error: {name}")

    def terminate(self, error: str = None):
        with contextlib.suppress(Exception):
            self._log.debug("Terminating session" + (f": {error}" if error else ""))
            if error and self.state != LSState.LSSTATE_TEARDOWN:
                with contextlib.suppress(Exception):
                    self.send(protocol.ErrorMessage(error, True))
            self.state = LSState.LSSTATE_ERROR
            self._terminate_process()
            self._call(self._prune, max(self.outlet.rtt * 3, 5))

    def _prune(self):
        self.state = LSState.LSSTATE_TEARDOWN
        with contextlib.suppress(ValueError):
            self.sessions.remove(self)
        with contextlib.suppress(Exception):
            self.outlet.teardown()

    def _check_protocol_timeout(self, fail_condition: Callable[[], bool], name: str):
        timeout = True
        try:
            timeout = self.state != LSState.LSSTATE_TEARDOWN and fail_condition()
        except Exception as ex:
                self._log.exception("Error in protocol timeout", ex)
        if timeout:
            self._protocol_timeout_error(name)

    def _link_closed(self, outlet: LSOutletBase):
        outlet.unset_link_closed_callback()

        if outlet != self.outlet:
            self._log.debug("Link closed received from incorrect outlet")
            return

        self._log.debug(f"link_closed {outlet}")
        self.messenger.clear_retries(self.outlet)
        self.terminate()

    def _initiator_identified(self, outlet, identity):
        if outlet != self.outlet:
            self._log.debug("Identity received from incorrect outlet")
            return

        self._log.info(f"initiator_identified {identity} on link {outlet}")
        if self.state != LSState.LSSTATE_WAIT_IDENT:
            self._protocol_error(LSState.LSSTATE_WAIT_IDENT.name)

        if not self.allow_all and identity.hash not in self.allowed_identity_hashes:
            self.terminate("Identity is not allowed.")

        self.remote_identity = identity
        self.outlet.set_packet_received_callback(self._packet_received)
        self._set_state(LSState.LSSTATE_WAIT_VERS)

    @classmethod
    async def pump_all(cls) -> True:
        processed_any = False
        for session in cls.sessions:
            processed = session.pump()
            processed_any = processed_any or processed
            await asyncio.sleep(0)


    @classmethod
    async def terminate_all(cls, reason: str):
        for session in cls.sessions:
            session.terminate(reason)
            await asyncio.sleep(0)

    def pump(self) -> bool:
        try:
            if self.state != LSState.LSSTATE_RUNNING:
                return False
            elif not self.messenger.is_outlet_ready(self.outlet):
                return False
            elif len(self.stderr_buf) > 0:
                mdu = self.outlet.mdu - 16
                data = self.stderr_buf[:mdu]
                self.stderr_buf = self.stderr_buf[mdu:]
                send_eof = self.process.stderr_eof and len(data) == 0 and not self.stderr_eof_sent
                self.stderr_eof_sent = self.stderr_eof_sent or send_eof
                msg = protocol.StreamDataMessage(protocol.StreamDataMessage.STREAM_ID_STDERR,
                                                 data, send_eof)
                self.send(msg)
                if send_eof:
                    self.stderr_eof_sent = True
                return True
            elif len(self.stdout_buf) > 0:
                mdu = self.outlet.mdu - 16
                data = self.stdout_buf[:mdu]
                self.stdout_buf = self.stdout_buf[mdu:]
                send_eof = self.process.stdout_eof and len(data) == 0 and not self.stdout_eof_sent
                self.stdout_eof_sent = self.stdout_eof_sent or send_eof
                msg = protocol.StreamDataMessage(protocol.StreamDataMessage.STREAM_ID_STDOUT,
                                                 data, send_eof)
                self.send(msg)
                if send_eof:
                    self.stdout_eof_sent = True
                return True
            elif self.return_code is not None and not self.return_code_sent:
                msg = protocol.CommandExitedMessage(self.return_code)
                self.send(msg)
                self.return_code_sent = True
                self._call(functools.partial(self._check_protocol_timeout,
                                             lambda: self.state == LSState.LSSTATE_RUNNING, "CommandExitedMessage"),
                           max(self.outlet.rtt * 5, 10))
                return False
        except Exception as ex:
            self._log.exception("Error during pump", ex)
        return False

    def _terminate_process(self):
        with contextlib.suppress(Exception):
            if self.process and self.process.running:
                self.process.terminate()

    def _start_cmd(self, cmdline: [str], pipe_stdin: bool, pipe_stdout: bool, pipe_stderr: bool, tcflags: [any],
                   term: str | None, rows: int, cols: int, hpix: int, vpix: int):

        self.cmdline = self.default_command
        if not self.allow_remote_command and cmdline and len(cmdline) > 0:
            self.terminate("Remote command line not allowed by listener")
            return

        if self.remote_cmd_as_args and cmdline and len(cmdline) > 0:
            self.cmdline.extend(cmdline)
        elif cmdline and len(cmdline) > 0:
            self.cmdline = cmdline


        self.stdin_is_pipe = pipe_stdin
        self.stdout_is_pipe = pipe_stdout
        self.stderr_is_pipe = pipe_stderr
        self.tcflags = tcflags
        self.term = term

        def stdout(data: bytes):
            self.stdout_buf.extend(data)

        def stderr(data: bytes):
            self.stderr_buf.extend(data)

        try:
            self.process = process.CallbackSubprocess(argv=self.cmdline,
                                                      env={"TERM": self.term or os.environ.get("TERM", None),
                                                            "RNS_REMOTE_IDENTITY": RNS.prettyhexrep(self.remote_identity.hash) or ""},
                                                      loop=self.loop,
                                                      stdout_callback=stdout,
                                                      stderr_callback=stderr,
                                                      terminated_callback=self._terminated,
                                                      stdin_is_pipe=self.stdin_is_pipe,
                                                      stdout_is_pipe=self.stdout_is_pipe,
                                                      stderr_is_pipe=self.stderr_is_pipe)
            self.process.start()
            self._set_window_size(rows, cols, hpix, vpix)
        except Exception as ex:
            self._log.exception(f"Unable to start process for link {self.outlet}", ex)
            self.terminate("Unable to start process")

    def _set_window_size(self, rows: int, cols: int, hpix: int, vpix: int):
        self.rows = rows
        self.cols = cols
        self.hpix = hpix
        self.vpix = vpix
        with contextlib.suppress(Exception):
            self.process.set_winsize(rows, cols, hpix, vpix)

    def _received_stdin(self, data: bytes, eof: bool):
        if data and len(data) > 0:
            self.process.write(data)
        if eof:
            self.process.close_stdin()

    def _handle_message(self, message: protocol.Message):
        if self.state == LSState.LSSTATE_WAIT_VERS:
            if not isinstance(message, protocol.VersionInfoMessage):
                self._protocol_error(self.state.name)
                return
            self._log.info(f"version {message.sw_version}, protocol {message.protocol_version} on link {self.outlet}")
            if message.protocol_version != protocol.PROTOCOL_VERSION:
                self.terminate("Incompatible protocol")
                return
            self.send(protocol.VersionInfoMessage())
            self._set_state(LSState.LSSTATE_WAIT_CMD)
            return
        elif self.state == LSState.LSSTATE_WAIT_CMD:
            if not isinstance(message, protocol.ExecuteCommandMesssage):
                return self._protocol_error(self.state.name)
            self._log.info(f"Execute command message on link {self.outlet}: {message.cmdline}")
            self._set_state(LSState.LSSTATE_RUNNING)
            self._start_cmd(message.cmdline, message.pipe_stdin, message.pipe_stdout, message.pipe_stderr,
                            message.tcflags, message.term, message.rows, message.cols, message.hpix, message.vpix)
            return
        elif self.state == LSState.LSSTATE_RUNNING:
            if isinstance(message, protocol.WindowSizeMessage):
                self._set_window_size(message.rows, message.cols, message.hpix, message.vpix)
            elif isinstance(message, protocol.StreamDataMessage):
                if message.stream_id != protocol.StreamDataMessage.STREAM_ID_STDIN:
                    self._log.error(f"Received stream data for invalid stream {message.stream_id} on link {self.outlet}")
                    return self._protocol_error(self.state.name)
                self._received_stdin(message.data, message.eof)
                return
            elif isinstance(message, protocol.NoopMessage):
                # echo noop only on listener--used for keepalive/connectivity check
                self.send(message)
                return
        elif self.state in [LSState.LSSTATE_ERROR, LSState.LSSTATE_TEARDOWN]:
            self._log.error(f"Received packet, but in state {self.state.name}")
            return
        else:
            self._protocol_error("unexpected message")
            return

    def _packet_received(self, outlet: protocol.MessageOutletBase, raw: bytes):
        if outlet != self.outlet:
            self._log.debug("Packet received from incorrect outlet")
            return

        try:
            message = self.messenger.receive(raw)
            self._handle_message(message)
        except Exception as ex:
            self._protocol_error(f"error receiving packet: {ex}")


class RNSOutlet(LSOutletBase):

    def set_initiator_identified_callback(self, cb: Callable[[LSOutletBase, _TIdentity], None]):
        def inner_cb(link, identity: _TIdentity):
            cb(self, identity)

        self.link.set_remote_identified_callback(inner_cb)

    def set_link_closed_callback(self, cb: Callable[[LSOutletBase], None]):
        def inner_cb(link):
            cb(self)

        self.link.set_link_closed_callback(inner_cb)

    def unset_link_closed_callback(self):
        self.link.set_link_closed_callback(None)

    def teardown(self):
        self.link.teardown()

    def send(self, raw: bytes) -> RNS.Packet:
        packet = RNS.Packet(self.link, raw)
        packet.send()
        return packet

    def resend(self, packet: RNS.Packet) -> RNS.Packet:
        packet.resend()
        return packet

    @property
    def mdu(self) -> int:
        return self.link.MDU

    @property
    def rtt(self) -> float:
        return self.link.rtt

    @property
    def is_usuable(self):
        return True #self.link.status in [RNS.Link.ACTIVE]

    def get_receipt_state(self, packet: RNS.Packet) -> MessageState:
        status = packet.receipt.get_status()
        if status == RNS.PacketReceipt.SENT:
            return protocol.MessageState.MSGSTATE_SENT
        if status == RNS.PacketReceipt.DELIVERED:
            return protocol.MessageState.MSGSTATE_DELIVERED
        if status == RNS.PacketReceipt.FAILED:
            return protocol.MessageState.MSGSTATE_FAILED
        else:
            raise Exception(f"Unexpected receipt state: {status}")

    def timed_out(self):
        self.link.teardown()

    def __str__(self):
        return f"Outlet RNS Link {self.link}"

    def set_packet_received_callback(self, cb: Callable[[MessageOutletBase, bytes], None]):
        def inner_cb(message, packet: RNS.Packet):
            packet.prove()
            cb(self, message)

        self.link.set_packet_callback(inner_cb)

    def __init__(self, link: RNS.Link):
        self.link = link
        link.lsoutlet = self
        link.msgoutlet = self
    @staticmethod
    def get_outlet(link: RNS.Link):
        if hasattr(link, "lsoutlet"):
            return link.lsoutlet

        return RNSOutlet(link)