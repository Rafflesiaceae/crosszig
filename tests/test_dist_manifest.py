"""Regression coverage for ELF loaders omitted from DT_NEEDED."""

import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tools" / "dist-manifest.py"
SPEC = importlib.util.spec_from_file_location("dist_manifest", TOOL)
dist_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dist_manifest)

LOADER = "/lib/ld-linux-aarch64.so.1"
LIBRARIES = ("libc.so.6", "libdl.so.2", "libpthread.so.0")


def elf_image(needs=LIBRARIES, interpreter=LOADER):
    """Build a minimal ARM64 ELF section table without requiring a compiler."""
    strings = b"\0"
    dynamic = b""
    for name in needs:
        dynamic += struct.pack("<qQ", 1, len(strings))  # DT_NEEDED
        strings += name.encode() + b"\0"
    dynamic += struct.pack("<qQ", 0, 0)  # DT_NULL

    # The reader uses section headers, so no executable code or program
    # headers are needed to exercise the real ELF parsing path.
    sections = [("", 0, b"", 0), (".dynstr", 3, strings, 0),
                (".dynamic", 6, dynamic, 1)]
    if interpreter is not None:
        sections.append((".interp", 1, interpreter.encode() + b"\0", 0))
    names = "\0".join([entry[0] for entry in sections] + [".shstrtab", ""])
    sections.append((".shstrtab", 3, names.encode(), 0))

    data = bytearray(64)
    headers = bytearray()
    for name, kind, payload, link in sections:
        headers += struct.pack("<IIQQQQIIQQ", names.index(name), kind, 0, 0,
                               len(data), len(payload), link, 0, 1, 0)
        data += payload
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<16sHHIQQQIHHHHHH", data, 0, ident, 2, 0xB7, 1,
                     0, 0, len(data), 0, 64, 0, 0, 64, len(sections),
                     len(sections) - 1)
    return data + headers


class ElfLoaderTests(unittest.TestCase):
    def test_loader_is_required_with_or_without_needed_entry(self):
        expected = sorted((*LIBRARIES, "ld-linux-aarch64.so.1"))
        for needs in (LIBRARIES, (*LIBRARIES, "ld-linux-aarch64.so.1")):
            with self.subTest(needs=needs):
                profile = dist_manifest._elf(elf_image(needs))
                self.assertEqual(profile["needs"], expected)
                self.assertEqual(profile["interpreter"], LOADER)

    def test_manifest_accepts_loader_omission_only(self):
        recorded = dist_manifest._elf(
            elf_image((*LIBRARIES, "ld-linux-aarch64.so.1")))
        manifest = {"files": {"crosszig-linux-aarch64": recorded}}
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "crosszig-linux-aarch64"
            binary.write_bytes(elf_image())
            self.assertEqual(dist_manifest.check(directory, manifest), [])

            # Normalizing the loader must preserve checks for its full path,
            # additional libraries, missing libraries, and static linkage.
            variants = (
                elf_image(interpreter="/other/ld-linux-aarch64.so.1"),
                elf_image((*LIBRARIES, "libunexpected.so.1")),
                elf_image(("libc.so.6",)),
                elf_image(interpreter=None),
            )
            for data in variants:
                with self.subTest(data=data):
                    binary.write_bytes(data)
                    self.assertTrue(dist_manifest.check(directory, manifest))

    def test_static_image_does_not_gain_loader_dependency(self):
        profile = dist_manifest._elf(elf_image(needs=(), interpreter=None))
        self.assertEqual(profile["needs"], [])
        self.assertIsNone(profile["interpreter"])

    def test_shared_library_keeps_explicit_loader_dependency(self):
        profile = dist_manifest._elf(
            elf_image(needs=("ld-linux-aarch64.so.1",), interpreter=None))
        self.assertEqual(profile["needs"], ["ld-linux-aarch64.so.1"])
        self.assertIsNone(profile["interpreter"])


if __name__ == "__main__":
    unittest.main()
