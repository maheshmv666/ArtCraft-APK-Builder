#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
artcraft_apk.py - build NATIVE Android APKs of the ArtCraft apps from their official source code.

What it does
------------
  1. Fetches the list of ArtCraft "Crafting Apps" (PhotoCraft, VectorCraft, ...).
  2. You pick one.
  3. Finds the app's latest released version on GitHub (or its development branch with --main).
  4. DETECTS the build tools on your computer (JDK, Android SDK + NDK, Rust, cargo-apk2). Anything
     missing is listed, you are told what will be downloaded and where it will be saved, and only
     then is it downloaded - into one cache folder (default: ~/.artcraft-apk).
  5. Downloads the app's source code and compiles it to a native arm64 Android app (Rust/egui,
     no WebView involved), signed and ready to install.

The compile itself is done by the MIT-licensed "artcraft-android" builder by MostafaXS1
(https://github.com/MostafaXS1/artcraft-android), which holds the per-app Android launchers and
patches. This script downloads that builder (pinned to a reviewed commit) and drives it; it does not
replace it. This project is not affiliated with ArtCraft or with the builder's author.

Requirements: Linux x86-64 (on Windows use WSL2), Python 3.11+, git and a C compiler/linker
(on Debian/Ubuntu: sudo apt install git build-essential). Everything else is detected or downloaded.

Examples
--------
  python3 artcraft_apk.py                      # interactive menu
  python3 artcraft_apk.py --list               # just list the apps
  python3 artcraft_apk.py --app photocraft     # no menu
  python3 artcraft_apk.py --app pdfcraft --yes
  python3 artcraft_apk.py --app photocraft --main              # newest development code
  python3 artcraft_apk.py --app photocraft --appname "My Editor" --packagename com.me.editor
  python3 artcraft_apk.py --app photocraft --setup-only        # install tools + fetch source only
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
SEED_REPO = "storytold/photocraft"  # its README has the descriptions of the Crafting Apps
README_URL = "https://raw.githubusercontent.com/{repo}/main/README.md"
APP_OWNER = "storytold"

# The builder that does the actual native compile (MIT licensed). Pinned to the commit that was
# reviewed; use --builder-ref main to follow its newest changes, or --builder-path for a local copy.
BUILDER_REPO = "MostafaXS1/artcraft-android"
BUILDER_PIN = "07ab398456a4356744c78a598599a78ba26f5df4"

TARGET_SDK = 35
BUILD_TOOLS = "35.0.0"
NDK_PINNED = "27.2.12479018"
NDK_MIN_MAJOR = 25
CMDLINE_TOOLS_REV = "11076708"
RUST_TARGET = "aarch64-linux-android"
DEFAULT_MIN_RUST = "1.85"
APK2_MIN_RUST = "1.95"  # cargo-apk2 (the APK packager) itself needs this Rust version
JDK_PREFERRED = (17, 21)
JDK_ACCEPT = range(17, 22)
PLATFORM_PKG = "platforms;android-%d" % TARGET_SDK
BUILD_TOOLS_PKG = "build-tools;%s" % BUILD_TOOLS
NDK_PKG = "ndk;%s" % NDK_PINNED

FALLBACK_APPS = [("photocraft", "PhotoCraft"), ("vectorcraft", "VectorCraft"), ("filmcraft", "FilmCraft"),
                 ("lightcraft", "LightCraft"), ("pdfcraft", "PdfCraft"), ("effectcraft", "EffectCraft"),
                 ("designcraft", "DesignCraft"), ("figcraft", "FigCraft"), ("wordcraft", "WordCraft"),
                 ("soundcraft", "SoundCraft"), ("deckcraft", "DeckCraft"), ("cadcraft", "CADCraft"),
                 ("gridcraft", "GridCraft")]

UA = "artcraft-apk/2.0"


def _host_os() -> str:
    s = platform.system().lower()
    return "windows" if s.startswith("win") else "mac" if s == "darwin" else "linux"


def _host_arch() -> str:
    m = platform.machine().lower()
    return "aarch64" if m in ("arm64", "aarch64") else "x64" if m in ("x86_64", "amd64") else m


OS = _host_os()
ARCH = _host_arch()


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------
def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, total: int, title: str) -> None:
    say("\n[%d/%d] %s" % (n, total, title))


def die(msg: str, code: int = 1) -> None:
    sys.stderr.write("\nERROR: %s\n" % msg)
    sys.exit(code)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def rmtree(path: Path) -> None:
    def _onerror(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            pass

    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(str(path), onerror=_onerror)


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        say("(non-interactive session: continuing)")
        return True
    try:
        ans = input("%s [Y/n] " % question).strip().lower()
    except EOFError:
        return True
    return ans in ("", "y", "yes")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def vtuple(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


# --------------------------------------------------------------------------------------
# Networking, downloads, archives
# --------------------------------------------------------------------------------------
def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": UA})


def http_get(url: str, timeout: int = 30) -> bytes:
    with urllib.request.urlopen(_request(url), timeout=timeout) as r:
        return r.read()


def _progress(label: str, done: int, total: int, final: bool = False) -> None:
    if not sys.stdout.isatty():
        if final:
            say("      %s: %s done" % (label, human(done)))
        return
    if total:
        text = "      %s  %3d%%  (%s / %s)" % (label, done * 100 // total, human(done), human(total))
    else:
        text = "      %s  %s" % (label, human(done))
    sys.stdout.write("\r" + text.ljust(78))
    if final:
        sys.stdout.write("\n")
    sys.stdout.flush()


def download(url: str, dest: Path, label: str, sha256: Optional[str] = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(_request(url), timeout=60) as r, open(str(part), "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, 0.0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                now = time.time()
                if now - last > 0.25:
                    _progress(label, done, total)
                    last = now
            _progress(label, done, total, final=True)
    except Exception:
        if part.exists():
            part.unlink()
        raise
    if sha256 and digest.hexdigest().lower() != sha256.lower():
        part.unlink()
        die("Checksum mismatch for %s (expected %s, got %s). Download discarded."
            % (label, sha256, digest.hexdigest()))
    os.replace(str(part), str(dest))


def _inside(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def extract_zip(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(archive)) as zf:
        for info in zf.infolist():
            target = dest / info.filename
            if not _inside(dest, target):
                die("Unsafe path in archive: %s" % info.filename)
            zf.extract(info, str(dest))
            mode = (info.external_attr >> 16) & 0o777
            if mode and not info.is_dir():
                try:
                    os.chmod(str(target), mode | 0o200)
                except OSError:
                    pass


def extract_tar(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(archive)) as tf:
        members = tf.getmembers()
        for m in members:
            if not _inside(dest, dest / m.name):
                die("Unsafe path in archive: %s" % m.name)
        tf.extractall(str(dest), members=members, filter="tar")


def extract(archive: Path, dest: Path) -> None:
    (extract_zip if archive.name.endswith(".zip") else extract_tar)(archive, dest)


# --------------------------------------------------------------------------------------
# Environment checks
# --------------------------------------------------------------------------------------
def ensure_python311() -> None:
    """The builder reads Cargo manifests with tomllib, which needs Python 3.11+."""
    if sys.version_info >= (3, 11):
        return
    for name in ("python3.14", "python3.13", "python3.12", "python3.11"):
        p = shutil.which(name)
        if p:
            os.execv(p, [p] + sys.argv)
    sys.stderr.write("\nERROR: Python 3.11 or newer is required (this is %d.%d).\n"
                     "       Debian/Ubuntu: sudo apt install python3.12   |   Fedora: sudo dnf install python3\n"
                     % sys.version_info[:2])
    sys.exit(1)


def check_platform() -> None:
    if OS == "windows":
        die("Native Android builds need Linux. On Windows, install WSL2 (Ubuntu) and run this script there:\n"
            "         wsl --install -d Ubuntu     (then open Ubuntu and follow the README)")
    if OS != "linux":
        die("The native builder is written for Linux (GNU tools, bash 4, the Linux Android NDK).\n"
            "       macOS is not supported. Use a Linux machine, a Linux VM, or WSL2 on Windows.")
    if ARCH != "x64":
        die("The Android NDK only ships for x86-64 Linux, and this computer is %s. "
            "Use an x86-64 Linux machine." % ARCH)


def preflight() -> None:
    """Things this script cannot install for you (they need root)."""
    missing = []
    if not shutil.which("git"):
        missing.append("git")
    if not (shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")):
        missing.append("a C compiler/linker (cc)")
    if not shutil.which("bash"):
        missing.append("bash")
    if missing:
        die("Missing system packages: %s\n"
            "       Debian/Ubuntu:  sudo apt install git build-essential\n"
            "       Fedora:         sudo dnf install git gcc\n"
            "       Arch:           sudo pacman -S git base-devel\n"
            "       Then run this script again." % ", ".join(missing))


def python_shim(cache: Path) -> Path:
    """The builder calls `python3` and needs 3.11+; make sure that name points at a new enough one."""
    d = cache / "shims"
    d.mkdir(parents=True, exist_ok=True)
    link = d / "python3"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(sys.executable)
    return d


# --------------------------------------------------------------------------------------
# The builder and the app list
# --------------------------------------------------------------------------------------
def fetch_builder(cache: Path, ref: str, local: Optional[str]) -> Tuple[Path, bool]:
    """Returns (builder_root, is_local). Downloads the builder unless a local copy was given."""
    if local:
        p = Path(local).expanduser().resolve()
        if not (p / "lib" / "build-common.sh").is_file():
            die("--builder-path %s does not look like an artcraft-android checkout "
                "(lib/build-common.sh is missing)." % p)
        return p, True
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", ref)
    dest = cache / "builder" / slug
    immutable = re.fullmatch(r"[0-9a-f]{40}", ref) is not None
    if not (immutable and (dest / ".complete").exists()):
        url = "https://codeload.github.com/%s/tar.gz/%s" % (BUILDER_REPO, ref)
        archive = cache / "downloads" / ("builder-%s.tar.gz" % slug)
        try:
            download(url, archive, "artcraft-android builder (%s)" % ref[:12])
        except urllib.error.HTTPError as e:
            die("Could not download the builder (%s): HTTP %s" % (url, e.code))
        tmp = cache / "downloads" / ("builder-%s-tmp" % slug)
        rmtree(tmp)
        extract(archive, tmp)
        archive.unlink()
        tops = [p for p in tmp.iterdir() if p.is_dir()]
        if len(tops) != 1 or not (tops[0] / "lib" / "build-common.sh").is_file():
            die("The downloaded builder has an unexpected layout.")
        rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tops[0]), str(dest))
        rmtree(tmp)
        (dest / ".complete").write_text(ref + "\n")
    return dest, False


def builder_apps(root: Path) -> List[Tuple[str, str]]:
    """The apps the builder can build, read from its own script."""
    try:
        text = (root / "lib" / "build-common.sh").read_text(errors="replace")
    except OSError:
        return list(FALLBACK_APPS)
    pairs = re.findall(r'^\s*([a-z]+)\)\s*TITLE="([^"]+)"\s*;;', text, re.M)
    return pairs or list(FALLBACK_APPS)


def readme_descriptions() -> Dict[str, str]:
    try:
        md = http_get(README_URL.format(repo=SEED_REPO)).decode("utf-8", "replace")
    except Exception:
        return {}
    rx = re.compile(r"\*\*([^*|]+?)\*\*\s*\|\s*([^|]*?)\s*\|\s*\[GitHub\]\(https://github\.com/[^/\s)]+/[^/\s)]+\)")
    out = {}
    for name, desc in rx.findall(md):
        out[norm(name)] = re.sub(r"\s*[\u00b7|-]?\s*you are here", "", desc.replace("*", "")).strip()
    return out


def discover_apps(root: Path) -> List[Dict[str, str]]:
    descs = readme_descriptions()
    apps = []
    for repo, title in builder_apps(root):
        apps.append({"name": title, "repo": repo, "owner": APP_OWNER,
                     "desc": descs.get(norm(title), descs.get(repo, ""))})
    return apps


def choose_app(apps: List[Dict[str, str]], wanted: Optional[str]) -> Dict[str, str]:
    if wanted:
        if wanted.isdigit() and 1 <= int(wanted) <= len(apps):
            return apps[int(wanted) - 1]
        w = norm(wanted)
        for a in apps:
            if w in (norm(a["name"]), norm(a["repo"])):
                return a
        for a in apps:
            if w and (norm(a["name"]).startswith(w) or norm(a["repo"]).startswith(w)):
                return a
        die("No app matches '%s'. Run with --list to see the choices." % wanted)
    if not sys.stdin.isatty():
        die("No terminal available to ask which app to build. Pass --app NAME (see --list).")
    say("\n    Available ArtCraft apps:")
    for i, a in enumerate(apps, 1):
        say("      %2d) %-12s %s" % (i, a["name"], a["desc"]))
    while True:
        try:
            ans = input("\n    Choose an app [1-%d]: " % len(apps)).strip()
        except EOFError:
            die("No selection made.")
        if ans.isdigit() and 1 <= int(ans) <= len(apps):
            return apps[int(ans) - 1]
        say("    Please type a number between 1 and %d." % len(apps))


# --------------------------------------------------------------------------------------
# Latest version of an app
# --------------------------------------------------------------------------------------
def git_tags(owner: str, repo: str) -> List[str]:
    """Release tags, read with `git ls-remote` (no API, so no rate limit)."""
    r = subprocess.run(["git", "ls-remote", "--tags", "--refs", "https://github.com/%s/%s" % (owner, repo)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        die("Could not list versions of %s/%s:\n%s" % (owner, repo, (r.stderr or "").strip()))
    return [line.split("refs/tags/", 1)[1].strip() for line in r.stdout.splitlines() if "refs/tags/" in line]


def latest_version(owner: str, repo: str) -> Optional[str]:
    """Newest stable release tag; a pre-release (-rc, -beta ...) is used only if there is no stable one."""
    stable, pre = [], []
    for t in git_tags(owner, repo):
        m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(.*)", t)
        if m:
            key = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            (pre if m.group(4) else stable).append((key, t))
    pool = stable or pre
    return max(pool)[1] if pool else None


def min_rust_version(owner: str, repo: str, ref: Optional[str]) -> str:
    url = "https://raw.githubusercontent.com/%s/%s/%s/Cargo.toml" % (owner, repo, ref or "HEAD")
    try:
        m = re.search(r'^\s*rust-version\s*=\s*"(\d+\.\d+(?:\.\d+)?)"', http_get(url).decode("utf-8", "replace"), re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return DEFAULT_MIN_RUST


# --------------------------------------------------------------------------------------
# Tool detection
# --------------------------------------------------------------------------------------
def _bin(home: Path, name: str, kind: str = "exe") -> Path:
    return home / "bin" / name


def java_major(home: Path) -> Optional[int]:
    for tool in ("java", "javac", "keytool"):
        if not _bin(home, tool).exists():
            return None
    try:
        out = subprocess.run([str(_bin(home, "java")), "-version"], capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', (out.stderr or "") + (out.stdout or ""))
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


def java_home_from_path() -> Optional[Path]:
    j = shutil.which("java")
    if not j:
        return None
    try:
        out = subprocess.run([j, "-XshowSettings:properties", "-version"],
                             capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    m = re.search(r"java\.home\s*=\s*(.+)", (out.stderr or "") + (out.stdout or ""))
    return Path(m.group(1).strip()) if m else None


def find_java_under(root: Path) -> Optional[Path]:
    if not root.exists():
        return None
    for g in ("*", "*/*", "*/*/*"):
        for p in sorted(root.glob(g)):
            if p.is_dir() and _bin(p, "java").exists():
                return p
    return None


def find_java(cache: Path) -> Tuple[Optional[Path], Optional[int]]:
    cands: List[Path] = []
    if os.environ.get("JAVA_HOME"):
        cands.append(Path(os.environ["JAVA_HOME"]))
    p = java_home_from_path()
    if p:
        cands.append(p)
    home = Path.home()
    cands += [Path("/opt/android-studio/jbr"), Path("/usr/local/android-studio/jbr"),
              home / "android-studio" / "jbr", Path("/snap/android-studio/current/android-studio/jbr")]
    cached = find_java_under(cache / "jdk")
    if cached:
        cands.append(cached)
    seen = set()
    for c in cands:
        if str(c) in seen:
            continue
        seen.add(str(c))
        m = java_major(c)
        if m and m in JDK_ACCEPT:
            return c, m
    return None, None


def find_sdkmanager(sdk: Path) -> Optional[Path]:
    latest = sdk / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    if latest.exists():
        return latest
    base = sdk / "cmdline-tools"
    if base.exists():
        for p in sorted(base.glob("*")):
            c = p / "bin" / "sdkmanager"
            if c.exists():
                return c
    return None


def ndk_major(p: Path) -> Optional[int]:
    try:
        for line in (p / "source.properties").read_text().splitlines():
            if line.startswith("Pkg.Revision"):
                return int(line.split("=", 1)[1].strip().split(".")[0])
    except Exception:
        pass
    m = re.match(r"(\d+)\.", p.name)
    return int(m.group(1)) if m else None


def ndk_ok(p: Path) -> bool:
    return (p / "toolchains" / "llvm" / "prebuilt").is_dir() and (ndk_major(p) or 0) >= NDK_MIN_MAJOR


def find_ndk(sdk: Optional[Path]) -> Optional[Path]:
    for v in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "NDK_HOME"):
        if os.environ.get(v) and ndk_ok(Path(os.environ[v])):
            return Path(os.environ[v])
    if sdk and (sdk / "ndk").is_dir():
        pinned = sdk / "ndk" / NDK_PINNED
        if ndk_ok(pinned):
            return pinned
        found = [d for d in (sdk / "ndk").glob("*") if d.is_dir() and ndk_ok(d)]
        found.sort(key=lambda d: [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", d.name)])
        if found:
            return found[-1]
    return None


@dataclass
class SdkPlan:
    path: Path
    missing: List[str]
    need_cmdline: bool
    private: bool
    existing: bool


def sdk_missing(sdk: Path) -> List[str]:
    miss = []
    if not (sdk / "platforms" / ("android-%d" % TARGET_SDK)).is_dir():
        miss.append(PLATFORM_PKG)
    if not (sdk / "build-tools" / BUILD_TOOLS).is_dir():
        miss.append(BUILD_TOOLS_PKG)
    if find_ndk(sdk) is None:
        miss.append(NDK_PKG)
    return miss


def plan_sdk(cache: Path) -> SdkPlan:
    cands: List[Path] = []
    for v in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(v):
            cands.append(Path(os.environ[v]))
    home = Path.home()
    cands += [home / "Android" / "Sdk", home / "Android" / "sdk", cache / "android-sdk"]
    seen, usable = set(), []
    for c in cands:
        if str(c) not in seen and c.is_dir():
            seen.add(str(c))
            usable.append(c)
    private = cache / "android-sdk"
    partial = None
    for sdk in usable:
        missing = sdk_missing(sdk)
        if not missing:
            return SdkPlan(sdk, [], False, sdk == private, True)
        if partial is None and find_sdkmanager(sdk) and os.access(str(sdk), os.W_OK):
            partial = (sdk, missing)
    if partial:
        return SdkPlan(partial[0], partial[1], False, partial[0] == private, True)
    return SdkPlan(private, sdk_missing(private), find_sdkmanager(private) is None, True, private.is_dir())


# ---- Rust -------------------------------------------------------------------------------
@dataclass
class RustSetup:
    action: str = "ok"  # ok | install | update
    private: bool = False
    rustup: Optional[Path] = None
    env: Dict[str, str] = field(default_factory=dict)  # CARGO_HOME / RUSTUP_HOME overrides
    bin_dirs: List[Path] = field(default_factory=list)
    version: Optional[str] = None


def _rust_env(over: Dict[str, str]) -> Dict[str, str]:
    env = os.environ.copy()
    env.update(over)
    env.pop("RUSTUP_TOOLCHAIN", None)
    return env


def stable_status(rustup: Path, over: Dict[str, str]) -> Tuple[Optional[str], bool]:
    """(version of the 'stable' toolchain or None, whether it has the Android target)."""
    env = _rust_env(over)
    try:
        r = subprocess.run([str(rustup), "run", "stable", "rustc", "--version"], capture_output=True,
                           text=True, timeout=120, env=env)
        if r.returncode != 0:
            return None, False
        m = re.search(r"rustc (\d+\.\d+\.\d+)", r.stdout)
        t = subprocess.run([str(rustup), "target", "list", "--installed", "--toolchain", "stable"],
                           capture_output=True, text=True, timeout=120, env=env)
    except Exception:
        return None, False
    return (m.group(1) if m else None), RUST_TARGET in (t.stdout or "").split()


def private_rust_paths(cache: Path) -> Tuple[Path, Path]:
    return cache / "rust" / "cargo", cache / "rust" / "rustup"


def plan_rust(cache: Path, min_ver: str) -> RustSetup:
    cargo_home, rustup_home = private_rust_paths(cache)
    cands: List[RustSetup] = []
    ru = shutil.which("rustup")
    if not ru:
        p = Path(os.environ.get("CARGO_HOME") or (Path.home() / ".cargo")) / "bin" / "rustup"
        ru = str(p) if p.exists() else None
    if ru:
        cands.append(RustSetup("ok", False, Path(ru), {}, [Path(ru).parent]))
    priv_ru = cargo_home / "bin" / "rustup"
    priv_env = {"CARGO_HOME": str(cargo_home), "RUSTUP_HOME": str(rustup_home)}
    if priv_ru.exists():
        cands.append(RustSetup("ok", True, priv_ru, priv_env, [cargo_home / "bin"]))
    for c in cands:
        ver, has_target = stable_status(c.rustup, c.env)  # type: ignore[arg-type]
        if ver and vtuple(ver) >= vtuple(min_ver) and has_target:
            c.version = ver
            return c
    # Nothing usable. We never modify your own Rust setup: everything goes into the cache.
    return RustSetup("update" if priv_ru.exists() else "install", True,
                     priv_ru if priv_ru.exists() else None, priv_env, [cargo_home / "bin"])


def tool_env(rust: RustSetup, cache: Path, extra_path: Optional[List[Path]] = None) -> Dict[str, str]:
    env = os.environ.copy()
    env.update(rust.env)
    env["RUSTUP_TOOLCHAIN"] = "stable"
    paths = list(extra_path or []) + list(rust.bin_dirs) + [cache / "cargo-tools" / "bin"]
    env["PATH"] = os.pathsep.join(str(p) for p in paths) + os.pathsep + env.get("PATH", "")
    return env


def apk2_present(rust: RustSetup, cache: Path) -> bool:
    if (cache / "cargo-tools" / "bin" / "cargo-apk2").exists():
        return True
    if rust.action != "ok":
        return False
    try:
        r = subprocess.run(["cargo", "apk2", "--help"], capture_output=True, text=True, timeout=60,
                           env=tool_env(rust, cache))
    except Exception:
        return False
    return r.returncode == 0


# --------------------------------------------------------------------------------------
# Installers
# --------------------------------------------------------------------------------------
def install_jdk(cache: Path) -> Path:
    dest = cache / "jdk"
    last_err = None
    for feat in JDK_PREFERRED:
        url = "https://api.adoptium.net/v3/binary/latest/%d/ga/linux/x64/jdk/hotspot/normal/eclipse" % feat
        archive = cache / "downloads" / ("jdk-%d.tar.gz" % feat)
        try:
            download(url, archive, "JDK %d" % feat)
        except urllib.error.HTTPError as e:
            last_err = e
            continue
        rmtree(dest)
        extract(archive, dest)
        archive.unlink()
        home = find_java_under(dest)
        if home and java_major(home):
            return home
        die("The downloaded JDK could not be used (%s)." % dest)
    die("Could not download a JDK (%s). Install JDK 17 yourself and set JAVA_HOME." % last_err)
    raise AssertionError


def install_sdk(plan: SdkPlan, cache: Path, java_home: Path) -> None:
    sdk = plan.path
    sdk.mkdir(parents=True, exist_ok=True)
    if plan.need_cmdline:
        url = "https://dl.google.com/android/repository/commandlinetools-linux-%s_latest.zip" % CMDLINE_TOOLS_REV
        archive = cache / "downloads" / "cmdline-tools.zip"
        download(url, archive, "Android command-line tools")
        tmp = cache / "downloads" / "cmdline-tools-tmp"
        rmtree(tmp)
        extract(archive, tmp)
        archive.unlink()
        target = sdk / "cmdline-tools" / "latest"
        rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp / "cmdline-tools"), str(target))
        rmtree(tmp)
    sm = find_sdkmanager(sdk)
    if not sm:
        die("sdkmanager not found in %s" % sdk)
    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = str(java_home / "bin") + os.pathsep + env.get("PATH", "")
    say("      Accepting Android SDK licenses ...")
    subprocess.run([str(sm), "--sdk_root=%s" % sdk, "--licenses"], input="y\n" * 80, text=True,
                   capture_output=True, env=env)
    if plan.missing:
        say("      Installing %s (the NDK is large - this can take a while) ..." % ", ".join(plan.missing))
        r = subprocess.run([str(sm), "--sdk_root=%s" % sdk] + plan.missing, input="y\n" * 10, text=True, env=env)
        if r.returncode != 0 and NDK_PKG in plan.missing:
            # The pinned NDK id may be unavailable: pick the newest NDK of the same major version instead.
            lst = subprocess.run([str(sm), "--sdk_root=%s" % sdk, "--list"], text=True, capture_output=True, env=env)
            vers = re.findall(r"ndk;(%s\.[0-9.]+)" % NDK_PINNED.split(".")[0], lst.stdout or "")
            if vers:
                best = max(vers, key=lambda v: [int(x) for x in v.split(".")])
                say("      Retrying with NDK %s ..." % best)
                r = subprocess.run([str(sm), "--sdk_root=%s" % sdk] + [p for p in plan.missing if p != NDK_PKG]
                                   + ["ndk;" + best], input="y\n" * 10, text=True, env=env)
        if r.returncode != 0:
            die("sdkmanager failed (see the output above). Check your internet connection / proxy and try again.")
    left = sdk_missing(sdk)
    if left:
        die("Android SDK packages are still missing after install: %s" % ", ".join(left))


def install_rust(rust: RustSetup, cache: Path) -> None:
    cargo_home, rustup_home = private_rust_paths(cache)
    env = os.environ.copy()
    env.update(rust.env)
    env.pop("RUSTUP_TOOLCHAIN", None)
    if rust.action == "install":
        url = "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init"
        sha = None
        try:
            sha = http_get(url + ".sha256").decode().split()[0]
        except Exception:
            say("      (checksum for rustup-init not available - continuing without verification)")
        exe = cache / "downloads" / "rustup-init"
        download(url, exe, "rustup-init", sha)
        exe.chmod(0o755)
        say("      Installing the Rust toolchain (stable) and the Android target ...")
        r = subprocess.run([str(exe), "-y", "--profile", "minimal", "--default-toolchain", "stable",
                            "--target", RUST_TARGET, "--no-modify-path"], env=env)
        exe.unlink()
        if r.returncode != 0:
            die("Installing Rust failed (see the output above).")
        rust.rustup = cargo_home / "bin" / "rustup"
    else:
        say("      Updating the Rust toolchain (stable) and adding the Android target ...")
        r = subprocess.run([str(rust.rustup), "toolchain", "install", "stable", "--profile", "minimal",
                            "--target", RUST_TARGET], env=env)
        if r.returncode != 0:
            die("Updating Rust failed (see the output above).")
    rust.action = "ok"
    rust.private = True
    rust.bin_dirs = [cargo_home / "bin"]
    rust.version, has = stable_status(rust.rustup, rust.env)  # type: ignore[arg-type]
    if not rust.version or not has:
        die("Rust was installed but the 'stable' toolchain with the %s target is not usable." % RUST_TARGET)


def install_apk2(rust: RustSetup, cache: Path, java_home: Optional[Path]) -> None:
    say("      Compiling cargo-apk2 (takes a few minutes) ...")
    env = tool_env(rust, cache)
    r = subprocess.run(["cargo", "install", "cargo-apk2", "--locked", "--root", str(cache / "cargo-tools")], env=env)
    if r.returncode != 0:
        die("Could not install cargo-apk2 (see the output above). If cargo reports a missing library,\n"
            "       install pkg-config and libssl-dev (Debian/Ubuntu) and try again.")


# --------------------------------------------------------------------------------------
# App source
# --------------------------------------------------------------------------------------
def git(args: List[str], cwd: Optional[Path] = None, quiet: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=str(cwd) if cwd else None, text=True,
                          capture_output=quiet)


def prepare_source(root: Path, app: Dict[str, str], ref: Optional[str], fresh: bool) -> Path:
    owner, repo = app["owner"], app["repo"]
    url = "https://github.com/%s/%s.git" % (owner, repo)
    src = root / "source" / repo
    (root / "source").mkdir(parents=True, exist_ok=True)
    if src.exists():
        if not (src / ".git").is_dir():
            die("%s exists but is not a git checkout. Move it away and run again." % src)
        origin = git(["remote", "get-url", "origin"], src, quiet=True).stdout.strip()
        if origin.removesuffix(".git").rstrip("/") != url.removesuffix(".git"):
            die("%s has an unexpected origin '%s'; refusing to touch it." % (src, origin))
        same = False
        if ref and not fresh:
            same = git(["describe", "--tags", "--exact-match"], src, quiet=True).stdout.strip() == ref
        if same:
            say("      Reusing the existing checkout of %s (already at %s)." % (repo, ref))
        else:
            rmtree(src)
    if not src.exists():
        say("      Downloading the source of %s (%s) ..." % (app["name"], ref or "main branch"))
        if ref and re.fullmatch(r"[0-9a-f]{7,40}", ref):
            src.mkdir(parents=True)
            for cmd in (["init", "-q"], ["remote", "add", "origin", url],
                        ["fetch", "--depth", "1", "origin", ref], ["-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"]):
                if git(cmd, src).returncode != 0:
                    die("git %s failed." % cmd[0])
        else:
            cmd = ["-c", "advice.detachedHead=false", "clone", "--depth", "1"]
            if ref:
                cmd += ["--branch", ref]
            if git(cmd + [url, str(src)]).returncode != 0:
                die("Could not download %s%s." % (url, (" at '%s'" % ref) if ref else ""))
    rev = git(["rev-parse", "--short", "HEAD"], src, quiet=True).stdout.strip()
    say("      Source ready: %s @ %s" % (repo, rev or "?"))
    return src


def link_persistent(root: Path, cache: Path) -> None:
    """Keep the builder's working folders in the cache so they survive builder updates."""
    base = cache / "native"
    for name in ("source", "build", "keys", "logs", "output"):
        target = base / name
        target.mkdir(parents=True, exist_ok=True)
        link = root / name
        if link.is_symlink():
            if link.resolve() != target.resolve():
                link.unlink()
                link.symlink_to(target)
        elif link.exists():
            if link.is_dir() and not any(link.iterdir()):
                link.rmdir()
                link.symlink_to(target)
        else:
            link.symlink_to(target)


# --------------------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------------------
def build_env(cache: Path, java_home: Path, sdk: Path, ndk: Path, rust: RustSetup) -> Dict[str, str]:
    env = tool_env(rust, cache, [python_shim(cache), java_home / "bin"])
    env["JAVA_HOME"] = str(java_home)
    env["ANDROID_HOME"] = env["ANDROID_SDK_ROOT"] = str(sdk)
    env["ANDROID_NDK_HOME"] = env["ANDROID_NDK_ROOT"] = env["NDK_HOME"] = str(ndk)
    env["ARTCRAFT_TARGET_SDK"] = str(TARGET_SDK)
    env["ARTCRAFT_BUILD_TOOLS_VERSION"] = BUILD_TOOLS
    return env


def run_builder(root: Path, app: Dict[str, str], args: argparse.Namespace, env: Dict[str, str]) -> Path:
    cmd = ["bash", str(root / "lib" / "build-common.sh"), "debug" if args.debug else "release", "-" + app["repo"]]
    if args.appname:
        cmd.append("-appname=%s" % args.appname)
    if args.packagename:
        cmd.append("-packagename=%s" % args.packagename)
    if args.appicon:
        cmd.append("-appicon=%s" % Path(args.appicon).expanduser().resolve())
    started = time.time()
    say("      (first build of an app compiles hundreds of Rust crates: expect 15-40 minutes)\n")
    rc = subprocess.call(cmd, env=env, cwd=str(root))
    out = root / "output"
    apks = sorted(out.glob("%s-*.apk" % app["repo"]), key=lambda p: p.stat().st_mtime, reverse=True)
    apks = [p for p in apks if p.name.endswith("-debug.apk") == bool(args.debug) and p.stat().st_mtime >= started - 5]
    if rc != 0 or not apks:
        logs = sorted((root / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        lines = ["The build failed (exit code %d)." % rc,
                 "  - Read the build log: %s" % (logs[0] if logs else root / "logs"),
                 "  - A release tag can be older than the builder expects: try   --main",
                 "  - The builder may have been fixed since the pinned version: try   --builder-ref main",
                 "  - Not enough disk space (needs ~15 GB) or memory (linking needs ~8 GB) are common causes"]
        die("\n".join(lines))
    return apks[0]


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Build native Android APKs of the ArtCraft apps from source.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Examples" + (__doc__ or "").split("Examples", 1)[1])
    ap.add_argument("--list", action="store_true", help="list the available apps and exit")
    ap.add_argument("--app", help="app name or number from --list (skips the menu)")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation before downloading tools")
    ap.add_argument("--cache", help="folder for downloaded tools and build files "
                                    "(default: $ARTCRAFT_APK_HOME or ~/.artcraft-apk)")
    ap.add_argument("--output", default="output", help="folder for the finished APK (default: ./output)")
    ap.add_argument("--main", action="store_true",
                    help="build the newest development code (main branch) instead of the latest release")
    ap.add_argument("--ref", help="build a specific tag, branch or commit of the app instead")
    ap.add_argument("--debug", action="store_true", help="build a debug APK (larger and slower than release)")
    ap.add_argument("--appname", help="override the app's display name")
    ap.add_argument("--packagename", help="override the Android package name (default: com.artcraft.<app>)")
    ap.add_argument("--appicon", help="path to a PNG to use as the launcher icon")
    ap.add_argument("--builder-ref", default=BUILDER_PIN,
                    help="commit/branch/tag of the artcraft-android builder to use (default: reviewed pin)")
    ap.add_argument("--builder-path", help="use a local checkout of the artcraft-android builder")
    ap.add_argument("--setup-only", action="store_true",
                    help="install the tools and fetch the source, but do not build")
    ap.add_argument("--fresh", action="store_true", help="always re-download the app source")
    return ap.parse_args()


def main() -> None:
    ensure_python311()
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    args = parse_args()
    cache = Path(args.cache or os.environ.get("ARTCRAFT_APK_HOME") or (Path.home() / ".artcraft-apk")).expanduser().resolve()
    total = 5 if args.setup_only else 7

    # ---- 1. builder + app list -------------------------------------------------------------
    step(1, total, "Fetching the list of ArtCraft apps ...")
    cache.mkdir(parents=True, exist_ok=True)
    builder_root, local_builder = fetch_builder(cache, args.builder_ref, args.builder_path)
    apps = discover_apps(builder_root)
    say("      Builder: %s  (MIT licensed, by MostafaXS1)" %
        (builder_root if local_builder else "github.com/%s @ %s" % (BUILDER_REPO, args.builder_ref[:10])))
    if args.list:
        for i, a in enumerate(apps, 1):
            say("  %2d) %-12s %s" % (i, a["name"], a["desc"]))
        return

    # ---- 2. choose ---------------------------------------------------------------------------
    step(2, total, "Choosing the app ...")
    app = choose_app(apps, args.app)
    say("      Selected: %s  (github.com/%s/%s)" % (app["name"], app["owner"], app["repo"]))
    check_platform()
    preflight()

    # ---- 3. latest version -------------------------------------------------------------------
    step(3, total, "Looking for the latest version ...")
    if args.ref:
        ref, label = args.ref, "%s (your choice)" % args.ref
    elif args.main:
        ref, label = None, "main branch (newest development code)"
    else:
        ref = latest_version(app["owner"], app["repo"])
        if ref:
            label = "%s (latest release)" % ref
        else:
            say("      No release tags found - using the main branch.")
            label = "main branch"
    min_rust = max(min_rust_version(app["owner"], app["repo"], ref), APK2_MIN_RUST, key=vtuple)
    say("      Version : %s" % label)
    say("      Needs   : Rust %s or newer" % min_rust)
    if not args.ref and not args.main and ref:
        say("      (The main branch is usually newer than the latest release. Use --main to build that.)")

    # ---- 4. tools ----------------------------------------------------------------------------
    step(4, total, "Checking the build tools on this computer ...")
    java_home, jmajor = find_java(cache)
    sdk_plan = plan_sdk(cache)
    rust = plan_rust(cache, min_rust)
    apk2 = apk2_present(rust, cache)

    say("      Java (JDK %d-%d)      : %s" % (JDK_ACCEPT[0], JDK_ACCEPT[-1],
        ("found  %s  (version %d)" % (java_home, jmajor)) if java_home else "NOT FOUND"))
    if not sdk_plan.missing:
        say("      Android SDK          : found  %s" % sdk_plan.path)
        say("      Android NDK          : found  %s" % find_ndk(sdk_plan.path))
    elif sdk_plan.existing and not sdk_plan.private:
        say("      Android SDK          : found  %s  but missing: %s" % (sdk_plan.path, ", ".join(sdk_plan.missing)))
    else:
        say("      Android SDK + NDK    : NOT FOUND (needs platform %d, build-tools %s, NDK %s)" %
            (TARGET_SDK, BUILD_TOOLS, NDK_PINNED))
    if rust.action == "ok":
        say("      Rust (stable)        : found  %s  (%s, has %s)" % ("your own install" if not rust.private else "in cache", rust.version, RUST_TARGET))
    else:
        say("      Rust %s+ (stable)     : %s" % (min_rust, "NOT FOUND" if rust.action == "install"
                                                   else "needs an update / the %s target" % RUST_TARGET))
    say("      cargo-apk2           : %s" % ("found" if apk2 else "NOT FOUND"))

    items: List[Tuple[str, int, str]] = []
    if not java_home:
        items.append(("JDK 17 (Eclipse Temurin)", 190, str(cache / "jdk")))
    if sdk_plan.need_cmdline:
        items.append(("Android SDK command-line tools", 150, str(sdk_plan.path / "cmdline-tools")))
    for pkg in sdk_plan.missing:
        mb = 65 if pkg.startswith("platforms") else 60 if pkg.startswith("build-tools") else 1000
        extra = "  (added to your existing SDK; nothing already there is changed)" if (sdk_plan.existing and not sdk_plan.private) else ""
        items.append(("Android SDK package: %s%s" % (pkg, extra), mb, str(sdk_plan.path)))
    if rust.action != "ok":
        items.append(("Rust toolchain (rustup + stable + %s)" % RUST_TARGET, 300, str(cache / "rust")))
    if not apk2:
        items.append(("cargo-apk2 (compiled from source, a few minutes)", 60, str(cache / "cargo-tools")))
    items.append(("Rust libraries for the app (downloaded by cargo during the build)", 700,
                  str((cache / "rust" / "cargo") if rust.private else Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo"))))
    items.append(("Build output of the app (the first build is large)", 5000, str(cache / "native" / "build")))

    install_needed = (not java_home) or sdk_plan.need_cmdline or bool(sdk_plan.missing) or rust.action != "ok" or not apk2
    bar = "=" * 74
    say("\n" + bar)
    say(" THE FOLLOWING WILL BE DOWNLOADED AND SAVED ON THIS COMPUTER")
    say(bar)
    for what, mb, where in items:
        size = ("about %.1f GB" % (mb / 1000.0)) if mb >= 1000 else ("about %d MB" % mb)
        say("  * %s\n      %s  ->  %s" % (what, size, where))
    say("\n  Total: roughly %.1f GB of disk space and 15-40 minutes for the first build."
        % (sum(i[1] for i in items) / 1000.0))
    say("  Tools that were found above are used as they are; your own Rust setup is never modified.")
    if sdk_plan.missing:
        say("  Android SDK licenses (https://developer.android.com/studio/terms) are accepted automatically.")
    say("  To remove everything this script saved, delete: %s" % cache)
    say(bar)
    if not confirm("Continue?", args.yes):
        say("\nCancelled. No build tools were downloaded.")
        sys.exit(0)
    try:
        free = shutil.disk_usage(str(cache)).free
        if free < 15 * 1024 ** 3:
            say("      Warning: only %s of free disk space (15 GB or more is recommended)." % human(free))
    except Exception:
        pass

    if install_needed:
        if not java_home:
            say("\n      Downloading the JDK ...")
            java_home = install_jdk(cache)
        if sdk_plan.need_cmdline or sdk_plan.missing:
            say("\n      Setting up the Android SDK and NDK ...")
            install_sdk(sdk_plan, cache, java_home)
        if rust.action != "ok":
            say("\n      Setting up Rust ...")
            install_rust(rust, cache)
        if not apk2:
            say("\n      Setting up cargo-apk2 ...")
            install_apk2(rust, cache, java_home)
    ndk = find_ndk(sdk_plan.path)
    if not ndk or not java_home:
        die("Internal error: the Android NDK or JDK is still not available.")
    if rust.version and vtuple(rust.version) < vtuple(min_rust):
        die("Rust %s is installed but %s needs %s or newer." % (rust.version, app["name"], min_rust))

    # ---- 5. source ---------------------------------------------------------------------------
    step(5, total, "Downloading the %s source code ..." % app["name"])
    if not local_builder:
        link_persistent(builder_root, cache)
    prepare_source(builder_root, app, ref, args.fresh)
    if args.setup_only:
        say("\nSetup finished. Run again without --setup-only to build.")
        return

    # ---- 6. build ----------------------------------------------------------------------------
    step(6, total, "Compiling %s for Android (arm64) ..." % app["name"])
    env = build_env(cache, java_home, sdk_plan.path, ndk, rust)
    apk = run_builder(builder_root, app, args, env)

    # ---- 7. finish ---------------------------------------------------------------------------
    step(7, total, "Collecting the APK ...")
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / apk.name
    shutil.copyfile(str(apk), str(final))
    sha = hashlib.sha256(final.read_bytes()).hexdigest()
    say("\n" + "=" * 74)
    say(" DONE:  %s  (%s)" % (final, human(final.stat().st_size)))
    say(" SHA-256: %s" % sha)
    say("=" * 74)
    say(" Install it on a 64-bit ARM Android phone or tablet:")
    say("   - copy the .apk to the device, open it, and allow \"install unknown apps\" when asked; or")
    say("   - with a USB cable and USB debugging on:  adb install -r \"%s\"" % final)
    say(" This is a locally signed build for personal use. Keep this key to install updates over it:")
    say("   %s" % (builder_root / "keys" / "artcraft-builder.jks").resolve())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        sys.exit(130)
    except urllib.error.URLError as exc:
        die("Network problem: %s\n       Check your internet connection (or proxy settings) and try again." % exc)
