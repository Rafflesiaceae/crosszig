# crosszig

A small sample C++ project that cross-compiles from any of the pixi platforms
to `linux-32`, `linux-64`, `linux-aarch64`, `win-32-msvc`, `win-64-msvc`,
`osx-64` and `osx-arm64`, using **zig** as the cross toolchain and **CMake** as
the build system. Boost is pulled in with `FetchContent`, so nothing has to be
installed on the machine beyond the pixi environment.

## Quick start

```sh
./dev.py build              # debug build for the host platform
./dev.py build -r -a        # release build for every supported platform
./dev.py build -t win-64-msvc  # debug build for Windows only
./dev.py build -v           # same, with the build output unfiltered
./dev.py run -r             # build for the host and run the binary
./dev.py dist               # collect the release artifacts into ./dist
./dev.py dist-check ./dist  # check a directory against dist-manifest.json
./dev.py clean              # remove build/ and dist/
```

`tools/dist-manifest.py` is a standalone script and works on its own, on any
binary from any project:

```sh
./tools/dist-manifest.py show /usr/bin/ls   # what it depends on, as JSON
./tools/dist-manifest.py diff a.so b.so     # how two of them differ
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
| `tools/xwin.py`           | Fetches and caches Microsoft's CRT and SDK for the Windows targets |
| `cmake/Dependencies.cmake`| Fetches Boost and shares one checkout across all platforms    |
| `dev.py`                  | Thin cross-platform wrapper around the CMake commands         |
| `tools/dist-manifest.py`  | Standalone lock-file tool for binaries: reads ELF, PE and Mach-O |
| `tools/universal.py`      | Standalone `lipo -create`: combines thin macOS files into one |
| `dist-manifest.json`      | The lock file it keeps for this project's distribution        |

`src/` produces two things:

* `crosszig_core` (`libcrosszig.a`) - the library with the actual logic
* `crosszig_cli` (`crosszig` / `crosszig.exe`) - a command line front end

## Using CMake directly

`dev.py` is only a convenience layer; the plain CMake invocation is:

```sh
cmake -S . -B build/win-64-msvc-release -G Ninja \
      --toolchain cmake/ZigToolchain.cmake \
      -DCROSSZIG_TARGET=win-64-msvc \
      -DCMAKE_BUILD_TYPE=Release
cmake --build build/win-64-msvc-release
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

* **`x86_64-linux-gnu.2.3.3`** and **`aarch64-linux-gnu.2.17`** pin glibc. Without the suffix zig would target
  the glibc of whichever machine runs the build, and the result would refuse to
  start on anything older. 2.3.3 is from 2004, so in practice the glibc version
  stops being a constraint at all. Only three symbols ever pushed the floor up:
  `memcpy@2.14`, `__isoc99_sscanf@2.7` and `aligned_alloc@2.16`. The first two
  resolve to older versions on their own once the triple asks for one, and the
  third is covered by `src/compat/glibc_compat.cpp`, because zig's prebuilt
  libc++abi calls `aligned_alloc` no matter which glibc is targeted. aarch64
  stops at 2.17 because that is the release the port was added in, so there is
  nothing older to target.
* **`macos.10.12`** and **`macos.11.0`** pin the deployment target, Sierra
  for Intel and Big Sur for Apple Silicon. zig's own default is considerably
  newer, which would exclude perfectly current machines for no reason. 11.0 is
  the floor for arm64 by definition, since that is where Apple Silicon starts;
  Intel can go lower and 10.9 still links, but zig's bundled `libSystem.tbd`
  carries no per-symbol availability information, so nothing would verify that
  every symbol referenced actually existed that far back. Note the version
  needs a minor component: `x86_64-macos.11-none` is rejected as
  `InvalidOperatingSystemVersion`.
* **`"cpu": "baseline"`** pins the instruction set. zig already defaults to
  this for a cross target, but it is stated explicitly because it is the one
  compatibility property that cannot be verified after the fact.

The two Windows targets additionally carry an `sdk`, a `cxx-driver`, a
`cxx-standard` and a `compile-flags`. See below for why.

### Why Windows goes through the MSVC ABI

The C runtime is what decides how far back a Windows binary reaches, and zig
offers two ways of getting one. Only one of them gets rid of it:

| Toolchain | Imports                        | Needs                             |
| --------- | ------------------------------ | --------------------------------- |
| mingw-w64 | `KERNEL32.dll`, `ucrtbase.dll` | nothing but pixi to build         |
| MSVC ABI  | `KERNEL32.dll`, `USER32.dll`   | a one-off SDK download, see below |

mingw-w64 wraps the UCRT rather than replacing it, so `ucrtbase.dll` cannot be
linked away: it *is* the C runtime there. `libucrt.a` in zig's tree is not a
static one despite the name, it is mingw's supplement of long double and
complex math routines, and `-static` leaves the import table untouched. Asking
for `-lmsvcrt` does not help either; it only stacks `msvcrt.dll` on top of the
`api-ms-win-crt-*` set that zig's startup objects reference anyway. So the
mingw target was dropped, and both Windows targets go through the MSVC ABI.

They link Microsoft's own static CRT and so import no C runtime at all, which
is what makes them runnable on a Windows that has never seen the UCRT update.
The one `USER32.dll` import is `LoadStringA`, from the static CRT's error
message path. `min-os` 6.0 is a real floor rather than a linker default: the
CRT reaches for SRW locks, condition variables, the thread pool API and
`CompareStringEx`, all of which arrived in Vista. The `imports` lists in the
manifest are what makes that checkable rather than merely asserted.

Those libraries only ship inside Visual Studio and the Windows SDK and are not
redistributable, so they have to be fetched once:

```sh
python tools/xwin.py setup --accept-license
```

That downloads [xwin](https://github.com/Jake-Shadle/xwin) at a pinned version,
checks it against the published sha256, and has it pull the CRT and SDK
straight from Microsoft into `.cache/xwin/` (around 150 MB over the wire and
1.1 GB unpacked for the x86 and x86_64 libraries together; the headers are
shared). It is cached, so this is a one-time cost, and
`tools/xwin.py` is standalone: no imports from the rest of the repository.
Both architectures are unpacked in one go, since they share the headers.
Until it has been run, building either Windows target stops with that command
in the error message.

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

`./dev.py dist` gathers the release artifacts of every platform into a flat
`./dist/`, suffixing each file with the platform it was built for:

```
dist/crosszig-linux-32        dist/libcrosszig-linux-32.a
dist/crosszig-linux-64        dist/libcrosszig-linux-64.a
dist/crosszig-linux-aarch64   dist/libcrosszig-linux-aarch64.a
dist/crosszig-win-32-msvc.exe dist/crosszig-win-32-msvc.lib
dist/crosszig-win-64-msvc.exe dist/crosszig-win-64-msvc.lib
dist/crosszig-osx-universal   dist/libcrosszig-osx-universal.a
```

macOS ships as one universal file rather than as two thin ones. The `universal`
section of `cmake/targets.json` says which platform builds get folded together:

```json
"universal": { "osx-universal": ["osx-64", "osx-arm64"] }
```

Those platforms are still built separately, they are just not copied out on
their own. Assembling the container is `tools/universal.py` rather than
`lipo`, because `lipo` only exists on macOS and this has to work on every
host. It handles static libraries too, which Apple wraps in the same format.

It then verifies the result against `dist-manifest.json` and fails if anything
is off. `./dev.py dist-check <dir>` runs the same verification on its own, so a
distribution can also be checked after the fact, for instance once it has been
unpacked somewhere else.

### The manifest tool

`tools/dist-manifest.py` is a standalone script, and it knows nothing about
crosszig. A distribution manifest is a lock file for binaries: it pins, for
every file shipped, the facts that decide where that file will actually run.
Regenerating it turns "the build changed" into a reviewable diff instead of a
bug report from whoever has the oldest machine.

```
tools/dist-manifest.py show   <path>...       what these files depend on
tools/dist-manifest.py record <dir>  -o FILE  lock a directory down
tools/dist-manifest.py check  <dir>  -m FILE  assert it still matches
tools/dist-manifest.py diff   <a> <b>         compare two of anything
```

`diff` takes a manifest, a directory or a single binary on either side, so a
fresh build can be held against a released manifest, or one project's binary
against a completely unrelated one:

```
$ ./tools/dist-manifest.py diff dist/crosszig-linux-64 ~/Downloads/omni-bot/omnibot_et.x86_64.so
interpreter: crosszig-linux-64 has '/lib64/ld-linux-x86-64.so.2', omnibot_et.x86_64.so has None
needs: only omnibot_et.x86_64.so has ['libgcc_s.so.1', 'libstdc++.so.6']
symbol-versions.GLIBC: crosszig-linux-64 has '2.3.3', omnibot_et.x86_64.so has '2.3'
```

A manifest holds an opaque `metadata` block, a list of `rules`, and a profile
of every file:

```json
{
    "schema": "dist-manifest/1",
    "metadata": { "cpu": "baseline", "targets": { "...": "..." } },
    "require-rule-coverage": true,
    "rules": [
        {
            "name": "linux-64",
            "match": ["*-linux-64", "*-linux-64.*"],
            "slice": "x86-64",
            "when":    { "format": "elf" },
            "require": { "symbol-versions.GLIBC": { "at-most": "2.3.3" } }
        }
    ],
    "files": {
        "crosszig-linux-64": {
            "format": "elf",
            "arch": "x86-64",
            "needs": ["libc.so.6", "..."],
            "symbol-versions": { "GLIBC": "2.3.3" },
            "runpath": [],
            "interpreter": "/lib64/ld-linux-x86-64.so.2",
            "min-kernel": "2.0.0"
        }
    }
}
```

Recording the profiles alone would not be enough. A manifest regenerated from
a broken build records the breakage and then happily agrees with itself, so
the rules are the half that does *not* come from the artifacts: they say what
has to be true no matter what was measured. Keys in `when` and `require` are
dotted paths into a profile, and a condition is either a plain value, meaning
equality, or a map of `is`, `is-version`, `one-of`, `at-most`, `at-least`,
`empty`, `present`, `subset-of`, `contains` and `matches`. A key that is
absent fails every condition except `{"empty": true}` and `{"present":
false}`, so a typo in a key name is reported rather than quietly passing;
`when` is the guard that scopes a rule to the files it can apply to.

`slice` narrows a rule to one architecture of a universal file. A thin file
counts as a single slice named after its own architecture, so one rule shape
covers both cases, and that is how the two macOS slices are held to different
floors.

Which profile keys appear depends on the format, because the formats record
genuinely different things:

| Format              | Keys                                                            |
| ------------------- | --------------------------------------------------------------- |
| all                 | `format`, `arch`                                                  |
| `elf`               | `needs`, `symbol-versions`, `runpath`, `min-kernel`, `interpreter`|
| `pe`                | `needs`, `imports`, `min-os`, `subsystem-version`                 |
| `macho`             | `needs`, `dylib-compat`, `rpath`, `macos-min`, `code-signature`   |
| `archive`           | `cxx-runtime`                                                     |
| `macho-universal`   | `slices`, one full `macho` profile per architecture               |
| `archive-universal` | `slices`, one `archive` profile per architecture                  |

`arch` is a string everywhere except on a universal file, where it is the
sorted list of architectures it carries, and `slices` holds one full profile
per architecture.

The formats are read by the script itself rather than by
`readelf`/`objdump`/`otool`, because no single one of those reads all
three and a given host generally has at most one of them.

Dependency names are whatever the container itself stores: a soname for ELF
(`libc.so.6`), an import table entry for PE (`KERNEL32.dll`), an install name
for Mach-O (`/usr/lib/libSystem.B.dylib`). `symbol-versions` is keyed on the
version namespace rather than on the library, because that is the part that
decides compatibility: `GLIBC_2.3.3` means glibc 2.3.3 or newer regardless of
which of libc, libm or libpthread pulled it in.

Three of those keys are there because the obvious reading of a file leaves a
real compatibility question open:

* **`interpreter`** is the dynamic loader from `.interp`, and `null` for a
  static binary. Without it a static image and a dynamic one that happens to
  need nothing are indistinguishable, and the path differs per architecture.
* **`imports`** lists the function names behind each PE dependency, not just
  the DLL. Nothing in a PE records the Windows version it actually requires:
  `min-os` and `subsystem-version` are whatever the linker was told to write,
  so the API names are the only evidence there is. Listing them means a CRT
  update that starts calling a newer API shows up as a manifest difference
  instead of passing silently.
* **`cxx-runtime`** is which C++ standard library an archive expects, worked
  out from the mangling of its undefined symbols. libc++, libstdc++ and the
  MSVC STL are not link compatible, and an archive otherwise says nothing
  about which one it was built against, so a consumer that assumes wrong only
  finds out at link time.

### What this project asks of it

Everything crosszig-specific about `dist-manifest.json` is generated by
`manifest_policy()` in `dev.py` out of `cmake/targets.json`. The metadata is
the build inputs recorded verbatim; the rules are derived from each zig
triple, so retargeting a platform rewrites its rules along with its triple
instead of leaving a stale one behind. `./dev.py dist` then hands that policy
back in when it checks, which is what makes a manifest written before the
targets changed fail rather than enforce yesterday's rules.

`dist-check` then asserts three separate things, and they fail in different
ways on purpose:

1. **The generated policy still matches the one in the manifest.**
   Retargeting a platform, or changing its link flags, cannot go unnoticed.
2. **Every file is present and reads back exactly its recorded profile.** Any
   file in the directory that the manifest does not mention is reported too,
   and so is any file no rule claims, since everything shipped here belongs
   to one of our platforms.
3. **Every rule holds against the profiles read now**, not against the
   recorded ones. This is the part that still catches a bad build after
   someone has regenerated the manifest from it:
   - the architectures have to be exactly the ones the triples name, so an
     `osx-arm64` slot cannot quietly hold an x86-64 binary and a universal
     file cannot quietly lose a slice,
   - no ELF may need a glibc above the version pinned in the triple,
   - a Mach-O has to declare exactly the macOS minimum the triple asks for,
   - an arm64 macOS binary has to carry a code signature, because arm64 macOS
     refuses to execute an unsigned one at all,
   - `runpath` / `rpath` have to be empty, since a non-empty one means a build
     directory leaked into the artifact.

One thing deliberately *not* checked: the CPU baseline the code was generated
for. Nothing in any of these containers records it unless the toolchain emits
a `.note.gnu.property` marker, and ours does not, so an `-march=native` leak
would build cleanly, pass every check here, and only fail on an older CPU at
runtime. That is why `cpu` is pinned in `cmake/targets.json` and passed to zig
explicitly, and why the manifest records it under `metadata` as provenance
rather than as something read back from the files.

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
