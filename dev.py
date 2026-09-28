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
TARGETS = ROOT / "cmake" / "targets.json"

# The one place the platforms are spelled out is cmake/targets.json, which the
# toolchain file reads as well. Must match `platforms` in pixi.toml.
PLATFORMS = sorted(json.loads(TARGETS.read_text())["platforms"])

# What the leading component of a zig triple says the architecture will be,
# named the way tools/binfmt.py reports it.
ARCH_BY_TRIPLE = {"x86_64": "x86-64", "aarch64": "arm64", "x86": "x86", "arm": "arm"}

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
    cpu, triples = toolchain_targets()
    stamp = output / ".crosszig-build-inputs"
    inputs = f"{triples[target]} -mcpu={cpu}\n"

    # The target triple is baked into the generated compiler shims rather than
    # into the compile command lines, so ninja cannot see it change and would
    # report "no work to do" against the old artifacts. Editing targets.json
    # therefore has to discard the build directory.
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
    run(build_command)

    run(["cmake", "--install", output, "--prefix", install_dir(target, config)])


def toolchain_targets():
    """The build inputs the toolchain uses, as recorded in cmake/targets.json."""
    data = json.loads(TARGETS.read_text())
    return data["cpu"], {name: entry["triple"] for name, entry in data["platforms"].items()}


def platform_of(name):
    """Which platform a collected file belongs to, from its name suffix."""
    stem = Path(name).stem
    for candidate in PLATFORMS:
        if stem.endswith(f"-{candidate}"):
            return candidate
    return None


def dist_name(path, target):
    """`libcrosszig.a` for osx-arm64 becomes `libcrosszig-osx-arm64.a`."""
    return f"{path.stem}-{target}{path.suffix}"


def file_profile(path):
    """The recorded description of one collected file."""
    return {"platform": platform_of(path.name), **binfmt.profile(path)}


def build_manifest(directory):
    """Describes `directory` the way dist-manifest.json records it."""
    cpu, triples = toolchain_targets()
    return {
        "cpu": cpu,
        "triples": dict(sorted(triples.items())),
        "files": {
            path.name: file_profile(path)
            for path in sorted(directory.iterdir())
            if path.is_file()
        },
    }


def compare_profile(name, recorded, found):
    """Reports every way the profile read back differs from the recorded one."""
    problems = []
    for key in sorted(set(recorded) | set(found)):
        if key not in recorded:
            problems.append(f"{name}: {key} appeared, now {found[key]!r}")
        elif key not in found:
            problems.append(f"{name}: {key} is gone, manifest has {recorded[key]!r}")
        elif recorded[key] != found[key]:
            problems.append(f"{name}: {key} is {found[key]!r}, manifest says {recorded[key]!r}")
    return problems


def check_invariants(name, found, triple):
    """Checks the profile against the build inputs rather than against the
    manifest, so that a manifest regenerated from a broken build still fails.
    """
    problems = []

    if triple is None:
        return [f"{name}: no known platform in the file name"]

    expected_arch = ARCH_BY_TRIPLE.get(triple.split("-")[0])
    if expected_arch and found.get("arch") != expected_arch:
        problems.append(
            f"{name}: built for {found.get('arch')}, but {triple} targets {expected_arch}")

    # Nothing here should ever need a search path baked into it; one showing up
    # means a build directory leaked into the artifact.
    for key in ("runpath", "rpath"):
        if found.get(key):
            problems.append(f"{name}: {key} is not empty: {found[key]}")

    if found.get("format") == "elf":
        ceiling = re.search(r"-gnu\.([0-9][0-9.]*)$", triple)
        needed = found.get("symbol-versions", {}).get("GLIBC")
        if ceiling and needed and \
                binfmt.version_tuple(needed) > binfmt.version_tuple(ceiling.group(1)):
            problems.append(
                f"{name}: needs glibc {needed}, above the targeted {ceiling.group(1)}")

    if found.get("format") == "macho":
        floor = re.search(r"-macos\.([0-9][0-9.]*)-", triple)
        declared = found.get("macos-min")
        if floor and (declared is None or
                      binfmt.version_tuple(declared) != binfmt.version_tuple(floor.group(1))):
            problems.append(
                f"{name}: declares macOS {declared}, but {triple} targets {floor.group(1)}")

        # arm64 macOS will not execute an unsigned binary at all.
        if found.get("arch", "").startswith("arm64") and not found.get("code-signature"):
            problems.append(f"{name}: arm64 macOS binary carries no code signature")

    return problems


def check_dist(directory):
    """Verifies `directory` against dist-manifest.json. Returns True when clean.

    Three things are checked. The manifest's record of the build inputs has to
    match cmake/targets.json, so that retargeting cannot go unnoticed. Every
    file has to be present and to read back exactly the profile recorded for
    it. And every profile has to satisfy the invariants that follow from the
    zig triple it was built with, which is what still catches a bad build after
    someone has regenerated the manifest from it.
    """
    if not MANIFEST.is_file():
        sys.exit(f"dev.py: no manifest at {MANIFEST}")

    manifest = json.loads(MANIFEST.read_text())
    cpu, triples = toolchain_targets()
    problems = []

    if manifest["cpu"] != cpu:
        problems.append(
            f"manifest builds for cpu {manifest['cpu']}, but {TARGETS.name} says {cpu}")
    for platform_name in sorted(set(manifest["triples"]) | set(triples)):
        recorded = manifest["triples"].get(platform_name)
        actual = triples.get(platform_name)
        if recorded != actual:
            problems.append(f"triple for {platform_name}: manifest has {recorded}, "
                            f"{TARGETS.name} has {actual}")

    for name, recorded in sorted(manifest["files"].items()):
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: missing")
            continue

        try:
            found = file_profile(path)
        except (binfmt.UnknownFormat, OSError, ValueError) as error:
            problems.append(f"{name}: {error}")
            continue

        problems += compare_profile(name, recorded, found)
        problems += check_invariants(name, found, triples.get(found.get("platform")))

    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name not in manifest["files"]:
            problems.append(f"{path.name}: present but not listed in the manifest")

    if problems:
        print(f"\ndist-check FAILED for {directory}", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return False

    print(f"\ndist-check ok: {len(manifest['files'])} files in {directory}")
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
