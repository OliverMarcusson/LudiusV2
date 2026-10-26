from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Rect(_message.Message):
    __slots__ = ("x", "y", "width", "height")
    X_FIELD_NUMBER: _ClassVar[int]
    Y_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    x: int
    y: int
    width: int
    height: int
    def __init__(self, x: _Optional[int] = ..., y: _Optional[int] = ..., width: _Optional[int] = ..., height: _Optional[int] = ...) -> None: ...

class Element(_message.Message):
    __slots__ = ("id", "key", "role", "name", "value", "enabled", "focused", "read_only", "password", "expanded", "selected", "bounds", "actions", "automation_id", "class_name", "framework_id", "ancestor_path", "hints", "window", "keyboard_focusable")
    ID_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    FOCUSED_FIELD_NUMBER: _ClassVar[int]
    READ_ONLY_FIELD_NUMBER: _ClassVar[int]
    PASSWORD_FIELD_NUMBER: _ClassVar[int]
    EXPANDED_FIELD_NUMBER: _ClassVar[int]
    SELECTED_FIELD_NUMBER: _ClassVar[int]
    BOUNDS_FIELD_NUMBER: _ClassVar[int]
    ACTIONS_FIELD_NUMBER: _ClassVar[int]
    AUTOMATION_ID_FIELD_NUMBER: _ClassVar[int]
    CLASS_NAME_FIELD_NUMBER: _ClassVar[int]
    FRAMEWORK_ID_FIELD_NUMBER: _ClassVar[int]
    ANCESTOR_PATH_FIELD_NUMBER: _ClassVar[int]
    HINTS_FIELD_NUMBER: _ClassVar[int]
    WINDOW_FIELD_NUMBER: _ClassVar[int]
    KEYBOARD_FOCUSABLE_FIELD_NUMBER: _ClassVar[int]
    id: int
    key: str
    role: str
    name: str
    value: str
    enabled: bool
    focused: bool
    read_only: bool
    password: bool
    expanded: bool
    selected: bool
    bounds: Rect
    actions: _containers.RepeatedScalarFieldContainer[str]
    automation_id: str
    class_name: str
    framework_id: str
    ancestor_path: str
    hints: _containers.RepeatedScalarFieldContainer[str]
    window: int
    keyboard_focusable: bool
    def __init__(self, id: _Optional[int] = ..., key: _Optional[str] = ..., role: _Optional[str] = ..., name: _Optional[str] = ..., value: _Optional[str] = ..., enabled: _Optional[bool] = ..., focused: _Optional[bool] = ..., read_only: _Optional[bool] = ..., password: _Optional[bool] = ..., expanded: _Optional[bool] = ..., selected: _Optional[bool] = ..., bounds: _Optional[_Union[Rect, _Mapping]] = ..., actions: _Optional[_Iterable[str]] = ..., automation_id: _Optional[str] = ..., class_name: _Optional[str] = ..., framework_id: _Optional[str] = ..., ancestor_path: _Optional[str] = ..., hints: _Optional[_Iterable[str]] = ..., window: _Optional[int] = ..., keyboard_focusable: _Optional[bool] = ...) -> None: ...

class Window(_message.Message):
    __slots__ = ("handle", "title", "class_name", "process_name", "pid", "framework_id", "bounds", "kind", "modal", "unresponsive", "elevated_unreadable", "no_tree")
    class Kind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        KIND_UNSPECIFIED: _ClassVar[Window.Kind]
        FOREGROUND: _ClassVar[Window.Kind]
        OWNED: _ClassVar[Window.Kind]
        POPUP: _ClassVar[Window.Kind]
        SHELL: _ClassVar[Window.Kind]
    KIND_UNSPECIFIED: Window.Kind
    FOREGROUND: Window.Kind
    OWNED: Window.Kind
    POPUP: Window.Kind
    SHELL: Window.Kind
    HANDLE_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CLASS_NAME_FIELD_NUMBER: _ClassVar[int]
    PROCESS_NAME_FIELD_NUMBER: _ClassVar[int]
    PID_FIELD_NUMBER: _ClassVar[int]
    FRAMEWORK_ID_FIELD_NUMBER: _ClassVar[int]
    BOUNDS_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    MODAL_FIELD_NUMBER: _ClassVar[int]
    UNRESPONSIVE_FIELD_NUMBER: _ClassVar[int]
    ELEVATED_UNREADABLE_FIELD_NUMBER: _ClassVar[int]
    NO_TREE_FIELD_NUMBER: _ClassVar[int]
    handle: int
    title: str
    class_name: str
    process_name: str
    pid: int
    framework_id: str
    bounds: Rect
    kind: Window.Kind
    modal: bool
    unresponsive: bool
    elevated_unreadable: bool
    no_tree: bool
    def __init__(self, handle: _Optional[int] = ..., title: _Optional[str] = ..., class_name: _Optional[str] = ..., process_name: _Optional[str] = ..., pid: _Optional[int] = ..., framework_id: _Optional[str] = ..., bounds: _Optional[_Union[Rect, _Mapping]] = ..., kind: _Optional[_Union[Window.Kind, str]] = ..., modal: _Optional[bool] = ..., unresponsive: _Optional[bool] = ..., elevated_unreadable: _Optional[bool] = ..., no_tree: _Optional[bool] = ...) -> None: ...

class Snapshot(_message.Message):
    __slots__ = ("seq", "guest_time_ms", "screen_width", "screen_height", "dpi", "windows", "elements", "context", "settled", "settle_ms", "busy_cursor", "indeterminate_progress", "locked", "cursor_x", "cursor_y", "dropped_elements", "user", "apps")
    SEQ_FIELD_NUMBER: _ClassVar[int]
    GUEST_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    SCREEN_WIDTH_FIELD_NUMBER: _ClassVar[int]
    SCREEN_HEIGHT_FIELD_NUMBER: _ClassVar[int]
    DPI_FIELD_NUMBER: _ClassVar[int]
    WINDOWS_FIELD_NUMBER: _ClassVar[int]
    ELEMENTS_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    SETTLED_FIELD_NUMBER: _ClassVar[int]
    SETTLE_MS_FIELD_NUMBER: _ClassVar[int]
    BUSY_CURSOR_FIELD_NUMBER: _ClassVar[int]
    INDETERMINATE_PROGRESS_FIELD_NUMBER: _ClassVar[int]
    LOCKED_FIELD_NUMBER: _ClassVar[int]
    CURSOR_X_FIELD_NUMBER: _ClassVar[int]
    CURSOR_Y_FIELD_NUMBER: _ClassVar[int]
    DROPPED_ELEMENTS_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    APPS_FIELD_NUMBER: _ClassVar[int]
    seq: int
    guest_time_ms: int
    screen_width: int
    screen_height: int
    dpi: int
    windows: _containers.RepeatedCompositeFieldContainer[Window]
    elements: _containers.RepeatedCompositeFieldContainer[Element]
    context: _containers.RepeatedScalarFieldContainer[str]
    settled: bool
    settle_ms: int
    busy_cursor: bool
    indeterminate_progress: bool
    locked: bool
    cursor_x: int
    cursor_y: int
    dropped_elements: int
    user: str
    apps: _containers.RepeatedCompositeFieldContainer[Window]
    def __init__(self, seq: _Optional[int] = ..., guest_time_ms: _Optional[int] = ..., screen_width: _Optional[int] = ..., screen_height: _Optional[int] = ..., dpi: _Optional[int] = ..., windows: _Optional[_Iterable[_Union[Window, _Mapping]]] = ..., elements: _Optional[_Iterable[_Union[Element, _Mapping]]] = ..., context: _Optional[_Iterable[str]] = ..., settled: _Optional[bool] = ..., settle_ms: _Optional[int] = ..., busy_cursor: _Optional[bool] = ..., indeterminate_progress: _Optional[bool] = ..., locked: _Optional[bool] = ..., cursor_x: _Optional[int] = ..., cursor_y: _Optional[int] = ..., dropped_elements: _Optional[int] = ..., user: _Optional[str] = ..., apps: _Optional[_Iterable[_Union[Window, _Mapping]]] = ...) -> None: ...

class ObserverRequest(_message.Message):
    __slots__ = ("id", "snapshot", "ping")
    ID_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    PING_FIELD_NUMBER: _ClassVar[int]
    id: int
    snapshot: SnapshotRequest
    ping: Ping
    def __init__(self, id: _Optional[int] = ..., snapshot: _Optional[_Union[SnapshotRequest, _Mapping]] = ..., ping: _Optional[_Union[Ping, _Mapping]] = ...) -> None: ...

class SnapshotRequest(_message.Message):
    __slots__ = ("quiet_ms", "timeout_ms", "wait_settle")
    QUIET_MS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    WAIT_SETTLE_FIELD_NUMBER: _ClassVar[int]
    quiet_ms: int
    timeout_ms: int
    wait_settle: bool
    def __init__(self, quiet_ms: _Optional[int] = ..., timeout_ms: _Optional[int] = ..., wait_settle: _Optional[bool] = ...) -> None: ...

class Ping(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Hello(_message.Message):
    __slots__ = ("version", "user", "session_id", "ui_access", "pixel_settle", "computer")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    UI_ACCESS_FIELD_NUMBER: _ClassVar[int]
    PIXEL_SETTLE_FIELD_NUMBER: _ClassVar[int]
    COMPUTER_FIELD_NUMBER: _ClassVar[int]
    version: str
    user: str
    session_id: int
    ui_access: bool
    pixel_settle: bool
    computer: str
    def __init__(self, version: _Optional[str] = ..., user: _Optional[str] = ..., session_id: _Optional[int] = ..., ui_access: _Optional[bool] = ..., pixel_settle: _Optional[bool] = ..., computer: _Optional[str] = ...) -> None: ...

class ObserverResponse(_message.Message):
    __slots__ = ("id", "hello", "snapshot", "error")
    ID_FIELD_NUMBER: _ClassVar[int]
    HELLO_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    id: int
    hello: Hello
    snapshot: Snapshot
    error: str
    def __init__(self, id: _Optional[int] = ..., hello: _Optional[_Union[Hello, _Mapping]] = ..., snapshot: _Optional[_Union[Snapshot, _Mapping]] = ..., error: _Optional[str] = ...) -> None: ...
