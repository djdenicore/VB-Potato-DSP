import os
import sys
import ctypes
import json
import logging
import queue
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

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table


# ============================================================
# APPLICATION
# ============================================================

APP_NAME = "Python Potato FX DSP"
APP_VERSION = "15.1"

STATE_FILE = Path("fx_state.json")
PRESET_DIR = Path("presets")
LOG_DIR = Path("logs")
VST3_FOLDER = Path(r"C:\Program Files\Common Files\VST3")

MAX_FX_SLOTS = 4

PRESET_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


# ============================================================
# LOGGING
# ============================================================

LOG_FILE = LOG_DIR / "release.log"

logger = logging.getLogger("PotatoFX")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)

console = Console()


def log_and_print(message, level="info"):
    text = str(message)

    if level == "debug":
        logger.debug(text)
        return

    if level == "warning":
        logger.warning(text)
        console.print(f"[yellow]{message}[/yellow]")
        return

    if level == "error":
        logger.error(text)
        console.print(f"[bold red]{message}[/bold red]")
        return

    logger.info(text)
    console.print(message)


def log_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(
            exc_type,
            exc_value,
            exc_traceback,
        )
        return

    logger.critical(
        "Uncaught exception",
        exc_info=(
            exc_type,
            exc_value,
            exc_traceback,
        ),
    )

    console.print(
        f"[bold red]Критическая ошибка: {exc_value}[/bold red]"
    )
    console.print(f"[dim]См. {LOG_FILE}[/dim]")


sys.excepthook = log_uncaught_exception


# ============================================================
# UI HELPERS
# ============================================================

def clear_screen():
    os.system("cls")


def pause(text="Нажмите Enter для возврата..."):
    try:
        input(f"\n{text}")
    except EOFError:
        pass


def screen_title(title, subtitle=None):
    clear_screen()

    body = f"[bold cyan]{title}[/bold cyan]"

    if subtitle:
        body += f"\n[dim]{subtitle}[/dim]"

    console.print(
        Panel(
            body,
            border_style="cyan",
            box=box.ROUNDED,
        )
    )


def result_screen(title, message, style="green"):
    clear_screen()

    console.print(
        Panel(
            message,
            title=title,
            border_style=style,
            box=box.ROUNDED,
        )
    )

    pause()


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

RUN_IDS_64 = {
    VM_TYPE_STANDARD: 4,
    VM_TYPE_BANANA: 5,
    VM_TYPE_POTATO: 6,
}

RUN_IDS_32 = {
    VM_TYPE_STANDARD: 1,
    VM_TYPE_BANANA: 2,
    VM_TYPE_POTATO: 3,
}


# ============================================================
# CHANNEL MAP
# ============================================================

INPUT_TARGETS = {
    "IN1": {"name": "IN 1", "start": 0, "channels": 2},
    "IN2": {"name": "IN 2", "start": 2, "channels": 2},
    "IN3": {"name": "IN 3", "start": 4, "channels": 2},
    "IN4": {"name": "IN 4", "start": 6, "channels": 2},
    "IN5": {"name": "IN 5", "start": 8, "channels": 2},
    "VAIO": {"name": "VAIO", "start": 10, "channels": 8},
    "AUX": {"name": "AUX", "start": 18, "channels": 8},
    "VAIO3": {"name": "VAIO3", "start": 26, "channels": 8},
}

OUTPUT_TARGETS_POTATO = {
    "A1": {"name": "A1", "start": 0, "channels": 8},
    "A2": {"name": "A2", "start": 8, "channels": 8},
    "A3": {"name": "A3", "start": 16, "channels": 8},
    "A4": {"name": "A4", "start": 24, "channels": 8},
    "A5": {"name": "A5", "start": 32, "channels": 8},
    "B1": {"name": "B1", "start": 40, "channels": 8},
    "B2": {"name": "B2", "start": 48, "channels": 8},
    "B3": {"name": "B3", "start": 56, "channels": 8},
}

TARGET_ORDER_POTATO = [
    "IN1",
    "IN2",
    "IN3",
    "IN4",
    "IN5",
    "VAIO",
    "AUX",
    "VAIO3",
    "A1",
    "A2",
    "A3",
    "A4",
    "A5",
    "B1",
    "B2",
    "B3",
]

# ВАЖНО: именно этого определения не хватало в v15.0.
ALL_TARGETS = {
    **INPUT_TARGETS,
    **OUTPUT_TARGETS_POTATO,
}


def target_list_for_vm(vm_type):
    if vm_type == VM_TYPE_POTATO:
        return list(TARGET_ORDER_POTATO)

    if vm_type == VM_TYPE_BANANA:
        return [
            "IN1",
            "IN2",
            "IN3",
            "IN4",
            "IN5",
            "VAIO",
        ]

    if vm_type == VM_TYPE_STANDARD:
        return [
            "IN1",
            "IN2",
            "IN3",
        ]

    return []


# ============================================================
# AUDIO STRUCTURES
# ============================================================

class VBVMR_AUDIOINFO(ctypes.Structure):
    _fields_ = [
        ("samplerate", ctypes.c_long),
        ("nbSamplePerFrame", ctypes.c_long),
    ]


class VBVMR_AUDIOBUFFER(ctypes.Structure):
    _fields_ = [
        ("audiobuffer_sr", ctypes.c_long),
        ("audiobuffer_nbs", ctypes.c_long),
        ("audiobuffer_nbi", ctypes.c_long),
        ("audiobuffer_nbo", ctypes.c_long),
        (
            "audiobuffer_r",
            ctypes.POINTER(ctypes.c_float) * 128,
        ),
        (
            "audiobuffer_w",
            ctypes.POINTER(ctypes.c_float) * 128,
        ),
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
        return any(
            entry is not None
            for entry in self.slots
        )

    def build_board(self):
        if self.bypass:
            return None

        plugins = [
            entry["plugin"]
            for entry in self.slots
            if entry is not None
        ]

        if not plugins:
            return None

        return Pedalboard(plugins)

    def add(self, slot, plugin, path):
        if not 1 <= slot <= MAX_FX_SLOTS:
            raise ValueError(
                f"Слот должен быть 1-{MAX_FX_SLOTS}"
            )

        self.slots[slot - 1] = {
            "plugin": plugin,
            "path": str(path),
        }

    def remove(self, slot):
        if not 1 <= slot <= MAX_FX_SLOTS:
            raise ValueError(
                f"Слот должен быть 1-{MAX_FX_SLOTS}"
            )

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
        self.input = input_boards or {}
        self.output = output_boards or {}


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

        self.input_chains = {
            name: FXChain(name)
            for name in INPUT_TARGETS
        }

        self.output_chains = {
            name: FXChain(name)
            for name in OUTPUT_TARGETS_POTATO
        }

        self.snapshot = DSPSnapshot()
        self.snapshot_lock = threading.Lock()

    def get_chain(self, target):
        target = target.upper()

        if target in self.input_chains:
            return self.input_chains[target]

        if target in self.output_chains:
            return self.output_chains[target]

        raise ValueError(
            f"Неизвестная цель: {target}"
        )

    def build_snapshot_from_chains(
        self,
        input_chains,
        output_chains,
    ):
        input_boards = {}
        output_boards = {}

        for name, chain in input_chains.items():
            if chain.has_plugins():
                input_boards[name] = chain.build_board()

        for name, chain in output_chains.items():
            if chain.has_plugins():
                output_boards[name] = chain.build_board()

        return DSPSnapshot(
            input_boards,
            output_boards,
        )

    def swap_snapshot(self, snapshot):
        with self.snapshot_lock:
            self.snapshot = snapshot

    def rebuild_snapshot(self):
        self.swap_snapshot(
            self.build_snapshot_from_chains(
                self.input_chains,
                self.output_chains,
            )
        )

    def add_plugin(self, target, slot, path):
        chain = self.get_chain(target)
        path = Path(path)

        if not path.is_file():
            raise FileNotFoundError(
                f"VST3 не найден:\n{path}"
            )

        logger.info("Loading VST3: %s", path)

        plugin = load_plugin(str(path))

        if not getattr(plugin, "is_effect", False):
            raise TypeError(
                f"{plugin.name} не является audio effect."
            )

        old_entry = chain.slots[slot - 1]
        chain.add(slot, plugin, path)

        try:
            self.rebuild_snapshot()
        except Exception:
            chain.slots[slot - 1] = old_entry
            self.rebuild_snapshot()
            raise

        self.last_error = None

        logger.info(
            "Added plugin '%s' to %s slot %d",
            plugin.name,
            target,
            slot,
        )

        return plugin

    def remove_plugin(self, target, slot):
        chain = self.get_chain(target)
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

        old = chain.bypass
        state = chain.toggle_bypass()

        try:
            self.rebuild_snapshot()
        except Exception:
            chain.bypass = old
            self.rebuild_snapshot()
            raise

        return state

    @staticmethod
    def pointer_to_array(pointer, samples):
        if not pointer or samples <= 0:
            return None

        return np.ctypeslib.as_array(
            pointer,
            shape=(samples,),
        )

    def process_stereo(
        self,
        board,
        left_in,
        right_in,
        left_out,
        right_out,
        samples,
        sample_rate,
    ):
        left_input = self.pointer_to_array(
            left_in, samples
        )
        right_input = self.pointer_to_array(
            right_in, samples
        )
        left_output = self.pointer_to_array(
            left_out, samples
        )
        right_output = self.pointer_to_array(
            right_out, samples
        )

        if any(
            value is None
            for value in (
                left_input,
                right_input,
                left_output,
                right_output,
            )
        ):
            return

        if board is None:
            left_output[:] = left_input
            right_output[:] = right_input
            return

        try:
            audio = np.empty(
                (2, samples),
                dtype=np.float32,
            )

            audio[0] = left_input
            audio[1] = right_input

            processed = board(
                audio,
                sample_rate,
                reset=False,
            )

            if (
                not isinstance(
                    processed,
                    np.ndarray,
                )
                or processed.ndim != 2
                or processed.shape[0] < 2
                or processed.shape[1] != samples
            ):
                raise RuntimeError(
                    "Неожиданный размер "
                    f"Pedalboard output: "
                    f"{getattr(processed, 'shape', None)}"
                )

            left_output[:] = processed[0]
            right_output[:] = processed[1]

        except Exception as exc:
            self.last_error = str(exc)

            logger.error(
                "DSP processing error: %s\n%s",
                exc,
                traceback.format_exc(),
            )

            left_output[:] = left_input
            right_output[:] = right_input

    def process_input_target(
        self,
        target,
        buffer,
        snapshot,
    ):
        info = INPUT_TARGETS[target]
        start = info["start"]
        channels = info["channels"]

        nbi = int(buffer.audiobuffer_nbi)
        nbo = int(buffer.audiobuffer_nbo)

        if (
            start + channels > nbi
            or start + channels > nbo
        ):
            return

        samples = int(
            buffer.audiobuffer_nbs
        )

        if samples <= 0:
            return

        for channel in range(
            start,
            start + channels,
        ):
            inp = buffer.audiobuffer_r[channel]
            out = buffer.audiobuffer_w[channel]

            if not inp or not out:
                continue

            input_array = self.pointer_to_array(
                inp, samples
            )
            output_array = self.pointer_to_array(
                out, samples
            )

            if (
                input_array is not None
                and output_array is not None
            ):
                output_array[:] = input_array

        board = snapshot.input.get(target)

        sample_rate = int(
            buffer.audiobuffer_sr
        )

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

    def process_output_target(
        self,
        target,
        buffer,
        snapshot,
    ):
        info = OUTPUT_TARGETS_POTATO[target]
        start = info["start"]
        channels = info["channels"]

        nbi = int(buffer.audiobuffer_nbi)
        nbo = int(buffer.audiobuffer_nbo)

        if (
            start + channels > nbi
            or start + channels > nbo
        ):
            return

        samples = int(
            buffer.audiobuffer_nbs
        )

        if samples <= 0:
            return

        for channel in range(
            start,
            start + channels,
        ):
            inp = buffer.audiobuffer_r[channel]
            out = buffer.audiobuffer_w[channel]

            if not inp or not out:
                continue

            input_array = self.pointer_to_array(
                inp, samples
            )
            output_array = self.pointer_to_array(
                out, samples
            )

            if (
                input_array is not None
                and output_array is not None
            ):
                output_array[:] = input_array

        board = snapshot.output.get(target)

        sample_rate = int(
            buffer.audiobuffer_sr
        )

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

    def callback(
        self,
        lp_user,
        command,
        lp_data,
        nnn,
    ):
        try:
            if command == VBVMR_CBCOMMAND_STARTING:
                if not lp_data:
                    return 0

                info = ctypes.cast(
                    lp_data,
                    ctypes.POINTER(
                        VBVMR_AUDIOINFO
                    ),
                ).contents

                self.sample_rate = int(
                    info.samplerate
                )
                self.block_size = int(
                    info.nbSamplePerFrame
                )
                self.running = True
                self.last_error = None

                logger.info(
                    "Audio STARTING: SR=%d BS=%d",
                    self.sample_rate,
                    self.block_size,
                )

                return 0

            if command == VBVMR_CBCOMMAND_ENDING:
                self.running = False
                logger.info("Audio ENDING")
                return 0

            if command == VBVMR_CBCOMMAND_CHANGE:
                if not lp_data:
                    return 0

                info = ctypes.cast(
                    lp_data,
                    ctypes.POINTER(
                        VBVMR_AUDIOINFO
                    ),
                ).contents

                self.sample_rate = int(
                    info.samplerate
                )
                self.block_size = int(
                    info.nbSamplePerFrame
                )

                logger.info(
                    "Audio CHANGE: SR=%d BS=%d",
                    self.sample_rate,
                    self.block_size,
                )

                return 0

            if command == VBVMR_CBCOMMAND_BUFFER_IN:
                if not lp_data:
                    return 0

                buffer = ctypes.cast(
                    lp_data,
                    ctypes.POINTER(
                        VBVMR_AUDIOBUFFER
                    ),
                ).contents

                snapshot = self.snapshot

                for target in INPUT_TARGETS:
                    self.process_input_target(
                        target,
                        buffer,
                        snapshot,
                    )

                self.input_callback_count += 1
                self.callback_count += 1
                return 0

            if command == VBVMR_CBCOMMAND_BUFFER_OUT:
                if not lp_data:
                    return 0

                buffer = ctypes.cast(
                    lp_data,
                    ctypes.POINTER(
                        VBVMR_AUDIOBUFFER
                    ),
                ).contents

                snapshot = self.snapshot

                for target in OUTPUT_TARGETS_POTATO:
                    self.process_output_target(
                        target,
                        buffer,
                        snapshot,
                    )

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

            logger.error(
                "Callback error: %s\n%s",
                exc,
                traceback.format_exc(),
            )

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
            logger.warning(
                "VST3 directory not found: %s",
                VST3_FOLDER,
            )

            with self.lock:
                self.plugins = {}

            return

        for root, _, files in os.walk(
            VST3_FOLDER
        ):
            for filename in files:
                if not filename.lower().endswith(
                    ".vst3"
                ):
                    continue

                if "minimeters" in filename.lower():
                    continue

                path = Path(root) / filename
                name = path.stem

                if name not in found:
                    found[name] = str(path)

        with self.lock:
            self.plugins = found

        logger.info(
            "VST3 scan complete: %d files",
            len(found),
        )

    def names(self):
        with self.lock:
            return sorted(self.plugins.keys())

    def get_by_number(self, number):
        names = self.names()

        if 1 <= number <= len(names):
            return self.plugins[
                names[number - 1]
            ]

        return None

    def count(self):
        with self.lock:
            return len(self.plugins)


# ============================================================
# VOICEMEETER REMOTE
# ============================================================

class VoicemeeterRemote:
    CALLBACK = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_long,
    )

    def __init__(self):
        self.python_bits = (
            ctypes.sizeof(ctypes.c_void_p) * 8
        )

        self.windows_bits = (
            64 if sys.maxsize > 2**32 else 32
        )

        self.dll_path = self.find_dll()

        if not self.dll_path:
            raise RuntimeError(
                "VoicemeeterRemote DLL не найден."
            )

        self.dll = ctypes.WinDLL(
            self.dll_path
        )

        self.configure()

        self.callback_function = None
        self.logged_in = False
        self.callback_registered = False
        self.callback_started = False
        self.detected_type = None
        self.detected_version = None

    @staticmethod
    def find_dll():
        dll_name = (
            "VoicemeeterRemote64.dll"
            if ctypes.sizeof(
                ctypes.c_void_p
            ) == 8
            else "VoicemeeterRemote.dll"
        )

        candidates = [
            Path(
                r"C:\Program Files (x86)\VB\Voicemeeter"
            ) / dll_name,
            Path(
                r"C:\Program Files\VB\Voicemeeter"
            ) / dll_name,
            Path(
                r"C:\Program Files (x86)\VB-Audio\Voicemeeter"
            ) / dll_name,
            Path(
                r"C:\Program Files\VB-Audio\Voicemeeter"
            ) / dll_name,
        ]

        for path in candidates:
            if path.is_file():
                return str(path)

        registry_paths = [
            (
                r"SOFTWARE\Microsoft\Windows"
                r"\CurrentVersion\Uninstall"
                r"\VB:Voicemeeter "
                r"{17359A74-1236-5467}"
            ),
            (
                r"SOFTWARE\WOW6432Node\Microsoft"
                r"\Windows\CurrentVersion\Uninstall"
                r"\VB:Voicemeeter "
                r"{17359A74-1236-5467}"
            ),
        ]

        for key_path in registry_paths:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    key_path,
                    0,
                    winreg.KEY_READ,
                ) as key:
                    uninstall = winreg.QueryValueEx(
                        key,
                        "UninstallString",
                    )[0]

                folder = Path(
                    os.path.dirname(
                        str(uninstall).strip('"')
                    )
                )

                candidate = folder / dll_name

                if candidate.is_file():
                    return str(candidate)

            except OSError:
                pass

        return None

    def configure(self):
        self.dll.VBVMR_Login.argtypes = []
        self.dll.VBVMR_Login.restype = ctypes.c_long

        self.dll.VBVMR_Logout.argtypes = []
        self.dll.VBVMR_Logout.restype = ctypes.c_long

        self.dll.VBVMR_RunVoicemeeter.argtypes = [
            ctypes.c_long
        ]
        self.dll.VBVMR_RunVoicemeeter.restype = ctypes.c_long

        self.dll.VBVMR_GetVoicemeeterType.argtypes = [
            ctypes.POINTER(ctypes.c_long)
        ]
        self.dll.VBVMR_GetVoicemeeterType.restype = ctypes.c_long

        self.dll.VBVMR_GetVoicemeeterVersion.argtypes = [
            ctypes.POINTER(ctypes.c_long)
        ]
        self.dll.VBVMR_GetVoicemeeterVersion.restype = ctypes.c_long

        self.dll.VBVMR_IsParametersDirty.argtypes = []
        self.dll.VBVMR_IsParametersDirty.restype = ctypes.c_long

        self.dll.VBVMR_AudioCallbackRegister.argtypes = [
            ctypes.c_long,
            self.CALLBACK,
            ctypes.c_void_p,
            ctypes.c_char_p,
        ]
        self.dll.VBVMR_AudioCallbackRegister.restype = ctypes.c_long

        self.dll.VBVMR_AudioCallbackStart.argtypes = []
        self.dll.VBVMR_AudioCallbackStart.restype = ctypes.c_long

        self.dll.VBVMR_AudioCallbackStop.argtypes = []
        self.dll.VBVMR_AudioCallbackStop.restype = ctypes.c_long

        self.dll.VBVMR_AudioCallbackUnregister.argtypes = []
        self.dll.VBVMR_AudioCallbackUnregister.restype = ctypes.c_long

    def get_type(self):
        value = ctypes.c_long()

        result = self.dll.VBVMR_GetVoicemeeterType(
            ctypes.byref(value)
        )

        if result == 0:
            return int(value.value)

        return None

    def get_version(self):
        value = ctypes.c_long()

        result = self.dll.VBVMR_GetVoicemeeterVersion(
            ctypes.byref(value)
        )

        if result != 0:
            return None

        raw = value.value & 0xFFFFFFFF

        return (
            f"{(raw >> 24) & 0xFF}."
            f"{(raw >> 16) & 0xFF}."
            f"{(raw >> 8) & 0xFF}."
            f"{raw & 0xFF}"
        )

    def get_type_name(self):
        return VM_NAMES.get(
            self.detected_type,
            "Unknown",
        )

    def probe_connection(self):
        try:
            dirty = int(
                self.dll.VBVMR_IsParametersDirty()
            )

            if dirty < 0:
                return False, None

            vm_type = self.get_type()

            if vm_type not in VM_NAMES:
                return False, None

            self.detected_type = vm_type
            self.detected_version = self.get_version()

            return True, vm_type

        except Exception:
            return False, None

    def run_voicemeeter(
        self,
        requested_type,
    ):
        run_map = (
            RUN_IDS_64
            if self.python_bits == 64
            else RUN_IDS_32
        )

        result = int(
            self.dll.VBVMR_RunVoicemeeter(
                run_map[requested_type]
            )
        )

        if result != 0:
            raise RuntimeError(
                f"VBVMR_RunVoicemeeter "
                f"вернул {result}"
            )

        time.sleep(1.0)

    def login(self, preferred_type=VM_TYPE_POTATO):
        result = int(
            self.dll.VBVMR_Login()
        )

        if result < 0:
            raise RuntimeError(
                f"VBVMR_Login вернул {result}"
            )

        self.logged_in = True

        connected, vm_type = (
            self.probe_connection()
        )

        if not connected:
            self.run_voicemeeter(
                preferred_type
            )

            deadline = time.monotonic() + 10.0

            while time.monotonic() < deadline:
                connected, vm_type = (
                    self.probe_connection()
                )

                if connected:
                    break

                time.sleep(0.25)

        if not connected:
            raise RuntimeError(
                "Активный Voicemeeter не обнаружен."
            )

        self.detected_type = vm_type
        self.detected_version = self.get_version()

        logger.info(
            "Detected Voicemeeter: %s %s",
            self.get_type_name(),
            self.detected_version,
        )

        return vm_type

    def register_callback(self, callback_func):
        if self.callback_registered:
            return

        self.callback_function = self.CALLBACK(
            callback_func
        )

        mode = (
            VBVMR_AUDIOCALLBACK_IN
            | VBVMR_AUDIOCALLBACK_OUT
        )

        result = int(
            self.dll.VBVMR_AudioCallbackRegister(
                mode,
                self.callback_function,
                None,
                APP_NAME.encode("ascii") + b"\0",
            )
        )

        if result != 0:
            self.callback_function = None

            raise RuntimeError(
                f"AudioCallbackRegister "
                f"вернул {result}"
            )

        self.callback_registered = True

        logger.info(
            "Audio callback registered"
        )

    def start_callback(self):
        if not self.callback_registered:
            raise RuntimeError(
                "Callback не зарегистрирован."
            )

        if self.callback_started:
            return

        result = int(
            self.dll.VBVMR_AudioCallbackStart()
        )

        if result != 0:
            raise RuntimeError(
                f"AudioCallbackStart "
                f"вернул {result}"
            )

        self.callback_started = True

        logger.info(
            "Audio callback started"
        )

    def stop_callback(self):
        if self.callback_started:
            try:
                self.dll.VBVMR_AudioCallbackStop()
            finally:
                self.callback_started = False

                logger.info(
                    "Audio callback stopped"
                )

    def unregister_callback(self):
        if self.callback_registered:
            try:
                self.dll.VBVMR_AudioCallbackUnregister()
            finally:
                self.callback_registered = False

        self.callback_function = None

    def logout(self):
        if self.logged_in:
            try:
                self.dll.VBVMR_Logout()
            finally:
                self.logged_in = False


# ============================================================
# ROUTING PROFILE
# ============================================================

VM_STRIP_COUNTS = {
    VM_TYPE_STANDARD: 3,
    VM_TYPE_BANANA: 5,
    VM_TYPE_POTATO: 8,
}

VM_BUS_COUNTS = {
    VM_TYPE_STANDARD: {"A": 1, "B": 1},
    VM_TYPE_BANANA: {"A": 3, "B": 2},
    VM_TYPE_POTATO: {"A": 5, "B": 3},
}


class RoutingProfileManager:
    def __init__(self, vm_type):
        self.vm_type = vm_type

    def set_type(self, vm_type):
        self.vm_type = vm_type

    def strip_count(self):
        return VM_STRIP_COUNTS.get(
            self.vm_type,
            0,
        )

    def bus_counts(self):
        return VM_BUS_COUNTS.get(
            self.vm_type,
            {"A": 0, "B": 0},
        )


# ============================================================
# PRESETS
# ============================================================

def get_plugin_parameter_state(plugin):
    result = {}

    try:
        for name in plugin.parameters.keys():
            try:
                value = getattr(plugin, name)

                if isinstance(value, np.generic):
                    value = value.item()

                if isinstance(
                    value,
                    (
                        str,
                        int,
                        float,
                        bool,
                        type(None),
                    ),
                ):
                    result[name] = value

            except Exception:
                pass

    except Exception:
        pass

    return result


def serialize_chain(chain):
    slots = []

    for entry in chain.slots:
        if entry is None:
            slots.append(None)
            continue

        slots.append(
            {
                "path": entry["path"],
                "plugin_name": entry["plugin"].name,
                "params": get_plugin_parameter_state(
                    entry["plugin"]
                ),
            }
        )

    return {
        "bypass": chain.bypass,
        "slots": slots,
    }


def build_state(engine):
    return {
        "version": APP_VERSION,
        "inputs": {
            name: serialize_chain(chain)
            for name, chain in engine.input_chains.items()
        },
        "outputs": {
            name: serialize_chain(chain)
            for name, chain in engine.output_chains.items()
        },
    }


def save_state(engine, filename):
    filename = Path(filename)
    filename.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_file = Path(f"{filename}.tmp")

    with temp_file.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            build_state(engine),
            file,
            indent=4,
            ensure_ascii=False,
        )

        file.flush()
        os.fsync(file.fileno())

    os.replace(
        temp_file,
        filename,
    )

    logger.info(
        "State saved: %s",
        filename,
    )


def stage_chain(target, data):
    chain = FXChain(target)

    if not isinstance(data, dict):
        return chain

    chain.bypass = bool(
        data.get("bypass", False)
    )

    slots = data.get(
        "slots",
        [],
    )

    if not isinstance(slots, list):
        raise ValueError(
            f"Неверный список слотов: {target}"
        )

    for index, slot_data in enumerate(
        slots[:MAX_FX_SLOTS]
    ):
        if slot_data is None:
            continue

        path = slot_data.get("path")

        if not path:
            raise FileNotFoundError(
                f"В пресете нет VST для "
                f"{target} / {index + 1}"
            )

        path_obj = Path(path)

        if not path_obj.is_file():
            raise FileNotFoundError(
                f"VST не найден:\n{path_obj}"
            )

        logger.info(
            "Loading preset VST: %s",
            path_obj,
        )

        plugin = load_plugin(
            str(path_obj)
        )

        if not getattr(
            plugin,
            "is_effect",
            False,
        ):
            raise TypeError(
                f"{plugin.name} "
                "не является audio effect."
            )

        params = slot_data.get(
            "params",
            {},
        )

        if isinstance(params, dict):
            for name, value in params.items():
                try:
                    if hasattr(plugin, name):
                        setattr(
                            plugin,
                            name,
                            value,
                        )
                except Exception:
                    logger.warning(
                        "Не удалось восстановить "
                        "параметр %s у %s",
                        name,
                        plugin.name,
                    )

        chain.slots[index] = {
            "plugin": plugin,
            "path": str(path_obj),
        }

    return chain


def load_state_atomic(
    engine,
    filename,
):
    filename = Path(filename)

    if not filename.is_file():
        raise FileNotFoundError(
            f"Пресет не найден:\n{filename}"
        )

    with filename.open(
        "r",
        encoding="utf-8",
    ) as file:
        state = json.load(file)

    staged_inputs = {
        name: stage_chain(
            name,
            state.get(
                "inputs",
                {},
            ).get(
                name,
                {},
            ),
        )
        for name in INPUT_TARGETS
    }

    staged_outputs = {
        name: stage_chain(
            name,
            state.get(
                "outputs",
                {},
            ).get(
                name,
                {},
            ),
        )
        for name in OUTPUT_TARGETS_POTATO
    }

    staged_snapshot = (
        engine.build_snapshot_from_chains(
            staged_inputs,
            staged_outputs,
        )
    )

    engine.input_chains = staged_inputs
    engine.output_chains = staged_outputs
    engine.swap_snapshot(
        staged_snapshot
    )

    logger.info(
        "Preset committed atomically: %s",
        filename,
    )


def list_presets():
    return sorted(
        file.stem
        for file in PRESET_DIR.glob("*.json")
    )


# ============================================================
# FILE DIALOGS
# ============================================================

def choose_open_preset():
    root = Tk()
    root.withdraw()

    try:
        root.attributes("-topmost", True)

        return filedialog.askopenfilename(
            title="Открыть пресет Potato FX",
            initialdir=str(
                PRESET_DIR.resolve()
            ),
            filetypes=[
                ("PotatoFX presets", "*.json"),
                ("JSON files", "*.json"),
                ("Все файлы", "*.*"),
            ],
        )

    finally:
        root.destroy()


def choose_save_preset():
    root = Tk()
    root.withdraw()

    try:
        root.attributes("-topmost", True)

        return filedialog.asksaveasfilename(
            title="Сохранить пресет Potato FX",
            initialdir=str(
                PRESET_DIR.resolve()
            ),
            defaultextension=".json",
            filetypes=[
                ("PotatoFX presets", "*.json"),
                ("JSON files", "*.json"),
            ],
        )

    finally:
        root.destroy()


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

            asio_hostapis = {
                i
                for i, hostapi in enumerate(
                    hostapis
                )
                if "ASIO"
                in str(
                    hostapi.get(
                        "name",
                        "",
                    )
                ).upper()
            }

            for index, device in enumerate(
                devices
            ):
                hostapi_index = int(
                    device["hostapi"]
                )

                if hostapi_index not in asio_hostapis:
                    continue

                found.append(
                    {
                        "index": index,
                        "name": str(
                            device["name"]
                        ),
                        "hostapi": str(
                            hostapis[
                                hostapi_index
                            ].get(
                                "name",
                                "",
                            )
                        ),
                        "inputs": int(
                            device[
                                "max_input_channels"
                            ]
                        ),
                        "outputs": int(
                            device[
                                "max_output_channels"
                            ]
                        ),
                        "sample_rate": float(
                            device[
                                "default_samplerate"
                            ]
                        ),
                    }
                )

            self.devices = found

            logger.info(
                "ASIO scan complete: %d devices",
                len(found),
            )

        except Exception as exc:
            self.devices = []

            logger.error(
                "ASIO scan failed: %s\n%s",
                exc,
                traceback.format_exc(),
            )

    def voicemeeter_devices(self):
        return [
            device
            for device in self.devices
            if "voicemeeter"
            in device["name"].lower()
        ]

    def print_devices(self):
        if not self.devices:
            console.print(
                "[yellow]ASIO устройства не найдены.[/yellow]"
            )
            return

        table = Table(
            title="ASIO устройства",
            box=box.ROUNDED,
        )

        table.add_column("#")
        table.add_column("Устройство")
        table.add_column("Host API")
        table.add_column("I/O")
        table.add_column("SR")

        for number, device in enumerate(
            self.devices,
            start=1,
        ):
            table.add_row(
                str(number),
                device["name"],
                device["hostapi"],
                f"{device['inputs']} / {device['outputs']}",
                f"{device['sample_rate']:.0f} Hz",
            )

        console.print(table)

    def select(self):
        self.refresh()

        if not self.devices:
            return None

        self.print_devices()

        number = IntPrompt.ask(
            "Номер ASIO",
            choices=[
                str(i)
                for i in range(
                    1,
                    len(self.devices) + 1,
                )
            ],
        )

        self.selected_device = self.devices[
            number - 1
        ]

        logger.info(
            "Selected ASIO: %s",
            self.selected_device["name"],
        )

        return self.selected_device


# ============================================================
# GUI
# ============================================================

class GUIManager:
    def __init__(self):
        self.events = {}
        self.lock = threading.Lock()

    def create_event(self, target, slot):
        key = f"{target}_{slot}"
        event = threading.Event()

        with self.lock:
            old = self.events.get(key)

            if old:
                old.set()

            self.events[key] = event

        return event

    def close_window(self, target, slot):
        key = f"{target}_{slot}"

        with self.lock:
            event = self.events.pop(
                key,
                None,
            )

        if event:
            event.set()

    def close_all(self):
        with self.lock:
            events = list(
                self.events.values()
            )
            self.events.clear()

        for event in events:
            event.set()


def open_plugin_gui(
    engine,
    gui_manager,
    target,
    slot,
):
    chain = engine.get_chain(target)
    entry = chain.slots[slot - 1]

    if entry is None:
        raise RuntimeError("Слот пуст.")

    plugin = entry["plugin"]

    if not hasattr(plugin, "show_editor"):
        raise RuntimeError(
            f"{plugin.name} не имеет GUI."
        )

    close_event = gui_manager.create_event(
        target,
        slot,
    )

    try:
        logger.info(
            "Opening GUI '%s' on %s slot %d",
            plugin.name,
            target,
            slot,
        )

        plugin.show_editor(
            close_event
        )

    finally:
        gui_manager.close_window(
            target,
            slot,
        )

        logger.info(
            "GUI closed: %s / slot %d",
            plugin.name,
            slot,
        )


# ============================================================
# TASK QUEUE
# ============================================================

class MainTask:
    def __init__(self, command, *args):
        self.command = command
        self.args = args
        self.event = threading.Event()
        self.result = None
        self.error = None


main_tasks = queue.Queue()


def submit_main_task(
    command,
    *args,
    wait=True,
    timeout=None,
):
    task = MainTask(
        command,
        *args,
    )

    main_tasks.put(task)

    if not wait:
        return task

    if not task.event.wait(timeout):
        raise TimeoutError(
            f"Операция '{command}' "
            "превысила таймаут."
        )

    if task.error:
        raise task.error

    return task.result


def process_main_task(
    task,
    engine,
    gui_manager,
):
    try:
        if task.command == "add":
            task.result = engine.add_plugin(
                task.args[0],
                task.args[1],
                task.args[2],
            )

        elif task.command == "remove":
            engine.remove_plugin(
                task.args[0],
                task.args[1],
            )

        elif task.command == "bypass":
            task.result = engine.toggle_bypass(
                task.args[0]
            )

        elif task.command == "clear":
            engine.clear_chain(
                task.args[0]
            )

        elif task.command == "save":
            save_state(
                engine,
                task.args[0],
            )

        elif task.command == "load":
            load_state_atomic(
                engine,
                task.args[0],
            )

        elif task.command == "gui":
            open_plugin_gui(
                engine,
                gui_manager,
                task.args[0],
                task.args[1],
            )

        else:
            raise ValueError(
                f"Неизвестная операция: "
                f"{task.command}"
            )

    except BaseException as exc:
        task.error = exc

        logger.error(
            "Main task '%s' failed: %s\n%s",
            task.command,
            exc,
            traceback.format_exc(),
        )

    finally:
        task.event.set()


def main_thread_loop(
    engine,
    gui_manager,
    stop_event,
):
    while not stop_event.is_set():
        try:
            task = main_tasks.get(
                timeout=0.1
            )
        except queue.Empty:
            continue

        if task is None:
            break

        process_main_task(
            task,
            engine,
            gui_manager,
        )


# ============================================================
# VM WATCHER
# ============================================================

def vm_watcher(
    vm,
    engine,
    routing_manager,
    stop_event,
):
    previous_type = vm.detected_type
    previous_connected = True

    while not stop_event.wait(0.5):
        connected, vm_type = (
            vm.probe_connection()
        )

        if not connected:
            if previous_connected:
                logger.warning(
                    "Voicemeeter disconnected."
                )

                try:
                    vm.stop_callback()
                except Exception:
                    pass

                try:
                    vm.unregister_callback()
                except Exception:
                    pass

            previous_connected = False
            continue

        if not previous_connected:
            logger.info(
                "Voicemeeter reconnected."
            )

        previous_connected = True

        if vm_type != previous_type:
            logger.info(
                "Voicemeeter changed: %s -> %s",
                VM_NAMES.get(
                    previous_type,
                    "Unknown",
                ),
                VM_NAMES.get(
                    vm_type,
                    "Unknown",
                ),
            )

            routing_manager.set_type(
                vm_type
            )

            try:
                vm.stop_callback()
            except Exception:
                pass

            try:
                vm.unregister_callback()
            except Exception:
                pass

            if vm_type == VM_TYPE_POTATO:
                try:
                    vm.register_callback(
                        engine.callback
                    )
                    vm.start_callback()
                except Exception:
                    logger.exception(
                        "Failed to restart Potato DSP"
                    )

            previous_type = vm_type


# ============================================================
# MENU SELECTORS
# ============================================================

def select_target(vm_type):
    targets = target_list_for_vm(vm_type)

    screen_title(
        "Выбор цели",
        "Выберите канал или шину",
    )

    if not targets:
        console.print(
            "[red]Нет доступных целей.[/red]"
        )
        pause()
        return None

    for number, target in enumerate(
        targets,
        start=1,
    ):
        info = ALL_TARGETS[target]

        console.print(
            f"{number:2}. "
            f"[bold]{target}[/bold] "
            f"[dim]{info['channels']}ch[/dim]"
        )

    number = IntPrompt.ask(
        "Номер",
        choices=[
            str(i)
            for i in range(
                1,
                len(targets) + 1,
            )
        ],
    )

    return targets[number - 1]


def select_slot(
    chain,
    title="Выбор слота",
):
    screen_title(
        title,
        "Выберите слот FX",
    )

    for number, entry in enumerate(
        chain.slots,
        start=1,
    ):
        if entry is None:
            console.print(
                f"{number}. [dim]Пусто[/dim]"
            )
        else:
            console.print(
                f"{number}. "
                f"[green]{entry['plugin'].name}[/green]"
            )

    return IntPrompt.ask(
        "Слот",
        choices=[
            str(i)
            for i in range(
                1,
                MAX_FX_SLOTS + 1,
            )
        ],
    )


def select_plugin(manager):
    names = manager.names()

    screen_title(
        "Выбор VST3",
        f"Найдено файлов: {len(names)}",
    )

    if not names:
        console.print(
            "[red]VST3 не найдены.[/red]"
        )
        pause()
        return None

    for number, name in enumerate(
        names,
        start=1,
    ):
        console.print(
            f"{number:3}. {name}"
        )

    number = IntPrompt.ask(
        "Номер VST3",
        choices=[
            str(i)
            for i in range(
                1,
                len(names) + 1,
            )
        ],
    )

    return manager.get_by_number(
        number
    )


# ============================================================
# DASHBOARD
# ============================================================

def format_chain(chain):
    plugins = [
        entry["plugin"].name
        for entry in chain.slots
        if entry is not None
    ]

    if not plugins:
        return "[dim]—[/dim]"

    text = " → ".join(plugins)

    if chain.bypass:
        return (
            "[yellow]BYPASS[/yellow] "
            + text
        )

    return text


def display_dashboard(
    engine,
    vm,
    plugin_manager,
    asio_manager,
):
    clear_screen()

    vm_name = VM_NAMES.get(
        vm.detected_type,
        "Неизвестно",
    )

    asio_name = (
        asio_manager.selected_device["name"]
        if asio_manager.selected_device
        else "не выбран"
    )

    dsp_state = (
        "[green]ON[/green]"
        if engine.running
        else "[red]OFF[/red]"
    )

    console.print(
        Panel(
            (
                f"[bold cyan]"
                f"Potato FX DSP v{APP_VERSION}"
                f"[/bold cyan]\n"
                f"{vm_name} "
                f"{vm.get_version() or '?'}"
                f"  •  "
                f"DSP {dsp_state}"
                f"  •  "
                f"{engine.sample_rate} Hz"
                f" / "
                f"{engine.block_size}"
                f"  •  "
                f"VST3 {plugin_manager.count()}"
            ),
            border_style="cyan",
        )
    )

    console.print(
        f"[dim]ASIO:[/dim] {asio_name}"
    )

    input_table = Table(
        title="Inputs",
        box=box.SIMPLE_HEAD,
        show_edge=False,
    )

    input_table.add_column(
        "Target",
        width=8,
    )
    input_table.add_column(
        "FX Chain"
    )

    for name, chain in (
        engine.input_chains.items()
    ):
        input_table.add_row(
            name,
            format_chain(chain),
        )

    console.print(
        input_table
    )

    output_table = Table(
        title="Buses",
        box=box.SIMPLE_HEAD,
        show_edge=False,
    )

    output_table.add_column(
        "Bus",
        width=8,
    )
    output_table.add_column(
        "FX Chain"
    )

    for name, chain in (
        engine.output_chains.items()
    ):
        output_table.add_row(
            name,
            format_chain(chain),
        )

    console.print(
        output_table
    )

    console.print()

    console.print(
        "[bold yellow]Меню[/bold yellow]"
    )

    console.print(
        " [cyan]1[/cyan] Добавить FX"
        "   "
        "[cyan]2[/cyan] Удалить FX"
        "   "
        "[cyan]3[/cyan] Bypass"
    )

    console.print(
        " [cyan]4[/cyan] GUI"
        "           "
        "[cyan]5[/cyan] Закрыть GUI"
        "   "
        "[cyan]6[/cyan] Закрыть все GUI"
    )

    console.print(
        " [cyan]7[/cyan] Очистить цепочку"
        " "
        "[cyan]8[/cyan] Цепочка"
    )

    console.print(
        " [cyan]9[/cyan] Сохранить"
        "       "
        "[cyan]10[/cyan] Загрузить"
    )

    console.print(
        " [cyan]11[/cyan] Пресеты"
        "       "
        "[cyan]12[/cyan] ASIO"
    )

    console.print(
        " [cyan]13[/cyan] Выбрать ASIO"
        " "
        "[cyan]14[/cyan] Voicemeeter"
    )

    console.print(
        " [cyan]15[/cyan] Обновить VST3"
        " "
        "[cyan]0[/cyan] DSP Status"
    )

    console.print(
        " [cyan]q[/cyan] Выход"
    )


# ============================================================
# ACTIONS
# ============================================================

def action_add_fx(
    engine,
    vm,
    plugin_manager,
):
    target = select_target(
        vm.detected_type
    )

    if target is None:
        return

    chain = engine.get_chain(
        target
    )

    slot = chain.find_free_slot()

    if slot is None:
        result_screen(
            "Нет свободных слотов",
            f"{target}: все {MAX_FX_SLOTS} слота заняты.",
            "yellow",
        )
        return

    path = select_plugin(
        plugin_manager
    )

    if path is None:
        return

    screen_title(
        "Загрузка FX",
        f"{target} / Slot {slot}",
    )

    try:
        plugin = submit_main_task(
            "add",
            target,
            slot,
            path,
            wait=True,
            timeout=60,
        )

        result_screen(
            "FX добавлен",
            (
                f"[green]{plugin.name}[/green]\n\n"
                f"Target: {target}\n"
                f"Slot: {slot}"
            ),
        )

    except Exception as exc:
        result_screen(
            "Ошибка загрузки",
            str(exc),
            "red",
        )


def action_remove_fx(
    engine,
    vm,
):
    target = select_target(
        vm.detected_type
    )

    if target is None:
        return

    chain = engine.get_chain(target)

    if not chain.has_plugins():
        result_screen(
            "Цепочка пуста",
            f"{target}: FX нет.",
            "yellow",
        )
        return

    slot = select_slot(
        chain,
        f"{target} → Удаление FX",
    )

    plugin_name = chain.slots[
        slot - 1
    ]["plugin"].name

    try:
        submit_main_task(
            "remove",
            target,
            slot,
            wait=True,
            timeout=30,
        )

        result_screen(
            "FX удалён",
            (
                f"{plugin_name}\n\n"
                f"{target} / Slot {slot}"
            ),
        )

    except Exception as exc:
        result_screen(
            "Ошибка",
            str(exc),
            "red",
        )


def action_bypass(
    engine,
    vm,
):
    target = select_target(
        vm.detected_type
    )

    if target is None:
        return

    screen_title(
        "Bypass",
        target,
    )

    try:
        state = submit_main_task(
            "bypass",
            target,
            wait=True,
            timeout=30,
        )

        result_screen(
            "Состояние изменено",
            (
                f"{target}\n\n"
                f"[bold]"
                f"{'BYPASS' if state else 'ACTIVE'}"
                f"[/bold]"
            ),
        )

    except Exception as exc:
        result_screen(
            "Ошибка",
            str(exc),
            "red",
        )


def action_open_gui(
    engine,
    vm,
):
    target = select_target(
        vm.detected_type
    )

    if target is None:
        return

    chain = engine.get_chain(
        target
    )

    if not chain.has_plugins():
        result_screen(
            "Цепочка пуста",
            f"{target}: FX нет.",
            "yellow",
        )
        return

    slot = select_slot(
        chain,
        f"{target} → GUI",
    )

    plugin_name = chain.slots[
        slot - 1
    ]["plugin"].name

    screen_title(
        "Открытие GUI",
        f"{plugin_name}\n{target} / Slot {slot}",
    )

    try:
        submit_main_task(
            "gui",
            target,
            slot,
            wait=False,
        )

        console.print(
            Panel(
                (
                    f"[green]"
                    f"GUI открыт: "
                    f"{plugin_name}"
                    f"[/green]\n\n"
                    "[dim]"
                    "Вернитесь в меню после "
                    "закрытия окна плагина."
                    "[/dim]"
                ),
                border_style="green",
            )
        )

        pause()

    except Exception as exc:
        result_screen(
            "Ошибка GUI",
            str(exc),
            "red",
        )


def action_close_gui(
    engine,
    vm,
    gui_manager,
):
    target = select_target(
        vm.detected_type
    )

    if target is None:
        return

    chain = engine.get_chain(
        target
    )

    if not chain.has_plugins():
        result_screen(
            "Цепочка пуста",
            f"{target}: FX нет.",
            "yellow",
        )
        return

    slot = select_slot(
        chain,
        f"{target} → Закрыть GUI",
    )

    gui_manager.close_window(
        target,
        slot,
    )

    result_screen(
        "GUI",
        (
            f"Запрос на закрытие:\n\n"
            f"{target} / Slot {slot}"
        ),
    )


def action_close_all_gui(
    gui_manager,
):
    clear_screen()

    gui_manager.close_all()

    result_screen(
        "GUI",
        "Запрос на закрытие всех GUI отправлен.",
    )


def action_clear_chain(
    engine,
    vm,
):
    target = select_target(
        vm.detected_type
    )

    if target is None:
        return

    screen_title(
        "Очистка цепочки",
        target,
    )

    try:
        submit_main_task(
            "clear",
            target,
            wait=True,
            timeout=30,
        )

        result_screen(
            "Готово",
            (
                f"{target}\n\n"
                "Все FX удалены."
            ),
        )

    except Exception as exc:
        result_screen(
            "Ошибка",
            str(exc),
            "red",
        )


def action_chain_details(
    engine,
    vm,
):
    target = select_target(
        vm.detected_type
    )

    if target is None:
        return

    chain = engine.get_chain(
        target
    )

    screen_title(
        f"{target} → FX Chain",
        "Состояние слотов",
    )

    table = Table(
        box=box.SIMPLE,
    )

    table.add_column(
        "Slot",
        width=6,
    )
    table.add_column(
        "Plugin",
    )
    table.add_column(
        "State",
        width=12,
    )

    for number, entry in enumerate(
        chain.slots,
        start=1,
    ):
        if entry is None:
            table.add_row(
                str(number),
                "[dim]Пусто[/dim]",
                "[dim]Empty[/dim]",
            )
        else:
            table.add_row(
                str(number),
                entry["plugin"].name,
                (
                    "[yellow]Bypass[/yellow]"
                    if chain.bypass
                    else "[green]Active[/green]"
                ),
            )

    console.print(table)
    pause()


def action_save_preset(
    engine,
):
    screen_title(
        "Сохранение пресета",
        "Выберите файл",
    )

    path = choose_save_preset()

    if not path:
        result_screen(
            "Отменено",
            "Сохранение отменено.",
            "yellow",
        )
        return

    try:
        submit_main_task(
            "save",
            path,
            wait=True,
            timeout=30,
        )

        result_screen(
            "Пресет сохранён",
            str(path),
        )

    except Exception as exc:
        result_screen(
            "Ошибка",
            str(exc),
            "red",
        )


def action_load_preset(
    engine,
):
    screen_title(
        "Загрузка пресета",
        "Выберите JSON-файл",
    )

    path = choose_open_preset()

    if not path:
        result_screen(
            "Отменено",
            "Загрузка отменена.",
            "yellow",
        )
        return

    screen_title(
        "Загрузка пресета",
        str(path),
    )

    try:
        submit_main_task(
            "load",
            path,
            wait=True,
            timeout=180,
        )

        result_screen(
            "Пресет загружен",
            (
                "[green]"
                "Конфигурация применена."
                "[/green]"
            ),
        )

    except Exception as exc:
        result_screen(
            "Пресет НЕ применён",
            (
                "[red]"
                f"{exc}\n\n"
                "Текущая рабочая конфигурация "
                "оставлена без изменений."
                "[/red]"
            ),
            "red",
        )


def action_presets():
    screen_title(
        "Пресеты",
        "Сохранённые JSON",
    )

    names = list_presets()

    if not names:
        console.print(
            "[dim]Пресетов нет.[/dim]"
        )
    else:
        table = Table(
            box=box.SIMPLE,
        )

        table.add_column("#", width=6)
        table.add_column("Имя")

        for number, name in enumerate(
            names,
            start=1,
        ):
            table.add_row(
                str(number),
                name,
            )

        console.print(table)

    pause()


def action_asio_browser(
    asio_manager,
):
    screen_title(
        "ASIO устройства",
        "Доступные ASIO host devices",
    )

    asio_manager.refresh()
    asio_manager.print_devices()

    vm_devices = (
        asio_manager.voicemeeter_devices()
    )

    if vm_devices:
        console.print(
            "\n[bold cyan]"
            "Voicemeeter ASIO"
            "[/bold cyan]"
        )

        for device in vm_devices:
            console.print(
                f"  • {device['name']}"
            )

    pause()


def action_select_asio(
    asio_manager,
):
    screen_title(
        "Выбор ASIO",
        "Выберите устройство",
    )

    device = asio_manager.select()

    if device is None:
        result_screen(
            "ASIO",
            "Устройство не выбрано.",
            "yellow",
        )
        return

    result_screen(
        "ASIO выбран",
        (
            f"[green]{device['name']}[/green]\n\n"
            f"Host API: {device['hostapi']}\n"
            f"I/O: {device['inputs']} / "
            f"{device['outputs']}\n"
            f"SR: {device['sample_rate']:.0f} Hz"
        ),
    )


def action_vm_info(
    vm,
    routing_manager,
):
    screen_title(
        "Voicemeeter",
        "Информация о подключении",
    )

    buses = routing_manager.bus_counts()

    table = Table(
        box=box.SIMPLE,
        show_header=False,
    )

    table.add_column(
        "Параметр",
        style="cyan",
    )
    table.add_column(
        "Значение"
    )

    table.add_row(
        "Версия",
        vm.get_version() or "?",
    )
    table.add_row(
        "Тип",
        vm.get_type_name(),
    )
    table.add_row(
        "Type ID",
        str(vm.detected_type),
    )
    table.add_row(
        "Python",
        f"{vm.python_bits}-bit",
    )
    table.add_row(
        "Windows",
        f"{vm.windows_bits}-bit",
    )
    table.add_row(
        "A buses",
        str(buses["A"]),
    )
    table.add_row(
        "B buses",
        str(buses["B"]),
    )
    table.add_row(
        "Strips",
        str(routing_manager.strip_count()),
    )
    table.add_row(
        "Callback",
        (
            "[green]RUNNING[/green]"
            if vm.callback_started
            else "[yellow]STOPPED[/yellow]"
        ),
    )
    table.add_row(
        "DLL",
        vm.dll_path,
    )

    console.print(table)
    pause()


def action_rescan_vst3(
    plugin_manager,
):
    screen_title(
        "Обновление VST3",
        "Сканируется только список файлов",
    )

    plugin_manager.scan()

    result_screen(
        "VST3 обновлены",
        (
            f"Найдено: "
            f"[green]{plugin_manager.count()}[/green]\n\n"
            "Плагины при сканировании не загружаются."
        ),
    )


def action_dsp_status(
    engine,
    vm,
    plugin_manager,
):
    screen_title(
        "DSP Status",
        "Подробная диагностика",
    )

    table = Table(
        box=box.SIMPLE,
        show_header=False,
    )

    table.add_column(
        "Параметр",
        style="cyan",
    )
    table.add_column(
        "Значение"
    )

    table.add_row(
        "Voicemeeter",
        vm.get_type_name(),
    )
    table.add_row(
        "Version",
        vm.get_version() or "?",
    )
    table.add_row(
        "DSP",
        (
            "[green]RUNNING[/green]"
            if engine.running
            else "[red]STOPPED[/red]"
        ),
    )
    table.add_row(
        "Callback",
        (
            "[green]RUNNING[/green]"
            if vm.callback_started
            else "[yellow]STOPPED[/yellow]"
        ),
    )
    table.add_row(
        "Sample Rate",
        f"{engine.sample_rate} Hz",
    )
    table.add_row(
        "Block Size",
        str(engine.block_size),
    )
    table.add_row(
        "Callbacks",
        str(engine.callback_count),
    )
    table.add_row(
        "Input",
        str(engine.input_callback_count),
    )
    table.add_row(
        "Output",
        str(engine.output_callback_count),
    )
    table.add_row(
        "MAIN",
        str(engine.main_callback_count),
    )
    table.add_row(
        "VST3 files",
        str(plugin_manager.count()),
    )
    table.add_row(
        "Last error",
        engine.last_error or "None",
    )

    console.print(table)
    pause()


# ============================================================
# CLI THREAD
# ============================================================

def cli_thread(
    engine,
    vm,
    plugin_manager,
    asio_manager,
    gui_manager,
    routing_manager,
    stop_event,
):
    while not stop_event.is_set():
        try:
            display_dashboard(
                engine,
                vm,
                plugin_manager,
                asio_manager,
            )

            choice = (
                Prompt.ask(
                    "\n[bold green]>>>[/bold green]"
                )
                .strip()
                .lower()
            )

            if choice == "1":
                action_add_fx(
                    engine,
                    vm,
                    plugin_manager,
                )

            elif choice == "2":
                action_remove_fx(
                    engine,
                    vm,
                )

            elif choice == "3":
                action_bypass(
                    engine,
                    vm,
                )

            elif choice == "4":
                action_open_gui(
                    engine,
                    vm,
                )

            elif choice == "5":
                action_close_gui(
                    engine,
                    vm,
                    gui_manager,
                )

            elif choice == "6":
                action_close_all_gui(
                    gui_manager,
                )

            elif choice == "7":
                action_clear_chain(
                    engine,
                    vm,
                )

            elif choice == "8":
                action_chain_details(
                    engine,
                    vm,
                )

            elif choice == "9":
                action_save_preset(
                    engine,
                )

            elif choice == "10":
                action_load_preset(
                    engine,
                )

            elif choice == "11":
                action_presets()

            elif choice == "12":
                action_asio_browser(
                    asio_manager,
                )

            elif choice == "13":
                action_select_asio(
                    asio_manager,
                )

            elif choice == "14":
                action_vm_info(
                    vm,
                    routing_manager,
                )

            elif choice == "15":
                action_rescan_vst3(
                    plugin_manager,
                )

            elif choice == "0":
                action_dsp_status(
                    engine,
                    vm,
                    plugin_manager,
                )

            elif choice == "q":
                stop_event.set()
                main_tasks.put(None)
                break

            else:
                result_screen(
                    "Неизвестная команда",
                    (
                        f"Команда "
                        f"[yellow]{choice}[/yellow] "
                        "не найдена."
                    ),
                    "yellow",
                )

        except (
            EOFError,
            KeyboardInterrupt,
        ):
            stop_event.set()
            main_tasks.put(None)
            break

        except Exception as exc:
            logger.error(
                "CLI error: %s\n%s",
                exc,
                traceback.format_exc(),
            )

            result_screen(
                "Ошибка",
                str(exc),
                "red",
            )


# ============================================================
# MAIN
# ============================================================

def main():
    vm = None
    cli = None
    watcher = None

    logger.info("=" * 60)
    logger.info("NEW SESSION START")
    logger.info("Potato FX DSP v%s", APP_VERSION)

    engine = DSPEngine()
    plugin_manager = PluginManager()
    asio_manager = ASIODeviceManager()
    gui_manager = GUIManager()
    stop_event = threading.Event()

    try:
        plugin_manager.scan()

        vm = VoicemeeterRemote()

        logger.info(
            "Remote DLL: %s",
            vm.dll_path,
        )

        logger.info(
            "Python=%d-bit Windows=%d-bit",
            vm.python_bits,
            vm.windows_bits,
        )

        for device in (
            asio_manager.voicemeeter_devices()
        ):
            logger.info(
                "Voicemeeter ASIO: %s",
                device["name"],
            )

        vm_type = vm.login(
            preferred_type=VM_TYPE_POTATO
        )

        routing_manager = (
            RoutingProfileManager(
                vm_type
            )
        )

        if vm_type == VM_TYPE_POTATO:
            vm.register_callback(
                engine.callback
            )
            vm.start_callback()
        else:
            logger.warning(
                "Detected %s. "
                "Potato DSP callback disabled.",
                VM_NAMES[vm_type],
            )

        watcher = threading.Thread(
            target=vm_watcher,
            args=(
                vm,
                engine,
                routing_manager,
                stop_event,
            ),
            daemon=True,
            name="VoicemeeterWatcher",
        )

        watcher.start()

        cli = threading.Thread(
            target=cli_thread,
            args=(
                engine,
                vm,
                plugin_manager,
                asio_manager,
                gui_manager,
                routing_manager,
                stop_event,
            ),
            daemon=True,
            name="PotatoFX-CLI",
        )

        cli.start()

        main_thread_loop(
            engine,
            gui_manager,
            stop_event,
        )

    except KeyboardInterrupt:
        stop_event.set()

    except Exception as exc:
        logger.critical(
            "Fatal error: %s\n%s",
            exc,
            traceback.format_exc(),
        )

        clear_screen()

        console.print(
            Panel(
                f"[bold red]{exc}[/bold red]",
                title="Критическая ошибка",
                border_style="red",
            )
        )

        pause(
            "Нажмите Enter для выхода..."
        )

        stop_event.set()

    finally:
        stop_event.set()

        gui_manager.close_all()
        main_tasks.put(None)

        if cli is not None:
            try:
                cli.join(timeout=1.0)
            except Exception:
                pass

        if watcher is not None:
            try:
                watcher.join(timeout=1.0)
            except Exception:
                pass

        if vm is not None:
            try:
                vm.stop_callback()
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

        logger.info("FX DSP stopped")
        logger.info("SESSION END")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()