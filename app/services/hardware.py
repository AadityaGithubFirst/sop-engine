"""Machine profiling and model recommendation.

The installer and the web application both need to answer one question: what
can this computer actually run? A 3B model on an 8 GB office laptop and a 70B
model on a GPU server are the same product with different weights, and the user
should never have to work that out themselves.

Detection uses only the standard library plus optional vendor CLIs that are
already present when relevant (`nvidia-smi`, `wmic`, `sysctl`). Nothing is
installed and nothing is sent off the machine.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 8  # seconds; vendor CLIs occasionally hang


# ---------------------------------------------------------------------------
# Model tiers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelTier:
    """A model the engine knows how to recommend."""

    key: str
    model: str
    label: str
    parameters: str
    download_gb: float
    min_ram_gb: int
    min_vram_gb: int          # 0 = runs acceptably on CPU
    quality: str
    speed_note: str
    description: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Ordered weakest to strongest. `min_ram_gb` assumes the model is the main
# workload on the machine, with headroom for the OS and a browser.
MODEL_TIERS: List[ModelTier] = [
    ModelTier(
        key="minimal",
        model="llama3.2:1b",
        label="Very Light",
        parameters="1B",
        download_gb=1.3,
        min_ram_gb=4,
        min_vram_gb=0,
        quality="Basic",
        speed_note="Fastest, roughly 1-3 minutes per document",
        description=(
            "For older or low-memory computers. Produces a complete, valid SOP, "
            "but with shallower technical detail. The structural guarantees still hold."
        ),
    ),
    ModelTier(
        key="light",
        model="llama3.2:3b",
        label="Light",
        parameters="3B",
        download_gb=2.0,
        min_ram_gb=8,
        min_vram_gb=0,
        quality="Good",
        speed_note="Fast, roughly 2-5 minutes per document",
        description=(
            "A sensible default for a standard office laptop with 8 GB of memory. "
            "Good balance of speed and detail."
        ),
    ),
    ModelTier(
        key="standard",
        model="llama3.1:8b",
        label="Standard",
        parameters="8B",
        download_gb=4.7,
        min_ram_gb=16,
        min_vram_gb=0,
        quality="High",
        speed_note="Moderate, roughly 4-8 minutes on CPU, under 2 minutes on a GPU",
        description=(
            "The recommended quality level for official documents. Strong instruction "
            "following and consistent table formatting."
        ),
    ),
    ModelTier(
        key="reasoning",
        model="deepseek-r1:8b",
        label="Standard (Reasoning)",
        parameters="8B",
        download_gb=4.9,
        min_ram_gb=16,
        min_vram_gb=0,
        quality="High, deeper analysis",
        speed_note="Slower, roughly 6-12 minutes on CPU",
        description=(
            "Thinks step by step before writing, which improves the technical depth of "
            "the execution phases. Slower, and its internal reasoning is stripped from "
            "the final document."
        ),
    ),
    ModelTier(
        key="advanced",
        model="qwen2.5:14b",
        label="Advanced",
        parameters="14B",
        download_gb=9.0,
        min_ram_gb=32,
        min_vram_gb=12,
        quality="Very High",
        speed_note="Needs a GPU for practical speed",
        description=(
            "Noticeably richer technical inference and better long-table consistency. "
            "Recommended when a dedicated GPU is available."
        ),
    ),
    ModelTier(
        key="maximum",
        model="llama3.3:70b",
        label="Maximum",
        parameters="70B",
        download_gb=43.0,
        min_ram_gb=64,
        min_vram_gb=40,
        quality="Highest",
        speed_note="Requires a server-class GPU",
        description=(
            "Best available document quality. Intended for a shared GPU server, not a "
            "personal computer."
        ),
    ),
]

MODEL_BY_KEY = {tier.key: tier for tier in MODEL_TIERS}
MODEL_BY_NAME = {tier.model: tier for tier in MODEL_TIERS}


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
def _run(command: List[str]) -> str:
    """Run a probe command, returning '' on any failure."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Probe %s failed: %s", command[0], exc)
        return ""


def total_ram_gb() -> float:
    """Physical RAM in GB, or 0.0 when it cannot be determined."""
    # Linux / most POSIX
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return round(pages * page_size / (1024**3), 1)
    except (AttributeError, ValueError, OSError):
        pass

    if platform.system() == "Windows":
        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        try:
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.ullTotalPhys / (1024**3), 1)
        except (AttributeError, OSError) as exc:
            logger.debug("GlobalMemoryStatusEx failed: %s", exc)

    if platform.system() == "Darwin":
        output = _run(["sysctl", "-n", "hw.memsize"])
        if output.isdigit():
            return round(int(output) / (1024**3), 1)

    return 0.0


def free_disk_gb(path: Optional[str] = None) -> float:
    """Free space in GB on the volume holding `path`."""
    try:
        usage = shutil.disk_usage(path or os.path.expanduser("~"))
        return round(usage.free / (1024**3), 1)
    except OSError:
        return 0.0


def cpu_name() -> str:
    """Human-readable processor name."""
    system = platform.system()
    if system == "Windows":
        name = os.environ.get("PROCESSOR_IDENTIFIER", "")
        if name:
            return name.split(",")[0].strip()
    elif system == "Linux":
        try:
            for line in open("/proc/cpuinfo", encoding="utf-8", errors="ignore"):
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    elif system == "Darwin":
        name = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if name:
            return name
    return platform.processor() or platform.machine() or "Unknown processor"


def detect_gpu() -> Dict[str, Any]:
    """Best-effort GPU detection.

    Returns vendor, name, and VRAM in GB. Apple Silicon is reported as a
    unified-memory GPU because Ollama uses Metal there, and the practical
    VRAM ceiling is a fraction of system RAM.
    """
    result: Dict[str, Any] = {"present": False, "vendor": "", "name": "", "vram_gb": 0.0}

    # NVIDIA - authoritative when the driver is installed.
    if shutil.which("nvidia-smi"):
        output = _run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
        )
        if output:
            first = output.splitlines()[0]
            parts = [part.strip() for part in first.split(",")]
            if len(parts) >= 2 and parts[1].replace(".", "").isdigit():
                result.update(
                    present=True,
                    vendor="NVIDIA",
                    name=parts[0],
                    vram_gb=round(float(parts[1]) / 1024, 1),
                )
                return result

    system = platform.system()

    if system == "Darwin" and platform.machine() == "arm64":
        ram = total_ram_gb()
        result.update(
            present=True,
            vendor="Apple",
            name=f"Apple Silicon ({platform.machine()}) unified memory",
            # Metal can address roughly 70% of unified memory for inference.
            vram_gb=round(ram * 0.7, 1),
        )
        return result

    if system == "Windows":
        # CIM returns structured JSON, which avoids the WMIC CSV column-order
        # trap (its first column is the hostname, not the adapter name).
        output = _run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_VideoController | "
                "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress",
            ]
        )
        adapters: List[Dict[str, Any]] = []
        if output:
            try:
                import json as _json

                parsed = _json.loads(output)
                adapters = parsed if isinstance(parsed, list) else [parsed]
            except (ValueError, TypeError):
                adapters = []

        best_vram, best_name = 0.0, ""
        for adapter in adapters:
            name = str(adapter.get("Name") or "").strip()
            raw_ram = adapter.get("AdapterRAM") or 0
            try:
                vram = round(int(raw_ram) / (1024**3), 1)
            except (TypeError, ValueError):
                vram = 0.0
            # Prefer a discrete adapter even when Windows misreports its VRAM.
            discrete = re.search(r"nvidia|geforce|rtx|gtx|quadro|radeon|rx\s|firepro", name, re.I)
            if vram > best_vram or (discrete and not best_name):
                best_vram = max(best_vram, vram)
                best_name = name or best_name

        if best_name:
            lowered = best_name.lower()
            vendor = (
                "NVIDIA" if re.search(r"nvidia|geforce|rtx|gtx|quadro", lowered)
                else "AMD" if re.search(r"radeon|amd|firepro", lowered)
                else "Intel" if "intel" in lowered
                else "Other"
            )
            result.update(present=True, vendor=vendor, name=best_name, vram_gb=best_vram)
            if vendor == "Intel" and "arc" not in lowered:
                # Integrated graphics share system RAM and give no real speedup.
                result["vram_gb"] = 0.0
            return result

    if system == "Linux" and shutil.which("lspci"):
        output = _run(["lspci"])
        for line in output.splitlines():
            if re.search(r"VGA|3D controller", line, re.IGNORECASE):
                vendor = (
                    "NVIDIA" if "nvidia" in line.lower()
                    else "AMD" if re.search(r"amd|radeon", line, re.IGNORECASE)
                    else "Intel" if "intel" in line.lower()
                    else "Other"
                )
                result.update(
                    present=True,
                    vendor=vendor,
                    name=line.split(":")[-1].strip()[:80],
                    vram_gb=0.0,  # lspci does not report VRAM reliably
                )
                return result

    return result


# ---------------------------------------------------------------------------
# Profile and recommendation
# ---------------------------------------------------------------------------
@dataclass
class MachineProfile:
    """Everything the recommender needs to know about this computer."""

    os_name: str
    os_version: str
    architecture: str
    cpu: str
    cpu_cores: int
    ram_gb: float
    free_disk_gb: float
    gpu_present: bool
    gpu_vendor: str
    gpu_name: str
    vram_gb: float
    tier_label: str = ""
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def profile_machine() -> MachineProfile:
    """Detect this machine's capability profile."""
    gpu = detect_gpu()
    ram = total_ram_gb()
    cores = os.cpu_count() or 1

    profile = MachineProfile(
        os_name=platform.system() or "Unknown",
        os_version=platform.release(),
        architecture=platform.machine(),
        cpu=cpu_name(),
        cpu_cores=cores,
        ram_gb=ram,
        free_disk_gb=free_disk_gb(),
        gpu_present=bool(gpu["present"]),
        gpu_vendor=str(gpu["vendor"]),
        gpu_name=str(gpu["name"]),
        vram_gb=float(gpu["vram_gb"]),
    )

    if profile.vram_gb >= 40:
        profile.tier_label = "GPU server"
    elif profile.vram_gb >= 12:
        profile.tier_label = "Workstation with dedicated GPU"
    elif profile.vram_gb >= 6:
        profile.tier_label = "Laptop or desktop with entry-level GPU"
    elif ram >= 16:
        profile.tier_label = "Standard office computer"
    elif ram >= 8:
        profile.tier_label = "Light office computer"
    else:
        profile.tier_label = "Low-memory computer"

    if ram and ram < 8:
        profile.notes.append(
            "This computer has limited memory. Document generation will work but will be slower."
        )
    if profile.free_disk_gb and profile.free_disk_gb < 10:
        profile.notes.append(
            f"Only {profile.free_disk_gb} GB of disk space is free. Models need several GB."
        )
    if not profile.gpu_present:
        profile.notes.append(
            "No dedicated graphics card detected. The engine will run on the processor, which is slower but fully supported."
        )
    if ram == 0.0:
        profile.notes.append(
            "Memory size could not be detected; the recommendation assumes a standard office computer."
        )
    return profile


def recommend(profile: Optional[MachineProfile] = None) -> Dict[str, Any]:
    """Recommend a model tier for a machine, and rank every other tier.

    A tier is `runnable` when the machine clears its RAM bar and has disk space
    for the download. `accelerated` means it also clears the VRAM bar, so it
    will be fast rather than merely possible.
    """
    profile = profile or profile_machine()
    ram = profile.ram_gb or 16.0  # assume a standard machine if detection failed
    vram = profile.vram_gb
    disk = profile.free_disk_gb or 999.0

    # Reported RAM is always a little under the marketed figure (firmware and
    # integrated graphics reserve some), so a "16 GB" machine reads as ~15.8.
    # Without tolerance every such machine is pushed down a tier for nothing.
    ram_tolerance = 0.94

    options: List[Dict[str, Any]] = []
    for tier in MODEL_TIERS:
        fits_ram = ram >= tier.min_ram_gb * ram_tolerance
        fits_disk = disk >= tier.download_gb + 2  # leave working headroom
        accelerated = tier.min_vram_gb == 0 or vram >= tier.min_vram_gb
        needs_gpu = tier.min_vram_gb > 0

        blockers: List[str] = []
        if not fits_ram:
            blockers.append(f"needs about {tier.min_ram_gb} GB of memory, this machine has {ram:g} GB")
        if not fits_disk:
            blockers.append(f"needs {tier.download_gb} GB free disk space")
        if needs_gpu and not accelerated:
            blockers.append(
                f"needs a {tier.min_vram_gb} GB graphics card to run at a usable speed"
            )

        option = tier.as_dict()
        option.update(
            runnable=fits_ram and fits_disk,
            accelerated=accelerated and (vram > 0 or not needs_gpu),
            recommended=False,
            blockers=blockers,
        )
        options.append(option)

    # Pick the strongest tier that runs comfortably: clears RAM and disk, and
    # either needs no GPU or has one. The reasoning tier is held back unless
    # there is GPU headroom for it - on CPU it is slow enough that a first-time
    # user assumes the application has hung.
    gpu_headroom = vram >= 8
    candidates = [
        option
        for option in options
        if option["runnable"]
        and not option["blockers"]
        and (option["key"] != "reasoning" or gpu_headroom)
    ]
    choice = candidates[-1] if candidates else options[0]  # 1B is the universal floor

    for option in options:
        option["recommended"] = option["key"] == choice["key"]

    return {
        "machine": profile.as_dict(),
        "recommended_key": choice["key"],
        "recommended_model": choice["model"],
        "reason": _reason(profile, choice),
        "options": options,
    }


def _reason(profile: MachineProfile, choice: Dict[str, Any]) -> str:
    """Plain-language justification, written for a non-technical reader."""
    ram = f"{profile.ram_gb:g} GB of memory" if profile.ram_gb else "an unknown amount of memory"
    if profile.gpu_present and profile.vram_gb >= 6:
        hardware = f"{profile.gpu_name} graphics ({profile.vram_gb:g} GB) and {ram}"
    elif profile.gpu_present and profile.vram_gb > 0:
        hardware = f"{profile.gpu_name} graphics and {ram}"
    else:
        hardware = f"{profile.cpu_cores} processor cores and {ram}"
    return (
        f"This computer has {hardware}. The {choice['label']} model "
        f"({choice['parameters']}, {choice['download_gb']} GB download) is the best match: "
        f"{choice['speed_note'].lower()}."
    )


def tier_for_model(model_name: str) -> Optional[ModelTier]:
    """Look up a tier by its Ollama model tag."""
    if model_name in MODEL_BY_NAME:
        return MODEL_BY_NAME[model_name]
    base = model_name.split(":")[0]
    for tier in MODEL_TIERS:
        if tier.model.split(":")[0] == base:
            return tier
    return None
