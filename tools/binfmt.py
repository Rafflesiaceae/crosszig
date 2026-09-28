"""Reads the dynamic library dependencies recorded in a binary.

`dist-check` has to inspect ELF, PE and Mach-O files from whichever host it
happens to run on, and no single system tool covers all three: readelf cannot
read Mach-O, otool only exists on macOS, and a Windows host has neither. So
instead of shelling out, the readers below walk just enough of each container
format to recover the list of libraries the file asks the loader for.

The name reported for a dependency is whatever the format itself stores:
a soname for ELF (`libc.so.6`), an import table entry for PE
(`KERNEL32.dll`), and an install name for Mach-O
(`/usr/lib/libSystem.B.dylib`).
"""

import re
import struct


class UnknownFormat(Exception):
    """Raised for a file whose container format is not recognised."""


def version_tuple(text):
    """`"2.28"` becomes `(2, 28)`, so that versions compare numerically."""
    return tuple(int(part) for part in text.split("."))


def dynamic_dependencies(path):
    """Returns the dynamic libraries `path` needs, in the order they appear.

    Static archives are recognised and report no dependencies: an archive is
    just a bag of object files and carries no link information of its own.
    """
    with open(path, "rb") as handle:
        data = handle.read()

    if data[:4] == b"\x7fELF":
        return _elf(data)
    if data[:2] == b"MZ":
        return _pe(data)
    if data[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                    b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"):
        return _macho(data)
    if data[:8] == b"!<arch>\n":
        return []

    raise UnknownFormat(f"{path}: unrecognised binary format")


def _cstring(data, offset):
    end = data.index(b"\0", offset)
    return data[offset:end].decode("utf-8", "replace")


def _elf_sections(data):
    """Returns the byte order and the section table as (type, offset, size, link)."""
    is_64 = data[4] == 2
    order = "<" if data[5] == 1 else ">"

    # Section headers are the shortest route to the tables below; walking the
    # program headers instead would mean translating virtual addresses.
    if is_64:
        section_offset = struct.unpack_from(order + "Q", data, 0x28)[0]
        entry_size, count = struct.unpack_from(order + "HH", data, 0x3A)
        fields = order + "II8x8xQQI"  # sh_name, sh_type, .., sh_offset, sh_size, sh_link
    else:
        section_offset = struct.unpack_from(order + "I", data, 0x20)[0]
        entry_size, count = struct.unpack_from(order + "HH", data, 0x2E)
        fields = order + "II4x4xIII"

    sections = []
    for index in range(count):
        _name, kind, offset, size, link = struct.unpack_from(
            fields, data, section_offset + index * entry_size)
        sections.append((kind, offset, size, link))

    return order, is_64, sections


def _elf(data):
    order, is_64, sections = _elf_sections(data)
    dyn_entry = order + ("qQ" if is_64 else "iI")
    entry_width = struct.calcsize(dyn_entry)

    needed = []
    for kind, offset, size, link in sections:
        if kind != 6:  # SHT_DYNAMIC
            continue

        # sh_link of .dynamic points at the string table holding the sonames.
        strtab_offset = sections[link][1]

        for position in range(offset, offset + size, entry_width):
            tag, value = struct.unpack_from(dyn_entry, data, position)
            if tag == 0:  # DT_NULL terminates the array
                break
            if tag == 1:  # DT_NEEDED
                needed.append(_cstring(data, strtab_offset + value))

    return needed


def _pe(data):
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise UnknownFormat("not a PE image")

    coff = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]

    optional = coff + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    # The data directory array sits right after the optional header proper,
    # whose size differs between PE32 and PE32+.
    directories = optional + (112 if magic == 0x20B else 96)
    import_rva = struct.unpack_from("<I", data, directories + 8)[0]  # entry 1
    if import_rva == 0:
        return []

    # Section table follows the optional header and maps RVAs to file offsets.
    sections = []
    table = optional + optional_size
    for index in range(section_count):
        base = table + index * 40
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


# LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, LC_REEXPORT_DYLIB, LC_LOAD_UPWARD_DYLIB
_MACHO_DYLIB_COMMANDS = {0x0C, 0x80000018, 0x8000001F, 0x80000023}


def _macho(data):
    magic = data[:4]
    order = "<" if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") else ">"
    is_64 = magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")

    command_count = struct.unpack_from(order + "I", data, 16)[0]
    position = 32 if is_64 else 28  # mach_header_64 has a trailing reserved field

    names = []
    for _ in range(command_count):
        command, size = struct.unpack_from(order + "II", data, position)
        if command in _MACHO_DYLIB_COMMANDS:
            # The name is a lc_str: an offset from the start of the command.
            name_offset = struct.unpack_from(order + "I", data, position + 8)[0]
            names.append(_cstring(data, position + name_offset))
        position += size

    return names


SHT_GNU_VERNEED = 0x6FFFFFFE

# Symbol version tokens look like `GLIBC_2.16`; `GLIBC_PRIVATE` also appears
# and carries no version, so it has to be filtered out rather than parsed.
_GLIBC_TOKEN = re.compile(r"^GLIBC_(\d+(?:\.\d+)*)$")


def glibc_requirement(path):
    """Highest glibc symbol version the file at `path` needs, e.g. `"2.16"`.

    Returns None when the file is not ELF, or is ELF but references no
    versioned glibc symbol at all. This is the number that decides which
    distributions the binary will actually load on.
    """
    with open(path, "rb") as handle:
        data = handle.read()

    if data[:4] != b"\x7fELF":
        return None

    order, _is_64, sections = _elf_sections(data)

    highest = None
    for kind, offset, _size, link in sections:
        if kind != SHT_GNU_VERNEED:
            continue

        # sh_link points at the string table holding the version names.
        strtab = sections[link][1]

        position = offset
        while True:
            # Elf_Verneed: version, count, file name, offset to the first aux
            # entry, offset to the next verneed. Both offsets are relative to
            # the entry they are stored in.
            _version, count, _file, aux, next_entry = struct.unpack_from(
                order + "HHIII", data, position)

            aux_position = position + aux
            for _ in range(count):
                # Elf_Vernaux: hash, flags, other, name, offset to the next.
                _hash, _flags, _other, name, aux_next = struct.unpack_from(
                    order + "IHHII", data, aux_position)

                match = _GLIBC_TOKEN.match(_cstring(data, strtab + name))
                if match:
                    found = version_tuple(match.group(1))
                    if highest is None or found > highest:
                        highest = found

                if aux_next == 0:
                    break
                aux_position += aux_next

            if next_entry == 0:
                break
            position += next_entry

    return ".".join(str(part) for part in highest) if highest else None
