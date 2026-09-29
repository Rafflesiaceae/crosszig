#!/usr/bin/env python3
"""Fetches, unpacks and caches the MSVC CRT and the Windows SDK.

Getting a Windows binary with no C runtime dependency at all needs Microsoft's
own static CRT (`libcmt.lib`, `libucrt.lib`, `libvcruntime.lib`), which only
ships inside Visual Studio and the Windows SDK and cannot be redistributed by
zig or by this repository. The mingw-w64 toolchain zig does bundle wraps the
UCRT instead and always leaves `ucrtbase.dll` in the import table, which is
why the Windows target builds against the MSVC ABI and needs this.

`xwin` (https://github.com/Jake-Shadle/xwin) downloads those packages straight
from Microsoft and rearranges them into a layout a cross compiler can use.
This script drives it: it pins the xwin version, gets hold of it whichever way
the host allows, unpacks the SDK once into the shared cache, and writes out the
paths the build needs. xwin publishes binaries for Linux and Windows only, so
on macOS it is compiled from source with cargo instead; either way the result
is cached and the rest of this script does not care which happened.

    python tools/xwin.py setup --accept-msvc-license
    python tools/xwin.py paths --arch x86_64
    python tools/xwin.py clean

Everything here is standard library only and independent of the rest of the
repository, so it also works as a plain script in another checkout.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "0.10.0"
RELEASE = f"https://github.com/Jake-Shadle/xwin/releases/download/{VERSION}"

# Checksums as published next to each release asset. xwin builds for Linux and
# Windows and nothing else, so a host missing from this table falls through to
# compiling the tool instead; see build_tool.
ASSETS = {
    ("Linux", "x86_64"): (
        "x86_64-unknown-linux-musl",
        "d870eb4b2f390878af6da1ccd3cf321d22fcb72720984853b4be732ae597fc88"),
    ("Linux", "aarch64"): (
        "aarch64-unknown-linux-musl",
        "6d56d28537a86f37aa3d041318898f25ee3100c6b6ec332ad873c28faf37be23"),
    ("Windows", "AMD64"): (
        "x86_64-pc-windows-msvc",
        "96e83665ef5c1406aabd0be603a8d08e67e604f36fda630b6807ee948a614e26"),
}

# The architectures to fetch Microsoft's packages for, named the way both xwin
# and the splat layout name them. Only the ones something is actually built
# for are downloaded; the full set is larger again.
TARGET_ARCHES = ("x86_64", "x86")

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "xwin"

# A constant because the notice below has to name it and dev.py forwards it by
# the same spelling. Qualified rather than just naming a licence, because in
# dev.py it sits among flags that have nothing to do with Microsoft and would
# otherwise read as a licence for the build itself.
ACCEPT_LICENSE_FLAG = "--accept-msvc-license"

LICENSE_NOTICE = f"""
The Windows SDK and the MSVC CRT are Microsoft's, and downloading them means
accepting their licence. Read it first:

    https://go.microsoft.com/fwlink/?LinkId=2086102

Re-run with {ACCEPT_LICENSE_FLAG} once you have, or set XWIN_ACCEPT_LICENSE=1.
"""


def host_asset():
    """The published xwin release for this machine, or None if there is none."""
    machine = platform.machine()
    # Linux and macOS report the same architecture under different names.
    if machine == "arm64":
        machine = "aarch64"

    return ASSETS.get((platform.system(), machine))


def build_tool():
    """Compiles xwin from source, for hosts it publishes no binary for.

    macOS is the case that matters: both Apple targets are in pixi.toml, and
    neither can use a release that does not exist. cargo checks the crate it
    downloads against crates.io's own checksum, which is the same kind of
    anchor the sha256 table gives the prebuilt binaries, and `--locked` builds
    against the Cargo.lock the crate ships so the dependency versions are
    pinned rather than resolved afresh on every machine.
    """
    # Kept apart from the downloaded binaries, so that a host which gains a
    # published release later picks it up instead of reusing this one.
    root = CACHE / "tool" / f"xwin-{VERSION}-source"
    binary = root / "bin" / ("xwin.exe" if os.name == "nt" else "xwin")
    if binary.is_file():
        return binary

    if shutil.which("cargo") is None:
        sys.exit(
            f"xwin.py: no xwin release is published for "
            f"{platform.system()}/{platform.machine()}, so it has to be built from "
            f"source, and cargo is not on PATH.\n"
            f"  In this repository, `pixi install` provides it.\n"
            f"  Otherwise install Rust (https://rustup.rs), or put an `xwin` "
            f"binary on PATH yourself and this script will use that instead.")

    print(f"no xwin release for {platform.system()}/{platform.machine()}; "
          f"building {VERSION} from source, which takes a few minutes",
          flush=True)
    # Only the compiled binary lands in the repository cache. cargo's own
    # registry and build caches stay wherever cargo keeps them, rather than
    # being duplicated per checkout.
    subprocess.run(["cargo", "install", "xwin", "--version", VERSION,
                    "--locked", "--root", str(root)], check=True)
    return binary


def download(url, destination):
    print(f"fetching {url}", flush=True)
    with urllib.request.urlopen(url) as response, open(destination, "wb") as handle:
        shutil.copyfileobj(response, handle)


def fetch_tool():
    """Returns the path to the pinned xwin binary, downloading it if needed."""
    # An xwin already on PATH wins, which is the escape hatch for hosts with no
    # published release.
    existing = shutil.which("xwin")
    if existing:
        return Path(existing)

    asset = host_asset()
    if asset is None:
        return build_tool()

    target, digest = asset
    binary = CACHE / "tool" / f"xwin-{VERSION}" / ("xwin.exe" if os.name == "nt" else "xwin")
    if binary.is_file():
        return binary

    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / "xwin.tar.gz"
        download(f"{RELEASE}/xwin-{VERSION}-{target}.tar.gz", archive)

        found = hashlib.sha256(archive.read_bytes()).hexdigest()
        if found != digest:
            sys.exit(f"xwin.py: checksum mismatch for the xwin release\n"
                     f"  expected {digest}\n  got      {found}")

        with tarfile.open(archive) as bundle:
            # The archive holds a single versioned directory; take the binary
            # out of it and drop the rest.
            member = next(m for m in bundle.getmembers()
                          if Path(m.name).name in ("xwin", "xwin.exe"))
            extracted = bundle.extractfile(member)
            binary.write_bytes(extracted.read())

    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def sdk_paths(arch):
    """Where the unpacked SDK lives and what the compiler needs from it.

    The headers are shared between architectures; only the libraries, and so
    the libc file pointing at them, are per architecture.
    """
    sdk = CACHE / "sdk"
    return {
        "arch": arch,
        "root": str(sdk),
        # zig refuses to target the MSVC ABI unless it is told where the CRT
        # is; ZIG_LIBC pointing at this file is how that is done for `zig cc`.
        "zig-libc": str(CACHE / f"zig-libc-{arch}.txt"),
        # The libc file above covers the CRT and ucrt headers. The Win32
        # headers are not part of it and have to be added separately.
        "include": [str(sdk / "sdk" / "include" / "um"),
                    str(sdk / "sdk" / "include" / "shared")],
    }


def write_libc_file(paths):
    sdk = Path(paths["root"])
    arch = paths["arch"]
    Path(paths["zig-libc"]).write_text(
        f"include_dir={sdk / 'sdk' / 'include' / 'ucrt'}\n"
        f"sys_include_dir={sdk / 'crt' / 'include'}\n"
        f"crt_dir={sdk / 'sdk' / 'lib' / 'ucrt' / arch}\n"
        f"msvc_lib_dir={sdk / 'crt' / 'lib' / arch}\n"
        f"kernel32_lib_dir={sdk / 'sdk' / 'lib' / 'um' / arch}\n"
        f"gcc_dir=\n")


def is_ready():
    """True once every architecture we build for is unpacked and described."""
    return all(Path(sdk_paths(arch)["root"], "crt", "lib", arch).is_dir()
               and Path(sdk_paths(arch)["zig-libc"]).is_file()
               for arch in TARGET_ARCHES)


def command_setup(args):
    if is_ready() and not args.force:
        print(f"already set up in {CACHE}")
        print(json.dumps([sdk_paths(arch) for arch in TARGET_ARCHES], indent=4))
        return

    if not (args.accept_license or os.environ.get("XWIN_ACCEPT_LICENSE")):
        sys.exit(LICENSE_NOTICE)

    tool = fetch_tool()
    root = sdk_paths(TARGET_ARCHES[0])["root"]
    shutil.rmtree(root, ignore_errors=True)

    # One splat for all architectures: they share the headers, and unpacking
    # them one at a time would only overwrite the same tree repeatedly.
    architectures = []
    for arch in TARGET_ARCHES:
        architectures += ["--arch", arch]

    subprocess.run([
        str(tool),
        # xwin's own flag, not ACCEPT_LICENSE_FLAG: consent has been given by
        # the time we get here, and this is what passes it on.
        "--accept-license",
        *architectures,
        # xwin's own download cache, kept so that a repeat run is offline.
        "--cache-dir", str(CACHE / "download"),
        "splat",
        "--output", root,
    ], check=True)

    for arch in TARGET_ARCHES:
        write_libc_file(sdk_paths(arch))
    print(json.dumps([sdk_paths(arch) for arch in TARGET_ARCHES], indent=4))


def command_paths(args):
    if not is_ready():
        sys.exit("xwin.py: not set up. Run "
                 f"`python tools/xwin.py setup {ACCEPT_LICENSE_FLAG}` first.")
    print(json.dumps(sdk_paths(args.arch)))


def command_clean(_args):
    print(f"removing {CACHE}")
    shutil.rmtree(CACHE, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="download and unpack the SDK if needed")
    setup.add_argument(ACCEPT_LICENSE_FLAG, dest="accept_license",
                       action="store_true",
                       help="accept Microsoft's licence for the SDK and CRT")
    setup.add_argument("--force", action="store_true",
                       help="unpack again even if the cache looks complete")
    setup.set_defaults(func=command_setup)

    paths = subparsers.add_parser("paths", help="print the cached SDK paths as JSON")
    paths.add_argument("--arch", choices=TARGET_ARCHES, default=TARGET_ARCHES[0],
                       help="which architecture's libraries to point at")
    paths.set_defaults(func=command_paths)

    clean = subparsers.add_parser("clean", help="delete the cached SDK and downloads")
    clean.set_defaults(func=command_clean)

    args = parser.parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)


if __name__ == "__main__":
    main()
