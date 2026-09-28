# CMake toolchain file that drives `zig cc` / `zig c++` as a cross compiler.
#
#   cmake -B build/win-64-msvc -S . -G Ninja \
#         --toolchain cmake/ZigToolchain.cmake -DCROSSZIG_TARGET=win-64-msvc
#
# CROSSZIG_TARGET is one of the platforms in targets.json. Because zig
# ships the headers, the libc stubs and the runtime libraries for every target
# it supports, the same host can produce binaries for all of them without any
# extra SDK being installed.

# CMake includes a toolchain file once per language and once more for every
# try_compile() project it spawns. Everything below is therefore written to be
# idempotent, and the variables the file depends on are forwarded explicitly.
list(APPEND CMAKE_TRY_COMPILE_PLATFORM_VARIABLES
     CROSSZIG_TARGET
     CROSSZIG_ZIG_EXECUTABLE
     CMAKE_MSVC_RUNTIME_LIBRARY
     CMAKE_USER_MAKE_RULES_OVERRIDE_C
     CMAKE_USER_MAKE_RULES_OVERRIDE_CXX)

if(NOT CROSSZIG_TARGET)
    message(FATAL_ERROR
            "CROSSZIG_TARGET is not set. Pass -DCROSSZIG_TARGET=<one of the "
            "platforms in cmake/targets.json>.")
endif()

# What each pixi platform maps onto lives in targets.json rather than here,
# because dev.py records those triples in the dist manifest and has to read the
# very same values. One file both sides parse cannot drift; two copies can.
set(_crosszig_targets_file "${CMAKE_CURRENT_LIST_DIR}/targets.json")
file(READ "${_crosszig_targets_file}" _crosszig_targets)

string(JSON _crosszig_platform ERROR_VARIABLE _crosszig_json_error
       GET "${_crosszig_targets}" "platforms" "${CROSSZIG_TARGET}")
if(NOT _crosszig_json_error STREQUAL "NOTFOUND")
    message(FATAL_ERROR
            "Unsupported CROSSZIG_TARGET '${CROSSZIG_TARGET}': ${_crosszig_json_error}")
endif()

string(JSON CMAKE_SYSTEM_NAME GET "${_crosszig_platform}" "system")
string(JSON CMAKE_SYSTEM_PROCESSOR GET "${_crosszig_platform}" "processor")
string(JSON _crosszig_triple GET "${_crosszig_platform}" "triple")

# The instruction set the generated code may assume. zig already defaults to
# `baseline` for a cross target, but saying it out loud keeps it from turning
# into an accident: a binary built for a newer CPU level still links fine and
# only dies at runtime, and nothing in the file format records which level was
# used, so there would be nothing left to check afterwards.
string(JSON _crosszig_cpu GET "${_crosszig_targets}" "cpu")

# Optional per-platform settings; everything below has a sensible default so
# that a platform entry only has to spell out what makes it different.
function(_crosszig_field out_var json field fallback)
    string(JSON _value ERROR_VARIABLE _error GET "${json}" "${field}")
    if(NOT _error STREQUAL "NOTFOUND")
        set(_value "${fallback}")
    endif()
    set(${out_var} "${_value}" PARENT_SCOPE)
endfunction()

# Extra flags a single platform needs. win-64-msvc uses compile-flags to
# silence the -nostdinc++ that some zig wrappers inject on their own; nothing
# currently needs link-flags, but the hook stays because it is where a
# platform specific library or linker option would go.
_crosszig_field(_crosszig_link_flags    "${_crosszig_platform}" "link-flags"    "")
_crosszig_field(_crosszig_compile_flags "${_crosszig_platform}" "compile-flags" "")
_crosszig_field(_crosszig_sdk           "${_crosszig_platform}" "sdk"           "")
_crosszig_field(_crosszig_cxx_standard  "${_crosszig_platform}" "cxx-standard"  "11")
_crosszig_field(_crosszig_cxx_driver    "${_crosszig_platform}" "cxx-driver"    "c++")

set(CROSSZIG_CXX_STANDARD "${_crosszig_cxx_standard}" CACHE STRING
    "C++ standard the sample is built with" FORCE)

# An SDK that has to be fetched before this platform can be built at all.
set(_crosszig_zig_libc "")
if(_crosszig_sdk STREQUAL "xwin")
    find_program(CROSSZIG_PYTHON NAMES python3 python REQUIRED)

    # The SDK is unpacked once per architecture and xwin names those the way
    # the leading component of a zig triple does, so the triple is all the
    # toolchain has to pass on.
    string(REGEX MATCH "^[^-]+" _crosszig_sdk_arch "${_crosszig_triple}")

    execute_process(
        COMMAND "${CROSSZIG_PYTHON}" "${CMAKE_CURRENT_LIST_DIR}/../tools/xwin.py"
                paths --arch "${_crosszig_sdk_arch}"
        OUTPUT_VARIABLE _crosszig_xwin
        ERROR_VARIABLE _crosszig_xwin_error
        RESULT_VARIABLE _crosszig_xwin_result
        OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(NOT _crosszig_xwin_result EQUAL 0)
        message(FATAL_ERROR
                "Target '${CROSSZIG_TARGET}' builds against Microsoft's CRT and Windows "
                "SDK, which are not redistributable and so have to be fetched once:\n"
                "    python tools/xwin.py setup --accept-license\n"
                "${_crosszig_xwin_error}")
    endif()

    # Statically, so that the result needs no C runtime DLL at all. The debug
    # variants of those libraries are not redistributable and xwin does not
    # fetch them, so this stays release in every configuration.
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded")

    # CMake's MSVC style link line carries a two component /version that zig
    # rejects; this hook is where that gets corrected.
    set(CMAKE_USER_MAKE_RULES_OVERRIDE_C "${CMAKE_CURRENT_LIST_DIR}/ZigLinkRules.cmake")
    set(CMAKE_USER_MAKE_RULES_OVERRIDE_CXX "${CMAKE_CURRENT_LIST_DIR}/ZigLinkRules.cmake")

    # zig refuses to target the MSVC ABI unless it is told where that CRT is,
    # and for `zig cc` the only way to say so is the ZIG_LIBC environment
    # variable, which the compiler shims below export.
    string(JSON _crosszig_zig_libc GET "${_crosszig_xwin}" "zig-libc")

    # The Win32 headers are not covered by the libc file above. They go in as
    # standard include directories rather than as flags so that they do not
    # also end up on every link command line.
    string(JSON _crosszig_include_count LENGTH "${_crosszig_xwin}" "include")
    math(EXPR _crosszig_include_last "${_crosszig_include_count} - 1")
    foreach(_crosszig_index RANGE ${_crosszig_include_last})
        string(JSON _crosszig_include GET "${_crosszig_xwin}" "include" ${_crosszig_index})
        list(APPEND CMAKE_C_STANDARD_INCLUDE_DIRECTORIES "${_crosszig_include}")
        list(APPEND CMAKE_CXX_STANDARD_INCLUDE_DIRECTORIES "${_crosszig_include}")
    endforeach()
endif()

find_program(CROSSZIG_ZIG_EXECUTABLE NAMES zig REQUIRED
             DOC "zig compiler used as the cross toolchain")

# Location of zig's bundled library tree (headers, libc stubs, compiler-rt).
# `zig env` prints a path relative to its working directory when it can, hence
# the explicit working directory plus the matching ABSOLUTE base below.
execute_process(
    COMMAND "${CROSSZIG_ZIG_EXECUTABLE}" env
    WORKING_DIRECTORY "${CMAKE_BINARY_DIR}"
    OUTPUT_VARIABLE _crosszig_zig_env
    OUTPUT_STRIP_TRAILING_WHITESPACE
    COMMAND_ERROR_IS_FATAL ANY)
# Matches both the ZON output of zig >= 0.14 and the older JSON output.
string(REGEX MATCH "lib_dir\"?[ \t]*[=:][ \t]*\"([^\"]*)\"" _crosszig_unused "${_crosszig_zig_env}")
get_filename_component(_crosszig_zig_lib_dir "${CMAKE_MATCH_1}" ABSOLUTE
                       BASE_DIR "${CMAKE_BINARY_DIR}")

# CMake wants a single executable per tool, but every zig tool is a sub-command
# of one binary. Small generated shim scripts bridge that gap, and they are also
# the place where the target triple is pinned so that it applies to compiling
# and linking alike.
set(_crosszig_bin_dir "${CMAKE_BINARY_DIR}/zig-toolchain")

if(_crosszig_zig_libc AND CMAKE_HOST_WIN32)
    set(CROSSZIG_WRAPPER_ENV "set \"ZIG_LIBC=${_crosszig_zig_libc}\"")
elseif(_crosszig_zig_libc)
    set(CROSSZIG_WRAPPER_ENV "export ZIG_LIBC=\"${_crosszig_zig_libc}\"")
else()
    set(CROSSZIG_WRAPPER_ENV "")
endif()

function(_crosszig_wrapper out_var name)
    # Everything after `name` becomes the fixed argument list of the shim.
    list(JOIN ARGN " " CROSSZIG_WRAPPER_ARGS)

    if(CMAKE_HOST_WIN32)
        set(_template "${CMAKE_CURRENT_LIST_DIR}/zig-wrapper.bat.in")
        set(_output "${_crosszig_bin_dir}/${name}.bat")
    else()
        set(_template "${CMAKE_CURRENT_LIST_DIR}/zig-wrapper.sh.in")
        set(_output "${_crosszig_bin_dir}/${name}")
    endif()

    # configure_file() rewrites the shim only when its content actually changes,
    # so re-running CMake does not invalidate the whole build.
    configure_file("${_template}" "${_output}" @ONLY
                   FILE_PERMISSIONS
                   OWNER_READ OWNER_WRITE OWNER_EXECUTE
                   GROUP_READ GROUP_EXECUTE
                   WORLD_READ WORLD_EXECUTE)

    set(${out_var} "${_output}" PARENT_SCOPE)
endfunction()

_crosszig_wrapper(_crosszig_cc  zig-cc  cc  -target ${_crosszig_triple} -mcpu=${_crosszig_cpu})
_crosszig_wrapper(_crosszig_cxx zig-cxx ${_crosszig_cxx_driver}
                  -target ${_crosszig_triple} -mcpu=${_crosszig_cpu})
_crosszig_wrapper(_crosszig_ar     zig-ar     ar)
_crosszig_wrapper(_crosszig_ranlib zig-ranlib ranlib)

if(CMAKE_SYSTEM_NAME STREQUAL "Windows")
    # CMake enables the RC language for Windows targets and insists on a real
    # program even when, as here, there is not a single .rc file to compile.
    _crosszig_wrapper(_crosszig_rc zig-rc rc)
    set(CMAKE_RC_COMPILER "${_crosszig_rc}" CACHE FILEPATH "zig rc" FORCE)
endif()

set(CMAKE_C_COMPILER "${_crosszig_cc}")
set(CMAKE_CXX_COMPILER "${_crosszig_cxx}")

# zig reports itself as clang, so CMake would otherwise go looking for a
# stand-alone llvm-ar / llvm-ranlib next to it and not find one.
set(CMAKE_AR "${_crosszig_ar}" CACHE FILEPATH "zig ar" FORCE)
set(CMAKE_RANLIB "${_crosszig_ranlib}" CACHE FILEPATH "zig ranlib" FORCE)
set(CMAKE_C_COMPILER_AR "${_crosszig_ar}" CACHE FILEPATH "zig ar" FORCE)
set(CMAKE_CXX_COMPILER_AR "${_crosszig_ar}" CACHE FILEPATH "zig ar" FORCE)
set(CMAKE_C_COMPILER_RANLIB "${_crosszig_ranlib}" CACHE FILEPATH "zig ranlib" FORCE)
set(CMAKE_CXX_COMPILER_RANLIB "${_crosszig_ranlib}" CACHE FILEPATH "zig ranlib" FORCE)

if(CMAKE_SYSTEM_NAME STREQUAL "Darwin")
    # zig carries its own copy of the macOS libc headers and of the libSystem
    # stub library, so there is no Xcode SDK to point CMake at. Clearing these
    # also stops CMake from shelling out to `xcrun`, which only exists on macOS.
    set(CMAKE_OSX_SYSROOT "" CACHE INTERNAL "handled by zig")
    set(CMAKE_OSX_DEPLOYMENT_TARGET "" CACHE INTERNAL "handled by zig")
    set(CMAKE_OSX_ARCHITECTURES "" CACHE INTERNAL "handled by zig")

    # Some zig distributions (the conda-forge one among them) wrap the compiler
    # and force `-fuse-ld=lld`. That bypasses the code path where zig hands its
    # bundled libSystem.tbd to the linker, and the link then fails with
    # "library not found for -lSystem". Putting the stub directory on the
    # library search path fixes that and is a no-op otherwise.
    string(APPEND _crosszig_link_flags " -L${_crosszig_zig_lib_dir}/libc/darwin")
endif()

# zig enables UBSan for C and C++ in debug builds and satisfies it by linking
# its own runtime. That runtime is Zig standard library code, and it calls
# openat64 and pwritev64, which arrived in glibc 2.4 and 2.10. Every target
# asking for anything older therefore fails to link a debug build at all -
# including the small program CMake links to read the compiler's ABI, which
# is why CMAKE_SIZEOF_VOID_P ends up unset and GNUInstallDirs then warns that
# it cannot pick a default library directory.
#
# Trapping keeps the checks and drops the runtime: a violation becomes an
# illegal instruction instead of a printed diagnostic. This only changes how
# checks that are already on report themselves, so release builds, where zig
# turns them off, are unaffected.
string(APPEND _crosszig_compile_flags " -fsanitize-trap=undefined")

# Plain assignment, not append: this file is re-read once per language and
# again for every try_compile, and appending would stack the flags up.
set(CMAKE_C_FLAGS_INIT "${_crosszig_compile_flags}")
set(CMAKE_CXX_FLAGS_INIT "${_crosszig_compile_flags}")
foreach(_crosszig_kind EXE SHARED MODULE)
    set(CMAKE_${_crosszig_kind}_LINKER_FLAGS_INIT "${_crosszig_link_flags}")
endforeach()

# There is no target sysroot on disk - zig resolves the target headers and
# libraries internally. Pointing CMAKE_FIND_ROOT_PATH at an empty directory
# makes the ONLY modes below actually bite, so that a cross build cannot
# accidentally pick up a host library from /usr while host tools such as ninja
# or python keep resolving normally.
file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/zig-sysroot")
set(CMAKE_FIND_ROOT_PATH "${CMAKE_BINARY_DIR}/zig-sysroot")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM BEFORE)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
