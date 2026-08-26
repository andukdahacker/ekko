"""Thin ctypes wrapper over the macOS Core Audio HAL.

Enough of Core Audio to let ekko set up online capture WITHOUT the user opening
Audio MIDI Setup:

  - enumerate devices (id / uid / name / has-input / has-output)
  - get + set the system default input/output device
  - create / destroy Aggregate and Multi-Output devices

An Aggregate Device (input) combines BlackHole + your mic so one device carries
system audio and your voice. A Multi-Output Device (a "stacked" aggregate)
fans system output to BlackHole + your speakers so you still HEAR the meeting
while it's captured. Both are made with the same call — the `stacked` flag is the
only difference.

macOS only. Everything here is user-level (no sudo); installing the BlackHole
driver itself is the one step this can't do (needs `brew install blackhole-2ch`).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import struct
from dataclasses import dataclass

_ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

# --- four-char-code helpers -------------------------------------------------
def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]

kSystemObject = 1
SCOPE_GLOBAL = _fourcc("glob")
SCOPE_INPUT = _fourcc("inpt")
SCOPE_OUTPUT = _fourcc("outp")

SEL_DEVICES = _fourcc("dev#")          # kAudioHardwarePropertyDevices
SEL_DEFAULT_OUT = _fourcc("dOut")      # kAudioHardwarePropertyDefaultOutputDevice
SEL_DEFAULT_IN = _fourcc("dIn ")       # kAudioHardwarePropertyDefaultInputDevice
SEL_NAME = _fourcc("lnam")             # kAudioObjectPropertyName
SEL_UID = _fourcc("uid ")              # kAudioDevicePropertyDeviceUID
SEL_STREAMS = _fourcc("stm#")          # kAudioDevicePropertyStreams
SEL_TRANSPORT = _fourcc("tran")        # kAudioDevicePropertyTransportType
TRANSPORT_AGGREGATE = _fourcc("grup")  # kAudioDeviceTransportTypeAggregate

_UTF8 = 0x08000100
_kCFNumberSInt32Type = 3


class _AOPA(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


# --- ctypes signatures ------------------------------------------------------
_ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
_ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
_ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
_ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
_ca.AudioHardwareCreateAggregateDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_ca.AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32
_ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]

_vp = ctypes.c_void_p
for _fn in ("CFStringCreateWithCString", "CFNumberCreate", "CFDictionaryCreateMutable",
            "CFArrayCreateMutable"):
    getattr(_cf, _fn).restype = _vp
# argtypes matter on 64-bit: without them, pointer args (esp. the callback-struct
# addresses) are truncated to 32 bits and Core Audio dereferences garbage.
_cf.CFStringCreateWithCString.argtypes = [_vp, ctypes.c_char_p, ctypes.c_uint32]
_cf.CFNumberCreate.argtypes = [_vp, ctypes.c_int, _vp]
_cf.CFDictionaryCreateMutable.argtypes = [_vp, ctypes.c_long, _vp, _vp]
_cf.CFArrayCreateMutable.argtypes = [_vp, ctypes.c_long, _vp]
_cf.CFDictionarySetValue.argtypes = [_vp, _vp, _vp]
_cf.CFDictionarySetValue.restype = None
_cf.CFArrayAppendValue.argtypes = [_vp, _vp]
_cf.CFArrayAppendValue.restype = None
_cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
_cf.CFStringGetCStringPtr.argtypes = [_vp, ctypes.c_uint32]
_cf.CFStringGetCString.argtypes = [_vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]


def _sym_addr(lib, name: str) -> int:
    return ctypes.addressof(ctypes.c_char.in_dll(lib, name))


# --- CoreFoundation object builders (one-shot CLI: we don't CFRelease) -------
def _cfstr(s: str) -> ctypes.c_void_p:
    return ctypes.c_void_p(_cf.CFStringCreateWithCString(None, s.encode("utf-8"), _UTF8))


def _cfnum(n: int) -> ctypes.c_void_p:
    v = ctypes.c_int32(n)
    return ctypes.c_void_p(_cf.CFNumberCreate(None, _kCFNumberSInt32Type, ctypes.byref(v)))


def _cfdict() -> ctypes.c_void_p:
    return ctypes.c_void_p(_cf.CFDictionaryCreateMutable(
        None, 0, _sym_addr(_cf, "kCFTypeDictionaryKeyCallBacks"),
        _sym_addr(_cf, "kCFTypeDictionaryValueCallBacks")))


def _cfarray() -> ctypes.c_void_p:
    return ctypes.c_void_p(_cf.CFArrayCreateMutable(
        None, 0, _sym_addr(_cf, "kCFTypeArrayCallBacks")))


def _dset(d, key: str, value) -> None:
    _cf.CFDictionarySetValue(d, _cfstr(key), value)


# --- property reads ---------------------------------------------------------
def _aopa(selector: int, scope: int = SCOPE_GLOBAL) -> _AOPA:
    return _AOPA(selector, scope, 0)


def _get_uint32(obj: int, selector: int) -> int:
    addr = _aopa(selector)
    out = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    st = _ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None,
                                        ctypes.byref(size), ctypes.byref(out))
    if st != 0:
        raise OSError(f"AudioObjectGetPropertyData({selector:#x}) failed: {st}")
    return out.value


def _get_cfstring(obj: int, selector: int, scope: int = SCOPE_GLOBAL) -> str | None:
    addr = _aopa(selector, scope)
    ref = ctypes.c_void_p(0)
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    st = _ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None,
                                        ctypes.byref(size), ctypes.byref(ref))
    if st != 0 or not ref.value:
        return None
    ptr = _cf.CFStringGetCStringPtr(ref, _UTF8)
    if ptr:
        return ptr.decode()
    buf = ctypes.create_string_buffer(512)
    _cf.CFStringGetCString(ref, buf, 512, _UTF8)
    return buf.value.decode()


def _has_streams(obj: int, scope: int) -> bool:
    addr = _aopa(SEL_STREAMS, scope)
    size = ctypes.c_uint32(0)
    st = _ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None,
                                            ctypes.byref(size))
    return st == 0 and size.value > 0


@dataclass
class CADevice:
    id: int
    uid: str
    name: str
    has_input: bool
    has_output: bool
    is_aggregate: bool


def list_devices() -> list[CADevice]:
    addr = _aopa(SEL_DEVICES)
    size = ctypes.c_uint32(0)
    st = _ca.AudioObjectGetPropertyDataSize(kSystemObject, ctypes.byref(addr), 0, None,
                                            ctypes.byref(size))
    if st != 0:
        raise OSError(f"list devices size failed: {st}")
    n = size.value // 4
    arr = (ctypes.c_uint32 * n)()
    st = _ca.AudioObjectGetPropertyData(kSystemObject, ctypes.byref(addr), 0, None,
                                        ctypes.byref(size), arr)
    if st != 0:
        raise OSError(f"list devices failed: {st}")
    out: list[CADevice] = []
    for dev in arr:
        try:
            transport = _get_uint32(dev, SEL_TRANSPORT)
        except OSError:
            transport = 0
        out.append(CADevice(
            id=dev,
            uid=_get_cfstring(dev, SEL_UID) or "",
            name=_get_cfstring(dev, SEL_NAME) or f"device {dev}",
            has_input=_has_streams(dev, SCOPE_INPUT),
            has_output=_has_streams(dev, SCOPE_OUTPUT),
            is_aggregate=(transport == TRANSPORT_AGGREGATE),
        ))
    return out


def find_by_uid(uid: str) -> CADevice | None:
    for d in list_devices():
        if d.uid == uid:
            return d
    return None


def wait_for_uid(uid: str, *, present: bool = True, timeout: float = 3.0) -> CADevice | None:
    """Poll until a device with `uid` appears (present=True) or disappears.

    Core Audio's device-list updates arrive asynchronously after create/destroy,
    so a bare list_devices() right after a mutation can be stale by one step.
    """
    import time
    deadline = time.monotonic() + timeout
    while True:
        dev = find_by_uid(uid)
        if (dev is not None) == present:
            return dev
        if time.monotonic() >= deadline:
            return dev
        time.sleep(0.1)


def find_by_name(substr: str, *, need_input=False, need_output=False) -> CADevice | None:
    s = substr.lower()
    for d in list_devices():
        if s in d.name.lower() and (not need_input or d.has_input) \
                and (not need_output or d.has_output):
            return d
    return None


def default_device(*, output: bool) -> CADevice | None:
    sel = SEL_DEFAULT_OUT if output else SEL_DEFAULT_IN
    dev_id = _get_uint32(kSystemObject, sel)
    for d in list_devices():
        if d.id == dev_id:
            return d
    return None


def set_default_device(device_id: int, *, output: bool) -> None:
    sel = SEL_DEFAULT_OUT if output else SEL_DEFAULT_IN
    addr = _aopa(sel)
    val = ctypes.c_uint32(device_id)
    st = _ca.AudioObjectSetPropertyData(kSystemObject, ctypes.byref(addr), 0, None,
                                        4, ctypes.byref(val))
    if st != 0:
        raise OSError(f"set default {'output' if output else 'input'} failed: {st}")


# --- aggregate / multi-output creation --------------------------------------
def create_aggregate(name: str, uid: str, subdevice_uids: list[str], *,
                     master_uid: str | None = None, stacked: bool = False,
                     drift_uids: set[str] | None = None,
                     private: bool = False) -> int:
    """Create an Aggregate (stacked=False) or Multi-Output (stacked=True) device.

    Returns the new AudioObjectID. `private=True` makes an ephemeral device that
    vanishes when this process exits — used to self-test without persisting.
    """
    drift_uids = drift_uids or set()
    desc = _cfdict()
    _dset(desc, "name", _cfstr(name))                       # kAudioAggregateDeviceNameKey
    _dset(desc, "uid", _cfstr(uid))                         # kAudioAggregateDeviceUIDKey
    _dset(desc, "stacked", _cfnum(1 if stacked else 0))     # kAudioAggregateDeviceIsStackedKey
    if private:
        _dset(desc, "private", _cfnum(1))                   # kAudioAggregateDeviceIsPrivateKey
    if master_uid:
        _dset(desc, "master", _cfstr(master_uid))           # kAudioAggregateDeviceMasterSubDeviceKey

    sublist = _cfarray()
    for sd_uid in subdevice_uids:
        sd_dict = _cfdict()
        _dset(sd_dict, "uid", _cfstr(sd_uid))               # kAudioSubDeviceUIDKey
        if sd_uid in drift_uids:
            _dset(sd_dict, "drift", _cfnum(1))              # kAudioSubDeviceDriftCompensationKey
        _cf.CFArrayAppendValue(sublist, sd_dict)
    _dset(desc, "subdevices", sublist)                      # kAudioAggregateDeviceSubDeviceListKey

    out = ctypes.c_uint32(0)
    st = _ca.AudioHardwareCreateAggregateDevice(desc, ctypes.byref(out))
    if st != 0:
        raise OSError(f"create aggregate {name!r} failed: {st}")
    return out.value


def destroy_aggregate(device_id: int) -> None:
    st = _ca.AudioHardwareDestroyAggregateDevice(device_id)
    if st != 0:
        raise OSError(f"destroy aggregate {device_id} failed: {st}")
