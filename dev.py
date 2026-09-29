#!/usr/bin/env python3
"""Build driver for crosszig.

Wraps the CMake/zig cross-compilation setup so that any supported host can
produce binaries for any supported target:

    ./dev.py build                 # debug, for the host platform
    ./dev.py build -r -a           # release, for every supported platform
    ./dev.py build -t win-64-msvc  # debug, for Windows only
    ./dev.py build -t              # list the platforms -t accepts
    ./dev.py run -r                # build for the host and run the binary
    ./dev.py dist                  # collect the release build of every platform
    ./dev.py dist --debug          # the same for the debug build, into ./dist-debug
    ./dev.py dist-check ./dist     # check a directory against dist-manifest.json
    ./dev.py clean
"""

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD_ROOT = ROOT / "build"

# Each configuration is collected into its own directory, but both are held to
# the same manifest: the debug build of every platform depends on exactly what
# the release build depends on, down to the last imported symbol. One lock file
# is therefore enough, and it makes the stronger statement of the two - should
# a configuration ever start depending on something the other does not, the
# check fails instead of the difference being written off as expected.
DIST_DIRS = {
    "Release": ROOT / "dist",
    "Debug": ROOT / "dist-debug",
}
MANIFEST = ROOT / "dist-manifest.json"
TOOLCHAIN = ROOT / "cmake" / "ZigToolchain.cmake"
TARGETS = ROOT / "cmake" / "targets.json"


def load_tool(file_name):
    """Imports a script out of tools/ whose name is not a module name.

    The tools there are deliberately standalone: each one is a single file
    that runs on its own and can be copied into another project unchanged,
    which rules out the hyphen-free names a plain import would need. That
    costs this helper and nothing else.
    """
    path = ROOT / "tools" / file_name
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Both are general tools that know nothing about this project: one reads
# binaries and locks down what they depend on, the other writes the universal
# macOS container.
dist_manifest = load_tool("dist-manifest.py")
universal = load_tool("macos-universal.py")
# Not general at all, but standalone for the same reason: it is driven both
# from here and straight from the toolchain file, which cannot import anything.
xwin = load_tool("xwin.py")

# The one place the platforms are spelled out is cmake/targets.json, which the
# toolchain file reads as well.
PLATFORMS = sorted(json.loads(TARGETS.read_text())["platforms"])

# Platforms whose builds are not shipped on their own but folded into a
# universal artifact, and the names those artifacts carry.
UNIVERSAL = json.loads(TARGETS.read_text()).get("universal", {})
GROUPED = {member for members in UNIVERSAL.values() for member in members}
ARTIFACTS = sorted((set(PLATFORMS) - GROUPED) | set(UNIVERSAL))

# Set once we have re-executed ourselves inside the pixi environment, so that
# the second run never tries to do it again.
PIXI_GUARD = "CROSSZIG_INSIDE_PIXI"

REQUIRED_TOOLS = ["cmake", "ninja", "zig"]

# The commands that shell out to the toolchain; the rest only touch files.
TOOLCHAIN_COMMANDS = {"build", "run"}

# What `-t` stands for when it is given no value, which asks for the list of
# platforms rather than naming one. argparse cannot offer `choices` and accept
# a valueless flag at the same time - the sentinel would have to be a choice
# too, and would then show up in the usage line - so the names given to `-t`
# are checked by hand instead.
LIST_PLATFORMS = object()


def host_platform():
    """The target platform matching the machine this script is running on."""
    system = platform.system()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")

    if system == "Linux":
        if arm:
            return "linux-aarch64"
        return "linux-32" if machine in ("i386", "i486", "i586", "i686") else "linux-64"
    if system == "Windows":
        # A 32 bit Python can be running on a 64 bit Windows, so this follows
        # the reported machine rather than the interpreter.
        return "win-32-msvc" if machine in ("x86", "i386", "i686") else "win-64-msvc"
    if system == "Darwin":
        return "osx-arm64" if arm else "osx-64"
    sys.exit(f"dev.py: unsupported host system '{system}'")


def reexec_in_pixi():
    """Re-run this script inside `pixi run` when the toolchain is not on PATH.

    That way `./dev.py ...` works the same whether or not the caller already
    activated the environment.
    """
    if os.environ.get(PIXI_GUARD):
        missing = [tool for tool in REQUIRED_TOOLS if not shutil.which(tool)]
        if missing:
            sys.exit(f"dev.py: missing in the pixi environment: {', '.join(missing)}")
        return

    if all(shutil.which(tool) for tool in REQUIRED_TOOLS):
        return

    pixi = shutil.which("pixi")
    if pixi is None:
        sys.exit(
            "dev.py: neither the required tools ({}) nor pixi were found on PATH".format(
                ", ".join(REQUIRED_TOOLS)
            )
        )

    env = dict(os.environ, **{PIXI_GUARD: "1"})
    command = [pixi, "run", "--manifest-path", str(ROOT / "pixi.toml"),
               "python", str(ROOT / "dev.py"), *sys.argv[1:]]
    sys.exit(subprocess.call(command, env=env))


# Microsoft's prebuilt CRT objects name PDB files that exist only on their
# build machines, and the several gigabytes of debug symbols that would
# satisfy them are deliberately not downloaded. lld says so once per object,
# which is around a hundred lines on every win-64-msvc link. It cannot be
# turned off at the source - zig silently drops unrecognised -Xlinker flags,
# so /IGNORE:4099 never reaches the linker - so it is dropped here instead,
# counted rather than hidden, and `--verbose` brings it all back.
LINKER_NOISE = re.compile(
    r"^(lld-link: warning: Cannot use debug info for .*\[LNK4099\]"
    r"|>>> failed to load reference .*\.pdb.*)$")


def run(command, filter_noise=False):
    text = [str(part) for part in command]
    print("+ " + " ".join(text), flush=True)

    if not filter_noise:
        subprocess.run(text, check=True)
        return

    process = subprocess.Popen(text, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    suppressed = 0
    dropping = False
    for line in process.stdout:
        line = line.rstrip("\n")
        # Each warning comes as a pair of lines followed by a blank one.
        if LINKER_NOISE.match(line) or (dropping and not line.strip()):
            suppressed += 1
            dropping = True
            continue
        dropping = False
        print(line, flush=True)

    if process.wait() != 0:
        raise subprocess.CalledProcessError(process.returncode, text)
    if suppressed:
        print(f"  ({suppressed} lines of LNK4099 debug-info noise from Microsoft's "
              f"prebuilt libraries hidden, --verbose shows them)", flush=True)


def config_name(release):
    return "Release" if release else "Debug"


def build_dir(target, config):
    return BUILD_ROOT / f"{target}-{config.lower()}"


def install_dir(target, config):
    """Where a single platform build is installed, before it is collected."""
    return build_dir(target, config) / "install"


def ensure_sdk(targets, accept_license=False):
    """Fetches whatever SDK the chosen platforms need and the cache lacks.

    Only the MSVC targets need one. `xwin.is_ready()` is what keeps this a
    no-op on every build after the first, so it can be called unconditionally
    the way the cmake invocation below is. Doing it here rather than leaving it
    to the toolchain file also means it happens once per `build`, before
    anything is configured, instead of once per platform after six of them
    have already been built.
    """
    _cpu, platforms = toolchain_targets()
    wanted = {platforms[target].get("sdk") for target in targets} - {None}

    unsupported = sorted(wanted - {"xwin"})
    if unsupported:
        sys.exit(f"dev.py: cmake/targets.json asks for the "
                 f"'{unsupported[0]}' SDK, which dev.py cannot set up")

    if not wanted or xwin.is_ready():
        return

    # One setup unpacks every architecture xwin.py knows about, so which of
    # the MSVC targets were asked for only decides whether to run it at all.
    #
    # The flag is forwarded rather than always passed: the download is
    # Microsoft's non-redistributable CRT and SDK, and agreeing to their terms
    # is not a thing this script gets to do on the caller's behalf. Without it
    # xwin.py prints the licence and the URL and stops, which is the one-time
    # cost of a first Windows build.
    command = [sys.executable, ROOT / "tools" / "xwin.py", "setup"]
    if accept_license:
        command.append(xwin.ACCEPT_LICENSE_FLAG)
    run(command)


def build_one(target, config, jobs=None, clean=False, verbose=False):
    """Configure, build and install a single target platform."""
    output = build_dir(target, config)
    cpu, platforms = toolchain_targets()
    stamp = output / ".crosszig-build-inputs"
    inputs = json.dumps({"cpu": cpu, **platforms[target]}, sort_keys=True) + "\n"

    # The target triple and the link flags are baked into the generated
    # compiler shims and into the cache rather than into the compile command
    # lines, so ninja cannot see them change and would report "no work to do"
    # against the old artifacts. Editing targets.json therefore has to discard
    # the build directory.
    if stamp.is_file() and stamp.read_text() != inputs:
        print(f"build inputs changed, discarding {output}", flush=True)
        clean = True

    if clean:
        shutil.rmtree(output, ignore_errors=True)

    output.mkdir(parents=True, exist_ok=True)
    stamp.write_text(inputs)

    # CMake is cheap to re-run and skips the work when nothing changed, so this
    # is called unconditionally instead of checking for an existing cache.
    run([
        "cmake",
        "-S", ROOT,
        "-B", output,
        "-G", "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={TOOLCHAIN}",
        f"-DCROSSZIG_TARGET={target}",
        f"-DCMAKE_BUILD_TYPE={config}",
    ])

    build_command = ["cmake", "--build", output]
    if jobs:
        build_command += ["--parallel", str(jobs)]
    run(build_command, filter_noise=not verbose)

    run(["cmake", "--install", output, "--prefix", install_dir(target, config)])


def toolchain_targets():
    """The build inputs the toolchain uses, as recorded in cmake/targets.json.

    The whole platform entry is carried around rather than just the triple, so
    that anything added there later lands in the manifest without this having
    to learn about it.
    """
    data = json.loads(TARGETS.read_text())
    return data["cpu"], data["platforms"]


def dist_name(path, target):
    """`libcrosszig.a` for osx-arm64 becomes `libcrosszig-osx-arm64.a`."""
    return f"{path.stem}-{target}{path.suffix}"


def check_dist(directory):
    """Verifies `directory` against dist-manifest.json. True when it is clean.

    The manifest is a lock file and nothing more: every file it lists has to
    be present and to read back the facts recorded for it, and nothing may be
    present that it does not list. Whether a difference is acceptable is a
    question for whoever reads the diff, not for this script.
    """
    if not MANIFEST.is_file():
        sys.exit(f"dev.py: no manifest at {MANIFEST}")

    manifest = json.loads(MANIFEST.read_text())
    problems = dist_manifest.check(directory, manifest)

    if problems:
        print(f"\ndist-check FAILED for {directory}", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return False

    print(f"\ndist-check ok: {len(manifest['files'])} files in {directory}")
    return True


def print_platforms():
    """Prints every platform `-t` accepts, and what each one builds for."""
    _cpu, platforms = toolchain_targets()
    host = host_platform()

    # The platforms that are not shipped on their own, and the artifact each
    # one is folded into, so that the list explains why `dist` produces fewer
    # names than there are platforms here.
    folded_into = {member: name
                   for name, members in UNIVERSAL.items() for member in members}

    name_width = max(len(name) for name in PLATFORMS)
    triple_width = max(len(platforms[name]["triple"]) for name in PLATFORMS)

    print("supported platforms:")
    for name in PLATFORMS:
        notes = []
        if name == host:
            notes.append("host")
        if name in folded_into:
            notes.append(f"shipped as {folded_into[name]}")
        line = (f"  {name:<{name_width}}  {platforms[name]['triple']:<{triple_width}}"
                f"  {', '.join(notes)}")
        print(line.rstrip())


def command_build(args):
    if args.all:
        targets = list(PLATFORMS)
    elif args.target:
        unknown = [name for name in args.target if name not in PLATFORMS]
        if unknown:
            sys.exit("dev.py: unknown platform {}; `./dev.py build -t` "
                     "lists the supported ones".format(", ".join(unknown)))
        targets = args.target
    else:
        targets = [host_platform()]

    config = config_name(args.release)

    ensure_sdk(targets, args.accept_license)

    for target in targets:
        print(f"\n== {target} ({config}) ==", flush=True)
        build_one(target, config, jobs=args.jobs, clean=args.clean,
                  verbose=args.verbose)


def command_run(args):
    target = host_platform()
    config = config_name(args.release)
    ensure_sdk([target], args.accept_license)
    build_one(target, config, jobs=args.jobs, verbose=args.verbose)

    _cpu, platforms = toolchain_targets()
    suffix = ".exe" if platforms[target]["system"] == "Windows" else ""
    run([install_dir(target, config) / "bin" / f"crosszig{suffix}", *args.args])


def installed_files(target, config):
    """The artifacts one platform build installed, in a stable order."""
    found = []
    for subdirectory in ("bin", "lib"):
        source_dir = install_dir(target, config) / subdirectory
        if source_dir.is_dir():
            found += [path for path in sorted(source_dir.iterdir()) if path.is_file()]
    return found


def collect_universal(name, members, config, dist_dir):
    """Folds the matching artifact of every member platform into one fat file.

    Members are paired up by file name, which is what the install layout gives
    us: every platform installs the same `crosszig` and `libcrosszig.a`.
    """
    by_name = {}
    for member in members:
        for source in installed_files(member, config):
            by_name.setdefault(source.name, []).append(source)

    for file_name, sources in sorted(by_name.items()):
        if len(sources) != len(members):
            sys.exit(f"dev.py: {file_name} was built for only "
                     f"{len(sources)} of the {len(members)} {name} platforms")

        destination = dist_dir / dist_name(Path(file_name), name)
        universal.write(sources, destination)
        # The writer only writes bytes; an executable has to stay one.
        shutil.copymode(sources[0], destination)
        print(f"  {destination.relative_to(ROOT)}", flush=True)


def command_dist(args):
    config = config_name(not args.debug)
    dist_dir = DIST_DIRS[config]

    missing = [target for target in PLATFORMS if not install_dir(target, config).is_dir()]
    if missing:
        sys.exit("dev.py: nothing built for {}; run `./dev.py build {}-a` first".format(
            ", ".join(missing), "" if args.debug else "-r "))

    # Rebuilt from scratch every time, so that artifacts of a platform that was
    # dropped in the meantime cannot linger.
    shutil.rmtree(dist_dir, ignore_errors=True)
    dist_dir.mkdir(parents=True)

    for target in sorted(set(PLATFORMS) - GROUPED):
        for source in installed_files(target, config):
            destination = dist_dir / dist_name(source, target)
            shutil.copy2(source, destination)
            print(f"  {destination.relative_to(ROOT)}", flush=True)

    for name, members in sorted(UNIVERSAL.items()):
        collect_universal(name, members, config, dist_dir)

    if args.write_manifest:
        MANIFEST.write_text(json.dumps(
            dist_manifest.record(dist_dir), indent=4) + "\n")
        print(f"\nwrote {MANIFEST.relative_to(ROOT)}", flush=True)

    if not check_dist(dist_dir):
        sys.exit(1)


def command_dist_check(args):
    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        sys.exit(f"dev.py: {directory} is not a directory")
    if not check_dist(directory):
        sys.exit(1)


def command_clean(_args):
    for directory in (BUILD_ROOT, *DIST_DIRS.values()):
        print(f"removing {directory}", flush=True)
        shutil.rmtree(directory, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_build_flags(subparser):
        subparser.add_argument("-r", "--release", action="store_true",
                               help="use the release configuration (default: debug)")
        subparser.add_argument("-v", "--verbose", action="store_true",
                               help="show the build output unfiltered")
        subparser.add_argument(xwin.ACCEPT_LICENSE_FLAG, dest="accept_license",
                               action="store_true",
                               help="accept Microsoft's licence for the CRT and SDK, "
                                    "which the Windows targets need fetched once")

    build = subparsers.add_parser("build", help="cross compile the project")
    add_build_flags(build)
    build.add_argument("-j", "--jobs", type=int, help="number of parallel compile jobs")
    build.add_argument("-a", "--all", action="store_true",
                       help="build for every supported platform")
    build.add_argument("-t", "--target", action="append", nargs="?",
                       const=LIST_PLATFORMS, metavar="PLATFORM",
                       help="build for this platform (repeatable, default: the "
                            "host); with no value, list the platforms instead")
    build.add_argument("--clean", action="store_true",
                       help="discard the existing build directory first")
    build.set_defaults(func=command_build)

    run_parser = subparsers.add_parser("run", help="build for the host and run the binary")
    add_build_flags(run_parser)
    run_parser.add_argument("-j", "--jobs", type=int, help="number of parallel compile jobs")
    run_parser.add_argument("args", nargs=argparse.REMAINDER,
                            help="arguments passed on to the binary")
    run_parser.set_defaults(func=command_run)

    dist = subparsers.add_parser(
        "dist", help="collect the release artifacts of every platform into ./dist "
                     "and check them")
    dist.add_argument("--debug", action="store_true",
                      help="collect the debug build into ./dist-debug instead; "
                           "it is checked against the same manifest")
    dist.add_argument("--write-manifest", action="store_true",
                      help="regenerate dist-manifest.json from what was collected, "
                           "instead of keeping the current one")
    dist.set_defaults(func=command_dist)

    dist_check = subparsers.add_parser(
        "dist-check", help="check a directory against dist-manifest.json")
    dist_check.add_argument("directory", help="directory holding the distribution")
    dist_check.set_defaults(func=command_dist_check)

    clean = subparsers.add_parser("clean", help="remove the build and dist directories")
    clean.set_defaults(func=command_clean)

    args = parser.parse_args()

    # Listing the platforms reads one JSON file and needs neither the toolchain
    # nor pixi, so it is answered before the re-exec rather than after it.
    if args.command == "build" and args.target and LIST_PLATFORMS in args.target:
        print_platforms()
        return

    if args.command in TOOLCHAIN_COMMANDS:
        reexec_in_pixi()

    try:
        args.func(args)
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)


if __name__ == "__main__":
    main()
