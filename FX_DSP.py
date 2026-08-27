#https://chat.deepseek.com/a/chat/s/ebca55cb-bc05-448e-bca3-e404f1c2cbbf

import atexit
import os
import sys
import ctypes
import json
import logging
import threading
import time
import traceback
import winreg

from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import Tk, filedialog

os.environ.setdefault("SD_ENABLE_ASIO", "1")

import numpy as np
import sounddevice as sd
from pedalboard import Pedalboard, load_plugin

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label,
    ListItem, ListView, RichLog, Select, Static,
    TabbedContent, TabPane
)


# ============================================================
# APPLICATION
# ============================================================

APP_NAME = "Python Potato FX DSP"
APP_VERSION = "16.2"

STATE_FILE = Path("fx_state.json")
PRESET_DIR = Path("presets")
LOG_DIR = Path("logs")
VST3_FOLDER = Path(r"C:\Program Files\Common Files\VST3")
MAX_FX_SLOTS = 4

PRESET_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOG_DIR / "release.log"
logger = logging.getLogger("PotatoFX")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(file_handler)


# ============================================================
# VOICEMEETER CONSTANTS
# ============================================================

VBVMR_AUDIOCALLBACK_IN = 0x00000001
VBVMR_AUDIOCALLBACK_OUT = 0x00000002
VBVMR_AUDIOCALLBACK_MAIN = 0x00000004

VBVMR_CBCOMMAND_STARTING = 1
VBVMR_CBCOMMAND_ENDING = 2
VBVMR_CBCOMMAND_CHANGE = 3
VBVMR_CBCOMMAND_BUFFER_IN = 10
VBVMR_CBCOMMAND_BUFFER_OUT = 11
VBVMR_CBCOMMAND_BUFFER_MAIN = 20

VM_TYPE_STANDARD = 1
VM_TYPE_BANANA = 2
VM_TYPE_POTATO = 3

VM_NAMES = {
    VM_TYPE_STANDARD: "Voicemeeter",
    VM_TYPE_BANANA: "Voicemeeter Banana",
    VM_TYPE_POTATO: "Voicemeeter Potato",
}

RUN_IDS_64 = {VM_TYPE_STANDARD: 4, VM_TYPE_BANANA: 5, VM_TYPE_POTATO: 6}
RUN_IDS_32 = {VM_TYPE_STANDARD: 1, VM_TYPE_BANANA: 2, VM_TYPE_POTATO: 3}


# ============================================================
# CHANNEL MAP (8 КАНАЛОВ ДЛЯ ВСЕХ ВИРТУАЛЬНЫХ И BUS)
# ============================================================

def input_targets_for_vm(vm_type):
    if vm_type == VM_TYPE_STANDARD:
        return {
            "IN1": {"start": 0, "channels": 2}, "IN2": {"start": 2, "channels": 2},
            "VAIO": {"start": 4, "channels": 8},
        }
    elif vm_type == VM_TYPE_BANANA:
        return {
            "IN1": {"start": 0, "channels": 2}, "IN2": {"start": 2, "channels": 2},
            "IN3": {"start": 4, "channels": 2}, "VAIO": {"start": 6, "channels": 8},
            "AUX": {"start": 14, "channels": 8},
        }
    elif vm_type == VM_TYPE_POTATO:
        return {
            "IN1": {"start": 0, "channels": 2}, "IN2": {"start": 2, "channels": 2},
            "IN3": {"start": 4, "channels": 2}, "IN4": {"start": 6, "channels": 2},
            "IN5": {"start": 8, "channels": 2}, "VAIO": {"start": 10, "channels": 8},
            "AUX": {"start": 18, "channels": 8}, "VAIO3": {"start": 26, "channels": 8},
        }
    return {}

def output_targets_for_vm(vm_type):
    if vm_type == VM_TYPE_STANDARD:
        return {
            "A1": {"start": 0, "channels": 8}, "B1": {"start": 8, "channels": 8},
        }
    elif vm_type == VM_TYPE_BANANA:
        return {
            "A1": {"start": 0, "channels": 8}, "A2": {"start": 8, "channels": 8},
            "A3": {"start": 16, "channels": 8}, "B1": {"start": 24, "channels": 8},
            "B2": {"start": 32, "channels": 8},
        }
    elif vm_type == VM_TYPE_POTATO:
        return {
            "A1": {"start": 0, "channels": 8}, "A2": {"start": 8, "channels": 8},
            "A3": {"start": 16, "channels": 8}, "A4": {"start": 24, "channels": 8},
            "A5": {"start": 32, "channels": 8}, "B1": {"start": 40, "channels": 8},
            "B2": {"start": 48, "channels": 8}, "B3": {"start": 56, "channels": 8},
        }
    return {}

def target_list_for_vm(vm_type):
    if vm_type == VM_TYPE_STANDARD:
        return {"inputs": ["IN1", "IN2", "VAIO"], "outputs": ["A1", "B1"]}
    if vm_type == VM_TYPE_BANANA:
        return {"inputs": ["IN1", "IN2", "IN3", "VAIO", "AUX"], "outputs": ["A1", "A2", "A3", "B1", "B2"]}
    if vm_type == VM_TYPE_POTATO:
        return {"inputs": ["IN1", "IN2", "IN3", "IN4", "IN5", "VAIO", "AUX", "VAIO3"], 
                "outputs": ["A1", "A2", "A3", "A4", "A5", "B1", "B2", "B3"]}
    return {"inputs": [], "outputs": []}


# ============================================================
# AUDIO STRUCTURES
# ============================================================

class VBVMR_AUDIOINFO(ctypes.Structure):
    _fields_ = [("samplerate", ctypes.c_long), ("nbSamplePerFrame", ctypes.c_long)]

class VBVMR_AUDIOBUFFER(ctypes.Structure):
    _fields_ = [
        ("audiobuffer_sr", ctypes.c_long),
        ("audiobuffer_nbs", ctypes.c_long),
        ("audiobuffer_nbi", ctypes.c_long),
        ("audiobuffer_nbo", ctypes.c_long),
        ("audiobuffer_r", ctypes.POINTER(ctypes.c_float) * 128),
        ("audiobuffer_w", ctypes.POINTER(ctypes.c_float) * 128),
    ]


# ============================================================
# FX CHAIN
# ============================================================

class FXChain:
    def __init__(self, target):
        self.target = target
        self.slots = [None] * MAX_FX_SLOTS
        self.bypass = False

    def has_plugins(self):
        return any(entry is not None for entry in self.slots)

    def build_board(self):
        if self.bypass:
            return None
        plugins = [entry["plugin"] for entry in self.slots if entry is not None]
        if not plugins:
            return None
        return Pedalboard(plugins)

    def add(self, slot, plugin, path):
        if not 1 <= slot <= MAX_FX_SLOTS:
            raise ValueError(f"Slot must be 1-{MAX_FX_SLOTS}")
        self.slots[slot - 1] = {"plugin": plugin, "path": str(path)}

    def remove(self, slot):
        if not 1 <= slot <= MAX_FX_SLOTS:
            raise ValueError(f"Slot must be 1-{MAX_FX_SLOTS}")
        self.slots[slot - 1] = None

    def clear(self):
        self.slots = [None] * MAX_FX_SLOTS

    def toggle_bypass(self):
        self.bypass = not self.bypass
        return self.bypass

    def find_free_slot(self):
        for index, entry in enumerate(self.slots):
            if entry is None:
                return index + 1
        return None


# ============================================================
# DSP SNAPSHOT
# ============================================================

class DSPSnapshot:
    def __init__(self, input_boards=None, output_boards=None):
        self.input = input_boards if input_boards is not None else {}
        self.output = output_boards if output_boards is not None else {}


# ============================================================
# DSP ENGINE
# ============================================================

class DSPEngine:
    def __init__(self):
        self.sample_rate = 48000
        self.block_size = 0
        self.running = False
        self.last_error = None

        self.callback_count = 0
        self.input_callback_count = 0
        self.output_callback_count = 0
        self.main_callback_count = 0

        self.audio_lock = threading.RLock()
        
        self.vm_type = VM_TYPE_POTATO
        self.input_chains = {name: FXChain(name) for name in input_targets_for_vm(self.vm_type)}
        self.output_chains = {name: FXChain(name) for name in output_targets_for_vm(self.vm_type)}
        self.snapshot = DSPSnapshot()
        self.snapshot_lock = threading.Lock()

    def set_vm_type(self, vm_type):
        self.vm_type = vm_type
        with self.audio_lock:
            self.input_chains = {name: FXChain(name) for name in input_targets_for_vm(vm_type)}
            self.output_chains = {name: FXChain(name) for name in output_targets_for_vm(vm_type)}
            self.rebuild_snapshot()

    def get_chain(self, target):
        target = target.upper()
        if target in self.input_chains:
            return self.input_chains[target]
        if target in self.output_chains:
            return self.output_chains[target]
        raise ValueError(f"Unknown target: {target}")

    def build_snapshot_from_chains(self, input_chains, output_chains):
        input_boards = {}
        output_boards = {}
        for name, chain in input_chains.items():
            if chain.has_plugins():
                input_boards[name] = chain.build_board()
        for name, chain in output_chains.items():
            if chain.has_plugins():
                output_boards[name] = chain.build_board()
        return DSPSnapshot(input_boards, output_boards)

    def swap_snapshot(self, snapshot):
        with self.snapshot_lock:
            self.snapshot = snapshot

    def rebuild_snapshot(self):
        with self.audio_lock:
            snapshot = self.build_snapshot_from_chains(self.input_chains, self.output_chains)
            self.swap_snapshot(snapshot)

    def add_plugin(self, target, slot, path):
        chain = self.get_chain(target)
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"VST3 not found:\n{path}")
        plugin = load_plugin(str(path))
        if not getattr(plugin, "is_effect", False):
            raise TypeError(f"{plugin.name} is not an audio effect")

        with self.audio_lock:
            old_entry = chain.slots[slot - 1]
            chain.add(slot, plugin, path)
            try:
                self.rebuild_snapshot()
            except Exception:
                chain.slots[slot - 1] = old_entry
                self.rebuild_snapshot()
                raise
        return plugin

    def remove_plugin(self, target, slot):
        chain = self.get_chain(target)
        with self.audio_lock:
            old_entry = chain.slots[slot - 1]
            chain.remove(slot)
            try:
                self.rebuild_snapshot()
            except Exception:
                chain.slots[slot - 1] = old_entry
                self.rebuild_snapshot()
                raise

    def clear_chain(self, target):
        chain = self.get_chain(target)
        with self.audio_lock:
            old_slots = list(chain.slots)
            old_bypass = chain.bypass
            chain.clear()
            try:
                self.rebuild_snapshot()
            except Exception:
                chain.slots = old_slots
                chain.bypass = old_bypass
                self.rebuild_snapshot()
                raise

    def toggle_bypass(self, target):
        chain = self.get_chain(target)
        with self.audio_lock:
            old_bypass = chain.bypass
            state = chain.toggle_bypass()
            try:
                self.rebuild_snapshot()
            except Exception:
                chain.bypass = old_bypass
                self.rebuild_snapshot()
                raise
        return state

    def move_plugin(self, target, src_slot, dst_slot):
        chain = self.get_chain(target)
        if not (1 <= src_slot <= MAX_FX_SLOTS and 1 <= dst_slot <= MAX_FX_SLOTS):
            raise ValueError(f"Slot must be 1-{MAX_FX_SLOTS}")
        if src_slot == dst_slot:
            return
        with self.audio_lock:
            old_src = chain.slots[src_slot - 1]
            old_dst = chain.slots[dst_slot - 1]
            chain.slots[src_slot - 1], chain.slots[dst_slot - 1] = old_dst, old_src
            try:
                self.rebuild_snapshot()
            except Exception:
                chain.slots[src_slot - 1] = old_src
                chain.slots[dst_slot - 1] = old_dst
                self.rebuild_snapshot()
                raise

    @staticmethod
    def pointer_to_array(pointer, samples):
        if not pointer or samples <= 0:
            return None
        return np.ctypeslib.as_array(pointer, shape=(samples,))

    def process_stereo(self, board, left_in, right_in, left_out, right_out, samples, sample_rate):
        left_input = self.pointer_to_array(left_in, samples)
        right_input = self.pointer_to_array(right_in, samples)
        left_output = self.pointer_to_array(left_out, samples)
        right_output = self.pointer_to_array(right_out, samples)

        if any(v is None for v in (left_input, right_input, left_output, right_output)):
            return
        if board is None:
            left_output[:] = left_input
            right_output[:] = right_input
            return

        try:
            audio = np.empty((2, samples), dtype=np.float32)
            audio[0] = left_input
            audio[1] = right_input
            processed = board(audio, sample_rate, reset=False)
            if (not isinstance(processed, np.ndarray) or processed.ndim != 2 or
                processed.shape[0] < 2 or processed.shape[1] != samples):
                raise RuntimeError(f"Unexpected Pedalboard output shape: {getattr(processed, 'shape', None)}")
            left_output[:] = processed[0]
            right_output[:] = processed[1]
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("DSP processing error: %s\n%s", exc, traceback.format_exc())
            left_output[:] = left_input
            right_output[:] = right_input

    def process_input_target(self, target, buffer, snapshot):
        input_map = input_targets_for_vm(self.vm_type)
        info = input_map[target]
        start = info["start"]
        channels = info["channels"]
        nbi = int(buffer.audiobuffer_nbi)
        nbo = int(buffer.audiobuffer_nbo)
        if start + channels > nbi or start + channels > nbo:
            return
        samples = int(buffer.audiobuffer_nbs)
        if samples <= 0:
            return

        for ch in range(start, start + channels):
            inp = buffer.audiobuffer_r[ch]
            out = buffer.audiobuffer_w[ch]
            if not inp or not out:
                continue
            inp_arr = self.pointer_to_array(inp, samples)
            out_arr = self.pointer_to_array(out, samples)
            if inp_arr is not None and out_arr is not None:
                out_arr[:] = inp_arr

        board = snapshot.input.get(target)
        sample_rate = int(buffer.audiobuffer_sr)
        if sample_rate <= 0:
            sample_rate = self.sample_rate
        self.process_stereo(
            board,
            buffer.audiobuffer_r[start],
            buffer.audiobuffer_r[start + 1],
            buffer.audiobuffer_w[start],
            buffer.audiobuffer_w[start + 1],
            samples,
            sample_rate,
        )

    def process_output_target(self, target, buffer, snapshot):
        output_map = output_targets_for_vm(self.vm_type)
        info = output_map[target]
        start = info["start"]
        channels = info["channels"]
        nbi = int(buffer.audiobuffer_nbi)
        nbo = int(buffer.audiobuffer_nbo)
        if start + channels > nbi or start + channels > nbo:
            return
        samples = int(buffer.audiobuffer_nbs)
        if samples <= 0:
            return

        for ch in range(start, start + channels):
            inp = buffer.audiobuffer_r[ch]
            out = buffer.audiobuffer_w[ch]
            if not inp or not out:
                continue
            inp_arr = self.pointer_to_array(inp, samples)
            out_arr = self.pointer_to_array(out, samples)
            if inp_arr is not None and out_arr is not None:
                out_arr[:] = inp_arr

        board = snapshot.output.get(target)
        sample_rate = int(buffer.audiobuffer_sr)
        if sample_rate <= 0:
            sample_rate = self.sample_rate
        self.process_stereo(
            board,
            buffer.audiobuffer_r[start],
            buffer.audiobuffer_r[start + 1],
            buffer.audiobuffer_w[start],
            buffer.audiobuffer_w[start + 1],
            samples,
            sample_rate,
        )

    def callback(self, lp_user, command, lp_data, nnn):
        try:
            if command == VBVMR_CBCOMMAND_STARTING:
                if not lp_data: return 0
                info = ctypes.cast(lp_data, ctypes.POINTER(VBVMR_AUDIOINFO)).contents
                self.sample_rate = int(info.samplerate)
                self.block_size = int(info.nbSamplePerFrame)
                self.running = True
                self.last_error = None
                return 0

            if command == VBVMR_CBCOMMAND_ENDING:
                self.running = False
                return 0

            if command == VBVMR_CBCOMMAND_CHANGE:
                if not lp_data: return 0
                info = ctypes.cast(lp_data, ctypes.POINTER(VBVMR_AUDIOINFO)).contents
                self.sample_rate = int(info.samplerate)
                self.block_size = int(info.nbSamplePerFrame)
                return 0

            if command == VBVMR_CBCOMMAND_BUFFER_IN:
                if not lp_data: return 0
                buffer = ctypes.cast(lp_data, ctypes.POINTER(VBVMR_AUDIOBUFFER)).contents
                with self.audio_lock:
                    snapshot = self.snapshot
                    for target in self.input_chains:
                        self.process_input_target(target, buffer, snapshot)
                self.input_callback_count += 1
                self.callback_count += 1
                return 0

            if command == VBVMR_CBCOMMAND_BUFFER_OUT:
                if not lp_data: return 0
                buffer = ctypes.cast(lp_data, ctypes.POINTER(VBVMR_AUDIOBUFFER)).contents
                with self.audio_lock:
                    snapshot = self.snapshot
                    for target in self.output_chains:
                        self.process_output_target(target, buffer, snapshot)
                self.output_callback_count += 1
                self.callback_count += 1
                return 0

            if command == VBVMR_CBCOMMAND_BUFFER_MAIN:
                self.main_callback_count += 1
                self.callback_count += 1
                return 0

            return 0
        except Exception as exc:
            self.last_error = str(exc)
            return 0


# ============================================================
# VST3 MANAGER
# ============================================================

class PluginManager:
    def __init__(self):
        self.plugins = {}
        self.lock = threading.Lock()

    def scan(self):
        found = {}
        if not VST3_FOLDER.is_dir():
            logger.warning("VST3 folder not found: %s", VST3_FOLDER)
            with self.lock:
                self.plugins = {}
            return
        for root, _, files in os.walk(VST3_FOLDER):
            for filename in files:
                if not filename.lower().endswith(".vst3"):
                    continue
                if "minimeters" in filename.lower():
                    continue
                path = Path(root) / filename
                found.setdefault(path.stem, str(path))
        with self.lock:
            self.plugins = found
        logger.info("VST3 scan complete: %d files", len(found))

    def names(self):
        with self.lock:
            return sorted(self.plugins.keys())

    def get_by_name(self, name):
        with self.lock:
            return self.plugins.get(name)

    def count(self):
        with self.lock:
            return len(self.plugins)


# ============================================================
# VOICEMEETER REMOTE
# ============================================================

class VoicemeeterRemote:
    CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_long)

    def __init__(self):
        self.python_bits = ctypes.sizeof(ctypes.c_void_p) * 8
        self.windows_bits = self.python_bits
        self.dll_path = self.find_dll()
        if not self.dll_path:
            raise RuntimeError("VoicemeeterRemote DLL not found.")
        self.dll = ctypes.WinDLL(self.dll_path)
        self.configure()
        self.callback_function = None
        self.logged_in = False
        self.callback_registered = False
        self.callback_started = False
        self.detected_type = None
        self.detected_version = None

    @staticmethod
    def find_dll():
        python_bits = ctypes.sizeof(ctypes.c_void_p) * 8
        dll_name = "VoicemeeterRemote64.dll" if python_bits == 64 else "VoicemeeterRemote.dll"
        candidates = [
            Path(r"C:\Program Files (x86)\VB\Voicemeeter") / dll_name,
            Path(r"C:\Program Files\VB\Voicemeeter") / dll_name,
            Path(r"C:\Program Files (x86)\VB-Audio\Voicemeeter") / dll_name,
            Path(r"C:\Program Files\VB-Audio\Voicemeeter") / dll_name,
        ]
        for path in candidates:
            if path.is_file(): return str(path)
        fallback_name = "VoicemeeterRemote.dll" if python_bits == 64 else "VoicemeeterRemote64.dll"
        for path in candidates:
            fallback_path = path.parent / fallback_name
            if fallback_path.is_file(): return str(fallback_path)
        return None

    def configure(self):
        self.dll.VBVMR_Login.argtypes = []
        self.dll.VBVMR_Login.restype = ctypes.c_long
        self.dll.VBVMR_Logout.argtypes = []
        self.dll.VBVMR_Logout.restype = ctypes.c_long
        self.dll.VBVMR_RunVoicemeeter.argtypes = [ctypes.c_long]
        self.dll.VBVMR_RunVoicemeeter.restype = ctypes.c_long
        self.dll.VBVMR_GetVoicemeeterType.argtypes = [ctypes.POINTER(ctypes.c_long)]
        self.dll.VBVMR_GetVoicemeeterType.restype = ctypes.c_long
        self.dll.VBVMR_GetVoicemeeterVersion.argtypes = [ctypes.POINTER(ctypes.c_long)]
        self.dll.VBVMR_GetVoicemeeterVersion.restype = ctypes.c_long
        self.dll.VBVMR_IsParametersDirty.argtypes = []
        self.dll.VBVMR_IsParametersDirty.restype = ctypes.c_long
        self.dll.VBVMR_AudioCallbackRegister.argtypes = [ctypes.c_long, self.CALLBACK, ctypes.c_void_p, ctypes.c_char_p]
        self.dll.VBVMR_AudioCallbackRegister.restype = ctypes.c_long
        self.dll.VBVMR_AudioCallbackStart.argtypes = []
        self.dll.VBVMR_AudioCallbackStart.restype = ctypes.c_long
        self.dll.VBVMR_AudioCallbackStop.argtypes = []
        self.dll.VBVMR_AudioCallbackStop.restype = ctypes.c_long
        self.dll.VBVMR_AudioCallbackUnregister.argtypes = []
        self.dll.VBVMR_AudioCallbackUnregister.restype = ctypes.c_long

    def get_type(self):
        value = ctypes.c_long()
        result = self.dll.VBVMR_GetVoicemeeterType(ctypes.byref(value))
        if result == 0: return int(value.value)
        return None

    def get_version(self):
        value = ctypes.c_long()
        result = self.dll.VBVMR_GetVoicemeeterVersion(ctypes.byref(value))
        if result != 0: return None
        raw = value.value & 0xFFFFFFFF
        return f"{(raw >> 24) & 0xFF}.{(raw >> 16) & 0xFF}.{(raw >> 8) & 0xFF}.{raw & 0xFF}"

    def get_type_name(self):
        return VM_NAMES.get(self.detected_type, "Unknown")

    def probe_connection(self):
        try:
            dirty = int(self.dll.VBVMR_IsParametersDirty())
            if dirty < 0: return False, None
            vm_type = self.get_type()
            if vm_type not in VM_NAMES: return False, None
            self.detected_type = vm_type
            self.detected_version = self.get_version()
            return True, vm_type
        except Exception:
            return False, None

    def run_voicemeeter(self, requested_type):
        run_map = RUN_IDS_64 if self.python_bits == 64 else RUN_IDS_32
        result = int(self.dll.VBVMR_RunVoicemeeter(run_map[requested_type]))
        if result != 0: raise RuntimeError(f"VBVMR_RunVoicemeeter returned {result}")
        time.sleep(1.0)

    def login(self, preferred_type=VM_TYPE_POTATO):
        result = int(self.dll.VBVMR_Login())
        if result == 1:
            logger.info("Voicemeeter not running; starting %s", VM_NAMES.get(preferred_type, "Voicemeeter"))
            self.run_voicemeeter(preferred_type)
            result = int(self.dll.VBVMR_Login())
        if result < 0: raise RuntimeError(f"VBVMR_Login returned {result}")
        self.logged_in = True
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            connected, vm_type = self.probe_connection()
            if connected:
                self.detected_type = vm_type
                self.detected_version = self.get_version()
                return vm_type
            time.sleep(0.25)
        raise RuntimeError("Active Voicemeeter not detected.")

    def register_callback(self, callback_func):
        if self.callback_registered: return
        self.callback_function = self.CALLBACK(callback_func)
        mode = VBVMR_AUDIOCALLBACK_IN | VBVMR_AUDIOCALLBACK_OUT
        result = int(self.dll.VBVMR_AudioCallbackRegister(mode, self.callback_function, None, APP_NAME.encode("ascii") + b"\0"))
        if result != 0:
            self.callback_function = None
            raise RuntimeError(f"AudioCallbackRegister returned {result}")
        self.callback_registered = True

    def start_callback(self):
        if not self.callback_registered: raise RuntimeError("Callback not registered")
        if self.callback_started: return
        result = int(self.dll.VBVMR_AudioCallbackStart())
        if result != 0: raise RuntimeError(f"AudioCallbackStart returned {result}")
        self.callback_started = True

    def stop_callback(self):
        if self.callback_started:
            try: self.dll.VBVMR_AudioCallbackStop()
            finally: self.callback_started = False

    def unregister_callback(self):
        if self.callback_registered:
            try: self.dll.VBVMR_AudioCallbackUnregister()
            finally: self.callback_registered = False
        self.callback_function = None

    def logout(self):
        if self.logged_in:
            try: self.dll.VBVMR_Logout()
            finally: self.logged_in = False


# ============================================================
# ROUTING PROFILE
# ============================================================

VM_STRIP_COUNTS = {VM_TYPE_STANDARD: 3, VM_TYPE_BANANA: 5, VM_TYPE_POTATO: 8}
VM_BUS_COUNTS = {
    VM_TYPE_STANDARD: {"A": 1, "B": 1},
    VM_TYPE_BANANA: {"A": 3, "B": 2},
    VM_TYPE_POTATO: {"A": 5, "B": 3},
}

class RoutingProfileManager:
    def __init__(self, vm_type): self.vm_type = vm_type
    def set_type(self, vm_type): self.vm_type = vm_type
    def strip_count(self): return VM_STRIP_COUNTS.get(self.vm_type, 0)
    def bus_counts(self): return VM_BUS_COUNTS.get(self.vm_type, {"A": 0, "B": 0})


# ============================================================
# GUI MANAGER
# ============================================================

class GUIManager:
    def __init__(self):
        self.events = {}
        self.lock = threading.Lock()
    def create_event(self, target, slot):
        key = f"{target}_{slot}"
        event = threading.Event()
        with self.lock:
            old_event = self.events.get(key)
            if old_event is not None: old_event.set()
            self.events[key] = event
        return event
    def close_window(self, target, slot):
        key = f"{target}_{slot}"
        with self.lock:
            event = self.events.pop(key, None)
        if event is not None: event.set()
    def close_all(self):
        with self.lock:
            events = list(self.events.values())
            self.events.clear()
        for event in events: event.set()

# ============================================================
# PLUGIN EDITOR WINDOW MANAGEMENT (Win32)
# ============================================================

try:
    _user32 = ctypes.windll.user32

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_long), ("rcMonitor", _RECT),
                    ("rcWork", _RECT), ("dwFlags", ctypes.c_long)]

    _WIN_CENTER_AVAILABLE = True
except Exception:
    _user32 = None
    _WIN_CENTER_AVAILABLE = False


def _list_top_level_windows():
    windows = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd):
            length = _user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buf, length + 1)
                windows.append((hwnd, buf.value))
        return True

    _user32.EnumWindows(enum_proc, 0)
    return windows


def _center_window(hwnd):
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    MONITOR_DEFAULTTONEAREST = 0x00000002
    monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not monitor:
        return
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return
    work = info.rcWork
    rect = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return
    x = work.left + max(0, (work.right - work.left - width) // 2)
    y = work.top + max(0, (work.bottom - work.top - height) // 2)
    _user32.SetWindowPos(hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER)


def _bring_to_front(hwnd):
    SW_RESTORE = 9
    _user32.ShowWindow(hwnd, SW_RESTORE)
    try:
        _user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _editor_watcher(plugin_name, window_title, before, stop):
    deadline = time.monotonic() + 6.0
    name_l = plugin_name.lower()
    centered = False
    while not stop.is_set() and time.monotonic() < deadline:
        candidates = []
        for hwnd, title in _list_top_level_windows():
            if hwnd in before:
                continue
            candidates.append((hwnd, title))
        if candidates:
            match = next((hwnd for hwnd, title in candidates if name_l and name_l in title.lower()), None)
            hwnd = match if match is not None else candidates[0][0]
            try:
                if window_title:
                    _user32.SetWindowTextW(hwnd, window_title)
                _bring_to_front(hwnd)
                _center_window(hwnd)
                centered = True
            except Exception:
                pass
        if centered:
            break
        time.sleep(0.1)


def open_plugin_gui(engine, gui_manager, target, slot):
    chain = engine.get_chain(target)
    if not 1 <= slot <= MAX_FX_SLOTS: raise ValueError("Invalid slot")
    entry = chain.slots[slot - 1]
    if entry is None: raise RuntimeError("FX slot is empty.")
    plugin = entry["plugin"]
    show_editor = getattr(plugin, "show_editor", None)
    if show_editor is None: raise RuntimeError(f"Plugin '{plugin.name}' does not expose a native editor.")

    stop = threading.Event()
    watcher = None
    if _WIN_CENTER_AVAILABLE:
        before = {hwnd for hwnd, _ in _list_top_level_windows()}
        window_title = f"{plugin.name} — {target} / Slot {slot}"
        watcher = threading.Thread(target=_editor_watcher, args=(plugin.name, window_title, before, stop), daemon=True)
        watcher.start()

    close_event = gui_manager.create_event(target, slot)
    try:
        show_editor(close_event)
    finally:
        stop.set()
        gui_manager.close_window(target, slot)


# ============================================================
# PRESETS
# ============================================================

def get_plugin_parameter_state(plugin):
    result = {}
    try:
        if hasattr(plugin, "parameters"):
            for name in plugin.parameters.keys():
                try:
                    value = getattr(plugin, name)
                    if isinstance(value, np.generic): value = value.item()
                    if isinstance(value, (str, int, float, bool, type(None))): result[name] = value
                except Exception: pass
    except Exception: pass
    return result

def serialize_chain(chain):
    slots = []
    for entry in chain.slots:
        if entry is None:
            slots.append(None); continue
        plugin = entry["plugin"]
        slots.append({"path": entry["path"], "plugin_name": plugin.name, "params": get_plugin_parameter_state(plugin)})
    return {"bypass": chain.bypass, "slots": slots}

def build_state(engine):
    return {
        "version": APP_VERSION,
        "inputs": {name: serialize_chain(chain) for name, chain in engine.input_chains.items()},
        "outputs": {name: serialize_chain(chain) for name, chain in engine.output_chains.items()},
    }

def save_state(engine, filename):
    filename = Path(filename)
    filename.parent.mkdir(parents=True, exist_ok=True)
    temp_file = Path(f"{filename}.tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(build_state(engine), f, indent=4, ensure_ascii=False)
        f.flush()
        try: os.fsync(f.fileno())
        except OSError: pass
    os.replace(temp_file, filename)

def stage_chain(target, data):
    chain = FXChain(target)
    if not isinstance(data, dict): return chain
    chain.bypass = bool(data.get("bypass", False))
    slots = data.get("slots", [])
    if not isinstance(slots, list): raise ValueError(f"Invalid slots data: {target}")
    for index, slot_data in enumerate(slots[:MAX_FX_SLOTS]):
        if slot_data is None: continue
        if not isinstance(slot_data, dict): raise ValueError("Invalid slot data")
        path = slot_data.get("path")
        if not path: raise FileNotFoundError(f"No VST path in preset: {target} / {index + 1}")
        path_obj = Path(path)
        if not path_obj.is_file(): raise FileNotFoundError(f"VST not found:\n{path_obj}")
        plugin = load_plugin(str(path_obj))
        if not getattr(plugin, "is_effect", False): raise TypeError(f"{plugin.name} is not an audio effect.")
        params = slot_data.get("params", {})
        if isinstance(params, dict):
            for name, value in params.items():
                try:
                    if hasattr(plugin, name): setattr(plugin, name, value)
                except Exception: pass
        chain.slots[index] = {"plugin": plugin, "path": str(path_obj)}
    return chain

def load_state_atomic(engine, filename):
    filename = Path(filename)
    if not filename.is_file(): raise FileNotFoundError(f"Preset not found:\n{filename}")
    with filename.open("r", encoding="utf-8") as f:
        state = json.load(f)
    inputs_state = state.get("inputs", {})
    outputs_state = state.get("outputs", {})
    staged_inputs = {name: stage_chain(name, inputs_state.get(name, {})) for name in input_targets_for_vm(engine.vm_type)}
    staged_outputs = {name: stage_chain(name, outputs_state.get(name, {})) for name in output_targets_for_vm(engine.vm_type)}
    staged_snapshot = engine.build_snapshot_from_chains(staged_inputs, staged_outputs)
    with engine.audio_lock:
        engine.input_chains = staged_inputs
        engine.output_chains = staged_outputs
        engine.swap_snapshot(staged_snapshot)


# ============================================================
# FILE DIALOGS
# ============================================================

def choose_open_preset():
    root = Tk(); root.withdraw()
    try:
        root.attributes("-topmost", True)
        result = filedialog.askopenfilename(title="Open Potato FX preset", initialdir=str(PRESET_DIR.resolve()), filetypes=[("PotatoFX presets", "*.json"), ("JSON files", "*.json"), ("All files", "*.*")])
        return result or None
    finally: root.destroy()

def choose_save_preset():
    root = Tk(); root.withdraw()
    try:
        root.attributes("-topmost", True)
        result = filedialog.asksaveasfilename(title="Save Potato FX preset", initialdir=str(PRESET_DIR.resolve()), defaultextension=".json", filetypes=[("PotatoFX presets", "*.json"), ("JSON files", "*.json")])
        return result or None
    finally: root.destroy()


# ============================================================
# ASIO
# ============================================================

class ASIODeviceManager:
    def __init__(self):
        self.devices = []
        self.selected_device = None
        self.refresh()
    def refresh(self):
        found = []
        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            asio_hostapis = {idx for idx, ha in enumerate(hostapis) if "ASIO" in str(ha.get("name", "")).upper()}
            for idx, dev in enumerate(devices):
                hostapi_idx = int(dev["hostapi"])
                if hostapi_idx not in asio_hostapis: continue
                found.append({"index": idx, "name": str(dev["name"]), "hostapi": str(hostapis[hostapi_idx].get("name", "")), "inputs": int(dev["max_input_channels"]), "outputs": int(dev["max_output_channels"]), "sample_rate": float(dev["default_samplerate"])})
            self.devices = found
        except Exception as exc:
            self.devices = []
    def voicemeeter_devices(self): return [d for d in self.devices if "voicemeeter" in d["name"].lower()]
    def set_selected_by_index(self, index):
        for device in self.devices:
            if device["index"] == index:
                self.selected_device = device
                return device
        return None


# ============================================================
# TEXTUAL MODALS
# ============================================================

class AddFXModal(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close")]
    def __init__(self, target, slot, plugin_manager):
        super().__init__()
        self.target = target
        self.slot = slot
        self.plugin_manager = plugin_manager
    def compose(self):
        names = self.plugin_manager.names()
        yield Container(
            Label(f"Add FX → {self.target} / Slot {self.slot}", id="modal_title"),
            Select([(name, name) for name in names], prompt="Select VST3", id="plugin_select"),
            Horizontal(Button("Load", id="load", variant="success"), Button("Cancel", id="cancel"), classes="modal_actions"),
            id="modal_box",
        )
    def action_close(self): self.dismiss(None)
    def on_button_pressed(self, event):
        if event.button.id == "cancel": self.dismiss(None); return
        select = self.query_one("#plugin_select", Select)
        if select.value is Select.BLANK: return
        self.dismiss(str(select.value))

class PresetModal(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close")]
    def __init__(self, mode, presets):
        super().__init__()
        self.mode = mode
        self.presets = presets
    def compose(self):
        title = "Save Preset" if self.mode == "save" else "Load Preset"
        yield Container(
            Label(title, id="modal_title"),
            Select([(p, p) for p in self.presets], prompt="Select preset" if self.presets else "No presets", id="preset_select"),
            Input(placeholder="Preset name" if self.mode == "save" else "JSON path (optional)", id="preset_input"),
            Horizontal(Button("Save" if self.mode == "save" else "Load", id="apply", variant="success"), Button("Cancel", id="cancel"), classes="modal_actions"),
            id="modal_box",
        )
    def action_close(self): self.dismiss(None)
    def on_button_pressed(self, event):
        if event.button.id == "cancel": self.dismiss(None); return
        input_widget = self.query_one("#preset_input", Input)
        typed = input_widget.value.strip()
        if typed: self.dismiss(typed); return
        select = self.query_one("#preset_select", Select)
        if select.value is Select.BLANK: return
        self.dismiss(str(select.value))

class ASIOModal(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close")]
    def __init__(self, asio_manager):
        super().__init__()
        self.asio_manager = asio_manager
    def compose(self):
        self.asio_manager.refresh()
        options = [(d["name"], str(d["index"])) for d in self.asio_manager.devices]
        yield Container(
            Label("ASIO Device", id="modal_title"),
            Select(options, prompt="Select ASIO" if options else "No ASIO devices", id="asio_select"),
            Horizontal(Button("Select", id="apply", variant="success"), Button("Cancel", id="cancel"), classes="modal_actions"),
            id="modal_box",
        )
    def action_close(self): self.dismiss(None)
    def on_button_pressed(self, event):
        if event.button.id == "cancel": self.dismiss(None); return
        select = self.query_one("#asio_select", Select)
        if select.value is Select.BLANK: return
        self.dismiss(int(select.value))

class ConfirmModal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    def __init__(self, title, message):
        super().__init__()
        self.title_text = title
        self.message_text = message
    def compose(self):
        yield Container(
            Label(self.title_text, id="modal_title"),
            Static(self.message_text, id="confirm_message"),
            Horizontal(Button("Yes", id="yes", variant="error"), Button("No", id="no"), classes="modal_actions"),
            id="modal_box",
        )
    def action_cancel(self): self.dismiss(False)
    def on_button_pressed(self, event): self.dismiss(event.button.id == "yes")

class InfoModal(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close")]
    def __init__(self, title, message):
        super().__init__()
        self.title_text = title
        self.message_text = message
    def compose(self):
        yield Container(
            Label(self.title_text, id="modal_title"),
            Static(self.message_text, id="info_message"),
            Button("Close", id="close"),
            id="modal_box",
        )
    def action_close(self): self.dismiss(None)
    def on_button_pressed(self, event): self.dismiss(None)


# ============================================================
# TEXTUAL APP
# ============================================================

class PotatoFXApp(App):
    TITLE = "Potato FX DSP"
    SUB_TITLE = "Realtime VST3 DSP"

    CSS = """
    Screen { background: #0c1016; color: #e7edf5; }
    Header { background: #111722; color: cyan; }
    Footer { background: #111722; }
    
    #main { height: 1fr; }
    
    #left { width: 26; min-width: 26; border: round #273141; background: #111722; padding: 1; }
    #center { width: 1fr; padding: 1; }
    #right { width: 30; min-width: 30; border: round #273141; background: #111722; padding: 1; }
    
    .panel { border: round #273141; background: #10151d; padding: 1; margin-bottom: 1; }
    .panel_title { color: cyan; text-style: bold; margin-bottom: 1; }
    
    #inputs_list, #outputs_list { height: 1fr; border: none; background: transparent; }
    #inputs_list > ListItem, #outputs_list > ListItem { height: 2; padding: 0 1; }
    #inputs_list > ListItem.--highlight, #outputs_list > ListItem.--highlight { background: #1a3040; color: cyan; }
    
    TabbedContent { height: 1fr; }
    TabPane { padding: 1; }
    
    #target_header { height: auto; margin-bottom: 1; }
    #selected_target { width: 1fr; text-style: bold; }
    #selected_target_state { width: 18; text-align: right; }
    #slot_table { height: 14; }
    
    .toolbar { height: 3; margin-top: 1; }
    .toolbar Button { margin-right: 1; }
    
    #system_status { height: auto; border: round #273141; background: #0b0f15; padding: 1; margin-bottom: 1; }
    #chain_summary { height: 9; border: round #273141; background: #0b0f15; padding: 1; }
    #log { height: 12; border: round #273141; background: #090c11; margin-top: 1; }
    #status_bar { height: 2; background: #111722; padding: 0 1; }
    
    #modal_box { width: 72; max-width: 90%; height: auto; border: thick cyan; background: #141a24; padding: 2; align: center middle; }
    #modal_title { color: cyan; text-style: bold; margin-bottom: 2; }
    #confirm_message, #info_message { margin-bottom: 2; }
    .modal_actions { height: 3; margin-top: 1; }
    .modal_actions Button { margin-right: 1; width: 1fr; }
    """

    BINDINGS = [
        Binding("f2", "add_fx", "Add FX"), Binding("f3", "remove_fx", "Remove"),
        Binding("f4", "bypass_fx", "Bypass"), Binding("f5", "open_gui", "GUI"),
        Binding("f6", "save_preset", "Save"), Binding("f7", "load_preset", "Load"),
        Binding("f8", "show_asio", "ASIO"), Binding("f9", "show_vm_info", "VM"),
        Binding("r", "rescan_vst3", "Rescan"), Binding("ctrl+r", "refresh_ui", "Refresh"),
        Binding("ctrl+up", "move_up", "Move Up"), Binding("ctrl+down", "move_down", "Move Down"),
        Binding("1", "select_slot_1", "Slot 1"), Binding("2", "select_slot_2", "Slot 2"),
        Binding("3", "select_slot_3", "Slot 3"), Binding("4", "select_slot_4", "Slot 4"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, engine, vm, plugin_manager, asio_manager, routing_manager, gui_manager):
        super().__init__()
        self.engine = engine
        self.vm = vm
        self.plugin_manager = plugin_manager
        self.asio_manager = asio_manager
        self.routing_manager = routing_manager
        self.gui_manager = gui_manager
        
        self.selected_target = "VAIO3"
        self.selected_slot = 1
        self.current_tab = "inputs"
        self.input_keys = []
        self.output_keys = []
        self._last_vm_type = vm.detected_type

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left"):
                with Vertical(classes="panel"):
                    yield Label("TARGETS", classes="panel_title")
                    # ИСПРАВЛЕНИЕ: initial="inputs_tab" вместо "inputs"
                    with TabbedContent(initial="inputs_tab"):
                        with TabPane("Inputs", id="inputs_tab"):
                            yield ListView(id="inputs_list")
                        with TabPane("Outputs", id="outputs_tab"):
                            yield ListView(id="outputs_list")
            with Vertical(id="center"):
                with Vertical(classes="panel"):
                    with Horizontal(id="target_header"):
                        yield Label("VAIO3", id="selected_target")
                        yield Label("ACTIVE", id="selected_target_state")
                    yield DataTable(id="slot_table")
                    with Horizontal(classes="toolbar"):
                        yield Button("Add FX", id="add_fx", variant="success")
                        yield Button("GUI", id="open_gui")
                        yield Button("Bypass", id="bypass")
                        yield Button("Remove", id="remove_fx")
                        yield Button("Up", id="move_up")
                        yield Button("Down", id="move_down")
                        yield Button("Clear", id="clear", variant="error")
                with Vertical(classes="panel"):
                    yield Label("FX CHAIN", classes="panel_title")
                    yield Static("—", id="chain_summary")
                yield RichLog(id="log", markup=True, highlight=True)
            with Vertical(id="right"):
                with Vertical(classes="panel"):
                    yield Label("DSP STATUS", classes="panel_title")
                    yield Static("Loading...", id="system_status")
                with Vertical(classes="panel"):
                    yield Label("PRESETS", classes="panel_title")
                    with Horizontal(classes="toolbar"):
                        yield Button("Save", id="save_preset")
                        yield Button("Load", id="load_preset")
                with Vertical(classes="panel"):
                    yield Label("SYSTEM", classes="panel_title")
                    with Horizontal(classes="toolbar"):
                        yield Button("ASIO", id="asio")
                        yield Button("Voicemeeter", id="vm")
                    with Horizontal(classes="toolbar"):
                        yield Button("Rescan VST3", id="rescan")
                        yield Button("Status", id="status")
        yield Static("F2 Add  •  F3 Remove  •  F4 Bypass  •  F5 GUI  •  F6 Save  •  F7 Load  •  F8 ASIO  •  F9 VM  •  Ctrl+↑/↓ Move  •  1-4 Slot  •  R Rescan  •  Q Quit", id="status_bar")
        yield Footer()

    def on_mount(self):
        self.title = f"Potato FX DSP v{APP_VERSION}"
        table = self.query_one("#slot_table", DataTable)
        table.add_columns("Slot", "Plugin", "State")
        self.rebuild_targets()
        self.refresh_slots()
        self.refresh_summary()
        self.refresh_status()
        self.set_interval(0.5, self.refresh_dynamic)
        self.add_log("[green]Potato FX DSP started[/green]")
        self.add_log(f"Voicemeeter: {self.vm.get_type_name()} {self.vm.get_version() or '?'}")
        self.add_log(f"VST3 files: {self.plugin_manager.count()}")

    def add_log(self, message):
        try: self.query_one("#log", RichLog).write(str(message))
        except Exception: pass

    def show_error(self, title, message):
        self.add_log(f"[red]{title}: {message}[/red]")
        self.push_screen(InfoModal(title, message))

    def rebuild_targets(self):
        targets = target_list_for_vm(self.vm.detected_type)
        self.input_keys = targets["inputs"]
        self.output_keys = targets["outputs"]

        input_list_view = self.query_one("#inputs_list", ListView)
        output_list_view = self.query_one("#outputs_list", ListView)
        input_list_view.clear()
        output_list_view.clear()

        input_map = input_targets_for_vm(self.vm.detected_type)
        output_map = output_targets_for_vm(self.vm.detected_type)

        for target in self.input_keys:
            info = input_map[target]
            input_list_view.append(ListItem(Label(f"{target} [dim]{info['channels']}ch[/dim]")))
        for target in self.output_keys:
            info = output_map[target]
            output_list_view.append(ListItem(Label(f"{target} [dim]{info['channels']}ch[/dim]")))

        if self.selected_target not in self.input_keys + self.output_keys:
            if self.input_keys:
                self.selected_target = self.input_keys[0]
                self.current_tab = "inputs"
            else:
                self.selected_target = self.output_keys[0]
                self.current_tab = "outputs"

        try:
            if self.current_tab == "inputs":
                input_list_view.index = self.input_keys.index(self.selected_target)
            else:
                output_list_view.index = self.output_keys.index(self.selected_target)
        except Exception:
            pass

        self.refresh_status()

    def on_list_view_selected(self, event):
        index = event.list_view.index
        if index is None: return

        list_view = event.list_view
        if list_view.id == "inputs_list":
            if 0 <= index < len(self.input_keys):
                self.selected_target = self.input_keys[index]
                self.current_tab = "inputs"
        elif list_view.id == "outputs_list":
            if 0 <= index < len(self.output_keys):
                self.selected_target = self.output_keys[index]
                self.current_tab = "outputs"
        else:
            return

        self.selected_slot = 1
        self.refresh_slots()
        self.refresh_summary()
        self.refresh_status()
        self.add_log(f"[cyan]Selected target: {self.selected_target}[/cyan]")

    def refresh_slots(self):
        table = self.query_one("#slot_table", DataTable)
        table.clear()
        chain = self.engine.get_chain(self.selected_target)
        for num, entry in enumerate(chain.slots, start=1):
            if entry is None:
                plugin_name = "Empty"; state = "—"
            else:
                plugin_name = entry["plugin"].name
                state = "BYPASS" if chain.bypass else "ACTIVE"
            table.add_row(str(num), plugin_name, state, key=str(num))
        try:
            table.cursor_type = "row"
            table.cursor_coordinate = (max(0, self.selected_slot - 1), 0)
        except Exception: pass

    def on_data_table_row_selected(self, event):
        try:
            slot = int(str(event.row_key))
            if 1 <= slot <= MAX_FX_SLOTS:
                self.selected_slot = slot
                self.refresh_summary()
                self.refresh_status()
        except Exception: pass

    def on_data_table_row_highlighted(self, event):
        try:
            slot = int(str(event.row_key))
            if 1 <= slot <= MAX_FX_SLOTS and slot != self.selected_slot:
                self.selected_slot = slot
                self.refresh_summary()
                self.refresh_status()
        except Exception: pass

    def refresh_status(self):
        try:
            vm_name = self.vm.get_type_name()
            version = self.vm.get_version() or "?"
            dsp_state = "[green]RUNNING[/green]" if self.engine.running else "[red]STOPPED[/red]"
            callback_state = "[green]RUNNING[/green]" if self.vm.callback_started else "[yellow]STOPPED[/yellow]"
            asio_name = self.asio_manager.selected_device["name"] if self.asio_manager.selected_device else "Not selected"
            text = (f"[bold cyan]{vm_name}[/bold cyan]\n"
                    f"Version: {version}\n"
                    f"DSP: {dsp_state}\n"
                    f"Callback: {callback_state}\n"
                    f"SR: {self.engine.sample_rate} Hz\n"
                    f"Block: {self.engine.block_size}\n"
                    f"Blocks: {self.engine.callback_count}\n"
                    f"VST3: {self.plugin_manager.count()}\n"
                    f"ASIO: {asio_name}")
            if self.engine.last_error:
                text += f"\n\n[bold red]DSP ERROR[/bold red]\n{self.engine.last_error}"
            self.query_one("#system_status", Static).update(text)
            self.query_one("#selected_target", Label).update(self.selected_target)
            chain = self.engine.get_chain(self.selected_target)
            self.query_one("#selected_target_state", Label).update("[yellow]BYPASS[/yellow]" if chain.bypass else "[green]ACTIVE[/green]")
        except Exception: pass

    def refresh_summary(self):
        try:
            chain = self.engine.get_chain(self.selected_target)
            lines = []
            for num, entry in enumerate(chain.slots, start=1):
                if entry is None: lines.append(f"{num}. [dim]Empty[/dim]")
                else:
                    state = "[yellow]BYPASS[/yellow]" if chain.bypass else "[green]ACTIVE[/green]"
                    lines.append(f"{num}. {entry['plugin'].name} {state}")
            self.query_one("#chain_summary", Static).update("\n".join(lines))
        except Exception: pass

    def refresh_dynamic(self):
        current_type = self.vm.detected_type
        if current_type != self._last_vm_type:
            self._last_vm_type = current_type
            self.routing_manager.set_type(current_type)
            self.engine.set_vm_type(current_type)
            self.rebuild_targets()
            self.add_log(f"[cyan]Voicemeeter type changed: {VM_NAMES.get(current_type, 'Unknown')}[/cyan]")
        self.refresh_status()

    def on_button_pressed(self, event):
        btn = event.button.id
        if btn == "add_fx": self.action_add_fx()
        elif btn == "open_gui": self.action_open_gui()
        elif btn == "bypass": self.action_bypass()
        elif btn == "remove_fx": self.action_remove_fx()
        elif btn == "move_up": self.action_move_up()
        elif btn == "move_down": self.action_move_down()
        elif btn == "clear": self.action_clear()
        elif btn == "save_preset": self.action_save_preset()
        elif btn == "load_preset": self.action_load_preset()
        elif btn == "asio": self.action_asio()
        elif btn == "vm": self.action_vm_info()
        elif btn == "rescan": self.action_rescan_vst3()
        elif btn == "status": self.action_status()

    def action_add_fx(self):
        chain = self.engine.get_chain(self.selected_target)
        slot = self.selected_slot
        if chain.slots[slot - 1] is not None: slot = chain.find_free_slot()
        if slot is None: self.notify("All FX slots are occupied", severity="warning"); return
        self.push_screen(AddFXModal(self.selected_target, slot, self.plugin_manager), self.on_add_fx_result)

    def on_add_fx_result(self, plugin_name):
        if not plugin_name: return
        path = self.plugin_manager.get_by_name(plugin_name)
        if path is None: self.notify("VST3 file not found", severity="error"); return
        target = self.selected_target
        chain = self.engine.get_chain(target)
        slot = self.selected_slot
        if chain.slots[slot - 1] is not None: slot = chain.find_free_slot()
        if slot is None: self.notify("No free slot", severity="warning"); return
        try:
            plugin = self.engine.add_plugin(target, slot, path)
            self._add_fx_finished(target, slot, plugin.name)
        except Exception as exc:
            logger.error("Add FX failed: %s\n%s", exc, traceback.format_exc())
            self.show_error("VST3 load error", str(exc))

    def _add_fx_finished(self, target, slot, plugin_name):
        self.selected_target = target; self.selected_slot = slot
        self.refresh_slots(); self.refresh_summary(); self.refresh_status()
        self.add_log(f"[green]Added {plugin_name} → {target} / Slot {slot}[/green]")

    def action_remove_fx(self):
        chain = self.engine.get_chain(self.selected_target)
        entry = chain.slots[self.selected_slot - 1]
        if entry is None: self.notify("Selected slot is empty", severity="warning"); return
        plugin_name = entry["plugin"].name; target = self.selected_target; slot = self.selected_slot
        self.push_screen(ConfirmModal("Remove FX?", f"{plugin_name}\n\n{target} / Slot {slot}"), lambda result: self._remove_confirmed(result, target, slot, plugin_name))

    def _remove_confirmed(self, confirmed, target, slot, plugin_name):
        if not confirmed: return
        try:
            self.engine.remove_plugin(target, slot)
            self._remove_finished(target, slot, plugin_name)
        except Exception as exc:
            self.show_error("Remove error", str(exc))

    def _remove_finished(self, target, slot, plugin_name):
        self.refresh_slots(); self.refresh_summary()
        self.add_log(f"[yellow]Removed {plugin_name} from {target} / Slot {slot}[/yellow]")

    def action_bypass(self):
        target = self.selected_target
        try:
            state = self.engine.toggle_bypass(target)
            self._bypass_finished(target, state)
        except Exception as exc: self.show_error("Bypass error", str(exc))

    def _bypass_finished(self, target, state):
        self.refresh_slots(); self.refresh_summary(); self.refresh_status()
        self.add_log(f"{'[yellow]BYPASS[/yellow]' if state else '[green]ACTIVE[/green]'}  {target}")

    def action_clear(self):
        target = self.selected_target; chain = self.engine.get_chain(target)
        if not chain.has_plugins(): self.notify("Chain is already empty", severity="warning"); return
        self.push_screen(ConfirmModal("Clear chain?", f"{target}\n\nAll FX slots will be removed."), lambda result: self._clear_confirmed(result, target))

    def _clear_confirmed(self, confirmed, target):
        if not confirmed: return
        try:
            self.engine.clear_chain(target)
            self._clear_finished(target)
        except Exception as exc: self.show_error("Clear error", str(exc))

    def _clear_finished(self, target):
        self.refresh_slots(); self.refresh_summary()
        self.add_log(f"[yellow]Cleared {target}[/yellow]")

    def action_move_up(self):
        if self.selected_slot > 1:
            self._move_slot(self.selected_slot, self.selected_slot - 1)

    def action_move_down(self):
        if self.selected_slot < MAX_FX_SLOTS:
            self._move_slot(self.selected_slot, self.selected_slot + 1)

    def _move_slot(self, src, dst):
        target = self.selected_target
        try:
            self.engine.move_plugin(target, src, dst)
            self.selected_slot = dst
            self.refresh_slots(); self.refresh_summary(); self.refresh_status()
            self.add_log(f"[cyan]Moved {target} slot {src} → {dst}[/cyan]")
        except Exception as exc:
            self.show_error("Move error", str(exc))

    def action_select_slot_1(self): self._select_slot(1)
    def action_select_slot_2(self): self._select_slot(2)
    def action_select_slot_3(self): self._select_slot(3)
    def action_select_slot_4(self): self._select_slot(4)

    def _select_slot(self, slot):
        if 1 <= slot <= MAX_FX_SLOTS:
            self.selected_slot = slot
            self.refresh_slots(); self.refresh_summary(); self.refresh_status()

    def action_open_gui(self):
        chain = self.engine.get_chain(self.selected_target)
        entry = chain.slots[self.selected_slot - 1]
        if entry is None: self.notify("Selected slot is empty", severity="warning"); return
        target = self.selected_target; slot = self.selected_slot; plugin_name = entry["plugin"].name
        self.add_log(f"[cyan]Opening GUI: {plugin_name}[/cyan]")
        self._open_gui_main_thread(target, slot)

    def _open_gui_main_thread(self, target, slot):
        try:
            open_plugin_gui(self.engine, self.gui_manager, target, slot)
            self.refresh_slots(); self.refresh_summary(); self.refresh_status()
            self.add_log("[green]Plugin GUI closed[/green]")
        except Exception as exc:
            logger.error("GUI error: %s\n%s", exc, traceback.format_exc())
            self.show_error("GUI error", str(exc))

    def action_save_preset(self):
        path = choose_save_preset()
        if not path: return
        try:
            save_state(self.engine, Path(path))
            self.notify(f"Preset saved: {Path(path).name}")
            self.add_log(f"[green]Preset saved: {Path(path).name}[/green]")
        except Exception as exc: self.show_error("Save error", str(exc))

    def action_load_preset(self):
        path = choose_open_preset()
        if not path: return
        try:
            load_state_atomic(self.engine, Path(path))
            self._load_preset_finished(Path(path))
        except Exception as exc:
            logger.error("Preset load failed: %s\n%s", exc, traceback.format_exc())
            self.show_error("Preset load failed", f"{exc}\n\nCurrent configuration was left unchanged.")

    def _load_preset_finished(self, path):
        self.refresh_all(); self.notify(f"Preset loaded: {path.name}")
        self.add_log(f"[green]Preset loaded: {path.name}[/green]")

    def action_asio(self):
        self.push_screen(ASIOModal(self.asio_manager), self._asio_result)

    def _asio_result(self, device_index):
        if device_index is None: return
        device = self.asio_manager.set_selected_by_index(device_index)
        if device is None: return
        self.add_log(f"[cyan]ASIO selected: {device['name']}[/cyan]")
        self.refresh_status()

    def action_vm_info(self):
        buses = self.routing_manager.bus_counts()
        message = (f"Voicemeeter: {self.vm.get_type_name()}\n"
                   f"Version: {self.vm.get_version() or '?'}\n"
                   f"Type ID: {self.vm.detected_type}\n\n"
                   f"Python: {self.vm.python_bits}-bit\n"
                   f"Windows: {self.vm.windows_bits}-bit\n\n"
                   f"Strips: {self.routing_manager.strip_count()}\n"
                   f"A buses: {buses['A']}\n"
                   f"B buses: {buses['B']}\n\n"
                   f"DLL:\n{self.vm.dll_path}")
        self.push_screen(InfoModal("Voicemeeter", message))

    def action_rescan_vst3(self):
        self.add_log("[cyan]Scanning VST3 files...[/cyan]")
        try:
            self.plugin_manager.scan()
            self._rescan_finished()
        except Exception as exc: self.show_error("VST3 scan error", str(exc))

    def _rescan_finished(self):
        count = self.plugin_manager.count()
        self.add_log(f"[green]VST3 scan complete: {count} files[/green]")
        self.notify(f"Found {count} VST3 files")

    def action_status(self):
        message = (f"Voicemeeter: {self.vm.get_type_name()}\n"
                   f"Version: {self.vm.get_version() or '?'}\n"
                   f"DSP: {'RUNNING' if self.engine.running else 'STOPPED'}\n"
                   f"Callback: {'RUNNING' if self.vm.callback_started else 'STOPPED'}\n\n"
                   f"Sample Rate: {self.engine.sample_rate} Hz\n"
                   f"Block Size: {self.engine.block_size}\n"
                   f"Callbacks: {self.engine.callback_count}\n"
                   f"Input: {self.engine.input_callback_count}\n"
                   f"Output: {self.engine.output_callback_count}\n"
                   f"MAIN: {self.engine.main_callback_count}\n\n"
                   f"VST3 files: {self.plugin_manager.count()}\n"
                   f"Last error: {self.engine.last_error or 'None'}")
        self.push_screen(InfoModal("DSP Status", message))

    def action_refresh_ui(self):
        self.refresh_all()
        self.notify("UI refreshed")

    def refresh_all(self):
        self.rebuild_targets()
        self.refresh_slots()
        self.refresh_summary()
        self.refresh_status()

    def on_unmount(self):
        try: save_state(self.engine, STATE_FILE)
        except Exception: logger.error("Auto-state save failed:\n%s", traceback.format_exc())
        try:
            if self.vm is not None:
                self.vm.stop_callback()
                self.vm.unregister_callback()
                self.vm.logout()
        except Exception: pass


# ============================================================
# MAIN
# ============================================================

def shutdown_voicemeeter(vm, stop_event, gui_manager):
    try:
        if stop_event is not None:
            stop_event.set()
    except Exception:
        pass
    try:
        if gui_manager is not None:
            gui_manager.close_all()
    except Exception:
        pass
    if vm is not None:
        try:
            vm.stop_callback()
        except Exception:
            pass
        try:
            time.sleep(0.2)
        except Exception:
            pass
        try:
            vm.unregister_callback()
        except Exception:
            pass
        try:
            vm.logout()
        except Exception:
            pass


def main():
    vm = None
    watcher_thread = None
    stop_event = threading.Event()

    logger.info("=" * 60)
    logger.info("NEW SESSION START")
    logger.info("Potato FX DSP v%s", APP_VERSION)

    engine = DSPEngine()
    plugin_manager = PluginManager()
    asio_manager = ASIODeviceManager()
    gui_manager = GUIManager()

    try:
        plugin_manager.scan()
        vm = VoicemeeterRemote()
        logger.info("Remote DLL: %s", vm.dll_path)

        def _atexit_shutdown():
            shutdown_voicemeeter(vm, stop_event, gui_manager)
        atexit.register(_atexit_shutdown)

        if sys.platform == "win32":
            try:
                _k32 = ctypes.windll.kernel32
                _ctrl_handler_t = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
                def _ctrl_handler(ctrl_type):
                    try: shutdown_voicemeeter(vm, stop_event, gui_manager)
                    except Exception: pass
                    return 0
                _CTRL_HANDLER_REF = _ctrl_handler_t(_ctrl_handler)
                globals()["_CTRL_HANDLER_REF"] = _CTRL_HANDLER_REF
                _k32.SetConsoleCtrlHandler(_CTRL_HANDLER_REF, 1)
            except Exception:
                pass

        vm_type = vm.login(preferred_type=VM_TYPE_POTATO)
        routing_manager = RoutingProfileManager(vm_type)

        engine.set_vm_type(vm_type)

        if STATE_FILE.is_file():
            try:
                load_state_atomic(engine, STATE_FILE)
                logger.info("Auto-loaded previous state from %s", STATE_FILE)
            except Exception as exc:
                logger.error("Auto-load failed: %s\n%s", exc, traceback.format_exc())

        try:
            vm.register_callback(engine.callback)
            vm.start_callback()
        except Exception as exc:
            logger.error("Could not start callback: %s\n%s", exc, traceback.format_exc())

        def vm_watchdog():
            previous_type = vm.detected_type
            previous_connected = True
            while not stop_event.wait(0.5):
                connected, current_type = vm.probe_connection()
                if not connected:
                    if previous_connected:
                        logger.warning("Voicemeeter disconnected.")
                        try: vm.stop_callback()
                        except Exception: pass
                        try: vm.unregister_callback()
                        except Exception: pass
                    previous_connected = False
                    continue

                if not previous_connected:
                    logger.info("Voicemeeter reconnected.")
                previous_connected = True

                if current_type != previous_type:
                    logger.info("Voicemeeter changed: %s -> %s", VM_NAMES.get(previous_type, "Unknown"), VM_NAMES.get(current_type, "Unknown"))
                    routing_manager.set_type(current_type)
                    engine.set_vm_type(current_type)
                    
                    try: vm.stop_callback()
                    except Exception: pass
                    try: vm.unregister_callback()
                    except Exception: pass
                    
                    try:
                        vm.register_callback(engine.callback)
                        vm.start_callback()
                        logger.info("DSP callback restarted.")
                    except Exception:
                        logger.exception("Failed to restart DSP callback")
                    
                    previous_type = current_type

        watcher_thread = threading.Thread(target=vm_watchdog, daemon=True, name="VoicemeeterWatchdog")
        watcher_thread.start()

        app = PotatoFXApp(engine=engine, vm=vm, plugin_manager=plugin_manager, asio_manager=asio_manager, routing_manager=routing_manager, gui_manager=gui_manager)
        app.run()

    except KeyboardInterrupt:
        logger.info("Interrupted.")
    except Exception as exc:
        logger.critical("Fatal error: %s\n%s", exc, traceback.format_exc())
        print(f"\nFATAL ERROR:\n{exc}\n\nSee {LOG_FILE}\n")
    finally:
        if watcher_thread is not None:
            try: watcher_thread.join(timeout=1.0)
            except Exception: pass
        shutdown_voicemeeter(vm, stop_event, gui_manager)
        logger.info("FX DSP stopped")
        logger.info("SESSION END")


if __name__ == "__main__":
    main()