# Fetches Boost through FetchContent. Nothing is expected to be installed on the
# machine; the archive is downloaded once and then shared by every target
# platform build.

include(FetchContent)

set(CROSSZIG_BOOST_VERSION "1.89.0" CACHE STRING "Boost version to fetch")
# Keep this in sync with CROSSZIG_BOOST_VERSION; it is the sha256 of the
# boost-<version>-cmake.tar.xz release asset.
set(CROSSZIG_BOOST_SHA256 "67acec02d0d118b5de9eb441f5fb707b3a1cdd884be00ca24b9a73c995511f74")

set(CROSSZIG_DEPS_CACHE "${CMAKE_SOURCE_DIR}/.cache/deps" CACHE PATH
    "Shared download and extraction cache for fetched dependencies")

set(_boost_src "${CROSSZIG_DEPS_CACHE}/boost-${CROSSZIG_BOOST_VERSION}")

# Boost is a ~100 MB archive that unpacks into a lot of files. Once it is on
# disk, point FetchContent straight at it so that adding another target platform
# does not download or unpack it a second time.
if(EXISTS "${_boost_src}/CMakeLists.txt")
    set(FETCHCONTENT_SOURCE_DIR_BOOST "${_boost_src}")
endif()

# Configure only the libraries we use. Boost resolves their transitive
# dependencies (core, assert, throw_exception, ...) by itself.
set(BOOST_INCLUDE_LIBRARIES regex)
set(BOOST_ENABLE_CMAKE ON)
# Static everywhere, so the cross-compiled binaries are self-contained.
set(BUILD_SHARED_LIBS OFF)

FetchContent_Declare(
    Boost
    URL "https://github.com/boostorg/boost/releases/download/boost-${CROSSZIG_BOOST_VERSION}/boost-${CROSSZIG_BOOST_VERSION}-cmake.tar.xz"
    URL_HASH "SHA256=${CROSSZIG_BOOST_SHA256}"
    DOWNLOAD_DIR "${CROSSZIG_DEPS_CACHE}/download"
    SOURCE_DIR "${_boost_src}"
    DOWNLOAD_EXTRACT_TIMESTAMP ON
    # Boost's own targets are not part of `all` and its headers are treated as
    # system headers, which keeps our warning output about our code only.
    EXCLUDE_FROM_ALL
    SYSTEM
)

FetchContent_MakeAvailable(Boost)
