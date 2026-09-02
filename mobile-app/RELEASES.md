# Android installation packages

Android source code lives in this directory, while generated APK files are
kept with other ignored build artifacts at the project root.

Default release directory:

```text
build/android/
```

Build and publish a debug APK on Windows:

```text
build-apk.bat
```

The published filename includes the Android `versionName`:

```text
LifeLink-v<version>-debug.apk
```

An unsigned release build is explicitly named `release-unsigned` so that it is
not mistaken for an installable, signed production package. `build/android/`
is an artifact directory, not a permanent archive: retain only packages that
still need installation or verification, and archive older APKs outside the
working checkout if they must be kept.

Set `LIFE_RADIO_RELEASES_DIR` to use another release directory. Gradle's
temporary output remains under `app/build/` and is intentionally ignored by
version control.
