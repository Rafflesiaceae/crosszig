#!/usr/bin/env python3
"""Combines thin Mach-O images, or thin static libraries, into one file.

This is the `lipo -create` half of the universal binary format. It exists
because `lipo` ships only with Apple's developer tools, so a macOS artifact
cross compiled anywhere else has no way to reach it, and because the format
is simple enough that shelling out was never worth the dependency.

    python tools/universal.py out/libfoo.a x86_64/libfoo.a arm64/libfoo.a

Apple wraps static libraries in the very same container, so a slice may be
either a thin Mach-O image or an entire ar archive; the two are told apart by
their magic and the archive case reads the architecture off the first object
inside. Only writing lives here. To read a universal file back, including the
profile of each slice, use tools/dist-manifest.py.

Everything here is standard library only and independent of the rest of the
repository, so it also works as a plain script in another checkout.
"""

import argparse
import struct
import sys
from pathlib import Path

# The four thin Mach-O magics: 64 and 32 bit, each little and big endian.
MACHO_MAGIC = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
               b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce")

# Slices start on a 2**14 boundary. That is what arm64 requires and what
# Apple's own tooling uses there; over-aligning the x86-64 slice too costs a
# few kilobytes of padding and saves the writer from having to care.
ALIGNMENT = 14


class NotUniversal(Exception):
    """Raised for an input that cannot become a slice of a universal file."""


def write(sources, destination):
    """Assembles `sources` into the universal file at `destination`."""
    bodies = [Path(source).read_bytes() for source in sources]

    entries = []
    offset = 8 + 20 * len(bodies)
    for body in bodies:
        offset = (offset + (1 << ALIGNMENT) - 1) & ~((1 << ALIGNMENT) - 1)
        cpu_type, cpu_subtype = slice_cpu(body)
        entries.append((cpu_type, cpu_subtype, offset, len(body), ALIGNMENT))
        offset += len(body)

    # The 32 bit header keeps offsets and sizes in four bytes each, and the 64
    # bit one is only worth reaching for if that is not enough, which for
    # these artifacts it always is.
    if offset > 0xFFFFFFFF:
        raise NotUniversal("slices do not fit in a 32 bit universal binary")

    # FAT_MAGIC, slice count, then one 20 byte record per slice. The header is
    # always big endian, whatever the slices inside it are.
    out = bytearray(struct.pack(">II", 0xCAFEBABE, len(bodies)))
    for entry in entries:
        out += struct.pack(">iIIII", *entry)
    for entry, body in zip(entries, bodies):
        out += b"\0" * (entry[2] - len(out))
        out += body

    Path(destination).write_bytes(bytes(out))
    return [entry[0] for entry in entries]


def slice_cpu(body):
    """The (cputype, cpusubtype) pair a universal header records for a slice."""
    if body[:4] in MACHO_MAGIC:
        return _cpu_of_macho(body)

    if body[:8] == b"!<arch>\n":
        for member in _archive_members(body):
            # The symbol table and the long name table are members too, but
            # neither carries Mach-O magic, so looking for the magic is all
            # the filtering this needs.
            if member[:4] in MACHO_MAGIC:
                return _cpu_of_macho(member)

    raise NotUniversal("input is neither a Mach-O image nor an archive of them")


def _cpu_of_macho(image):
    order = "<" if image[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") else ">"
    return struct.unpack_from(order + "iI", image, 4)


def _archive_members(data):
    """Yields the body of each member of an ar archive."""
    position = 8
    while position + 60 <= len(data):
        header = data[position:position + 16].decode("ascii", "replace").strip()
        size = int(data[position + 48:position + 58].decode("ascii", "replace").strip() or 0)
        body = position + 60
        member_end = body + size + (size % 2)  # members are padded to even offsets

        if header.startswith("#1/"):
            # BSD archives, which is what zig ar emits for macOS targets, put
            # a long member name in the first bytes of the member data and
            # record only its length in the header field.
            name_length = int(header[3:])
            body += name_length
            size -= name_length

        yield data[body:body + size]
        position = member_end


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", help="the universal file to write")
    parser.add_argument("input", nargs="+", help="the thin files to combine")
    args = parser.parse_args()

    try:
        written = write(args.input, args.output)
    except (NotUniversal, OSError, ValueError, struct.error) as error:
        sys.exit(f"universal.py: {error}")

    print(f"wrote {args.output} with {len(written)} slices")


if __name__ == "__main__":
    main()
