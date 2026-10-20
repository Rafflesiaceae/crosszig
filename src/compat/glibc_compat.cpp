// Compatibility fillers for glibc versions older than what the bundled C++
// runtime expects. This translation unit is empty on every other platform, so
// it can be listed as a source unconditionally.
//
// glibc grew aligned_alloc in 2.16. zig's prebuilt libc++abi calls it from
// operator new(size_t, align_val_t) and from __aligned_malloc_with_fallback()
// whatever the targeted glibc is, so linking against an older one fails with
// an undefined reference to it. posix_memalign has been there since glibc
// 2.1.91 and has the same semantics apart from reporting failure through its
// return value, which makes it a faithful stand-in.
//
// <stdlib.h> is what defines __GLIBC_MINOR__ here, and it reflects the version
// the target triple asks for rather than the one on the build host.

#include <stdlib.h>

#if defined(__GLIBC__) && (__GLIBC__ < 2 || (__GLIBC__ == 2 && __GLIBC_MINOR__ < 7))

#include <stdarg.h>

// libc++'s money_get calls __isoc99_sscanf for "%Lf", but glibc only added
// that entry point in 2.7. The legacy scanner handles this conversion too.
// This is a fallback for libc++, not a complete C99 scanf implementation:
// legacy glibc interprets %as, %aS and %a[ as allocating string conversions.
// Bind the old symbol explicitly because <stdio.h> redirects vsscanf to the
// equally unavailable __isoc99_vsscanf, even for an older target version.
extern "C" int crosszig_legacy_vsscanf(const char *input, const char *format,
                                      va_list args) noexcept __asm__("vsscanf");

extern "C" int __isoc99_sscanf(const char *input, const char *format, ...) noexcept
{
    va_list args;
    va_start(args, format);
    const int result = crosszig_legacy_vsscanf(input, format, args);
    va_end(args);
    return result;
}

#endif

#if defined(__GLIBC__) && (__GLIBC__ < 2 || (__GLIBC__ == 2 && __GLIBC_MINOR__ < 16))

extern "C" void *aligned_alloc(size_t alignment, size_t size)
{
    void *memory = 0;
    if (posix_memalign(&memory, alignment, size) != 0) {
        return 0;
    }
    return memory;
}

#endif
