"""Fresh, local observations of a recorder loop, not native-runtime readiness.

One nonblocking Linux abstract datagram query, with no retry or stored-health
fallback. Importing this module creates no sockets or process observations.
"""

from __future__ import annotations

from dataclasses import asdict
import errno
import os
import select
import socket
import struct
import sys
import time
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt,
    TypeAdapter, field_validator, model_validator,
)

from ..runner.models import ProcessFingerprint, RunnerError
from ..runner.process import fingerprint, verify_process
from .spool import MAX_OFFSET, RunBinding, _uuid


MAX_DATAGRAM_BYTES = 8192
MAX_REPLIES = 8
_CREDENTIALS = struct.Struct("iII")  # Linux struct ucred: signed pid, unsigned uid/gid.
_Address = Annotated[str, Field(strict=True, min_length=1, max_length=80,
                                pattern=r"\A[A-Za-z0-9_.-]+\z")]
_SourceId = Annotated[str, Field(strict=True, min_length=36, max_length=36), AfterValidator(_uuid)]
_Timeout = Annotated[StrictInt, Field(ge=1, le=1000)]
Reason = Literal["active", "inactive", "unavailable", "target_changed", "invalid_response", "unsupported"]


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ProbeRequest(_Model):
    version: Annotated[StrictInt, Field(ge=1, le=1)]
    nonce: UUID
    binding: RunBinding
    source_id: _SourceId
    source_epoch: _SourceId


class ProbeObservation(ProbeRequest):
    recorder: ProcessFingerprint
    child: ProcessFingerprint | None
    terminal_active: StrictBool

    @field_validator("recorder", "child")
    @classmethod
    def bounded_fingerprint(cls, value):
        if value is not None and any(
            number > MAX_OFFSET for number in
            (value.pid, value.start_ticks, value.uid, value.pid_namespace)
        ):
            raise ValueError("process coordinate exceeds the supported range")
        return value

    @model_validator(mode="after")
    def coherent_observation(self):
        if str(self.recorder.boot_id) != self.binding.boot_id:
            raise ValueError("recorder boot does not match capture binding")
        if self.child is not None and self.child.boot_id != self.recorder.boot_id:
            raise ValueError("child boot does not match recorder")
        if self.terminal_active and self.child is None:
            raise ValueError("active capture requires an observed direct child")
        return self


class ProbeResult(_Model):
    verified: StrictBool
    terminal_active: StrictBool
    observation: ProbeObservation | None = None
    reason: Reason

    @model_validator(mode="after")
    def coherent_result(self):
        if self.verified:
            if (self.observation is None
                    or self.terminal_active != self.observation.terminal_active
                    or self.reason != ("active" if self.terminal_active else "inactive")):
                raise ValueError("verified result requires a matching observation")
        elif self.terminal_active or self.observation is not None or self.reason in {"active", "inactive"}:
            raise ValueError("unverified result cannot claim an observation")
        return self


def _address(name: str) -> str:
    return "\0qorqut-recorder-" + TypeAdapter(_Address).validate_python(name)


def _supported() -> bool:
    return sys.platform == "linux" and all(hasattr(socket, name) for name in (
        "AF_UNIX", "SOCK_DGRAM", "SO_PASSCRED", "SCM_CREDENTIALS", "MSG_TRUNC",
        "MSG_CTRUNC", "MSG_CMSG_CLOEXEC",
    ))


def _open_listener(name: str) -> socket.socket:
    address = _address(name)
    if not _supported():
        raise OSError(errno.EAFNOSUPPORT, "local recorder observation is unsupported")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        listener.setblocking(False)
        listener.bind(address)
        return listener
    except BaseException:
        listener.close()
        raise


def _receive_request(listener: socket.socket) -> tuple[ProbeRequest, bytes] | None:
    try:
        # No ancillary buffer: passed descriptors are discarded by the kernel,
        # and MSG_CTRUNC rejects any ancillary-bearing request.
        data, _, flags, peer = listener.recvmsg(MAX_DATAGRAM_BYTES, 0)
    except (BlockingIOError, InterruptedError):
        return None
    if (flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
            or not isinstance(peer, bytes) or not peer.startswith(b"\0")):
        return None
    try:
        return ProbeRequest.model_validate_json(data, strict=True), peer
    except ValueError:
        return None  # Never log or echo unvalidated request bodies.


def _send_observation(listener: socket.socket, observation: ProbeObservation, peer: bytes) -> None:
    data = observation.model_dump_json().encode()
    if len(data) > MAX_DATAGRAM_BYTES:
        return
    try:
        listener.sendto(data, peer)
    except OSError as exc:
        # A departed client, full receive queue or invalid destination is local
        # to that single observation. All other endpoint failures disable it.
        if exc.errno not in {
            errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR, errno.ENOBUFS,
            errno.ECONNREFUSED, errno.ENOENT, errno.EACCES,
        }:
            raise


def _sender_matches(ancillary, flags: int, expected: ProcessFingerprint) -> bool:
    credentials = []
    unexpected = False
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            # Even a rejected/truncated response may have installed descriptors.
            # Close only descriptors actually delivered to this receiving socket.
            for (descriptor,) in struct.iter_unpack("i", data[:len(data) // 4 * 4]):
                os.close(descriptor)
            unexpected = True
        elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
            if len(data) == _CREDENTIALS.size:
                credentials.append(_CREDENTIALS.unpack(data))
            else:
                unexpected = True
        else:
            unexpected = True
    return (not unexpected and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
            and len(credentials) == 1
            and credentials[0][:2] == (expected.pid, expected.uid))


def _failure(reason: Reason) -> ProbeResult:
    return ProbeResult(verified=False, terminal_active=False, reason=reason)


def probe_recorder(address: str, expected_binding: RunBinding, source_id: str,
                   source_epoch: str, expected_recorder: ProcessFingerprint, *,
                   timeout_ms: int = 200) -> ProbeResult:
    """Query once in the same local PID namespace; timeout is not proof of death.

    Socket waits honor one monotonic deadline. Process/filesystem inspection
    still depends on the kernel; this is neither hard real-time nor a lease.
    Invalid local arguments raise validation errors before any I/O.
    """
    endpoint = _address(address)
    timeout_ms = TypeAdapter(_Timeout).validate_python(timeout_ms)
    if not isinstance(expected_binding, RunBinding):
        raise TypeError("expected_binding must be a capture RunBinding")
    if not isinstance(expected_recorder, ProcessFingerprint):
        raise TypeError("expected_recorder must be a ProcessFingerprint")
    request = ProbeRequest(
        version=1, nonce=uuid4(), binding=RunBinding(**asdict(expected_binding)),
        source_id=source_id, source_epoch=source_epoch,
    )
    expected_recorder = ProcessFingerprint.model_validate(expected_recorder.model_dump(), strict=True)
    ProbeObservation.bounded_fingerprint(expected_recorder)
    if not _supported():
        return _failure("unsupported")
    try:
        verify_process(expected_recorder)
        if fingerprint(os.getpid()).pid_namespace != expected_recorder.pid_namespace:
            return _failure("unsupported")
    except RunnerError:
        return _failure("target_changed")
    if str(expected_recorder.boot_id) != expected_binding.boot_id:
        return _failure("target_changed")
    deadline = time.monotonic() + timeout_ms / 1000
    invalid = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.setblocking(False)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            client.bind("\0qorqut-probe-" + uuid4().hex)
            client.sendto(request.model_dump_json().encode(), endpoint)
            for _ in range(MAX_REPLIES):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([client], [], [], remaining)[0]:
                    break
                data, ancillary, flags, _ = client.recvmsg(
                    MAX_DATAGRAM_BYTES, socket.CMSG_SPACE(_CREDENTIALS.size), socket.MSG_CMSG_CLOEXEC,
                )
                if not _sender_matches(ancillary, flags, expected_recorder):
                    invalid = True
                    continue
                try:
                    observation = ProbeObservation.model_validate_json(data, strict=True)
                except ValueError:
                    invalid = True
                    continue
                if (observation.nonce != request.nonce or observation.version != request.version
                        or observation.binding != request.binding or observation.source_id != source_id
                        or observation.source_epoch != source_epoch or observation.recorder != expected_recorder):
                    invalid = True
                    continue
                try:
                    verify_process(expected_recorder)
                except RunnerError:
                    return _failure("target_changed")
                if time.monotonic() >= deadline:
                    return _failure("unavailable")
                return ProbeResult(
                    verified=True, terminal_active=observation.terminal_active,
                    observation=observation, reason="active" if observation.terminal_active else "inactive",
                )
    except OSError as exc:
        if exc.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.ENOPROTOOPT, errno.ENOSYS}:
            return _failure("unsupported")
        return _failure("unavailable")
    return _failure("invalid_response" if invalid else "unavailable")
