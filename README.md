# ArtCraft APK Builder

A single-file Python script that builds **native Android apps** from the [ArtCraft](https://getartcraft.com/) apps (PhotoCraft, VectorCraft, FilmCraft and the other Crafting Apps).

It fetches the list of apps, lets you pick one, finds its latest version, checks which build tools you already have, downloads only what is missing (after telling you exactly what and where), and compiles the app's Rust source into a signed arm64 APK. There is no WebView: the result is the real native app, and it works fully offline.

> **Unofficial.** This project is not affiliated with or endorsed by ArtCraft or by the author of the builder it uses (see [Credits](#credits)).

---

## Features

- **Same simple flow every time:** list apps, choose one, get the latest version, set up tools, build.
- **Latest release by default.** Builds the newest released version of the app, or the newest development code with `--main`.
- **Detects existing tools first.** Uses the JDK, Android SDK/NDK and Rust already on your machine, and downloads only what is missing.
- **Tells you before downloading.** Lists every download with its size and where it will be saved, then asks for confirmation.
- **Self-contained.** Everything it downloads goes into one cache folder. Your own Rust setup is never modified.
- **Supports all 13 apps** the builder knows: PhotoCraft, VectorCraft, FilmCraft, LightCraft, PdfCraft, EffectCraft, DesignCraft, FigCraft, WordCraft, SoundCraft, DeckCraft, CADCraft and GridCraft. The list is read live, so new apps appear automatically.
- **Pure Python standard library.** Nothing to `pip install`.

## Requirements

| Needed | Notes |
|---|---|
| **Linux x86-64** | On Windows, use [WSL2](https://learn.microsoft.com/windows/wsl/install) (Ubuntu). macOS and ARM Linux are not supported (the Android NDK and the builder are Linux x86-64 only). |
| **Python 3.11+** | Needed to read Cargo manifests. |
| **git** and a **C compiler** | Debian/Ubuntu: `sudo apt install git build-essential` |
| **~15 GB free disk space** | The Android NDK, the Rust toolchain and the first build are large. |
| **~8 GB RAM** | Linking the Rust apps is memory-hungry. |
| **Internet** | For the first run. |

Everything else (JDK, Android SDK and NDK, Rust, `cargo-apk2`) is detected, or downloaded by the script if missing. You can't install the system packages above automatically because they need root, so the script checks them first and tells you what to install.

The APK is for **64-bit ARM (arm64-v8a)** Android devices, which is nearly every phone and tablet from the last several years.

## Quick start

```bash
git clone https://github.com/maheshmv666/ArtCraft-APK-Builder.git
cd <this-repo>
python3 artcraft_apk.py
```

Pick an app from the menu. The first build compiles hundreds of Rust crates and takes **15-40 minutes**; later builds are much faster. When it finishes, the APK is in `./output/`.

Skip the menu:

```bash
python3 artcraft_apk.py --app photocraft
```

## What gets downloaded, and where

The script checks your system first. If something is missing you will see a warning like this before anything is downloaded:

```
==========================================================================
 THE FOLLOWING WILL BE DOWNLOADED AND SAVED ON THIS COMPUTER
==========================================================================
  * Android SDK package: ndk;27.2.12479018
      about 1.0 GB  ->  /home/you/.artcraft-apk/android-sdk
  * Rust toolchain (rustup + stable + aarch64-linux-android)
      about 300 MB  ->  /home/you/.artcraft-apk/rust
  ...
Continue? [Y/n]
```

| Tool | Detected from | Downloaded if missing |
|---|---|---|
| JDK 17-21 (`java`, `javac`, `keytool`) | `JAVA_HOME`, `java` on `PATH`, Android Studio's JDK, the cache | Eclipse Temurin 17 |
| Android SDK (platform 35, build-tools 35.0.0) and NDK 25+ | `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `ANDROID_NDK_HOME`, `~/Android/Sdk`, the cache | Google's command-line tools, then the packages (NDK 27) |
| Rust (stable, with the `aarch64-linux-android` target, new enough for the app) | your `rustup`, the cache | `rustup` into the cache |
| `cargo-apk2` | `cargo apk2` on `PATH`, the cache | compiled from source into the cache |

- Everything downloaded goes into the **cache folder**, by default `~/.artcraft-apk`. Delete it to remove everything. (Cargo's library cache is the one exception if you use your own Rust: it lives in your usual `~/.cargo`.)
- If your own Rust install already satisfies the requirements, it is used as it is. Otherwise a **separate private copy** is installed in the cache, and your own setup is never touched.
- If an existing Android SDK is only missing some packages, those are added and nothing already there is changed.
- Android SDK licenses are accepted automatically. By continuing you agree to the [Android SDK terms](https://developer.android.com/studio/terms).

## Options

| Option | Description |
|---|---|
| `--list` | List the available apps and exit |
| `--app NAME` | App name or number from `--list`; skips the menu |
| `--yes`, `-y` | Don't ask for confirmation before downloading |
| `--main` | Build the newest development code instead of the latest release |
| `--ref REF` | Build a specific tag, branch or commit of the app |
| `--debug` | Build a debug APK (larger and slower) |
| `--appname NAME` | Override the app's display name |
| `--packagename PKG` | Override the Android package name (default `com.artcraft.<app>`) |
| `--appicon FILE.png` | Use your own launcher icon |
| `--cache DIR` | Where tools and build files are saved (default `$ARTCRAFT_APK_HOME` or `~/.artcraft-apk`) |
| `--output DIR` | Where the finished APK is saved (default `./output`) |
| `--setup-only` | Install the tools and fetch the source, but don't build |
| `--fresh` | Always re-download the app source |
| `--builder-ref REF` | Use a different commit/branch/tag of the builder (default: the reviewed pin; `main` follows its newest changes) |
| `--builder-path DIR` | Use a local checkout of the builder |

### Examples

```bash
python3 artcraft_apk.py --list
python3 artcraft_apk.py --app pdfcraft --yes
python3 artcraft_apk.py --app photocraft --main
python3 artcraft_apk.py --app photocraft --appname "My Editor" --packagename com.me.editor
python3 artcraft_apk.py --app vectorcraft --setup-only
```

## Installing the APK

- **Copy the file** to your phone, open it, and allow "install unknown apps" for your file manager or browser when asked; or
- **Use a USB cable** with USB debugging enabled:

  ```bash
  adb install -r output/photocraft-0.10.0.apk
  ```

## How it works

1. Downloads the builder (a small archive, pinned to a reviewed commit) and reads the list of supported apps from it, with descriptions from the PhotoCraft README.
2. You choose an app.
3. Finds the app's newest release tag with `git ls-remote` (no GitHub API, so no rate limit) and reads the Rust version it needs.
4. Detects the tools, warns about anything missing, and installs it after you confirm.
5. Downloads that version's source from `github.com/storytold/<app>`.
6. Runs the builder, which generates an Android launcher for the app, compiles it with the Android NDK, packages it with `cargo-apk2` and signs it.
7. Copies the finished APK to `./output/`.

## Signing key

APKs are signed with a key generated on first use, stored at `<cache>/native/keys/artcraft-builder.jks`. It is suitable for **personal use**, not for Google Play. **Keep it**: Android only installs an update over an existing app if both builds used the same key.

## Troubleshooting

**The build fails.**
Read the build log (the script prints its path). Then try, in order:
- `--main` (a release can be older than the builder expects),
- `--builder-ref main` (the builder may have been fixed since the pinned version),
- freeing disk space (15 GB) or memory (8 GB).

**"Missing system packages."**
Install `git` and a C compiler, then run again: `sudo apt install git build-essential`.

**"Rust ... is installed but the app needs ... or newer."**
The newest stable Rust is older than what the app requires. Run `rustup update stable` (or delete the `rust` folder in the cache so a fresh one is installed) and try again, or build an older release with `--ref`.

**It's slow.**
The first build of each app compiles hundreds of Rust crates. Build output is kept in `<cache>/native/build`, so later builds of the same app are much faster.

**cargo-apk2 won't install.**
Install `pkg-config` and `libssl-dev`, then run again.

## Limitations

- **Linux x86-64 only** (or WSL2 on Windows), because the Android NDK and the underlying builder are.
- **arm64 devices only.**
- The ArtCraft apps are early software, and their desktop-style interfaces are not designed for phones, so a tablet or a stylus works best. Native Android features (file picker, keyboard, fonts) are provided by the builder and may be incomplete per app.
- Release versions are built from tags; `main` has newer features but can occasionally fail to compile.
- The signing key is for personal use only.

The ArtCraft apps themselves are separate projects with their own licenses (PhotoCraft, for example, is MIT OR Apache-2.0). Check each app's repository before redistributing any APK.

## License

This project is licensed under the MIT License. See the `LICENSE` file.
