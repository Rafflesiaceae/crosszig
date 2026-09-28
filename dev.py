#!/usr/bin/env python3
"""Build driver for crosszig.

Wraps the CMake/zig cross-compilation setup so that any supported host can
produce binaries for any supported target:

    ./dev.py build            # debug, for the host platform
    ./dev.py build -r -a      # release, for every supported platform
    ./dev.py build -t win-64  # debug, for Windows only
    ./dev.py run -r           # build for the host and run the binary
    ./dev.py clean
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD_ROOT = ROOT / "build"
DIST_ROOT = ROOT / "dist"
TOOLCHAIN = ROOT / "cmake" / "ZigToolchain.cmake"

# Must stay in sync with `platforms` in pixi.toml and with ZigToolchain.cmake.
PLATFORMS = ["linux-64", "win-64", "osx-64", "osx-arm64"]

# Set once we have re-executed ourselves inside the pixi environment, so that
# the second run never tries to do it again.
PIXI_GUARD = "CROSSZIG_INSIDE_PIXI"

REQUIRED_TOOLS = ["cmake", "ninja", "zig"]


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


def build_one(target, config, jobs=None, clean=False):
    """Configure, build and install a single target platform."""
    build_dir = BUILD_ROOT / f"{target}-{config.lower()}"
    install_dir = DIST_ROOT / f"{target}-{config.lower()}"

    if clean:
        shutil.rmtree(build_dir, ignore_errors=True)
        shutil.rmtree(install_dir, ignore_errors=True)

    # CMake is cheap to re-run and skips the work when nothing changed, so this
    # is called unconditionally instead of checking for an existing cache.
    run([
        "cmake",
        "-S", ROOT,
        "-B", build_dir,
        "-G", "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={TOOLCHAIN}",
        f"-DCROSSZIG_TARGET={target}",
        f"-DCMAKE_BUILD_TYPE={config}",
    ])

    build_command = ["cmake", "--build", build_dir]
    if jobs:
        build_command += ["--parallel", str(jobs)]
    run(build_command)

    run(["cmake", "--install", build_dir, "--prefix", install_dir])


def command_build(args):
    if args.all:
        targets = list(PLATFORMS)
    elif args.target:
        targets = args.target
    else:
        targets = [host_platform()]

    config = "Release" if args.release else "Debug"

    for target in targets:
        print(f"\n== {target} ({config}) ==", flush=True)
        build_one(target, config, jobs=args.jobs, clean=args.clean)

    print(f"\nartifacts in {DIST_ROOT}", flush=True)


def command_run(args):
    target = host_platform()
    config = "Release" if args.release else "Debug"
    build_one(target, config, jobs=args.jobs)

    suffix = ".exe" if target == "win-64" else ""
    binary = DIST_ROOT / f"{target}-{config.lower()}" / "bin" / f"crosszig{suffix}"
    run([binary, *args.args])


def command_clean(_args):
    for directory in (BUILD_ROOT, DIST_ROOT):
        print(f"removing {directory}", flush=True)
        shutil.rmtree(directory, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser):
        subparser.add_argument("-r", "--release", action="store_true",
                               help="build with optimisations (default: debug)")
        subparser.add_argument("-j", "--jobs", type=int,
                               help="number of parallel compile jobs")

    build = subparsers.add_parser("build", help="cross compile the project")
    add_common(build)
    build.add_argument("-a", "--all", action="store_true",
                       help="build for every supported platform")
    build.add_argument("-t", "--target", action="append", choices=PLATFORMS,
                       help="build for this platform (repeatable, default: the host)")
    build.add_argument("--clean", action="store_true",
                       help="discard the existing build directory first")
    build.set_defaults(func=command_build)

    run_parser = subparsers.add_parser("run", help="build for the host and run the binary")
    add_common(run_parser)
    run_parser.add_argument("args", nargs=argparse.REMAINDER,
                            help="arguments passed on to the binary")
    run_parser.set_defaults(func=command_run)

    clean = subparsers.add_parser("clean", help="remove build and dist directories")
    clean.set_defaults(func=command_clean)

    args = parser.parse_args()

    if args.command != "clean":
        reexec_in_pixi()

    try:
        args.func(args)
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)


if __name__ == "__main__":
    main()
