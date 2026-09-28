# crosszig

A small sample C++ project that cross-compiles from any of the pixi platforms
to `linux-64`, `win-64`, `win-64-msvc`, `osx-64` and `osx-arm64`, using **zig**
as the cross toolchain and **CMake** as the build system. Boost is pulled in with
`FetchContent`, so nothing has to be installed on the machine beyond the pixi
environment.

## Quick start

```sh
./dev.py build              # debug build for the host platform
./dev.py build -r -a        # release build for every supported platform
./dev.py build -t win-64    # debug build for Windows only
./dev.py build -v           # same, with the build output unfiltered
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
| `cmake/targets.json`      | The zig triple and CPU baseline each platform builds with     |
| `cmake/ZigLinkRules.cmake`| Two link rule corrections needed for the MSVC ABI target      |
| `tools/xwin.py`           | Fetches and caches Microsoft's CRT and SDK for `win-64-msvc`  |
| `cmake/Dependencies.cmake`| Fetches Boost and shares one checkout across all platforms    |
| `dev.py`                  | Thin cross-platform wrapper around the CMake commands         |
| `tools/binfmt.py`         | Reads compatibility facts out of ELF, PE and Mach-O files     |
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

Which triple each platform maps to lives in `cmake/targets.json`, read both by
the toolchain file and by `dev.py`, so the two cannot drift apart. Three parts
of those triples are deliberate choices rather than defaults:

* **`x86_64-linux-gnu.2.3.3`** pins glibc. Without the suffix zig would target
  the glibc of whichever machine runs the build, and the result would refuse to
  start on anything older. 2.3.3 is from 2004, so in practice the glibc version
  stops being a constraint at all. Only three symbols ever pushed the floor up:
  `memcpy@2.14`, `__isoc99_sscanf@2.7` and `aligned_alloc@2.16`. The first two
  resolve to older versions on their own once the triple asks for one, and the
  third is covered by `src/compat/glibc_compat.cpp`, because zig's prebuilt
  libc++abi calls `aligned_alloc` no matter which glibc is targeted.
* **`macos.11.0`** pins the macOS deployment target to Big Sur. zig's own
  default is considerably newer, which would exclude perfectly current
  machines for no reason. Note the version needs a minor component:
  `x86_64-macos.11-none` is rejected as `InvalidOperatingSystemVersion`.
* **`"cpu": "baseline"`** pins the instruction set. zig already defaults to
  this for a cross target, but it is stated explicitly because it is the one
  compatibility property that cannot be verified after the fact.

`win-64` additionally carries `"link-flags": "-lucrtbase"`, and `win-64-msvc`
carries an `sdk`, a `cxx-driver` and a `cxx-standard`. See below for why.

### The two Windows targets

There are two, because the C runtime is the thing that decides how far back a
Windows binary reaches, and the two ways of getting one land in different
places:

| Target        | Imports                        | Needs                              |
| ------------- | ------------------------------ | ---------------------------------- |
| `win-64`      | `KERNEL32.dll`, `ucrtbase.dll` | nothing but pixi to build          |
| `win-64-msvc` | `KERNEL32.dll`, `USER32.dll`   | a one-off SDK download, see below  |

**`win-64`** goes through mingw-w64, which wraps the UCRT rather than
replacing it, so `ucrtbase.dll` cannot be linked away: it *is* the C runtime.
`libucrt.a` in zig's tree is not a static one despite the name, it is mingw's
supplement of long double and complex math routines. `-static` leaves the
import table untouched. What `-lucrtbase` does buy is resolving against
`ucrtbase.dll` directly rather than against the dozen `api-ms-win-crt-*.dll`
entries, which are only forwarders to it: twelve import table entries become
two, with no change to which Windows versions run the binary, since both forms
live in System32 from Windows 10 on and on 8.1 and earlier both arrive with the
same UCRT update. Microsoft documents the API sets as the supported contract,
so importing `ucrtbase.dll` directly sits outside that; delete the
`link-flags` entry in `cmake/targets.json` to go back to them.

**`win-64-msvc`** links Microsoft's own static CRT and so imports no C runtime
at all, which is what makes it runnable on a Windows that has never seen the
UCRT update. Those libraries only ship inside Visual Studio and the Windows
SDK and are not redistributable, so they have to be fetched once:

```sh
python tools/xwin.py setup --accept-license
```

That downloads [xwin](https://github.com/Jake-Shadle/xwin) at a pinned version,
checks it against the published sha256, and has it pull the CRT and SDK
straight from Microsoft into `.cache/xwin/` (about 82 MB over the wire, 630 MB
unpacked, x86_64 only). It is cached, so this is a one-time cost, and
`tools/xwin.py` is standalone: no imports from the rest of the repository.
Until it has been run, building `win-64-msvc` stops with that command in the
error message.

Three things make this target different from the others, all of them recorded
in `cmake/targets.json`:

* **It builds at C++14, not C++11.** MSVC's own STL headers use deduced return
  types and relaxed constexpr, so C++11 is not available with them at all.
  Every other target stays at C++11.
* **It compiles through `zig cc`, not `zig c++`.** The `c++` driver always
  builds and links zig's libc++, whose `std::bad_cast` and `type_info` collide
  with MSVC's `vcruntime_typeinfo.h`. The plain driver still compiles C++ by
  file extension and leaves the standard library to MSVC.
* **Two of CMake's link rules need correcting**, in
  `cmake/ZigLinkRules.cmake`. See the comment at the top of that file.

Linking it also produces about a hundred `LNK4099` warnings, because
Microsoft's prebuilt CRT objects name PDB files that exist only on their build
machines and the gigabytes of debug symbols that would satisfy them are
deliberately not downloaded. There is no way to switch that off at the source:
zig silently drops unrecognised `-Xlinker` flags, so `/IGNORE:4099` never
reaches the linker. `dev.py` drops those lines from the build output and
prints how many it dropped; `./dev.py build --verbose` shows them.

Editing a triple discards the affected build directory automatically. The
triple lives inside the generated compiler shims rather than in the compile
command lines, so ninja cannot see it change and would otherwise happily
report "no work to do" against artifacts built for the old target.

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

The manifest records two things: the build inputs, and a profile of every
file that was collected.

```json
{
    "cpu": "baseline",
    "targets": { "linux-64": { "triple": "x86_64-linux-gnu.2.3.3", "...": "..." } },
    "files": {
        "crosszig-linux-64": {
            "platform": "linux-64",
            "format": "elf",
            "arch": "x86-64",
            "needs": ["libc.so.6", "..."],
            "symbol-versions": { "GLIBC": "2.3.3" },
            "runpath": [],
            "min-kernel": "2.0.0"
        }
    }
}
```

Which profile keys appear depends on the format, because the formats record
genuinely different things:

| Format            | Keys                                                          |
| ----------------- | ------------------------------------------------------------- |
| all               | `format`, `arch`                                                |
| `elf`             | `needs`, `symbol-versions`, `runpath`, `min-kernel`             |
| `pe`              | `needs`, `min-os`, `subsystem-version`                          |
| `macho`           | `needs`, `dylib-compat`, `rpath`, `macos-min`, `code-signature` |
| `macho-universal` | `slices`, one full `macho` profile per architecture             |
| `archive`         | nothing further                                                 |

`arch` is a string everywhere except on a universal binary, where it is the
sorted list of architectures the file carries. Such a file satisfies its
triple when the right architecture is among them, and the Mach-O rules below
are then applied to every slice separately.

Dependency names are whatever the container itself stores: a soname for ELF
(`libc.so.6`), an import table entry for PE (`KERNEL32.dll`), an install name
for Mach-O (`/usr/lib/libSystem.B.dylib`). `symbol-versions` is keyed on the
version namespace rather than on the library, because that is the part that
decides compatibility: `GLIBC_2.3.3` means glibc 2.3.3 or newer regardless of
which of libc, libm or libpthread pulled it in.

`dist-check` then asserts three separate things:

1. **The build inputs still match `cmake/targets.json`.** Retargeting a
   platform, or changing its link flags, cannot go unnoticed.
2. **Every file is present and reads back exactly its recorded profile.** Any
   file in the directory that the manifest does not mention is reported too.
3. **Every profile satisfies the invariants implied by its zig triple.** This
   is the part that still catches a bad build after someone has regenerated
   the manifest from it:
   - the architecture has to be the one the triple names, so an `osx-arm64`
     slot cannot quietly hold an x86-64 binary,
   - no ELF may need a glibc above the version pinned in the triple,
   - a Mach-O has to declare exactly the macOS minimum the triple asks for,
   - an arm64 macOS binary has to carry a code signature, because arm64 macOS
     refuses to execute an unsigned one at all,
   - `runpath` / `rpath` have to be empty, since a non-empty one means a build
     directory leaked into the artifact.

Reading those formats is done by `tools/binfmt.py` rather than by
`readelf`/`objdump`/`otool`, because no single one of those reads all three
and a given host generally has at most one of them.

One thing deliberately *not* checked: the CPU baseline the code was generated
for. Nothing in any of these containers records it unless the toolchain emits
a `.note.gnu.property` marker, and ours does not, so an `-march=native` leak
would build cleanly, pass every check here, and only fail on an older CPU at
runtime. That is why `cpu` is pinned in `cmake/targets.json` and passed to zig
explicitly, and why the manifest records it as provenance rather than as
something read back from the files.

`dist` deliberately only handles the release build: a debug binary picks up
extra imports (`ntdll.dll` on Windows, for one), so a single manifest could not
describe both configurations. Use `./dev.py dist --write-manifest` to
regenerate the manifest after an intentional change.

## Dependency cache

The Boost release archive is roughly 100 MB. It is downloaded into
`.cache/deps/download/` and unpacked into `.cache/deps/boost-<version>/` once;
every additional target platform reuses that same checkout instead of
downloading and unpacking its own copy. Delete `.cache/` to force a refetch.

To move to another Boost version, update both `CROSSZIG_BOOST_VERSION` and
`CROSSZIG_BOOST_SHA256` in `cmake/Dependencies.cmake`.
