cmake_minimum_required(VERSION 3.25)

project(crosszig VERSION 0.1.0 LANGUAGES CXX)

list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")

# Normally 11; the toolchain file overrides it for targets whose standard
# library cannot go that low. See cmake/targets.json.
set(CROSSZIG_CXX_STANDARD "11" CACHE STRING "C++ standard the sample is built with")

# Single-config generators default to an unoptimised, undefined build type.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Debug CACHE STRING "Build type" FORCE)
endif()

# Collect everything in one place per build directory so that dev.py and
# `cmake --install` have predictable paths to work with.
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib")
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib")
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

include(Dependencies)

add_subdirectory(src)
