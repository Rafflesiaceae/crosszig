#!/usr/bin/env python3
"""Records, checks and compares what a set of built artifacts depends on.

A distribution manifest is a lock file for binaries. It pins, for every file
shipped, the facts that decide where that file will actually run: the
container format, the architecture, the shared libraries and symbol versions
it needs, the minimum OS it declares. Regenerating it turns "the build
changed" into a reviewable diff instead of a bug report from whoever has the
oldest machine.

Nothing here knows about any particular project, and nothing here writes a
binary; the four subcommands only ever read one. Assembling a universal file
is the one job that goes the other way, and it lives in tools/universal.py.

    dist-manifest.py show   <path>...            what these files depend on
    dist-manifest.py record <dir>  -o FILE       lock a directory down
    dist-manifest.py check  <dir>  -m FILE       assert it still matches
    dist-manifest.py diff   <a> <b>              compare two of anything

and `record()`, `check()`, `diff()` and `profile()` are importable under the
same names by a build script that would rather generate its policy than
hand-write it.

Reading the formats
-------------------

The files have to be inspected from whichever host happens to run the check,
and no system tool covers all three containers: readelf cannot read Mach-O,
otool only exists on macOS, and a Windows host has neither. So instead of
shelling out, the readers below walk each container themselves.

`profile()` is the entry point. Which keys come back depends on the format,
because the formats genuinely record different things:

    all                format, arch
    elf                needs, symbol-versions, runpath, min-kernel, interpreter
    pe                 needs, imports, min-os, subsystem-version
    macho              needs, dylib-compat, rpath, macos-min, code-signature
    archive            cxx-runtime
    macho-universal    slices, one full macho profile per architecture
    archive-universal  slices, one archive profile per architecture

`arch` is a string everywhere except for a universal binary, where it is the
sorted list of the architectures the file carries.

Dependency names are whatever the format itself stores: a soname for ELF
(`libc.so.6`), an import table entry for PE (`KERNEL32.dll`), an install name
for Mach-O (`/usr/lib/libSystem.B.dylib`).

One thing deliberately absent: the CPU baseline the code was generated for.
Nothing in any of these containers records it unless the toolchain emits a
`.note.gnu.property` marker, and most do not, so that has to be pinned at
build time instead of checked afterwards.

The manifest
------------

    {
        "schema": "dist-manifest/1",
        "metadata": {...},              opaque to this tool, compared verbatim
        "require-rule-coverage": true,  every file must be claimed by a rule
        "rules": [...],                 see below
        "files": {"<relative path>": {<profile>}}
    }

Recording the profiles alone is not enough. A manifest regenerated from a
broken build records the breakage and then happily agrees with itself, so the
rules are the half that does not come from the artifacts: they say what has to
be true no matter what was measured.

    {
        "name": "linux-64",                        used in problem messages
        "match": ["*-linux-64", "*-linux-64.*"],   globs over the file name
        "slice": "x86-64",                         optional, see below
        "when":    {"format": {"is": "elf"}},      skip the rule unless met
        "require": {"symbol-versions.GLIBC": {"at-most": "2.3.3"}}
    }

Keys in `when` and `require` are dotted paths into the profile. A condition is
either a plain value, meaning equality, or a map of these operators, all of
which have to hold:

    is          equal to this value
    is-version  the same version, however many components it is written with
    one-of      equal to one of these values
    at-most     version no higher than this, compared componentwise
    at-least    version no lower than this
    empty       true: absent, or an empty string/list/map. false: the reverse
    present     whether the key is there at all
    subset-of   every element (or map key) appears in this list
    contains    every one of these appears among the elements
    matches     the value, as a string, fully matches this regular expression

A key that is absent fails every condition except `{"empty": true}` and
`{"present": false}`, so a typo in a key name is reported rather than quietly
passing. Use `when` to scope a rule to the files it can apply to.

`slice` narrows a rule to one architecture of a universal file. A thin file
counts as a single slice named after its own architecture, so one rule shape
covers both, and a rule naming a slice its file does not carry is a failure.
"""

import argparse
import fnmatch
import json
import re
import struct
import sys
from pathlib import Path


class UnknownFormat(Exception):
    """Raised for a file whose container format is not recognised."""


def version_tuple(text):
    """`"2.28"` becomes `(2, 28, 0, 0)`, so versions of unequal length compare."""
    parts = tuple(int(part) for part in text.split("."))
    return parts + (0,) * (4 - len(parts)) if len(parts) < 4 else parts


_ELF_MACHINES = {0x03: "x86", 0x28: "arm", 0x3E: "x86-64", 0xB7: "arm64", 0xF3: "riscv64"}
_PE_MACHINES = {0x014C: "x86", 0x01C0: "arm", 0x01C4: "armv7", 0x8664: "x86-64",
                0xAA64: "arm64"}
_MACHO_CPUS = {7: "x86", 12: "arm", 0x01000007: "x86-64", 0x0100000C: "arm64"}

_MACHO_MAGIC = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                b"\xfe\xed\xfa\xce")
# FAT_MAGIC and FAT_MAGIC_64, the wrappers around several thin Mach-O images.
_FAT_MAGIC = (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf")


def profile(path):
    """Everything about the file at `path` that bears on where it will run."""
    with open(path, "rb") as handle:
        data = handle.read()

    if data[:4] == b"\x7fELF":
        return _elf(data)
    if data[:2] == b"MZ":
        return _pe(data)
    if data[:4] in _FAT_MAGIC:
        return _fat(data)
    if data[:4] in _MACHO_MAGIC:
        return _macho(data)
    if data[:8] == b"!<arch>\n":
        return _archive(data)

    raise UnknownFormat(f"{path}: unrecognised binary format")


def _cstring(data, offset):
    return data[offset:data.index(b"\0", offset)].decode("utf-8", "replace")


def _unknown(value):
    return f"unknown({value:#x})"


# --------------------------------------------------------------------------
# ELF
# --------------------------------------------------------------------------

# Symbol version tokens look like `GLIBC_2.16` or `GLIBCXX_3.4.21`. The greedy
# prefix makes the split fall on the last underscore that is followed by a
# plain version, which is what every toolchain library in practice uses.
# `GLIBC_PRIVATE` carries no version and so is skipped rather than parsed.
_VERSION_TOKEN = re.compile(r"^(.+)_([0-9][0-9.]*)$")


def _elf_sections(data):
    """Byte order, 64-bit flag, and the sections as (name, type, offset, size, link)."""
    is_64 = data[4] == 2
    order = "<" if data[5] == 1 else ">"

    # Section headers are the shortest route to the tables below; going through
    # the program headers instead would mean translating virtual addresses.
    if is_64:
        table = struct.unpack_from(order + "Q", data, 0x28)[0]
        entry_size, count, name_index = struct.unpack_from(order + "HHH", data, 0x3A)
        fields = order + "II8x8xQQI"  # sh_name, sh_type, .., sh_offset, sh_size, sh_link
    else:
        table = struct.unpack_from(order + "I", data, 0x20)[0]
        entry_size, count, name_index = struct.unpack_from(order + "HHH", data, 0x2E)
        fields = order + "II4x4xIII"

    raw = [struct.unpack_from(fields, data, table + index * entry_size)
           for index in range(count)]

    strings = raw[name_index][2] if count else 0
    return order, is_64, [(_cstring(data, strings + entry[0]), *entry[1:]) for entry in raw]


def _elf(data):
    order, is_64, sections = _elf_sections(data)

    entry_format = order + ("qQ" if is_64 else "iI")
    entry_width = struct.calcsize(entry_format)

    needs = []
    runpath = []
    for _name, kind, offset, size, link in sections:
        if kind != 6:  # SHT_DYNAMIC
            continue

        # sh_link of .dynamic points at the string table holding the names.
        strings = sections[link][2]

        for position in range(offset, offset + size, entry_width):
            tag, value = struct.unpack_from(entry_format, data, position)
            if tag == 0:  # DT_NULL terminates the array
                break
            if tag == 1:  # DT_NEEDED
                needs.append(_cstring(data, strings + value))
            elif tag in (15, 29):  # DT_RPATH, DT_RUNPATH
                runpath += [part for part in _cstring(data, strings + value).split(":") if part]

    result = {
        "format": "elf",
        "arch": _ELF_MACHINES.get(struct.unpack_from(order + "H", data, 0x12)[0],
                                  _unknown(struct.unpack_from(order + "H", data, 0x12)[0])),
        "needs": sorted(needs),
        "symbol-versions": _elf_symbol_versions(data, order, sections),
        "runpath": sorted(runpath),
        # null for a statically linked image. Without this a static binary and
        # a dynamic one that happens to need nothing look identical, and the
        # path itself is architecture specific, so it is worth pinning down.
        "interpreter": _elf_interpreter(data, sections),
    }

    kernel = _elf_min_kernel(data, order, sections)
    if kernel is not None:
        result["min-kernel"] = kernel
    return result


def _elf_symbol_versions(data, order, sections):
    """Highest version required per token namespace, e.g. `{"GLIBC": "2.16"}`.

    Keyed on the namespace rather than on the library, because that is the part
    that decides compatibility: `GLIBC_2.16` means glibc 2.16 or newer no
    matter which of libc/libm/libpthread happened to pull it in.
    """
    highest = {}
    for _name, kind, offset, _size, link in sections:
        if kind != 0x6FFFFFFE:  # SHT_GNU_verneed
            continue

        strings = sections[link][2]
        position = offset
        while True:
            # Elf_Verneed: version, aux count, file name, offset to the first
            # aux entry, offset to the next verneed. Both offsets are relative
            # to the record they are stored in.
            _version, count, _file, aux, next_entry = struct.unpack_from(
                order + "HHIII", data, position)

            aux_position = position + aux
            for _ in range(count):
                # Elf_Vernaux: hash, flags, other, name, offset to the next.
                _hash, _flags, _other, name, aux_next = struct.unpack_from(
                    order + "IHHII", data, aux_position)

                match = _VERSION_TOKEN.match(_cstring(data, strings + name))
                if match:
                    namespace, found = match.group(1), match.group(2)
                    if namespace not in highest or \
                            version_tuple(found) > version_tuple(highest[namespace]):
                        highest[namespace] = found

                if aux_next == 0:
                    break
                aux_position += aux_next

            if next_entry == 0:
                break
            position += next_entry

    return dict(sorted(highest.items()))


def _elf_interpreter(data, sections):
    """The dynamic loader named in .interp, or None for a static binary."""
    for name, kind, offset, _size, _link in sections:
        if kind == 1 and name == ".interp":  # SHT_PROGBITS
            return _cstring(data, offset)
    return None


def _elf_min_kernel(data, order, sections):
    """Oldest Linux kernel the binary declares it runs on, from .note.ABI-tag."""
    for name, kind, offset, _size, _link in sections:
        if kind != 7 or name != ".note.ABI-tag":  # SHT_NOTE
            continue

        name_size, _desc_size, note_type = struct.unpack_from(order + "III", data, offset)
        if note_type != 1:  # NT_GNU_ABI_TAG
            continue

        # The note name is padded out to a four byte boundary.
        descriptor = offset + 12 + (name_size + 3) // 4 * 4
        os_id, major, minor, patch = struct.unpack_from(order + "IIII", data, descriptor)
        if os_id != 0:  # 0 is Linux; the tag is also used for other kernels
            continue
        return f"{major}.{minor}.{patch}"

    return None


# --------------------------------------------------------------------------
# PE
# --------------------------------------------------------------------------

def _pe(data):
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise UnknownFormat("not a PE image")

    coff = pe_offset + 4
    machine = struct.unpack_from("<H", data, coff)[0]
    section_count = struct.unpack_from("<H", data, coff + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]

    optional = coff + 20
    magic = struct.unpack_from("<H", data, optional)[0]

    # PE32 spends four bytes on BaseOfData where PE32+ widens ImageBase, so the
    # version fields land at the same place in both; only the data directory
    # array that follows the optional header proper moves.
    os_major, os_minor, _image_major, _image_minor, subsystem_major, subsystem_minor = \
        struct.unpack_from("<HHHHHH", data, optional + 40)
    directories = optional + (112 if magic == 0x20B else 96)

    imports = _pe_imports(data, optional + optional_size, section_count,
                          struct.unpack_from("<I", data, directories + 8)[0],
                          magic == 0x20B)

    return {
        "format": "pe",
        "arch": _PE_MACHINES.get(machine, _unknown(machine)),
        "needs": sorted(imports),
        # Which Windows version a PE actually requires is not recorded anywhere
        # in the file; min-os and the subsystem version below are whatever the
        # linker was told to write. The function names are the evidence, since
        # an API that did not exist yet is what really stops the image from
        # loading, so they are listed rather than summarised away.
        "imports": {name: sorted(imports[name]) for name in sorted(imports)},
        # The loader refuses to start an image whose subsystem version is above
        # the running Windows; min-os is advisory but recorded alongside it.
        "min-os": f"{os_major}.{os_minor}",
        "subsystem-version": f"{subsystem_major}.{subsystem_minor}",
    }


def _pe_imports(data, section_table, section_count, import_rva, is_64):
    """The import table as `{"KERNEL32.dll": ["CloseHandle", ...]}`."""
    if import_rva == 0:
        return {}

    sections = []
    for index in range(section_count):
        base = section_table + index * 40
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, base + 8)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset))

    def to_offset(rva):
        for virtual_address, size, raw_offset in sections:
            if virtual_address <= rva < virtual_address + size:
                return raw_offset + (rva - virtual_address)
        raise UnknownFormat(f"RVA {rva:#x} is outside every section")

    # Thunks are pointer sized, and the top bit means "imported by ordinal"
    # rather than "RVA of a hint/name entry".
    thunk_format = "<Q" if is_64 else "<I"
    thunk_width = struct.calcsize(thunk_format)
    ordinal_flag = 1 << (thunk_width * 8 - 1)

    found = {}
    descriptor = to_offset(import_rva)
    while True:
        # A 20 byte all-zero descriptor terminates the import directory.
        lookup_rva, _timestamp, _forwarder, name_rva, address_rva = struct.unpack_from(
            "<IIIII", data, descriptor)
        if not any((lookup_rva, _timestamp, _forwarder, name_rva, address_rva)):
            break

        # The lookup table is the one that survives binding; fall back to the
        # address table for images that have none.
        functions = []
        thunk = to_offset(lookup_rva or address_rva)
        while True:
            value = struct.unpack_from(thunk_format, data, thunk)[0]
            if value == 0:
                break
            if value & ordinal_flag:
                functions.append(f"#{value & 0xFFFF}")
            else:
                # A hint/name entry is a two byte hint followed by the name.
                functions.append(_cstring(data, to_offset(value) + 2))
            thunk += thunk_width

        found[_cstring(data, to_offset(name_rva))] = functions
        descriptor += 20

    return found


# --------------------------------------------------------------------------
# Mach-O
# --------------------------------------------------------------------------

# LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, LC_REEXPORT_DYLIB, LC_LOAD_UPWARD_DYLIB
_MACHO_DYLIB_COMMANDS = {0x0C, 0x80000018, 0x8000001F, 0x80000023}
_LC_VERSION_MIN_MACOSX = 0x24
_LC_BUILD_VERSION = 0x32
_LC_CODE_SIGNATURE = 0x1D
_LC_RPATH = 0x8000001C
_PLATFORM_MACOS = 1


def _macho_version(packed):
    return f"{packed >> 16}.{(packed >> 8) & 0xFF}.{packed & 0xFF}"


def _macho_order(magic):
    """Struct byte order prefix for a thin Mach-O with this magic."""
    return "<" if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") else ">"


def _macho(data):
    magic = data[:4]
    order = _macho_order(magic)
    is_64 = magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")

    cpu_type, cpu_subtype = struct.unpack_from(order + "iI", data, 4)
    command_count = struct.unpack_from(order + "I", data, 16)[0]
    position = 32 if is_64 else 28  # mach_header_64 has a trailing reserved field

    needs = []
    compatibility = {}
    rpath = []
    macos_min = None
    signed = False

    for _ in range(command_count):
        command, size = struct.unpack_from(order + "II", data, position)

        if command in _MACHO_DYLIB_COMMANDS:
            # The name is an lc_str: an offset from the start of the command.
            name_offset, _timestamp, _current, compat = struct.unpack_from(
                order + "IIII", data, position + 8)
            name = _cstring(data, position + name_offset)
            needs.append(name)
            # The loader rejects the image if the installed dylib is older than
            # this, so it is part of what decides compatibility.
            compatibility[name] = _macho_version(compat)
        elif command == _LC_RPATH:
            rpath.append(_cstring(
                data, position + struct.unpack_from(order + "I", data, position + 8)[0]))
        elif command == _LC_BUILD_VERSION:
            platform_id, minimum, _sdk, _tools = struct.unpack_from(
                order + "IIII", data, position + 8)
            if platform_id == _PLATFORM_MACOS:
                macos_min = _macho_version(minimum)
        elif command == _LC_VERSION_MIN_MACOSX:
            # Superseded by LC_BUILD_VERSION, still emitted by older linkers.
            macos_min = _macho_version(struct.unpack_from(order + "I", data, position + 8)[0])
        elif command == _LC_CODE_SIGNATURE:
            signed = True

        position += size

    result = {
        "format": "macho",
        "arch": _macho_arch(cpu_type, cpu_subtype),
        "needs": sorted(needs),
        "dylib-compat": dict(sorted(compatibility.items())),
        "rpath": sorted(rpath),
        # arm64 macOS refuses to execute an unsigned binary outright, so this
        # is a hard runs / does-not-run bit rather than a hardening detail.
        "code-signature": signed,
    }
    if macos_min is not None:
        result["macos-min"] = macos_min
    return result


def _fat(data):
    """A universal binary: several thin images concatenated into one file.

    Apple wraps static libraries the same way, so a slice is either a thin
    Mach-O image or an entire ar archive.
    """
    # The fat header is always big endian, whatever the slices inside it are.
    is_64 = data[:4] == b"\xca\xfe\xba\xbf"
    count = struct.unpack_from(">I", data, 4)[0]
    entry = ">iiQQII" if is_64 else ">iiIII"
    width = struct.calcsize(entry)

    # `cafebabe` is also the Java class file magic, where this field holds a
    # version number instead. A plausible slice count plus in-range slices is
    # what tells the two apart.
    if not 0 < count <= 64:
        raise UnknownFormat("not a universal binary")

    slices = {}
    for index in range(count):
        cpu_type, cpu_subtype, offset, size = struct.unpack_from(
            entry, data, 8 + index * width)[:4]
        if offset + size > len(data):
            raise UnknownFormat("universal binary slice runs past the end of the file")
        body = data[offset:offset + size]
        slices[_macho_arch(cpu_type, cpu_subtype)] = (
            _archive(body) if body[:8] == b"!<arch>\n" else _macho(body))

    # A fat container full of archives is not a loadable image, and calling it
    # macho-universal would suggest it is, so the two are named apart.
    kind = "archive" if all(s["format"] == "archive" for s in slices.values()) else "macho"
    return {
        "format": f"{kind}-universal",
        "arch": sorted(slices),
        "slices": dict(sorted(slices.items())),
    }


def _macho_arch(cpu_type, cpu_subtype):
    name = _MACHO_CPUS.get(cpu_type, _unknown(cpu_type))
    # The subtype can narrow the architecture to CPUs newer than the baseline.
    variant = cpu_subtype & 0x00FFFFFF
    if name == "x86-64" and variant == 8:
        return "x86-64h"
    if name == "arm64" and variant == 2:
        return "arm64e"
    return name


# --------------------------------------------------------------------------
# ar archives
# --------------------------------------------------------------------------

# Neither the symbol table nor the long-name table is an object file. The
# first two spellings are GNU's (`/` and `//`), the rest are BSD's, which is
# what zig ar emits for macOS targets.
_ARCHIVE_SPECIAL = {"", "/SYM64", "__.SYMDEF", "__.SYMDEF SORTED"}


def _archive_members(data):
    """Yields the bytes of each archive member that is an object file."""
    position = 8
    while position + 60 <= len(data):
        header = data[position:position + 16].decode("ascii", "replace").strip()
        size = int(data[position + 48:position + 58].decode("ascii", "replace").strip() or 0)
        body = position + 60
        member_end = body + size + (size % 2)  # members are padded to even offsets

        if header.startswith("#1/"):
            # BSD archives put a long member name in the first bytes of the
            # member data and record only its length in the header field.
            name_length = int(header[3:])
            name = data[body:body + name_length].rstrip(b"\0").decode("ascii", "replace")
            body += name_length
            size -= name_length
        else:
            name = header

        if name.rstrip("/") not in _ARCHIVE_SPECIAL:
            yield data[body:body + size]

        position = member_end


def _archive(data):
    """A static library records no dependencies of its own, so what can be
    recovered is what its members are: the architecture they were built for,
    and which C++ standard library they expect to be linked against."""
    arch = None
    undefined = []
    for member in _archive_members(data):
        if arch is None:
            arch = _member_arch(member)
        undefined += _member_undefined(member)

    return {
        "format": "archive",
        "arch": arch or "unknown",
        "cxx-runtime": _cxx_runtime(undefined),
    }


def _cxx_runtime(undefined):
    """Which C++ standard library the undefined symbols come from, if any.

    The three are not link compatible, and an archive says nothing about which
    one it was built against, so a consumer that assumes the wrong one only
    finds out at link time. libc++ wraps everything in the inline namespace
    `std::__1`, which mangles as `St3__1`; libstdc++ has no such namespace;
    MSVC does not use Itanium mangling at all.
    """
    evidence = set()
    for name in undefined:
        if "St3__1" in name:
            evidence.add("libc++")
        elif name.startswith("?") and "@std@@" in name:
            evidence.add("msvc-stl")
        elif name.startswith("_Z") and "St" in name:
            evidence.add("libstdc++")

    # A handful of `std::` symbols mangle identically under libc++ and
    # libstdc++, so the specific finding has to win over the generic one.
    for candidate in ("libc++", "msvc-stl", "libstdc++"):
        if candidate in evidence:
            return candidate
    return None


def _member_undefined(member):
    """Externally undefined symbol names of one archive member."""
    if member[:4] == b"\x7fELF":
        return _elf_undefined(member)
    if member[:4] in _MACHO_MAGIC:
        return _macho_undefined(member)
    if len(member) >= 20 and struct.unpack_from("<H", member, 0)[0] in _PE_MACHINES \
            and struct.unpack_from("<H", member, 16)[0] == 0:
        return _coff_undefined(member)
    return []


def _elf_undefined(data):
    """SHT_SYMTAB entries whose section index is SHN_UNDEF."""
    order, is_64, sections = _elf_sections(data)
    # Elf64_Sym is name, info, other, shndx, value, size; Elf32_Sym orders the
    # same fields differently, hence the two index positions below.
    entry = order + ("IBBHQQ" if is_64 else "IIIBBH")
    width = struct.calcsize(entry)
    shndx_at = 3 if is_64 else 5

    names = []
    for _name, kind, offset, size, link in sections:
        if kind != 2:  # SHT_SYMTAB
            continue
        strings = sections[link][2]
        for position in range(offset, offset + size, width):
            fields = struct.unpack_from(entry, data, position)
            if fields[shndx_at] == 0 and fields[0]:
                names.append(_cstring(data, strings + fields[0]))
    return names


_LC_SYMTAB = 0x02


def _macho_undefined(data):
    """LC_SYMTAB entries that are external and of type N_UNDF."""
    magic = data[:4]
    order = _macho_order(magic)
    is_64 = magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")
    command_count = struct.unpack_from(order + "I", data, 16)[0]
    position = 32 if is_64 else 28

    entry = order + ("IBBHQ" if is_64 else "IBBhI")
    width = struct.calcsize(entry)

    names = []
    for _ in range(command_count):
        command, size = struct.unpack_from(order + "II", data, position)
        if command == _LC_SYMTAB:
            symbols, count, strings, _length = struct.unpack_from(
                order + "IIII", data, position + 8)
            for index in range(count):
                name, kind = struct.unpack_from(entry, data, symbols + index * width)[:2]
                # N_TYPE is bits 1 to 3; 0 there is N_UNDF, and bit 0 is N_EXT.
                if kind & 0x0E == 0 and kind & 0x01 and name:
                    names.append(_cstring(data, strings + name))
        position += size
    return names


def _coff_undefined(data):
    """COFF symbols with section number 0 and external storage class."""
    table, count = struct.unpack_from("<II", data, 8)
    if table == 0:
        return []
    strings = table + 18 * count

    names = []
    index = 0
    while index < count:
        record = table + index * 18
        section, _kind, storage, auxiliary = struct.unpack_from("<hHBB", data, record + 12)
        if section == 0 and storage == 2:  # IMAGE_SYM_CLASS_EXTERNAL
            # A name of eight characters or less sits in the record; a longer
            # one is a zero word followed by an offset into the string table.
            if struct.unpack_from("<I", data, record)[0] == 0:
                names.append(_cstring(data, strings + struct.unpack_from(
                    "<I", data, record + 4)[0]))
            else:
                names.append(data[record:record + 8].rstrip(b"\0").decode(
                    "utf-8", "replace"))
        index += 1 + auxiliary  # auxiliary records follow their symbol
    return names


def _member_arch(member):
    if member[:4] == b"\x7fELF":
        order = "<" if member[5] == 1 else ">"
        machine = struct.unpack_from(order + "H", member, 0x12)[0]
        return _ELF_MACHINES.get(machine, _unknown(machine))

    if member[:4] in _MACHO_MAGIC:
        cpu_type, cpu_subtype = struct.unpack_from(
            _macho_order(member[:4]) + "iI", member, 4)
        return _macho_arch(cpu_type, cpu_subtype)

    # A COFF object has no magic; a known machine plus the optional header size
    # being zero, as it always is for an object file, is enough to recognise it.
    if len(member) >= 20:
        machine, _sections = struct.unpack_from("<HH", member, 0)
        if machine in _PE_MACHINES and struct.unpack_from("<H", member, 16)[0] == 0:
            return _PE_MACHINES[machine]

    return None


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------

SCHEMA = "dist-manifest/1"

# The keys a caller may put in a manifest besides the recorded files. They are
# kept apart from `files` so that a policy can be carried from one manifest to
# the next while the profiles underneath it are regenerated.
POLICY_KEYS = ("metadata", "require-rule-coverage", "rules")


def contents(directory):
    """Every regular file under `directory`, keyed by its relative path."""
    directory = Path(directory)
    return {path.relative_to(directory).as_posix(): path
            for path in sorted(directory.rglob("*")) if path.is_file()}


def record(directory, policy=None):
    """Locks a directory down: the given policy plus a profile of every file.

    Reading a file that is not a binary at all is left to fail, because a
    manifest that silently skipped what it could not parse would go on
    claiming the directory was fully described.
    """
    manifest = {"schema": SCHEMA}
    for key in POLICY_KEYS:
        if policy and key in policy:
            manifest[key] = policy[key]
    manifest["files"] = {name: profile(path)
                         for name, path in contents(directory).items()}
    return manifest


def check(directory, manifest, policy=None):
    """Asserts `directory` against `manifest`. Returns the list of problems.

    Three separate things are checked, and they fail in different ways on
    purpose. Every recorded file has to still read back byte-identical facts,
    which catches a changed build. Every rule has to hold against the profiles
    read now rather than against the recorded ones, which is what still fails
    after someone has regenerated the manifest from a broken build. And when
    the caller generates its policy, `policy` is compared against the one in
    the manifest, which catches a manifest written before the generator
    changed.
    """
    problems = []

    found_schema = manifest.get("schema")
    if found_schema != SCHEMA:
        return [f"manifest schema is {found_schema!r}, this tool speaks {SCHEMA!r}"]

    if policy is not None:
        for key in POLICY_KEYS:
            problems += _differences(key, manifest.get(key), policy.get(key),
                                     ("manifest", "policy"))

    rules = manifest.get("rules", [])
    present = contents(directory)

    for name, recorded in sorted(manifest.get("files", {}).items()):
        path = present.get(name)
        if path is None:
            problems.append(f"{name}: missing")
            continue

        try:
            found = profile(path)
        except (UnknownFormat, OSError, ValueError, struct.error) as error:
            problems.append(f"{name}: {error}")
            continue

        problems += _differences(name, recorded, found, ("manifest", "file"))

        claimed = False
        for rule in rules:
            matched, failures = _apply_rule(name, found, rule)
            claimed = claimed or matched
            problems += failures
        if manifest.get("require-rule-coverage") and not claimed:
            problems.append(f"{name}: no rule in the manifest claims this file")

    for name in present:
        if name not in manifest.get("files", {}):
            problems.append(f"{name}: present but not listed in the manifest")

    # Two rules about the same slice of the same file report a missing slice
    # identically, and saying it twice adds nothing. Every message names the
    # file it is about, so equal text really is the same finding.
    return list(dict.fromkeys(problems))


def diff(left, right):
    """Compares two manifests, two directories or two single binaries.

    Either side may be any of the three, so a directory of fresh builds can be
    held against a manifest from a release, or one project's binary against
    another's.
    """
    left_files, left_name = _side(left)
    right_files, right_name = _side(right)

    # Two single files are the interesting cross-project case, and there their
    # names almost never agree, so they are paired up positionally instead.
    if len(left_files) == 1 and len(right_files) == 1:
        only_left, = left_files.values()
        only_right, = right_files.values()
        return _differences("", only_left, only_right, (left_name, right_name))

    problems = []
    for name in sorted(set(left_files) | set(right_files)):
        if name not in left_files:
            problems.append(f"{name}: only in {right_name}")
        elif name not in right_files:
            problems.append(f"{name}: only in {left_name}")
        else:
            problems += _differences(name, left_files[name], right_files[name],
                                     (left_name, right_name))
    return problems


def _side(source):
    """One side of a diff as (profiles by name, what to call it)."""
    path = Path(source)
    if path.is_dir():
        return ({name: profile(found) for name, found in contents(path).items()},
                path.name or str(path))

    # A manifest is the only JSON this tool reads, so the suffix is enough to
    # tell it from a binary that happens to be passed alongside one.
    if path.suffix == ".json":
        return json.loads(path.read_text()).get("files", {}), path.name

    return {path.name: profile(path)}, path.name


def _differences(label, left, right, names):
    """Every leaf at which `left` and `right` disagree, one problem each.

    Descending rather than printing the two sides whole is what keeps a
    changed glibc ceiling from arriving as a screenful of unchanged rules.
    """
    if isinstance(left, dict) and isinstance(right, dict):
        problems = []
        for key in sorted(set(left) | set(right)):
            where = f"{label}.{key}" if label else key
            if key not in left:
                problems.append(f"{where}: only {names[1]} has it, {right[key]!r}")
            elif key not in right:
                problems.append(f"{where}: only {names[0]} has it, {left[key]!r}")
            else:
                problems += _differences(where, left[key], right[key], names)
        return problems

    if isinstance(left, list) and isinstance(right, list):
        # A list of plain values is a set of facts, where saying what came and
        # went reads far better than a positional diff. A list of maps is an
        # ordered structure, so that one is walked position by position.
        if all(not isinstance(item, (dict, list)) for item in left + right):
            return _list_differences(label, left, right, names)

        problems = []
        for index in range(max(len(left), len(right))):
            where = f"{label}[{index}]"
            if index >= len(left):
                problems.append(f"{where}: only {names[1]} has it, {right[index]!r}")
            elif index >= len(right):
                problems.append(f"{where}: only {names[0]} has it, {left[index]!r}")
            else:
                problems += _differences(where, left[index], right[index], names)
        return problems

    if left != right:
        where = f"{label}: " if label else ""
        return [f"{where}{names[0]} has {left!r}, {names[1]} has {right!r}"]
    return []


def _list_differences(label, left, right, names):
    gone = [item for item in left if item not in right]
    added = [item for item in right if item not in left]

    problems = []
    if gone:
        problems.append(f"{label}: only {names[0]} has {gone!r}")
    if added:
        problems.append(f"{label}: only {names[1]} has {added!r}")
    # Search paths are consulted in the order they are stored, so a reordering
    # with the same entries is still a difference and has to be said out loud.
    if not problems and left != right:
        problems.append(f"{label}: the same entries in a different order")
    return problems


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def _apply_rule(name, found, rule):
    """Runs one rule over one profile: (did it match the file, problems)."""
    patterns = rule.get("match", "*")
    if isinstance(patterns, str):
        patterns = [patterns]
    if not any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
        return False, []

    label = name
    if "name" in rule:
        label += f" ({rule['name']})"

    target = found
    if "slice" in rule:
        label += f" [{rule['slice']}]"
        # A thin file is treated as its own single slice so that per-slice and
        # whole-file rules are written the same way.
        slices = found.get("slices") or {found.get("arch"): found}
        if rule["slice"] not in slices:
            return True, [f"{label}: the file carries no such slice"]
        target = slices[rule["slice"]]

    # `when` is a guard rather than an assertion: an unmet one means the rule
    # has nothing to say about this file, which is how a rule about ELF images
    # is kept off the static library sitting next to them.
    for key, condition in sorted(rule.get("when", {}).items()):
        if _condition_problems(key, target, condition):
            return True, []

    problems = []
    for key, condition in sorted(rule.get("require", {}).items()):
        problems += [f"{label}: {failure}"
                     for failure in _condition_problems(key, target, condition)]
    return True, problems


def _lookup(profile_data, key):
    """Follows a dotted key such as `symbol-versions.GLIBC` into a profile."""
    current = profile_data
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _condition_problems(key, target, condition):
    """Every way one `require` or `when` entry is not satisfied."""
    # A condition that is not a map is the value itself, which is what makes
    # the common case read as `"format": "elf"`.
    if not isinstance(condition, dict):
        condition = {"is": condition}

    found, value = _lookup(target, key)
    problems = []
    for operator, expected in sorted(condition.items()):
        problems += _operator_problems(key, operator, found, value, expected)
    return problems


def _operator_problems(key, operator, found, value, expected):
    # The two conditions that are about absence come first, because they are
    # the only ones a missing key can satisfy.
    if operator == "present":
        if found != bool(expected):
            return [f"{key} is {'present' if found else 'absent'}"]
        return []

    if operator == "empty":
        if (not found or not value) != bool(expected):
            return [f"{key} is {value!r}, wanted "
                    f"{'nothing' if expected else 'something'}"]
        return []

    if not found:
        return [f"{key} is not recorded at all, so {operator} {expected!r} "
                f"cannot hold"]

    if operator == "is":
        return [] if value == expected else [f"{key} is {value!r}, wanted {expected!r}"]

    if operator == "one-of":
        return [] if value in expected else \
            [f"{key} is {value!r}, wanted one of {expected!r}"]

    if operator in ("at-most", "at-least", "is-version"):
        try:
            actual, limit = version_tuple(str(value)), version_tuple(str(expected))
        except ValueError:
            return [f"{key} is {value!r}, which is not a version {operator} "
                    f"{expected!r} could be compared against"]
        # `10.12` and `10.12.0` are the same floor written two ways, and which
        # of the two a container stores is the container's business, so these
        # compare the padded tuples rather than the text.
        if operator == "at-most" and actual > limit:
            return [f"{key} is {value}, above the permitted {expected}"]
        if operator == "at-least" and actual < limit:
            return [f"{key} is {value}, below the required {expected}"]
        if operator == "is-version" and actual != limit:
            return [f"{key} is {value}, wanted {expected}"]
        return []

    if operator == "subset-of":
        extra = sorted(set(_elements(value)) - set(expected))
        return [f"{key} has {extra!r}, which {expected!r} does not allow"] if extra else []

    if operator == "contains":
        missing = sorted(set(expected) - set(_elements(value)))
        return [f"{key} is missing {missing!r}"] if missing else []

    if operator == "matches":
        return [] if re.fullmatch(expected, str(value)) else \
            [f"{key} is {value!r}, which does not match {expected!r}"]

    raise ValueError(f"unknown condition {operator!r} on {key}")


def _elements(value):
    """What `subset-of` and `contains` compare: a map's keys, a list's items."""
    if isinstance(value, dict):
        return list(value)
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _report(problems, heading):
    """Prints the problems, if any, and returns the process exit status."""
    if not problems:
        return 0
    # The commands print to stdout before calling this; without the flush the
    # two streams arrive interleaved whenever either one is redirected.
    sys.stdout.flush()
    print(heading, file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return 1


def _load_policy(source):
    """The policy out of a policy file or out of an existing manifest."""
    data = json.loads(Path(source).read_text())
    return {key: data[key] for key in POLICY_KEYS if key in data}


def _command_show(args):
    print(json.dumps({str(path): profile(path) for path in args.path}, indent=4))
    return 0


def _command_record(args):
    policy = _load_policy(args.policy) if args.policy else None
    manifest = record(args.directory, policy)
    text = json.dumps(manifest, indent=4) + "\n"
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {len(manifest['files'])} files to {args.output}")
    else:
        print(text, end="")

    # Locking down artifacts that already break the policy would write the
    # breakage into the lock file, so it is reported straight away.
    return _report(check(args.directory, manifest),
                   f"\nrecorded, but {args.directory} does not satisfy its own rules")


def _command_check(args):
    manifest = json.loads(Path(args.manifest).read_text())
    status = _report(check(args.directory, manifest),
                     f"\n{args.directory} does not match {args.manifest}")
    if status == 0:
        print(f"ok: {len(manifest.get('files', {}))} files in {args.directory}")
    return status


def _command_diff(args):
    problems = diff(args.left, args.right)
    for problem in problems:
        print(problem)
    if not problems:
        print("identical")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show", help="print what the given files depend on")
    show.add_argument("path", nargs="+", type=Path)
    show.set_defaults(func=_command_show)

    record_parser = subparsers.add_parser(
        "record", help="write a manifest describing every file in a directory")
    record_parser.add_argument("directory", type=Path)
    record_parser.add_argument("-o", "--output", help="write here instead of stdout")
    record_parser.add_argument("--policy",
                               help="take metadata and rules from this manifest "
                                    "or policy file instead of writing none")
    record_parser.set_defaults(func=_command_record)

    check_parser = subparsers.add_parser(
        "check", help="assert a directory against a manifest")
    check_parser.add_argument("directory", type=Path)
    check_parser.add_argument("-m", "--manifest", required=True)
    check_parser.set_defaults(func=_command_check)

    diff_parser = subparsers.add_parser(
        "diff", help="compare two manifests, directories or binaries")
    diff_parser.add_argument("left")
    diff_parser.add_argument("right")
    diff_parser.set_defaults(func=_command_diff)

    args = parser.parse_args()
    try:
        return args.func(args)
    except (UnknownFormat, OSError, ValueError) as error:
        sys.exit(f"dist-manifest.py: {error}")


if __name__ == "__main__":
    sys.exit(main())
