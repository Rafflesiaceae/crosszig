#!/usr/bin/env python3
"""Build driver for crosszig.

Wraps the CMake/zig cross-compilation setup so that any supported host can
produce binaries for any supported target:

    ./dev.py build                 # debug, for the host platform
    ./dev.py build -r -a           # release, for every supported platform
    ./dev.py build -t win-64       # debug, for Windows only
    ./dev.py run -r                # build for the host and run the binary
    ./dev.py dist                  # collect the release build of every platform
    ./dev.py dist-check ./dist     # check a directory against dist-manifest.json
    ./dev.py clean
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from tools import binfmt

ROOT = Path(__file__).resolve().parent
BUILD_ROOT = ROOT / "build"
DIST_DIR = ROOT / "dist"
MANIFEST = ROOT / "dist-manifest.json"
TOOLCHAIN = ROOT / "cmake" / "ZigToolchain.cmake"

# Must stay in sync with `platforms` in pixi.toml and with ZigToolchain.cmake.
PLATFORMS = ["linux-64", "win-64", "osx-64", "osx-arm64"]

# Set once we have re-executed ourselves inside the pixi environment, so that
# the second run never tries to do it again.
PIXI_GUARD = "CROSSZIG_INSIDE_PIXI"

REQUIRED_TOOLS = ["cmake", "ninja", "zig"]

# The commands that shell out to the toolchain; the rest only touch files.
TOOLCHAIN_COMMANDS = {"build", "run"}


def host_platform():
    """The pixi platform name this script is currently running on."""
    system = platform.system()
    if system == "Linux":
        return "linux-64"
    if system == "Windows":
        return "win-64"
    if system == "Darwin":
        return "osx-arm64" if platform.machine().lower() in ("arm64", "aarch64") else "osx-64"
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


def run(command, **kwargs):
    print("+ " + " ".join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def config_name(release):
    return "Release" if release else "Debug"


def build_dir(target, config):
    return BUILD_ROOT / f"{target}-{config.lower()}"


def install_dir(target, config):
    """Where a single platform build is installed, before it is collected."""
    return build_dir(target, config) / "install"


def build_one(target, config, jobs=None, clean=False):
    """Configure, build and install a single target platform."""
    output = build_dir(target, config)

    if clean:
        shutil.rmtree(output, ignore_errors=True)

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
    run(build_command)

    run(["cmake", "--install", output, "--prefix", install_dir(target, config)])


def targeted_glibc():
    """The glibc version the linux-64 target is pinned to in the toolchain file.

    Read back from ZigToolchain.cmake rather than duplicated here, so that the
    manifest cannot quietly disagree with what is actually being built.
    """
    match = re.search(r"x86_64-linux-gnu\.([0-9][0-9.]*)", TOOLCHAIN.read_text())
    if not match:
        sys.exit(f"dev.py: no pinned glibc version found in {TOOLCHAIN}")
    return match.group(1)


def dist_name(path, target):
    """`libcrosszig.a` for osx-arm64 becomes `libcrosszig-osx-arm64.a`."""
    return f"{path.stem}-{target}{path.suffix}"


def build_manifest(directory):
    """Describes `directory` the way dist-manifest.json records it."""
    return {
        "glibc": targeted_glibc(),
        "files": {
            path.name: binfmt.dynamic_dependencies(path)
            for path in sorted(directory.iterdir())
            if path.is_file()
        },
    }


def check_dist(directory):
    """Verifies `directory` against dist-manifest.json. Returns True when clean.

    The manifest defines the complete expected content: every file it lists has
    to be there, has to need every library it lists, and must not need any
    other. Files in the directory that the manifest does not mention are
    reported too, so that a stale artifact cannot slip through unnoticed.

    On top of that, no ELF file may need a glibc newer than the recorded one,
    which is what keeps the binaries loadable on older distributions.
    """
    if not MANIFEST.is_file():
        sys.exit(f"dev.py: no manifest at {MANIFEST}")

    manifest = json.loads(MANIFEST.read_text())
    expected_glibc = manifest["glibc"]
    problems = []

    # A changed pin in the toolchain file has to be acknowledged by
    # regenerating the manifest, otherwise the ceiling checked below would
    # silently stop matching what is being built.
    pinned_glibc = targeted_glibc()
    if expected_glibc != pinned_glibc:
        problems.append(
            f"manifest records glibc {expected_glibc}, but {TOOLCHAIN.name} "
            f"pins {pinned_glibc}")

    for name, expected in sorted(manifest["files"].items()):
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: missing")
            continue

        try:
            actual = binfmt.dynamic_dependencies(path)
            needed_glibc = binfmt.glibc_requirement(path)
        except (binfmt.UnknownFormat, OSError) as error:
            problems.append(f"{name}: {error}")
            continue

        for library in sorted(set(expected) - set(actual)):
            problems.append(f"{name}: expected dependency is not linked: {library}")
        for library in sorted(set(actual) - set(expected)):
            problems.append(f"{name}: unexpected dependency: {library}")

        if needed_glibc is not None and \
                binfmt.version_tuple(needed_glibc) > binfmt.version_tuple(expected_glibc):
            problems.append(
                f"{name}: needs glibc {needed_glibc}, above the targeted {expected_glibc}")

    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name not in manifest["files"]:
            problems.append(f"{path.name}: present but not listed in the manifest")

    if problems:
        print(f"\ndist-check FAILED for {directory}", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return False

    print(f"\ndist-check ok: {len(manifest['files'])} files in {directory}, "
          f"glibc <= {expected_glibc}")
    return True


def command_build(args):
    if args.all:
        targets = list(PLATFORMS)
    elif args.target:
        targets = args.target
    else:
        targets = [host_platform()]

    config = config_name(args.release)

    for target in targets:
        print(f"\n== {target} ({config}) ==", flush=True)
        build_one(target, config, jobs=args.jobs, clean=args.clean)


def command_run(args):
    target = host_platform()
    config = config_name(args.release)
    build_one(target, config, jobs=args.jobs)

    suffix = ".exe" if target == "win-64" else ""
    run([install_dir(target, config) / "bin" / f"crosszig{suffix}", *args.args])


def command_dist(args):
    # Always the release build: a debug binary pulls in extra imports (the
    # Windows one picks up ntdll.dll, for instance), so a manifest could not
    # describe both configurations at once.
    config = "Release"

    missing = [target for target in PLATFORMS if not install_dir(target, config).is_dir()]
    if missing:
        sys.exit("dev.py: nothing built for {}; run `./dev.py build -r -a` first".format(
            ", ".join(missing)))

    # Rebuilt from scratch every time, so that artifacts of a platform that was
    # dropped in the meantime cannot linger.
    shutil.rmtree(DIST_DIR, ignore_errors=True)
    DIST_DIR.mkdir(parents=True)

    for target in PLATFORMS:
        for subdirectory in ("bin", "lib"):
            source_dir = install_dir(target, config) / subdirectory
            if not source_dir.is_dir():
                continue
            for source in sorted(source_dir.iterdir()):
                if not source.is_file():
                    continue
                destination = DIST_DIR / dist_name(source, target)
                shutil.copy2(source, destination)
                print(f"  {destination.relative_to(ROOT)}", flush=True)

    if args.write_manifest:
        MANIFEST.write_text(json.dumps(build_manifest(DIST_DIR), indent=4) + "\n")
        print(f"\nwrote {MANIFEST.relative_to(ROOT)}", flush=True)

    if not check_dist(DIST_DIR):
        sys.exit(1)


def command_dist_check(args):
    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        sys.exit(f"dev.py: {directory} is not a directory")
    if not check_dist(directory):
        sys.exit(1)


def command_clean(_args):
    for directory in (BUILD_ROOT, DIST_DIR):
        print(f"removing {directory}", flush=True)
        shutil.rmtree(directory, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_release_flag(subparser):
        subparser.add_argument("-r", "--release", action="store_true",
                               help="use the release configuration (default: debug)")

    build = subparsers.add_parser("build", help="cross compile the project")
    add_release_flag(build)
    build.add_argument("-j", "--jobs", type=int, help="number of parallel compile jobs")
    build.add_argument("-a", "--all", action="store_true",
                       help="build for every supported platform")
    build.add_argument("-t", "--target", action="append", choices=PLATFORMS,
                       help="build for this platform (repeatable, default: the host)")
    build.add_argument("--clean", action="store_true",
                       help="discard the existing build directory first")
    build.set_defaults(func=command_build)

    run_parser = subparsers.add_parser("run", help="build for the host and run the binary")
    add_release_flag(run_parser)
    run_parser.add_argument("-j", "--jobs", type=int, help="number of parallel compile jobs")
    run_parser.add_argument("args", nargs=argparse.REMAINDER,
                            help="arguments passed on to the binary")
    run_parser.set_defaults(func=command_run)

    dist = subparsers.add_parser(
        "dist", help="collect the release artifacts of every platform into ./dist "
                     "and check them")
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

    if args.command in TOOLCHAIN_COMMANDS:
        reexec_in_pixi()

    try:
        args.func(args)
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)


if __name__ == "__main__":
    main()
