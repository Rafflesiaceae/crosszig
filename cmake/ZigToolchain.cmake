# CMake toolchain file that drives `zig cc` / `zig c++` as a cross compiler.
#
#   cmake -B build/win-64 -S . -G Ninja \
#         --toolchain cmake/ZigToolchain.cmake -DCROSSZIG_TARGET=win-64
#
# CROSSZIG_TARGET is one of the pixi platform names from pixi.toml. Because zig
# ships the headers, the libc stubs and the runtime libraries for every target
# it supports, the same host can produce binaries for all of them without any
# extra SDK being installed.

# CMake includes a toolchain file once per language and once more for every
# try_compile() project it spawns. Everything below is therefore written to be
# idempotent, and the variables the file depends on are forwarded explicitly.
list(APPEND CMAKE_TRY_COMPILE_PLATFORM_VARIABLES
     CROSSZIG_TARGET
     CROSSZIG_ZIG_EXECUTABLE)

if(NOT CROSSZIG_TARGET)
    message(FATAL_ERROR
            "CROSSZIG_TARGET is not set. Pass -DCROSSZIG_TARGET=<linux-64|win-64|osx-64|osx-arm64>.")
endif()

# Map the pixi platform name onto what CMake and zig each want to hear.
if(CROSSZIG_TARGET STREQUAL "linux-64")
    set(CMAKE_SYSTEM_NAME Linux)
    set(CMAKE_SYSTEM_PROCESSOR x86_64)
    # Pinning the glibc version keeps the output runnable on distributions that
    # are older than whatever host happens to build it.
    set(_crosszig_triple "x86_64-linux-gnu.2.28")
elseif(CROSSZIG_TARGET STREQUAL "win-64")
    set(CMAKE_SYSTEM_NAME Windows)
    set(CMAKE_SYSTEM_PROCESSOR AMD64)
    set(_crosszig_triple "x86_64-windows-gnu")
elseif(CROSSZIG_TARGET STREQUAL "osx-64")
    set(CMAKE_SYSTEM_NAME Darwin)
    set(CMAKE_SYSTEM_PROCESSOR x86_64)
    set(_crosszig_triple "x86_64-macos-none")
elseif(CROSSZIG_TARGET STREQUAL "osx-arm64")
    set(CMAKE_SYSTEM_NAME Darwin)
    set(CMAKE_SYSTEM_PROCESSOR arm64)
    set(_crosszig_triple "aarch64-macos-none")
else()
    message(FATAL_ERROR "Unsupported CROSSZIG_TARGET '${CROSSZIG_TARGET}'.")
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

_crosszig_wrapper(_crosszig_cc     zig-cc     cc     -target ${_crosszig_triple})
_crosszig_wrapper(_crosszig_cxx    zig-cxx    c++    -target ${_crosszig_triple})
_crosszig_wrapper(_crosszig_ar     zig-ar     ar)
_crosszig_wrapper(_crosszig_ranlib zig-ranlib ranlib)

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
    foreach(_var EXE SHARED MODULE)
        string(APPEND CMAKE_${_var}_LINKER_FLAGS_INIT
               " -L${_crosszig_zig_lib_dir}/libc/darwin")
    endforeach()
endif()

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
