# crosszig

A small sample C++ project that cross-compiles from any of the pixi platforms
(`linux-64`, `win-64`, `osx-64`, `osx-arm64`) to any of them, using **zig** as
the cross toolchain and **CMake** as the build system. Boost is pulled in with
`FetchContent`, so nothing has to be installed on the machine beyond the pixi
environment.

## Quick start

```sh
./dev.py build              # debug build for the host platform
./dev.py build -r -a        # release build for every supported platform
./dev.py build -t win-64    # debug build for Windows only
./dev.py run -r             # build for the host and run the binary
./dev.py dist               # collect the release artifacts into ./dist
./dev.py dist-check ./dist  # check a directory against dist-manifest.json
./dev.py clean              # remove build/ and dist/
```

`dev.py` re-executes itself through `pixi run` when `cmake`, `ninja` or `zig`
are not already on `PATH`, so it works both inside and outside an activated
environment. Build trees end up in `build/<platform>-<config>/` and each one
installs into `build/<platform>-<config>/install/`.

## Layout

| Path                      | What it is                                                   |
| ------------------------- | ------------------------------------------------------------ |
| `src/`                    | Sample code: a log-line parser built on Boost.Regex           |
| `cmake/ZigToolchain.cmake`| Toolchain file that turns `zig cc` / `zig c++` into a cross compiler |
| `cmake/Dependencies.cmake`| Fetches Boost and shares one checkout across all platforms    |
| `dev.py`                  | Thin cross-platform wrapper around the CMake commands         |
| `tools/binfmt.py`         | Reads the dynamic dependencies out of ELF, PE and Mach-O files|
| `dist-manifest.json`      | The expected content of a distribution                        |

`src/` produces two things:

* `crosszig_core` (`libcrosszig.a`) - the library with the actual logic
* `crosszig_cli` (`crosszig` / `crosszig.exe`) - a command line front end

## Using CMake directly

`dev.py` is only a convenience layer; the plain CMake invocation is:

```sh
cmake -S . -B build/win-64-release -G Ninja \
      --toolchain cmake/ZigToolchain.cmake \
      -DCROSSZIG_TARGET=win-64 \
      -DCMAKE_BUILD_TYPE=Release
cmake --build build/win-64-release
```

## How the cross compilation works

CMake expects one executable per tool, while zig exposes everything as
sub-commands of a single binary. The toolchain file therefore generates small
shim scripts (`zig-cc`, `zig-cxx`, `zig-ar`, `zig-ranlib`) into the build
directory and pins the zig target triple inside them, so the triple applies to
compiling and linking alike. Beyond that it only has to tell CMake which system
it is building for and keep `find_package` away from host libraries.

Since zig ships the headers, libc stubs and runtime libraries for all of its
targets, no additional SDK is needed - including for the macOS targets.

## Distribution

`./dev.py dist` gathers the release artifacts of all four platforms into a flat
`./dist/`, suffixing each file with the platform it was built for:

```
dist/crosszig-linux-64        dist/libcrosszig-linux-64.a
dist/crosszig-win-64.exe      dist/libcrosszig-win-64.a
dist/crosszig-osx-64          dist/libcrosszig-osx-64.a
dist/crosszig-osx-arm64       dist/libcrosszig-osx-arm64.a
```

It then verifies the result against `dist-manifest.json` and fails if anything
is off. `./dev.py dist-check <dir>` runs the same verification on its own, so a
distribution can also be checked after the fact, for instance once it has been
unpacked somewhere else.

The manifest describes the complete expected content: every file it lists has
to be present, has to need exactly the dynamic libraries it lists, and any file
in the directory that the manifest does not mention is reported as well. This
is what catches a cross build that accidentally picked up a host library. Names
are whatever the container format records - a soname for ELF (`libc.so.6`), an
import table entry for PE (`KERNEL32.dll`), an install name for Mach-O
(`/usr/lib/libSystem.B.dylib`). Static archives carry no link information of
their own and therefore map to an empty list; for them only presence is
checked.

Reading those three formats is done by `tools/binfmt.py` rather than by
`readelf`/`objdump`/`otool`, because no single one of those tools reads all
three and a given host generally has at most one of them.

`dist` deliberately only handles the release build: a debug binary picks up
extra imports (`ntdll.dll` on Windows, for one), so a single manifest could not
describe both configurations. Use `./dev.py dist --write-manifest` to
regenerate the manifest from what was just collected, after an intentional
change.

## Dependency cache

The Boost release archive is roughly 100 MB. It is downloaded into
`.cache/deps/download/` and unpacked into `.cache/deps/boost-<version>/` once;
every additional target platform reuses that same checkout instead of
downloading and unpacking its own copy. Delete `.cache/` to force a refetch.

To move to another Boost version, update both `CROSSZIG_BOOST_VERSION` and
`CROSSZIG_BOOST_SHA256` in `cmake/Dependencies.cmake`.
