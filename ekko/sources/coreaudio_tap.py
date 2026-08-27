"""macOS system-audio capture via Core Audio process taps (macOS 14.4+).

The modern, correct way to capture "everyone else" in an online meeting: a
process tap reads the system audio mix *in place*, so — unlike the BlackHole +
Multi-Output approach — it needs **no virtual driver, no output rerouting, and
doesn't disable your volume keys**. We combine the tap with your mic in a small
aggregate (the mic provides the driving clock; a tap-only aggregate won't run IO)
and read it with a Core Audio IOProc, downmixing to mono.

Everything here is ctypes + the Obj-C runtime (no PyObjC), consistent with
`coreaudio.py`. Used only on macOS 14.4+; older macOS falls back to BlackHole.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import platform
import queue
import struct
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from ..models import MeetingKind
from . import coreaudio as ca
from .base import AudioSource
from .ringbuffer import RingBuffer

_vp, _u32, _f32 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_float
_objc = ctypes.CDLL(ctypes.util.find_library("objc"))
ctypes.CDLL(ctypes.util.find_library("Foundation"))     # registers NSArray/NSString/NSUUID
_objc.objc_getClass.restype = _vp
_objc.objc_getClass.argtypes = [ctypes.c_char_p]
_objc.sel_registerName.restype = _vp
_objc.sel_registerName.argtypes = [ctypes.c_char_p]


def _cls(name: str):
    return _objc.objc_getClass(name.encode())


def _sel(name: str):
    return _objc.sel_registerName(name.encode())


def _msg(restype, receiver, selname, args=(), argtypes=()):
    fn = _objc.objc_msgSend
    fn.restype = restype
    fn.argtypes = [_vp, _vp] + list(argtypes)
    return fn(receiver, _sel(selname), *args)


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]


def tap_supported() -> bool:
    """True on macOS 14.4+ with the process-tap API available."""
    if not hasattr(ca._ca, "AudioHardwareCreateProcessTap"):
        return False
    try:
        parts = platform.mac_ver()[0].split(".")
        ver = (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except Exception:
        return False
    return ver >= (14, 4)


# --- tap + aggregate lifecycle ---------------------------------------------
def _create_system_tap() -> tuple[int, str, object]:
    """Create a global stereo system-audio tap. Returns (tapID, tapUID, desc)."""
    empty = _msg(_vp, _cls("NSArray"), "array")
    desc = _msg(_vp, _msg(_vp, _cls("CATapDescription"), "alloc"),
                "initStereoGlobalTapButExcludeProcesses:", (empty,), [_vp])
    _msg(None, desc, "setName:",
         (_msg(_vp, _cls("NSString"), "stringWithUTF8String:", (b"ekko-tap",),
               [ctypes.c_char_p]),), [_vp])
    ca._ca.AudioHardwareCreateProcessTap.restype = ctypes.c_int32
    ca._ca.AudioHardwareCreateProcessTap.argtypes = [_vp, ctypes.POINTER(_u32)]
    tap = _u32(0)
    st = ca._ca.AudioHardwareCreateProcessTap(desc, ctypes.byref(tap))
    if st != 0:
        raise OSError(f"AudioHardwareCreateProcessTap failed: {st}")
    uid = _msg(ctypes.c_char_p, _msg(_vp, _msg(_vp, desc, "UUID"), "UUIDString"),
               "UTF8String").decode()
    return tap.value, uid, desc


def _destroy_tap(tap_id: int) -> None:
    ca._ca.AudioHardwareDestroyProcessTap.restype = ctypes.c_int32
    ca._ca.AudioHardwareDestroyProcessTap.argtypes = [_u32]
    ca._ca.AudioHardwareDestroyProcessTap(tap_id)


def _create_tap_aggregate(tap_uid: str, mic_uid: str) -> int:
    """Private aggregate: mic sub-device (drives the clock) + the system tap."""
    d = ca._cfdict()
    ca._dset(d, "name", ca._cfstr("ekko tap capture"))
    ca._dset(d, "uid", ca._cfstr("com.ekko.tapcapture"))
    ca._dset(d, "private", ca._cfnum(1))               # ephemeral; dies with us
    ca._dset(d, "master", ca._cfstr(mic_uid))
    subs = ca._cfarray()
    sd = ca._cfdict()
    ca._dset(sd, "uid", ca._cfstr(mic_uid))
    ca._cf.CFArrayAppendValue(subs, sd)
    ca._dset(d, "subdevices", subs)
    taps = ca._cfarray()
    td = ca._cfdict()
    ca._dset(td, "uid", ca._cfstr(tap_uid))
    ca._cf.CFArrayAppendValue(taps, td)
    ca._dset(d, "taps", taps)                          # kAudioAggregateDeviceTapListKey
    out = _u32(0)
    st = ca._ca.AudioHardwareCreateAggregateDevice(d, ctypes.byref(out))
    if st != 0:
        raise OSError(f"create tap aggregate failed: {st}")
    return out.value


def _nominal_sample_rate(dev: int, default: int = 48_000) -> int:
    class AOPA(ctypes.Structure):
        _fields_ = [("sel", _u32), ("scope", _u32), ("elem", _u32)]
    addr = AOPA(_fourcc("nsrt"), _fourcc("glob"), 0)   # kAudioDevicePropertyNominalSampleRate
    val = ctypes.c_double(0)
    size = _u32(8)
    st = ca._ca.AudioObjectGetPropertyData(dev, ctypes.byref(addr), 0, None,
                                           ctypes.byref(size), ctypes.byref(val))
    return int(val.value) if st == 0 and val.value else default


class _AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", _u32), ("mDataByteSize", _u32), ("mData", _vp)]


_IOProc = ctypes.CFUNCTYPE(ctypes.c_int32, _u32, _vp, _vp, _vp, _vp, _vp, _vp)


class TapSource(AudioSource):
    """AudioSource that records system audio + mic via a process tap, mono WAV.
    No output rerouting; the user's speakers and volume are untouched."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        self._path: Path | None = None
        self._tap: int | None = None
        self._desc = None
        self._agg: int | None = None
        self._procid = _vp(0)
        self._cb = None          # keep the CFUNCTYPE alive
        self._rate = 48_000
        self._ring = RingBuffer(48_000)   # recent audio for live preview

    def latest_audio(self, seconds: float):
        return self._ring.latest(seconds)

    def read_new_audio(self, marker: int):
        return self._ring.read_from(marker)

    def start(self, kind: MeetingKind) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        self._path = self.out_dir / f"meeting-{datetime.now():%Y%m%d-%H%M%S}.wav"
        self._stop.clear()

        mic = (ca.find_by_name("MacBook Pro Microphone", need_input=True)
               or ca.find_by_name("Microphone", need_input=True)
               or ca.default_device(output=False))
        if not mic:
            raise OSError("no microphone found for tap capture")

        self._tap, tap_uid, self._desc = _create_system_tap()
        self._agg = _create_tap_aggregate(tap_uid, mic.uid)
        self._rate = _nominal_sample_rate(self._agg)
        self._ring = RingBuffer(self._rate)

        def on_io(inDev, inNow, inData, inInTime, outData, outOutTime, client):
            if not inData:
                return 0
            nbuf = ctypes.cast(inData, ctypes.POINTER(_u32))[0]

            class _ABL(ctypes.Structure):
                _fields_ = [("mNumberBuffers", _u32), ("mBuffers", _AudioBuffer * nbuf)]
            abl = ctypes.cast(inData, ctypes.POINTER(_ABL))[0]
            monos = []
            for i in range(nbuf):
                b = abl.mBuffers[i]
                if not b.mData or not b.mDataByteSize:
                    continue
                arr = np.ctypeslib.as_array((_f32 * (b.mDataByteSize // 4))
                                            .from_address(b.mData))
                if b.mNumberChannels > 1:
                    arr = arr.reshape(-1, b.mNumberChannels).mean(axis=1)
                monos.append(np.array(arr, copy=True))
            if monos:
                m = min(len(x) for x in monos)
                mixed = np.mean([x[:m] for x in monos], axis=0).astype(np.float32)
                self._q.put(mixed)
                self._ring.push(mixed)
            return 0

        self._cb = _IOProc(on_io)
        ca._ca.AudioDeviceCreateIOProcID.restype = ctypes.c_int32
        ca._ca.AudioDeviceCreateIOProcID.argtypes = [_u32, _IOProc, _vp,
                                                     ctypes.POINTER(_vp)]
        st = ca._ca.AudioDeviceCreateIOProcID(self._agg, self._cb, None,
                                              ctypes.byref(self._procid))
        if st != 0:
            raise OSError(f"AudioDeviceCreateIOProcID failed: {st}")

        self._writer = threading.Thread(target=self._drain_to_file, daemon=True)
        self._writer.start()

        ca._ca.AudioDeviceStart.restype = ctypes.c_int32
        ca._ca.AudioDeviceStart.argtypes = [_u32, _vp]
        st = ca._ca.AudioDeviceStart(self._agg, self._procid)
        if st != 0:
            raise OSError(f"AudioDeviceStart failed: {st}")

    def _drain_to_file(self) -> None:
        with sf.SoundFile(self._path, mode="w", samplerate=self._rate,
                          channels=1, subtype="PCM_16") as f:
            while not (self._stop.is_set() and self._q.empty()):
                try:
                    f.write(self._q.get(timeout=0.2))
                except queue.Empty:
                    continue

    def stop(self) -> Path:
        for fn, args in (("AudioDeviceStop", (self._agg, self._procid)),
                         ("AudioDeviceDestroyIOProcID", (self._agg, self._procid))):
            try:
                f = getattr(ca._ca, fn)
                f.restype = ctypes.c_int32
                f.argtypes = [_u32, _vp]
                f(*args)
            except Exception:
                pass
        if self._agg:
            try:
                ca.destroy_aggregate(self._agg)
            except Exception:
                pass
        if self._tap:
            _destroy_tap(self._tap)
        self._stop.set()
        if self._writer:
            self._writer.join()
        assert self._path is not None
        return self._path
