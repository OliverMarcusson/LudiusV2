from ludius.v1 import observation_pb2 as _observation_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Move(_message.Message):
    __slots__ = ("x", "y", "duration_ms")
    X_FIELD_NUMBER: _ClassVar[int]
    Y_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    x: int
    y: int
    duration_ms: int
    def __init__(self, x: _Optional[int] = ..., y: _Optional[int] = ..., duration_ms: _Optional[int] = ...) -> None: ...

class Click(_message.Message):
    __slots__ = ("button", "count")
    class Button(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        LEFT: _ClassVar[Click.Button]
        RIGHT: _ClassVar[Click.Button]
        MIDDLE: _ClassVar[Click.Button]
    LEFT: Click.Button
    RIGHT: Click.Button
    MIDDLE: Click.Button
    BUTTON_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    button: Click.Button
    count: int
    def __init__(self, button: _Optional[_Union[Click.Button, str]] = ..., count: _Optional[int] = ...) -> None: ...

class Key(_message.Message):
    __slots__ = ("vk", "down")
    VK_FIELD_NUMBER: _ClassVar[int]
    DOWN_FIELD_NUMBER: _ClassVar[int]
    vk: int
    down: bool
    def __init__(self, vk: _Optional[int] = ..., down: _Optional[bool] = ...) -> None: ...

class Chord(_message.Message):
    __slots__ = ("vk",)
    VK_FIELD_NUMBER: _ClassVar[int]
    vk: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, vk: _Optional[_Iterable[int]] = ...) -> None: ...

class TypeText(_message.Message):
    __slots__ = ("text", "mean_ms", "jitter_ms")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    MEAN_MS_FIELD_NUMBER: _ClassVar[int]
    JITTER_MS_FIELD_NUMBER: _ClassVar[int]
    text: str
    mean_ms: int
    jitter_ms: int
    def __init__(self, text: _Optional[str] = ..., mean_ms: _Optional[int] = ..., jitter_ms: _Optional[int] = ...) -> None: ...

class CtrlAltDel(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Scroll(_message.Message):
    __slots__ = ("notches",)
    NOTCHES_FIELD_NUMBER: _ClassVar[int]
    notches: int
    def __init__(self, notches: _Optional[int] = ...) -> None: ...

class InputRequest(_message.Message):
    __slots__ = ("vm", "move", "click", "key", "chord", "type", "ctrl_alt_del", "scroll")
    VM_FIELD_NUMBER: _ClassVar[int]
    MOVE_FIELD_NUMBER: _ClassVar[int]
    CLICK_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    CHORD_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    CTRL_ALT_DEL_FIELD_NUMBER: _ClassVar[int]
    SCROLL_FIELD_NUMBER: _ClassVar[int]
    vm: str
    move: Move
    click: Click
    key: Key
    chord: Chord
    type: TypeText
    ctrl_alt_del: CtrlAltDel
    scroll: Scroll
    def __init__(self, vm: _Optional[str] = ..., move: _Optional[_Union[Move, _Mapping]] = ..., click: _Optional[_Union[Click, _Mapping]] = ..., key: _Optional[_Union[Key, _Mapping]] = ..., chord: _Optional[_Union[Chord, _Mapping]] = ..., type: _Optional[_Union[TypeText, _Mapping]] = ..., ctrl_alt_del: _Optional[_Union[CtrlAltDel, _Mapping]] = ..., scroll: _Optional[_Union[Scroll, _Mapping]] = ...) -> None: ...

class InputResult(_message.Message):
    __slots__ = ("host_time_ms",)
    HOST_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    host_time_ms: int
    def __init__(self, host_time_ms: _Optional[int] = ...) -> None: ...

class ObserveRequest(_message.Message):
    __slots__ = ("vm", "request")
    VM_FIELD_NUMBER: _ClassVar[int]
    REQUEST_FIELD_NUMBER: _ClassVar[int]
    vm: str
    request: _observation_pb2.SnapshotRequest
    def __init__(self, vm: _Optional[str] = ..., request: _Optional[_Union[_observation_pb2.SnapshotRequest, _Mapping]] = ...) -> None: ...

class ObserveResult(_message.Message):
    __slots__ = ("snapshot", "hello", "host_time_ms")
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    HELLO_FIELD_NUMBER: _ClassVar[int]
    HOST_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    snapshot: _observation_pb2.Snapshot
    hello: _observation_pb2.Hello
    host_time_ms: int
    def __init__(self, snapshot: _Optional[_Union[_observation_pb2.Snapshot, _Mapping]] = ..., hello: _Optional[_Union[_observation_pb2.Hello, _Mapping]] = ..., host_time_ms: _Optional[int] = ...) -> None: ...

class LifecycleRequest(_message.Message):
    __slots__ = ("vm", "op", "checkpoint", "target")
    class Op(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        START: _ClassVar[LifecycleRequest.Op]
        STOP: _ClassVar[LifecycleRequest.Op]
        CHECKPOINT: _ClassVar[LifecycleRequest.Op]
        RESTORE: _ClassVar[LifecycleRequest.Op]
        STATE: _ClassVar[LifecycleRequest.Op]
        CLONE: _ClassVar[LifecycleRequest.Op]
        DELETE: _ClassVar[LifecycleRequest.Op]
    START: LifecycleRequest.Op
    STOP: LifecycleRequest.Op
    CHECKPOINT: LifecycleRequest.Op
    RESTORE: LifecycleRequest.Op
    STATE: LifecycleRequest.Op
    CLONE: LifecycleRequest.Op
    DELETE: LifecycleRequest.Op
    VM_FIELD_NUMBER: _ClassVar[int]
    OP_FIELD_NUMBER: _ClassVar[int]
    CHECKPOINT_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    vm: str
    op: LifecycleRequest.Op
    checkpoint: str
    target: str
    def __init__(self, vm: _Optional[str] = ..., op: _Optional[_Union[LifecycleRequest.Op, str]] = ..., checkpoint: _Optional[str] = ..., target: _Optional[str] = ...) -> None: ...

class LifecycleResult(_message.Message):
    __slots__ = ("state",)
    STATE_FIELD_NUMBER: _ClassVar[int]
    state: str
    def __init__(self, state: _Optional[str] = ...) -> None: ...

class ScreenshotRequest(_message.Message):
    __slots__ = ("vm",)
    VM_FIELD_NUMBER: _ClassVar[int]
    vm: str
    def __init__(self, vm: _Optional[str] = ...) -> None: ...

class ScreenshotResult(_message.Message):
    __slots__ = ("png", "width", "height")
    PNG_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    png: bytes
    width: int
    height: int
    def __init__(self, png: _Optional[bytes] = ..., width: _Optional[int] = ..., height: _Optional[int] = ...) -> None: ...
