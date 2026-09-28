#!/usr/bin/env python3
"""Build driver for crosszig.

Wraps the CMake/zig cross-compilation setup so that any supported host can
produce binaries for any supported target:

    ./dev.py build                 # debug, for the host platform
    ./dev.py build -r -a           # release, for every supported platform
    ./dev.py build -t win-64-msvc  # debug, for Windows only
    ./dev.py run -r                # build for the host and run the binary
    ./dev.py dist                  # collect the release build of every platform
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
DIST_DIR = ROOT / "dist"
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
universal = load_tool("universal.py")

# The one place the platforms are spelled out is cmake/targets.json, which the
# toolchain file reads as well.
PLATFORMS = sorted(json.loads(TARGETS.read_text())["platforms"])

# Platforms whose builds are not shipped on their own but folded into a
# universal artifact, and the names those artifacts carry.
UNIVERSAL = json.loads(TARGETS.read_text()).get("universal", {})
GROUPED = {member for members in UNIVERSAL.values() for member in members}
ARTIFACTS = sorted((set(PLATFORMS) - GROUPED) | set(UNIVERSAL))

# What the leading component of a zig triple says the architecture will be,
# named the way tools/dist-manifest.py reports it.
ARCH_BY_TRIPLE = {"x86_64": "x86-64", "aarch64": "arm64", "x86": "x86", "arm": "arm"}

# Set once we have re-executed ourselves inside the pixi environment, so that
# the second run never tries to do it again.
PIXI_GUARD = "CROSSZIG_INSIDE_PIXI"

REQUIRED_TOOLS = ["cmake", "ninja", "zig"]

# The commands that shell out to the toolchain; the rest only touch files.
TOOLCHAIN_COMMANDS = {"build", "run"}


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


def arch_of_triple(triple):
    """What tools/dist-manifest.py calls the architecture a triple names."""
    leading = triple.split("-")[0]
    return ARCH_BY_TRIPLE.get(leading, leading)


def member_rules(member, triple, match):
    """The manifest rules for the image one zig triple produced.

    They are written against a named slice, which for a file that is not
    universal is the file itself, so that both cases come out the same shape.
    """
    common = {"name": member, "match": match, "slice": arch_of_triple(triple)}

    # A search path baked into a shipped artifact means a build directory
    # leaked into it. An archive records none at all, which `empty` allows.
    rules = [{**common, "require": {"runpath": {"empty": True},
                                    "rpath": {"empty": True}}}]

    glibc = re.search(r"-gnu\.([0-9][0-9.]*)$", triple)
    if glibc:
        rules.append({
            **common,
            "when": {"format": "elf"},
            "require": {"symbol-versions.GLIBC": {"at-most": glibc.group(1)}},
        })

    macos = re.search(r"-macos\.([0-9][0-9.]*)-", triple)
    if macos:
        # Exactly the targeted floor, not merely at or above it: a higher one
        # is a build that quietly stopped supporting the machines we claim.
        require = {"macos-min": {"is-version": macos.group(1)}}
        # arm64 macOS will not execute an unsigned binary at all.
        if common["slice"] == "arm64":
            require["code-signature"] = True
        rules.append({**common, "when": {"format": "macho"}, "require": require})

    return rules


def artifact_rules(artifact, platforms):
    """The manifest rules for one shipped file name suffix.

    `crosszig-linux-64` and `libcrosszig-linux-64.a` both belong to linux-64,
    and nothing else collected into dist/ ends that way.
    """
    members = UNIVERSAL.get(artifact, [artifact])
    triples = [platforms[member]["triple"] for member in members]
    match = [f"*-{artifact}", f"*-{artifact}.*"]
    arches = sorted(arch_of_triple(triple) for triple in triples)

    # The architectures have to be exactly these rather than merely include
    # them, so that a lost slice and a stray one both show up.
    rules = [{
        "name": artifact,
        "match": match,
        "require": {"arch": arches[0] if len(arches) == 1 else arches},
    }]

    for member, triple in zip(members, triples):
        rules += member_rules(member, triple, match)
    return rules


def manifest_policy():
    """What tools/dist-manifest.py enforces on our dist/, generated from
    cmake/targets.json.

    This function is the whole of what is crosszig-specific about
    dist-manifest.json; the tool that reads and writes it knows nothing about
    this project. The metadata half is the build inputs recorded verbatim, so
    that retargeting a platform and leaving the manifest alone is caught. The
    rules half is what has to hold whatever the artifacts happen to say, so
    that regenerating the manifest from a broken build is caught too.
    """
    cpu, platforms = toolchain_targets()

    rules = []
    for artifact in ARTIFACTS:
        rules += artifact_rules(artifact, platforms)

    return {
        "metadata": {
            "cpu": cpu,
            "targets": dict(sorted(platforms.items())),
            "universal": {name: sorted(members)
                          for name, members in sorted(UNIVERSAL.items())},
        },
        # Every file in dist/ is built for one of our platforms, so one that
        # no rule claims is a file nobody meant to ship.
        "require-rule-coverage": True,
        "rules": rules,
    }


def check_dist(directory):
    """Verifies `directory` against dist-manifest.json. True when it is clean.

    Passing the generated policy in as well means the manifest's copy of it
    is compared against the one cmake/targets.json implies right now, so a
    manifest written before the targets changed fails rather than enforcing
    yesterday's rules.
    """
    if not MANIFEST.is_file():
        sys.exit(f"dev.py: no manifest at {MANIFEST}")

    manifest = json.loads(MANIFEST.read_text())
    problems = dist_manifest.check(directory, manifest, policy=manifest_policy())

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
        build_one(target, config, jobs=args.jobs, clean=args.clean,
                  verbose=args.verbose)


def command_run(args):
    target = host_platform()
    config = config_name(args.release)
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


def collect_universal(name, members, config):
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

        destination = DIST_DIR / dist_name(Path(file_name), name)
        universal.write(sources, destination)
        # The writer only writes bytes; an executable has to stay one.
        shutil.copymode(sources[0], destination)
        print(f"  {destination.relative_to(ROOT)}", flush=True)


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

    for target in sorted(set(PLATFORMS) - GROUPED):
        for source in installed_files(target, config):
            destination = DIST_DIR / dist_name(source, target)
            shutil.copy2(source, destination)
            print(f"  {destination.relative_to(ROOT)}", flush=True)

    for name, members in sorted(UNIVERSAL.items()):
        collect_universal(name, members, config)

    if args.write_manifest:
        MANIFEST.write_text(json.dumps(
            dist_manifest.record(DIST_DIR, manifest_policy()), indent=4) + "\n")
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
        subparser.add_argument("-v", "--verbose", action="store_true",
                               help="show the build output unfiltered")

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
