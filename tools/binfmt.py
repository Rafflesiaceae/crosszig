"""Reads the facts that decide where a built artifact will actually run.

`dist-check` has to inspect ELF, PE and Mach-O files from whichever host it
happens to run on, and no system tool covers all three: readelf cannot read
Mach-O, otool only exists on macOS, and a Windows host has neither. So instead
of shelling out, the readers below walk each container far enough to recover
its compatibility profile.

`profile()` is the entry point. Which keys come back depends on the format,
because the formats genuinely record different things:

    all         format, arch
    elf         needs, symbol-versions, runpath, min-kernel
    pe          needs, min-os, subsystem-version
    macho       needs, dylib-compat, rpath, macos-min, code-signature
    archive     nothing beyond format and arch

Dependency names are whatever the format itself stores: a soname for ELF
(`libc.so.6`), an import table entry for PE (`KERNEL32.dll`), an install name
for Mach-O (`/usr/lib/libSystem.B.dylib`).

One thing deliberately absent: the CPU baseline the code was generated for.
Nothing in any of these containers records it unless the toolchain emits a
`.note.gnu.property` marker, and ours does not, so that has to be pinned at
build time instead of checked afterwards.
"""

import re
import struct


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


def profile(path):
    """Everything about the file at `path` that bears on where it will run."""
    with open(path, "rb") as handle:
        data = handle.read()

    if data[:4] == b"\x7fELF":
        return _elf(data)
    if data[:2] == b"MZ":
        return _pe(data)
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

    return {
        "format": "pe",
        "arch": _PE_MACHINES.get(machine, _unknown(machine)),
        "needs": sorted(_pe_imports(data, optional + optional_size, section_count,
                                    struct.unpack_from("<I", data, directories + 8)[0])),
        # The loader refuses to start an image whose subsystem version is above
        # the running Windows; min-os is advisory but recorded alongside it.
        "min-os": f"{os_major}.{os_minor}",
        "subsystem-version": f"{subsystem_major}.{subsystem_minor}",
    }


def _pe_imports(data, section_table, section_count, import_rva):
    if import_rva == 0:
        return []

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

    names = []
    descriptor = to_offset(import_rva)
    while True:
        # A 20 byte all-zero descriptor terminates the import directory.
        entry = struct.unpack_from("<IIIII", data, descriptor)
        if not any(entry):
            break
        names.append(_cstring(data, to_offset(entry[3])))
        descriptor += 20

    return names


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


def _macho(data):
    magic = data[:4]
    order = "<" if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") else ">"
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


def _archive(data):
    """An archive has no link information of its own, but its members have an
    architecture, and that is worth pinning down: it is the only thing telling
    a static library for one platform apart from one for another."""
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
            arch = _member_arch(data[body:body + size])
            if arch is not None:
                return {"format": "archive", "arch": arch}

        position = member_end

    return {"format": "archive", "arch": "unknown"}


def _member_arch(member):
    if member[:4] == b"\x7fELF":
        order = "<" if member[5] == 1 else ">"
        machine = struct.unpack_from(order + "H", member, 0x12)[0]
        return _ELF_MACHINES.get(machine, _unknown(machine))

    if member[:4] in _MACHO_MAGIC:
        order = "<" if member[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") else ">"
        cpu_type, cpu_subtype = struct.unpack_from(order + "iI", member, 4)
        return _macho_arch(cpu_type, cpu_subtype)

    # A COFF object has no magic; a known machine plus the optional header size
    # being zero, as it always is for an object file, is enough to recognise it.
    if len(member) >= 20:
        machine, _sections = struct.unpack_from("<HH", member, 0)
        if machine in _PE_MACHINES and struct.unpack_from("<H", member, 16)[0] == 0:
            return _PE_MACHINES[machine]

    return None
