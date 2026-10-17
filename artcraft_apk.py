#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
artcraft_apk.py - turn an ArtCraft web app into an offline Android APK.

What it does
------------
  1. Reads the list of ArtCraft "Crafting Apps" (PhotoCraft, VectorCraft, ...).
  2. You pick one.
  3. Finds that app's newest GitHub release that contains a web build (a *-web-*.zip),
     downloads it and verifies its SHA-256 checksum.
  4. Wraps the files in a tiny Android WebView app (the web files are bundled inside the
     APK, so the app runs fully offline) and builds + signs an APK.

Build tools (JDK 17-21, Android SDK, Gradle 8.7+) are DETECTED first. Whatever is missing
is listed, you are told what will be downloaded and where it will be saved, and only then
is it downloaded - into one cache folder (default: ~/.artcraft-apk).

Requirements: Python 3.8+ and an internet connection (for the first run). Nothing to pip install.

Examples
--------
  python3 artcraft_apk.py                      # interactive menu
  python3 artcraft_apk.py --list               # just list the apps
  python3 artcraft_apk.py --app photocraft     # no menu
  python3 artcraft_apk.py --app photocraft --yes --webgl
  python3 artcraft_apk.py --app photocraft --generate-only   # write the Android project only
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
SEED_REPO = "storytold/photocraft"  # its README holds the table of Crafting Apps
README_URL = "https://raw.githubusercontent.com/{repo}/main/README.md"

GRADLE_VERSION = "8.7"  # minimum Gradle for AGP 8.5
AGP_VERSION = "8.5.2"  # Android Gradle Plugin
COMPILE_SDK = 34
BUILD_TOOLS = "34.0.0"
MIN_SDK = 24  # Android 7.0
CMDLINE_TOOLS_REV = "11076708"  # Android command-line tools build
JDK_PREFERRED = (17, 21)  # versions we try to download, in order
JDK_ACCEPT = range(17, 22)  # versions we accept when detecting (17..21)
REQUIRED_SDK_PACKAGES = ["platforms;android-%d" % COMPILE_SDK, "build-tools;%s" % BUILD_TOOLS]

FALLBACK_APPS = [
    ("PhotoCraft", "Image editing: layers, masks, type and real PSD files", "storytold", "photocraft"),
    ("VectorCraft", "Vector illustration", "storytold", "vectorcraft"),
    ("FilmCraft", "Video editing, color and sound", "storytold", "filmcraft"),
    ("LightCraft", "Photo library and raw development", "storytold", "lightcraft"),
    ("PdfCraft", "Reading, organizing and protecting PDFs", "storytold", "pdfcraft"),
    ("EffectCraft", "Motion graphics and visual effects", "storytold", "effectcraft"),
    ("DesignCraft", "Page layout and publishing", "storytold", "designcraft"),
]

UA = "artcraft-apk/1.0"
KEYSTORE_PASSWORD = "artcraft"  # local, personal-use signing key (not for the Play Store)


def _host_os() -> str:
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "mac"
    return "linux"


def _host_arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "aarch64"
    if m in ("x86_64", "amd64"):
        return "x64"
    return m


OS = _host_os()
ARCH = _host_arch()
IS_WIN = OS == "windows"


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

    if path.exists():
        shutil.rmtree(str(path), onerror=_onerror)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


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


# --------------------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------------------
def _request(url: str, accept: Optional[str] = None) -> urllib.request.Request:
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and urllib.parse.urlparse(url).hostname == "api.github.com":
        headers["Authorization"] = "Bearer " + token
    return urllib.request.Request(url, headers=headers)


def http_get(url: str, accept: Optional[str] = None, timeout: int = 30) -> bytes:
    with urllib.request.urlopen(_request(url, accept), timeout=timeout) as r:
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


# --------------------------------------------------------------------------------------
# Archive extraction (path-traversal safe, keeps executable bits)
# --------------------------------------------------------------------------------------
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
        try:
            tf.extractall(str(dest), members=members, filter="tar")  # Python 3.12+
        except TypeError:
            tf.extractall(str(dest), members=members)


def extract(archive: Path, dest: Path) -> None:
    if archive.name.endswith(".zip"):
        extract_zip(archive, dest)
    else:
        extract_tar(archive, dest)


# --------------------------------------------------------------------------------------
# Step 1: the list of apps
# --------------------------------------------------------------------------------------
def discover_apps() -> List[Dict[str, str]]:
    # 1) the table in the PhotoCraft README
    try:
        md = http_get(README_URL.format(repo=SEED_REPO)).decode("utf-8", "replace")
        rx = re.compile(
            r"\*\*([^*|]+?)\*\*\s*\|\s*([^|]*?)\s*\|\s*\[GitHub\]\(https://github\.com/([^/\s)]+)/([^/\s)]+)\)")
        apps, seen = [], set()
        for name, desc, owner, repo in rx.findall(md):
            if repo.lower() in seen:
                continue
            seen.add(repo.lower())
            apps.append({"name": name.strip(), "desc": re.sub(r"\s*[\u00b7|-]?\s*you are here", "", desc.replace("*", "")).strip(),
                         "owner": owner, "repo": repo})
        if apps:
            return apps
    except Exception as e:
        say("      (could not read the README app table: %s)" % e)
    # 2) GitHub API: repos named *craft
    try:
        data = json.loads(http_get("https://api.github.com/users/storytold/repos?per_page=100",
                                   "application/vnd.github+json").decode())
        apps = []
        for r in data:
            n = r["name"]
            if n.lower().endswith("craft") and not r.get("archived"):
                apps.append({"name": n[:1].upper() + n[1:], "desc": r.get("description") or "",
                             "owner": r["owner"]["login"], "repo": n})
        if apps:
            return apps
    except Exception as e:
        say("      (could not list repositories: %s)" % e)
    # 3) built-in list
    say("      Using the built-in app list.")
    return [{"name": n, "desc": d, "owner": o, "repo": r} for n, d, o, r in FALLBACK_APPS]


def choose_app(apps: List[Dict[str, str]], wanted: Optional[str], repo_arg: Optional[str]) -> Dict[str, str]:
    if repo_arg:
        if "/" not in repo_arg:
            die("--repo must look like owner/name, e.g. storytold/photocraft")
        owner, repo = repo_arg.split("/", 1)
        return {"name": repo[:1].upper() + repo[1:], "desc": "", "owner": owner, "repo": repo}

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

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
        say("      %d) %-12s %s" % (i, a["name"], a["desc"]))
    while True:
        try:
            ans = input("\n    Choose an app [1-%d]: " % len(apps)).strip()
        except EOFError:
            die("No selection made.")
        if ans.isdigit() and 1 <= int(ans) <= len(apps):
            return apps[int(ans) - 1]
        say("    Please type a number between 1 and %d." % len(apps))


# --------------------------------------------------------------------------------------
# Step 3: newest release that contains a web build
# --------------------------------------------------------------------------------------
def is_web_zip(name: str) -> bool:
    n = name.lower()
    return n.endswith(".zip") and "web" in n and "sha" not in n


def release_via_api(owner: str, repo: str) -> Optional[Dict]:
    url = "https://api.github.com/repos/%s/%s/releases?per_page=20" % (owner, repo)
    data = json.loads(http_get(url, "application/vnd.github+json").decode())
    for rel in data:
        if rel.get("draft"):
            continue
        assets = rel.get("assets", [])
        zips = [a for a in assets if is_web_zip(a["name"])]
        if not zips:
            continue
        sums = [a for a in assets if a["name"].lower().startswith("sha256sums")]
        return {"tag": rel["tag_name"], "prerelease": bool(rel.get("prerelease")),
                "name": zips[0]["name"], "url": zips[0]["browser_download_url"],
                "size": zips[0].get("size", 0),
                "sums_url": sums[0]["browser_download_url"] if sums else None}
    return None


def release_via_html(owner: str, repo: str) -> Optional[Dict]:
    """API-free fallback (the API allows only 60 anonymous requests/hour per IP)."""
    base = "https://github.com/%s/%s" % (owner, repo)
    atom = http_get(base + "/releases.atom").decode("utf-8", "replace")
    tags: List[str] = []
    for t in re.findall(r'/releases/tag/([^"<\s]+)"', atom):
        if t not in tags:
            tags.append(t)
    for tag in tags[:10]:
        try:
            html = http_get("%s/releases/expanded_assets/%s" % (base, tag)).decode("utf-8", "replace")
        except Exception:
            continue
        hrefs = re.findall(r'href="([^"]*/releases/download/[^"]+)"', html)
        zips = [h for h in hrefs if is_web_zip(urllib.parse.unquote(h.rsplit("/", 1)[-1]))]
        if not zips:
            continue
        sums = [h for h in hrefs if h.rsplit("/", 1)[-1].lower().startswith("sha256sums")]
        full = lambda h: h if h.startswith("http") else "https://github.com" + h
        return {"tag": urllib.parse.unquote(tag), "prerelease": False,
                "name": urllib.parse.unquote(zips[0].rsplit("/", 1)[-1]), "url": full(zips[0]),
                "size": 0, "sums_url": full(sums[0]) if sums else None}
    return None


def find_web_release(app: Dict[str, str]) -> Dict:
    owner, repo = app["owner"], app["repo"]
    rel = None
    try:
        rel = release_via_api(owner, repo)
        if rel is None:
            die("%s has no release that contains a web build (no *-web-*.zip file found).\n"
                "       Only apps that publish a web build can be wrapped by this tool.\n"
                "       See https://github.com/%s/%s/releases" % (app["name"], owner, repo))
    except urllib.error.HTTPError as e:
        why = "GitHub API rate limit reached" if e.code in (403, 429) else "GitHub API error %s" % e.code
        say("      (%s - trying without the API)" % why)
    except SystemExit:
        raise
    except Exception as e:
        say("      (GitHub API not reachable: %s - trying without the API)" % e)
    if rel is None:
        try:
            rel = release_via_html(owner, repo)
        except Exception as e:
            die("Could not look up releases for %s/%s: %s" % (owner, repo, e))
        if rel is None:
            die("%s has no release that contains a web build (no *-web-*.zip file found)." % app["name"])
    sha = None
    if rel.get("sums_url"):
        try:
            text = http_get(rel["sums_url"]).decode("utf-8", "replace")
            for line in text.splitlines():
                m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
                if m and m.group(2).strip() == rel["name"]:
                    sha = m.group(1)
        except Exception:
            pass
    rel["sha256"] = sha
    rel["version"] = rel["tag"].lstrip("vV")
    return rel


# --------------------------------------------------------------------------------------
# Step 4: detect / install build tools
# --------------------------------------------------------------------------------------
def _bin(home: Path, name: str, kind: str = "exe") -> Path:
    suffix = (".exe" if kind == "exe" else ".bat") if IS_WIN else ""
    return home / "bin" / (name + suffix)


def java_major(home: Path) -> Optional[int]:
    # AGP (compileSdk 34) also runs jlink from the JDK, and some bundled JDKs lack it.
    for tool in ("java", "javac", "keytool", "jlink"):
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
    for depth_glob in ("*", "*/*", "*/*/*"):
        for p in sorted(root.glob(depth_glob)):
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
    if OS == "linux":
        cands += [Path("/opt/android-studio/jbr"), Path("/usr/local/android-studio/jbr"),
                  home / "android-studio" / "jbr", Path("/snap/android-studio/current/android-studio/jbr")]
    elif OS == "mac":
        cands += [Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home")]
    else:
        for env in ("ProgramFiles", "LOCALAPPDATA"):
            if os.environ.get(env):
                cands.append(Path(os.environ[env]) / "Android" / "Android Studio" / "jbr")
        if os.environ.get("LOCALAPPDATA"):
            cands.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Android Studio" / "jbr")
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
    latest = _bin(sdk / "cmdline-tools" / "latest", "sdkmanager", "bat")
    if latest.exists():
        return latest
    for p in sorted((sdk / "cmdline-tools").glob("*")) if (sdk / "cmdline-tools").exists() else []:
        c = _bin(p, "sdkmanager", "bat")
        if c.exists():
            return c
    return None


class SdkPlan:
    def __init__(self, path: Path, missing: List[str], need_cmdline: bool, private: bool, existing: bool):
        self.path, self.missing, self.need_cmdline = path, missing, need_cmdline
        self.private, self.existing = private, existing


def plan_sdk(cache: Path) -> SdkPlan:
    cands: List[Path] = []
    for v in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(v):
            cands.append(Path(os.environ[v]))
    home = Path.home()
    if OS == "linux":
        cands += [home / "Android" / "Sdk", home / "Android" / "sdk"]
    elif OS == "mac":
        cands += [home / "Library" / "Android" / "sdk"]
    elif os.environ.get("LOCALAPPDATA"):
        cands += [Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk"]
    cands.append(cache / "android-sdk")
    seen, usable = set(), []
    for c in cands:
        if str(c) not in seen and c.is_dir():
            seen.add(str(c))
            usable.append(c)
    partial = None
    for sdk in usable:
        missing = [p for p in REQUIRED_SDK_PACKAGES if not (sdk / p.replace(";", "/")).is_dir()]
        if not missing:
            return SdkPlan(sdk, [], False, sdk == cache / "android-sdk", True)
        if partial is None and find_sdkmanager(sdk) and os.access(str(sdk), os.W_OK):
            partial = (sdk, missing)
    if partial:
        return SdkPlan(partial[0], partial[1], False, partial[0] == cache / "android-sdk", True)
    private = cache / "android-sdk"
    return SdkPlan(private, list(REQUIRED_SDK_PACKAGES), find_sdkmanager(private) is None, True, private.is_dir())


def gradle_ok(gradle: Path, env: Dict[str, str]) -> bool:
    try:
        out = subprocess.run([str(gradle), "--version"], capture_output=True, text=True, timeout=120, env=env)
    except Exception:
        return False
    m = re.search(r"Gradle (\d+)\.(\d+)", out.stdout or "")
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    need_major, need_minor = [int(x) for x in GRADLE_VERSION.split(".")[:2]]
    return major == need_major and minor >= need_minor


def find_gradle(cache: Path, env: Dict[str, str]) -> Optional[Path]:
    cached = _bin(cache / "gradle" / ("gradle-" + GRADLE_VERSION), "gradle", "bat")
    if cached.exists():
        return cached
    sysg = shutil.which("gradle")
    if sysg and gradle_ok(Path(sysg), env):
        return Path(sysg)
    return None


def gradle_user_home() -> Path:
    return Path(os.environ.get("GRADLE_USER_HOME") or (Path.home() / ".gradle"))


def agp_cached() -> bool:
    return (gradle_user_home() / "caches" / "modules-2" / "files-2.1" / "com.android.tools.build" /
            "gradle" / AGP_VERSION).is_dir()


def make_env(java_home: Path, sdk: Optional[Path]) -> Dict[str, str]:
    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = str(java_home / "bin") + os.pathsep + env.get("PATH", "")
    if sdk:
        env["ANDROID_HOME"] = env["ANDROID_SDK_ROOT"] = str(sdk)
    return env


def install_jdk(cache: Path) -> Path:
    dest = cache / "jdk"
    ext = "zip" if IS_WIN else "tar.gz"
    last_err = None
    for feat in JDK_PREFERRED:
        url = "https://api.adoptium.net/v3/binary/latest/%d/ga/%s/%s/jdk/hotspot/normal/eclipse" % (feat, OS, ARCH)
        archive = cache / "downloads" / ("jdk-%d.%s" % (feat, ext))
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
    die("Could not download a JDK for %s/%s (%s). Install JDK 17 yourself and set JAVA_HOME." % (OS, ARCH, last_err))
    raise AssertionError  # unreachable


def install_gradle(cache: Path) -> Path:
    base = "https://services.gradle.org/distributions/gradle-%s-bin.zip" % GRADLE_VERSION
    sha = None
    try:
        sha = http_get(base + ".sha256").decode().split()[0]
    except Exception:
        say("      (checksum for Gradle not available - continuing without verification)")
    archive = cache / "downloads" / ("gradle-%s-bin.zip" % GRADLE_VERSION)
    download(base, archive, "Gradle %s" % GRADLE_VERSION, sha)
    dest = cache / "gradle"
    rmtree(dest / ("gradle-" + GRADLE_VERSION))
    extract(archive, dest)
    archive.unlink()
    g = _bin(dest / ("gradle-" + GRADLE_VERSION), "gradle", "bat")
    if not g.exists():
        die("Gradle was downloaded but %s is missing." % g)
    if not IS_WIN:
        g.chmod(0o755)
    return g


def _tail(text: str, n: int = 25) -> str:
    return "\n".join((text or "").strip().splitlines()[-n:])


def install_sdk(plan: SdkPlan, cache: Path, java_home: Path) -> None:
    sdk = plan.path
    sdk.mkdir(parents=True, exist_ok=True)
    if plan.need_cmdline:
        key = {"linux": "linux", "mac": "mac", "windows": "win"}[OS]
        url = "https://dl.google.com/android/repository/commandlinetools-%s-%s_latest.zip" % (key, CMDLINE_TOOLS_REV)
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
    env = make_env(java_home, sdk)
    say("      Accepting Android SDK licenses ...")
    subprocess.run([str(sm), "--sdk_root=%s" % sdk, "--licenses"], input="y\n" * 80, text=True,
                   capture_output=True, env=env)
    if plan.missing:
        say("      Installing %s (this can take a few minutes) ..." % ", ".join(plan.missing))
        r = subprocess.run([str(sm), "--sdk_root=%s" % sdk] + plan.missing, input="y\n" * 10, text=True,
                           capture_output=True, env=env)
        if r.returncode != 0:
            die("sdkmanager failed:\n%s\n\nCheck your internet connection / proxy and try again."
                % _tail((r.stdout or "") + (r.stderr or "")))
    left = [p for p in REQUIRED_SDK_PACKAGES if not (sdk / p.replace(";", "/")).is_dir()]
    if left:
        die("Android SDK packages are still missing after install: %s" % ", ".join(left))


def ensure_keystore(cache: Path, java_home: Path) -> Path:
    ks = cache / "signing" / "artcraft-release.keystore"
    if ks.exists():
        return ks
    ks.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([str(_bin(java_home, "keytool")), "-genkeypair", "-keystore", str(ks),
                        "-storepass", KEYSTORE_PASSWORD, "-keypass", KEYSTORE_PASSWORD, "-alias", "artcraft",
                        "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                        "-dname", "CN=ArtCraft APK Builder, O=Local"], capture_output=True, text=True)
    if r.returncode != 0:
        die("Could not create the signing key:\n%s" % _tail(r.stdout + r.stderr))
    say("      Created a signing key: %s" % ks)
    say("      (Keep it: APKs signed with the same key can be installed over each other as updates.)")
    return ks


# --------------------------------------------------------------------------------------
# Step 5/6: Android project
# --------------------------------------------------------------------------------------
HOOK_JS = r"""(function () {
  if (window.__acHook) return;
  var B = window.AndroidBridge;
  if (!B) return;
  window.__acHook = true;

  // Saves blob:/data: downloads (what web apps use for "Save"/"Export") into the Downloads folder.
  async function save(url, name) {
    try {
      var r = await fetch(url);
      var b = await r.blob();
      var id = B.begin(name || 'download', b.type || '');
      if (id < 0) { return; }
      var step = 2 * 1024 * 1024;
      for (var o = 0; o < b.size; o += step) {
        var part = await new Promise(function (res, rej) {
          var fr = new FileReader();
          fr.onload = function () { res(String(fr.result).split(',')[1] || ''); };
          fr.onerror = function () { rej(fr.error); };
          fr.readAsDataURL(b.slice(o, o + step));
        });
        if (!B.write(id, part)) { B.end(id, false); return; }
      }
      B.end(id, true);
    } catch (e) {
      B.fail(String(e));
    }
  }
  window.__acSave = save;

  function special(a) {
    var h = a.href || '';
    return a.hasAttribute('download') && (h.indexOf('blob:') === 0 || h.indexOf('data:') === 0);
  }

  // Apps often create a detached <a download> and call .click() on it.
  var origClick = HTMLElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () {
    if (special(this)) { save(this.href, this.getAttribute('download')); return; }
    return origClick.apply(this, arguments);
  };
  // Real clicks on <a download> elements that are in the page.
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[download]') : null;
    if (a && special(a)) { e.preventDefault(); e.stopPropagation(); save(a.href, a.getAttribute('download')); }
  }, true);
})();
"""

MAIN_ACTIVITY_JAVA = r'''package @@PACKAGE@@;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ContentValues;
import android.content.Intent;
import android.content.res.AssetManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.SystemClock;
import android.provider.MediaStore;
import android.util.Base64;
import android.view.ViewGroup;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.MimeTypeMap;
import android.webkit.ServiceWorkerClient;
import android.webkit.ServiceWorkerController;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

/**
 * Hosts the bundled web app (assets/www) in a WebView. Everything is served from the APK
 * through a virtual https origin, so the app works fully offline.
 */
public class MainActivity extends Activity {
    private static final String HOST = "appassets.androidplatform.net";
    private static final String START_URL = "https://" + HOST + "/@@START_PATH@@";
    private static final boolean BLOCK_NETWORK = @@BLOCK_NETWORK@@;
    private static final boolean DEBUG_WEBVIEW = @@DEBUG_WEBVIEW@@;
    private static final int REQ_FILE = 1001;

    private WebView web;
    private ValueCallback<Uri[]> fileCallback;
    private String hookJs = "";
    private long lastBack = 0;
    private final DownloadBridge bridge = new DownloadBridge();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (DEBUG_WEBVIEW) {
            WebView.setWebContentsDebuggingEnabled(true);
        }
        hookJs = readAsset("artcraft_hook.js");

        web = new WebView(this);
        web.setLayoutParams(new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);

        web.addJavascriptInterface(bridge, "AndroidBridge");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return serve(request);
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                String scheme = u.getScheme();
                if (!"http".equals(scheme) && !"https".equals(scheme)) {
                    return false;
                }
                if (HOST.equals(u.getHost())) {
                    return false;
                }
                if (!BLOCK_NETWORK) {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, u));
                    } catch (ActivityNotFoundException ignored) {
                    }
                }
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                super.onPageStarted(view, url, favicon);
                inject(view);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                inject(view);
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (fileCallback != null) {
                    fileCallback.onReceiveValue(null);
                }
                fileCallback = callback;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");
                if (params != null && params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) {
                    i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                }
                try {
                    startActivityForResult(Intent.createChooser(i, "Open file"), REQ_FILE);
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    callback.onReceiveValue(null);
                }
                return true;
            }
        });

        web.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition,
                                        String mimeType, long contentLength) {
                if (url.startsWith("blob:") || url.startsWith("data:")) {
                    String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
                    web.evaluateJavascript("window.__acSave && window.__acSave("
                            + JSONObject.quote(url) + "," + JSONObject.quote(name) + ")", null);
                } else if (!BLOCK_NETWORK) {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                    } catch (ActivityNotFoundException ignored) {
                    }
                }
            }
        });

        try {
            ServiceWorkerController.getInstance().setServiceWorkerClient(new ServiceWorkerClient() {
                @Override
                public WebResourceResponse shouldInterceptRequest(WebResourceRequest request) {
                    return serve(request);
                }
            });
        } catch (Throwable ignored) {
        }

        web.loadUrl(START_URL);
    }

    private void inject(WebView view) {
        if (hookJs.length() > 0) {
            view.evaluateJavascript(hookJs, null);
        }
    }

    // ---- serving the bundled files -----------------------------------------------------

    private WebResourceResponse serve(WebResourceRequest req) {
        Uri u = req.getUrl();
        Map<String, String> headers = new HashMap<String, String>();
        headers.put("Cache-Control", "no-cache");
        if (!HOST.equals(u.getHost())) {
            if (BLOCK_NETWORK) {
                return new WebResourceResponse("text/plain", "utf-8", 403, "Blocked", headers,
                        new ByteArrayInputStream(new byte[0]));
            }
            return null; // let the WebView fetch it from the network
        }
        String path = u.getPath();
        if (path == null || path.length() == 0 || "/".equals(path)) {
            path = "/index.html";
        } else if (path.endsWith("/")) {
            path = path + "index.html";
        }
        String rel = path.substring(1);
        if (rel.contains("..")) {
            return notFound(headers);
        }
        try {
            InputStream in = getAssets().open("www/" + rel, AssetManager.ACCESS_STREAMING);
            String mime = mimeFor(rel);
            boolean text = mime.startsWith("text/") || mime.equals("application/json")
                    || mime.equals("image/svg+xml");
            return new WebResourceResponse(mime, text ? "utf-8" : null, 200, "OK", headers, in);
        } catch (IOException e) {
            return notFound(headers);
        }
    }

    private static WebResourceResponse notFound(Map<String, String> headers) {
        return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", headers,
                new ByteArrayInputStream("Not found".getBytes()));
    }

    private static String mimeFor(String path) {
        String p = path.toLowerCase(Locale.ROOT);
        int q = p.lastIndexOf('.');
        String ext = q >= 0 ? p.substring(q + 1) : "";
        switch (ext) {
            case "html": case "htm": return "text/html";
            case "js": case "mjs": return "text/javascript";
            case "css": return "text/css";
            case "json": case "map": return "application/json";
            case "wasm": return "application/wasm";
            case "txt": return "text/plain";
            case "svg": return "image/svg+xml";
            case "png": return "image/png";
            case "jpg": case "jpeg": return "image/jpeg";
            case "gif": return "image/gif";
            case "webp": return "image/webp";
            case "ico": return "image/x-icon";
            case "woff2": return "font/woff2";
            case "woff": return "font/woff";
            case "ttf": return "font/ttf";
            case "otf": return "font/otf";
            case "webmanifest": return "application/manifest+json";
            default: return "application/octet-stream";
        }
    }

    private String readAsset(String name) {
        try {
            InputStream in = getAssets().open(name);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) != -1) {
                out.write(buf, 0, n);
            }
            in.close();
            return out.toString("UTF-8");
        } catch (IOException e) {
            return "";
        }
    }

    // ---- file picker -------------------------------------------------------------------

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE) {
            if (fileCallback != null) {
                Uri[] results = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
                fileCallback.onReceiveValue(results);
                fileCallback = null;
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    // ---- saving files to Downloads -----------------------------------------------------

    private void toast(final String msg) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                Toast.makeText(MainActivity.this, msg, Toast.LENGTH_LONG).show();
            }
        });
    }

    private static String cleanName(String name, String mime) {
        String n = name == null ? "" : name.replaceAll("[\\\\/:*?\"<>|]", "_").trim();
        if (n.length() == 0) {
            n = "download";
        }
        if (n.indexOf('.') < 0 && mime != null && mime.length() > 0) {
            String ext = MimeTypeMap.getSingleton().getExtensionFromMimeType(mime);
            if (ext != null) {
                n = n + "." + ext;
            }
        }
        return n;
    }

    private static File uniqueFile(File dir, String name) {
        File f = new File(dir, name);
        String base = name;
        String ext = "";
        int dot = name.lastIndexOf('.');
        if (dot > 0) {
            base = name.substring(0, dot);
            ext = name.substring(dot);
        }
        int i = 1;
        while (f.exists()) {
            f = new File(dir, base + " (" + i + ")" + ext);
            i++;
        }
        return f;
    }

    public final class DownloadBridge {
        private int nextId = 1;
        private final Map<Integer, OutputStream> streams = new HashMap<Integer, OutputStream>();
        private final Map<Integer, String> names = new HashMap<Integer, String>();

        @JavascriptInterface
        public synchronized int begin(String name, String mime) {
            try {
                String safe = cleanName(name, mime);
                OutputStream os;
                String shown = safe;
                if (Build.VERSION.SDK_INT >= 29) {
                    ContentValues v = new ContentValues();
                    v.put(MediaStore.Downloads.DISPLAY_NAME, safe);
                    v.put(MediaStore.Downloads.MIME_TYPE,
                            (mime == null || mime.length() == 0) ? "application/octet-stream" : mime);
                    v.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
                    Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
                    if (uri == null) {
                        return -1;
                    }
                    os = getContentResolver().openOutputStream(uri);
                    shown = "Downloads/" + safe;
                } else {
                    File dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
                    if (dir == null) {
                        return -1;
                    }
                    dir.mkdirs();
                    File f = uniqueFile(dir, safe);
                    os = new FileOutputStream(f);
                    shown = f.getAbsolutePath();
                }
                if (os == null) {
                    return -1;
                }
                int id = nextId++;
                streams.put(id, os);
                names.put(id, shown);
                return id;
            } catch (Exception e) {
                toast("Save failed: " + e.getMessage());
                return -1;
            }
        }

        @JavascriptInterface
        public synchronized boolean write(int id, String base64) {
            OutputStream os = streams.get(id);
            if (os == null) {
                return false;
            }
            try {
                os.write(Base64.decode(base64, Base64.DEFAULT));
                return true;
            } catch (Exception e) {
                return false;
            }
        }

        @JavascriptInterface
        public synchronized void end(int id, boolean ok) {
            OutputStream os = streams.remove(id);
            String name = names.remove(id);
            if (os == null) {
                return;
            }
            try {
                os.close();
            } catch (IOException ignored) {
            }
            toast(ok ? "Saved: " + name : "Save failed: " + name);
        }

        @JavascriptInterface
        public void fail(String message) {
            toast("Save failed: " + message);
        }
    }

    // ---- lifecycle ---------------------------------------------------------------------

    @Override
    public void onBackPressed() {
        // The web apps have no autosave, so a stray back-swipe must not throw work away.
        long now = SystemClock.elapsedRealtime();
        if (now - lastBack < 2000) {
            super.onBackPressed();
        } else {
            lastBack = now;
            Toast.makeText(this, "Press back again to exit", Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }
}
'''

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <!-- Required for WebView to work at all. The app itself is served from inside the APK. -->
    <uses-permission android:name="android.permission.INTERNET" />

    <application
        android:allowBackup="false"
        android:hardwareAccelerated="true"
        android:icon="@drawable/ic_launcher"
        android:label="@string/app_name"
        android:largeHeap="true"
        android:theme="@android:style/Theme.Material.NoActionBar"
        android:usesCleartextTraffic="false">
        <activity
            android:name=".MainActivity"
            android:configChanges="orientation|screenSize|smallestScreenSize|screenLayout|keyboard|keyboardHidden|navigation|density|uiMode|layoutDirection|fontScale"
            android:exported="true"
            android:windowSoftInputMode="adjustResize">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
"""

DEFAULT_ICON_XML = """<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp"
    android:height="108dp"
    android:viewportWidth="108"
    android:viewportHeight="108">
    <path android:fillColor="#FF2B2F36" android:pathData="M0,0h108v108h-108z" />
    <path android:fillColor="#FFE8A33D"
        android:pathData="M54,54m-26,0a26,26 0 1,0 52,0a26,26 0 1,0 -52,0" />
</vector>
"""


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("'", "\\'").replace('"', '\\"'))


def gradle_str(s: str) -> str:
    return s.replace("\\", "/").replace("'", "\\'")


def fetch_icon(owner: str, repo: str) -> Optional[bytes]:
    for branch in ("main", "master"):
        for size in (512, 256, 128, 64):
            url = ("https://raw.githubusercontent.com/%s/%s/%s/assets/app-icon/hicolor/%dx%d/apps/ai.storyteller.%s.png"
                   % (owner, repo, branch, size, size, repo))
            try:
                data = http_get(url, timeout=20)
            except Exception:
                continue
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return data
    return None


def find_web_root(extracted: Path) -> Path:
    found = sorted(extracted.rglob("index.html"), key=lambda p: len(p.parts))
    if not found:
        die("The downloaded web build has no index.html - cannot wrap it.")
    return found[0].parent


def generate_project(project: Path, app: Dict[str, str], version: str, web_root: Path,
                     keystore: Path, opts: argparse.Namespace) -> None:
    slug = re.sub(r"[^a-z0-9]", "", app["repo"].lower()) or "app"
    if slug[0].isdigit():
        slug = "a" + slug
    package = "com.artcraft." + slug
    vname = re.sub(r"[^0-9A-Za-z._-]", "", version) or "1.0"
    vcode = int(time.time() // 60)
    start_path = "index.html" + ("?webgl" if opts.webgl else "")

    rmtree(project)
    main = project / "app" / "src" / "main"

    write(project / "settings.gradle", """pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = 'ArtCraftApp'
include ':app'
""")
    write(project / "build.gradle",
          "plugins {\n    id 'com.android.application' version '%s' apply false\n}\n" % AGP_VERSION)
    write(project / "gradle.properties",
          "org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8\nandroid.nonTransitiveRClass=true\n")
    write(project / "app" / "build.gradle", """plugins {
    id 'com.android.application'
}

android {
    namespace '%(pkg)s'
    compileSdk %(compile)d
    buildToolsVersion '%(bt)s'

    defaultConfig {
        applicationId '%(pkg)s'
        minSdk %(minsdk)d
        targetSdk %(compile)d
        versionCode %(vcode)d
        versionName '%(vname)s'
    }

    signingConfigs {
        release {
            storeFile file('%(ks)s')
            storePassword '%(pw)s'
            keyAlias 'artcraft'
            keyPassword '%(pw)s'
        }
    }

    buildTypes {
        release {
            minifyEnabled false
            signingConfig signingConfigs.release
        }
    }

    compileOptions {
        sourceCompatibility JavaVersion.VERSION_17
        targetCompatibility JavaVersion.VERSION_17
    }

    // Keep every bundled web file (including folders that start with "_").
    androidResources {
        ignoreAssetsPattern = '!.svn:!.git'
    }

    lint {
        checkReleaseBuilds = false
        abortOnError = false
    }
}
""" % {"pkg": package, "compile": COMPILE_SDK, "bt": BUILD_TOOLS, "minsdk": MIN_SDK, "vcode": vcode,
       "vname": vname, "ks": gradle_str(str(keystore)), "pw": KEYSTORE_PASSWORD})

    write(main / "AndroidManifest.xml", MANIFEST)
    java = (MAIN_ACTIVITY_JAVA.replace("@@PACKAGE@@", package).replace("@@START_PATH@@", start_path)
            .replace("@@BLOCK_NETWORK@@", "true" if opts.block_network else "false")
            .replace("@@DEBUG_WEBVIEW@@", "true" if opts.debug_webview else "false"))
    write(main / "java" / Path(*package.split(".")) / "MainActivity.java", java)
    write(main / "res" / "values" / "strings.xml",
          '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <string name="app_name">%s</string>\n</resources>\n'
          % xml_escape(app["name"]))
    write(main / "assets" / "artcraft_hook.js", HOOK_JS)

    icon = fetch_icon(app["owner"], app["repo"])
    if icon:
        p = main / "res" / "drawable-xxxhdpi" / "ic_launcher.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(icon)
    else:
        say("      (no app icon found online - using a plain default icon)")
        write(main / "res" / "drawable" / "ic_launcher.xml", DEFAULT_ICON_XML)

    shutil.copytree(str(web_root), str(main / "assets" / "www"))


def build_apk(project: Path, java_home: Path, sdk: Path, gradle: Path) -> Path:
    write(project / "local.properties", "sdk.dir=%s\n" % sdk.as_posix())
    env = make_env(java_home, sdk)
    say("      Running Gradle (first run downloads the Android Gradle Plugin - please wait) ...\n")
    rc = subprocess.call([str(gradle), "--no-daemon", "--console=plain", "-p", str(project), "assembleRelease"],
                         env=env, cwd=str(project))
    apk = project / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
    if rc != 0 or not apk.exists():
        hint = ["The Gradle build failed (exit code %d). Common causes:" % rc,
                "  - no internet access to dl.google.com / maven.google.com / repo.maven.apache.org",
                "    (behind a proxy? set JAVA_TOOL_OPTIONS or ~/.gradle/gradle.properties proxy settings)",
                "  - the Android build tools do not run on this CPU (e.g. ARM Linux)",
                "  - not enough disk space or memory (Gradle wants ~2 GB RAM)",
                "The generated project was kept for inspection: %s" % project]
        die("\n".join(hint))
    return apk


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Build an offline Android APK from an ArtCraft web app.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=("Examples" + __doc__.split("Examples", 1)[1])
                                 if "Examples" in (__doc__ or "") else "")
    ap.add_argument("--list", action="store_true", help="list the available apps and exit")
    ap.add_argument("--app", help="app name or number from --list (skips the menu)")
    ap.add_argument("--repo", help="build any GitHub repo that publishes a web build, as owner/name")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation before downloading tools")
    ap.add_argument("--cache", help="folder for downloaded tools "
                                    "(default: $ARTCRAFT_APK_HOME or ~/.artcraft-apk)")
    ap.add_argument("--output", default="output", help="folder for the finished APK (default: ./output)")
    ap.add_argument("--webgl", action="store_true", help="start with ?webgl (force the WebGL2 fallback; PhotoCraft)")
    ap.add_argument("--block-network", action="store_true",
                    help="make the app strictly offline: block every request that is not bundled")
    ap.add_argument("--debug-webview", action="store_true",
                    help="allow inspecting the app from chrome://inspect on a computer")
    ap.add_argument("--generate-only", action="store_true",
                    help="only write the Android Studio project; do not install tools or build")
    ap.add_argument("--keep-project", action="store_true", help="keep the generated Android project after building")
    return ap.parse_args()


def main() -> None:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    args = parse_args()
    cache = Path(args.cache or os.environ.get("ARTCRAFT_APK_HOME") or (Path.home() / ".artcraft-apk")).expanduser()
    total = 5 if args.generate_only else 7

    # ---- 1. apps -------------------------------------------------------------------------
    step(1, total, "Fetching the list of ArtCraft apps ...")
    apps = discover_apps()
    if args.list:
        for i, a in enumerate(apps, 1):
            say("  %d) %-12s %-14s %s" % (i, a["name"], "(%s/%s)" % (a["owner"], a["repo"]), a["desc"]))
        return

    # ---- 2. choose -----------------------------------------------------------------------
    step(2, total, "Choosing the app ...")
    app = choose_app(apps, args.app, args.repo)
    say("      Selected: %s  (github.com/%s/%s)" % (app["name"], app["owner"], app["repo"]))

    # ---- 3. latest web build ---------------------------------------------------------------
    step(3, total, "Looking for the latest web build ...")
    rel = find_web_release(app)
    size = (" (%s)" % human(rel["size"])) if rel.get("size") else ""
    say("      Latest web build: %s  ->  %s%s%s" % (rel["tag"], rel["name"], size,
                                                  "  [pre-release]" if rel["prerelease"] else ""))
    if not rel["sha256"]:
        say("      Warning: no checksum was published for this file, so it cannot be verified.")

    java_home: Optional[Path] = None
    sdk_plan: Optional[SdkPlan] = None
    gradle: Optional[Path] = None

    # ---- 4. tools --------------------------------------------------------------------------
    if not args.generate_only:
        step(4, total, "Checking the build tools on this computer ...")
        if ARCH == "aarch64" and OS == "linux":
            say("      Warning: Google's Android build tools are built for x86-64. On ARM Linux the build may fail.")
        java_home, jmajor = find_java(cache)
        sdk_plan = plan_sdk(cache)
        env0 = make_env(java_home, None) if java_home else os.environ.copy()
        gradle = find_gradle(cache, env0)

        say("      Java (JDK %d-%d) : %s" % (JDK_ACCEPT[0], JDK_ACCEPT[-1],
            ("found  %s  (version %d)" % (java_home, jmajor)) if java_home else "NOT FOUND"))
        if not sdk_plan.missing:
            say("      Android SDK       : found  %s  (platform %d + build-tools %s)" % (sdk_plan.path, COMPILE_SDK, BUILD_TOOLS))
        elif sdk_plan.existing and not sdk_plan.private:
            say("      Android SDK       : found  %s  but missing: %s" % (sdk_plan.path, ", ".join(sdk_plan.missing)))
        else:
            say("      Android SDK       : NOT FOUND")
        say("      Gradle %s+      : %s" % (GRADLE_VERSION, ("found  %s" % gradle) if gradle else "NOT FOUND"))

        items: List[Tuple[str, int, str]] = []  # (what, MB, where)
        if not java_home:
            items.append(("JDK 17 (Eclipse Temurin)", 190, str(cache / "jdk")))
        if not gradle:
            items.append(("Gradle %s" % GRADLE_VERSION, 130, str(cache / "gradle")))
        if sdk_plan.need_cmdline:
            items.append(("Android SDK command-line tools", 150, str(sdk_plan.path / "cmdline-tools")))
        if sdk_plan.missing:
            where = str(sdk_plan.path)
            label = "Android SDK: " + ", ".join(sdk_plan.missing)
            if sdk_plan.existing and not sdk_plan.private:
                label += "  (added to your existing SDK; nothing already there is changed)"
            mb = sum(65 if pkg.startswith("platforms") else 60 for pkg in sdk_plan.missing)
            items.append((label, mb, where))
        if not agp_cached():
            items.append(("Android Gradle Plugin + libraries (fetched by Gradle while building)", 150,
                          str(gradle_user_home())))

        if items:
            bar = "=" * 74
            say("\n" + bar)
            say(" THE FOLLOWING WILL BE DOWNLOADED AND SAVED ON THIS COMPUTER")
            say(bar)
            for what, mb, where in items:
                say("  * %s\n      about %d MB  ->  %s" % (what, mb, where))
            say("\n  Total: roughly %d MB. Tools that were found above are used as they are." % sum(i[1] for i in items))
            if sdk_plan.missing:
                say("  Android SDK licenses (https://developer.android.com/studio/terms) are accepted automatically.")
            say("  To remove everything this script saved, delete: %s" % cache)
            say(bar)
            if not confirm("Download and continue?", args.yes):
                say("\nCancelled. Nothing was downloaded.")
                sys.exit(0)
            try:
                free = shutil.disk_usage(str(cache if cache.exists() else Path.home())).free
                if free < 3 * 1024 ** 3:
                    say("      Warning: only %s of free disk space." % human(free))
            except Exception:
                pass
            cache.mkdir(parents=True, exist_ok=True)
        else:
            say("      Everything needed is already installed - no tool downloads.")

        if not java_home:
            say("\n      Downloading the JDK ...")
            java_home = install_jdk(cache)
        if not gradle:
            say("\n      Downloading Gradle ...")
            gradle = install_gradle(cache)
        if sdk_plan.need_cmdline or sdk_plan.missing:
            say("\n      Setting up the Android SDK ...")
            install_sdk(sdk_plan, cache, java_home)

    # ---- 5. download the web build -----------------------------------------------------------
    step(4 if args.generate_only else 5, total, "Downloading the %s web build ..." % app["name"])
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / "downloads" / rel["name"]
    download(rel["url"], archive, rel["name"], rel["sha256"])
    if rel["sha256"]:
        say("      Checksum OK (SHA-256).")
    work = cache / "build" / app["repo"]
    extracted = work / "web"
    rmtree(work)
    extract(archive, extracted)
    archive.unlink()
    web_root = find_web_root(extracted)

    # ---- 6. generate project ---------------------------------------------------------------
    step(5 if args.generate_only else 6, total, "Generating the Android project ...")
    if args.generate_only:
        project = Path(args.output).resolve() / ("%s-android-project" % app["repo"])
        keystore = cache / "signing" / "artcraft-release.keystore"
    else:
        project = work / "android"
        keystore = ensure_keystore(cache, java_home)  # type: ignore[arg-type]
    generate_project(project, app, rel["version"], web_root, keystore, args)
    say("      Project: %s" % project)
    if args.generate_only:
        say("\nDone. Open this folder in Android Studio, or build it with: gradle assembleRelease")
        rmtree(work)
        return

    # ---- 7. build ----------------------------------------------------------------------------
    step(7, total, "Building and signing the APK ...")
    apk = build_apk(project, java_home, sdk_plan.path, gradle)  # type: ignore[arg-type]
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / ("%s-%s.apk" % (re.sub(r"[^A-Za-z0-9._-]", "", app["name"]) or app["repo"], rel["version"]))
    shutil.copyfile(str(apk), str(final))
    if not args.keep_project:
        rmtree(work)

    say("\n" + "=" * 74)
    say(" DONE:  %s  (%s)" % (final, human(final.stat().st_size)))
    say("=" * 74)
    say(" Install it on your phone:")
    say("   - copy the .apk to the phone, open it, and allow \"install unknown apps\" when asked; or")
    say("   - with a USB cable and USB debugging on:  adb install -r \"%s\"" % final)
    say(" If the screen stays blank, rebuild with --webgl (and --debug-webview to inspect it).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        sys.exit(130)
    except urllib.error.URLError as exc:
        die("Network problem: %s\n       Check your internet connection (or proxy settings) and try again." % exc)
