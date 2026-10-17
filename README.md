# ArtCraft APK Builder

A single-file Python script that turns an [ArtCraft](https://getartcraft.com/) web app (PhotoCraft, VectorCraft, FilmCraft and the other Crafting Apps) into an **offline Android APK**.

It fetches the app list, lets you pick one, downloads the app's latest official web build, verifies its checksum, bundles it into a small Android WebView shell, and builds and signs the APK. The web files live inside the APK, so the installed app runs without an internet connection.

> **Unofficial.** This project is not affiliated with or endorsed by ArtCraft. It only wraps the web builds that each app publishes on its GitHub releases page.

---

## Features

- **Zero dependencies.** Python 3.8+ standard library only. Nothing to `pip install`.
- **Always the latest build.** Finds the newest GitHub release that contains a `*-web-*.zip` and verifies it against the published `SHA256SUMS.txt`.
- **Detects existing tools first.** Uses the JDK, Android SDK and Gradle already on your machine, and downloads only what is missing.
- **Tells you before downloading.** Lists exactly what will be downloaded, how big it is and where it will be saved, then asks for confirmation.
- **Fully offline app.** Web files are served from inside the APK. An optional strict mode blocks all other network requests.
- **Works for phones and tablets.** Handles file opening and saving (exports go to your Downloads folder).
- **No API limit problems.** If GitHub's API rate limit is hit, it falls back to reading the public release pages.
- **Cross-platform.** Windows, macOS and Linux.

## Supported apps

At the time of writing, all seven Crafting Apps publish a web build:

| App | What it is for |
|---|---|
| PhotoCraft | Image editing: layers, masks, type and real PSD files |
| VectorCraft | Vector illustration |
| FilmCraft | Video editing, color and sound |
| LightCraft | Photo library and raw development |
| PdfCraft | Reading, organizing and protecting PDFs |
| EffectCraft | Motion graphics and visual effects |
| DesignCraft | Page layout and publishing |

The list is read live from the PhotoCraft README, so new apps appear automatically. Any other GitHub repo that publishes a `*-web-*.zip` release can be built with `--repo owner/name`.

## Quick start

```bash
git clone https://github.com/maheshmv666/ArtCraft-APK-Builder.git
cd <ArtCraft-APK-Builder>
python3 artcraft_apk.py
```

Pick an app from the menu. When it finishes, your APK is in `./output/`.

Skip the menu:

```bash
python3 artcraft_apk.py --app photocraft
```

## Requirements

- **Python 3.8 or newer**
- **An internet connection** for the first run (to fetch the web build and any missing tools)
- About **3 GB of free disk space** if the build tools must be downloaded
- About **2 GB of RAM** for the Gradle build

The build tools themselves (JDK 17-21, Android SDK, Gradle 8.7+) are detected, and installed by the script if missing.

## What gets downloaded, and where

The script checks your system first. If something is missing you will see a warning like this before anything is downloaded:

```
==========================================================================
 THE FOLLOWING WILL BE DOWNLOADED AND SAVED ON THIS COMPUTER
==========================================================================
  * Gradle 8.7
      about 130 MB  ->  /home/you/.artcraft-apk/gradle
  * Android SDK: platforms;android-34, build-tools;34.0.0
      about 130 MB  ->  /home/you/.artcraft-apk/android-sdk
  ...
Download and continue? [Y/n]
```

| Tool | Detected from | Downloaded if missing |
|---|---|---|
| JDK 17-21 (with `javac`, `keytool`, `jlink`) | `JAVA_HOME`, `java` on `PATH`, Android Studio's bundled JDK, the cache | Eclipse Temurin 17 from Adoptium |
| Android SDK (platform 34 + build-tools 34.0.0) | `ANDROID_HOME`, `ANDROID_SDK_ROOT`, default install locations, the cache | Google's command-line tools, then the packages |
| Gradle 8.7+ | the cache, `gradle` on `PATH` | Gradle 8.7 from services.gradle.org |
| Android Gradle Plugin and libraries | `~/.gradle` cache | Fetched by Gradle during the build |

- Everything the script downloads goes into one **cache folder**, by default `~/.artcraft-apk`. Delete it to remove everything. (The Gradle plugin cache is the exception: Gradle keeps it in `~/.gradle`.)
- If an existing SDK is only missing some packages, those are added to it and nothing already there is changed. If the existing SDK is read-only, a private copy is used instead.
- Android SDK licenses are accepted automatically. By continuing you agree to the [Android SDK terms](https://developer.android.com/studio/terms).

## Options

| Option | Description |
|---|---|
| `--list` | List the available apps and exit |
| `--app NAME` | App name or number from `--list`; skips the menu |
| `--repo OWNER/NAME` | Build any GitHub repo that publishes a web build |
| `--yes`, `-y` | Don't ask for confirmation before downloading tools |
| `--cache DIR` | Where downloaded tools are saved (default: `$ARTCRAFT_APK_HOME` or `~/.artcraft-apk`) |
| `--output DIR` | Where the finished APK is saved (default: `./output`) |
| `--webgl` | Start the app with `?webgl` to force the WebGL2 fallback (PhotoCraft) |
| `--block-network` | Strictly offline: block every request that is not bundled in the APK |
| `--debug-webview` | Allow inspecting the app from `chrome://inspect` on a computer |
| `--generate-only` | Only write the Android Studio project; don't install tools or build |
| `--keep-project` | Keep the generated Android project after building |

Setting a `GITHUB_TOKEN` environment variable raises GitHub's API rate limit, but is not required.

### Examples

```bash
python3 artcraft_apk.py --list
python3 artcraft_apk.py --app vectorcraft --yes
python3 artcraft_apk.py --app photocraft --webgl
python3 artcraft_apk.py --app pdfcraft --block-network
python3 artcraft_apk.py --app photocraft --generate-only   # open in Android Studio
python3 artcraft_apk.py --app photocraft --cache /mnt/tools/artcraft-cache
```

## Installing the APK on your phone

- **Copy the file** to your phone, open it, and allow "install unknown apps" for your file manager or browser when asked; or
- **Use a USB cable** with USB debugging enabled:

  ```bash
  adb install -r output/PhotoCraft-0.9.0.apk
  ```

The APK works on **Android 7.0 (API 24) or newer**.

## How it works

1. Reads the table of Crafting Apps from the PhotoCraft README (falls back to the GitHub API, then to a built-in list).
2. You choose an app.
3. Finds the newest release containing a web build and downloads it, verifying SHA-256.
4. Detects the JDK, SDK and Gradle, warns about anything it must download, then installs it.
5. Generates a minimal Android project: one activity hosting a `WebView`. The web files are bundled as assets and served from a virtual `https://appassets.androidplatform.net/` origin, which gives the app a secure context for WebGPU and the clipboard.
6. Builds a release APK with Gradle and signs it with a local key.

The shell also handles opening files from your phone, saving exports to Downloads, hardware back-button protection (press twice to exit, since the web apps have no autosave) and keeping the app from reloading on rotation.

## Signing key

APKs are signed with a key generated on first use and stored at `<cache>/signing/artcraft-release.keystore` (password `artcraft`). This is suitable for **personal use**, not for publishing to Google Play.

**Keep this key.** Android only installs an update over an existing app if both APKs were signed with the same key. If you lose it, uninstall the old app before installing a new build.

## Troubleshooting

**The app opens but the screen is blank or shows an error.**
The apps need WebGPU or WebGL2 in Android's WebView. Update *Android System WebView* and *Chrome* from the Play Store, then try rebuilding with `--webgl`. Add `--debug-webview` and open `chrome://inspect` on a computer to see the console.

**The Gradle build fails.**
Common causes: no access to `dl.google.com`, `maven.google.com` or `repo.maven.apache.org` (check your network or proxy settings); too little RAM or disk space; or ARM Linux, where Google's Android build tools may not run. The generated project is kept so you can inspect it.

**"No release with a web build."**
That app has not published a `*-web-*.zip` yet, and only apps that do can be wrapped.

**GitHub API rate limit.**
The script automatically falls back to public release pages. You can also set `GITHUB_TOKEN`.

**An old Java is found.**
Only JDK 17-21 with `javac`, `keytool` and `jlink` is accepted. Anything else is ignored and a suitable JDK is downloaded into the cache.

## Limitations

- Performance depends on your phone. Large documents and heavy apps such as FilmCraft and EffectCraft may hit browser memory limits on a phone, and the apps are desktop-style, so a tablet works best.
- The ArtCraft apps are early software, and their web builds may change layout between versions.
- Web app features that depend on a desktop (CLI, MCP server, local control channel) are not available in a browser build.
- The signing key is for personal use only.

## License

Add your chosen license here (for example, MIT) and include a `LICENSE` file in the repository.

The ArtCraft apps themselves are separate projects with their own licenses. PhotoCraft, for example, is dual-licensed under MIT or Apache-2.0. This script downloads and bundles their official release files unmodified; check each app's repository for its license and trademark terms before redistributing any APK.
