# The sample project builds twice over: once as a library that holds the actual
# logic, and once as a thin command line binary on top of it.

add_library(crosszig_core STATIC
    log_parser.cpp
    # Fills in libc functions the targeted platform is too old to provide;
    # compiles to nothing where they are present. Part of the library rather
    # than of the binary so that a consumer linking libcrosszig.a gets them too.
    compat/glibc_compat.cpp
)
add_library(crosszig::core ALIAS crosszig_core)

target_include_directories(crosszig_core PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>
)

# PUBLIC because boost/regex.hpp is not exposed by our headers, but Boost.Regex
# is header-only these days and consumers still need its include path.
target_link_libraries(crosszig_core PUBLIC Boost::regex)

set_target_properties(crosszig_core PROPERTIES
    OUTPUT_NAME crosszig
    POSITION_INDEPENDENT_CODE ON
)

# Anything consuming the library needs at least C++11 as well.
target_compile_features(crosszig_core PUBLIC cxx_std_11)

add_executable(crosszig_cli
    main.cpp
)

target_link_libraries(crosszig_cli PRIVATE crosszig::core)

set_target_properties(crosszig_cli PROPERTIES OUTPUT_NAME crosszig)

# Pin the standard rather than letting the compiler default decide;
# CXX_STANDARD_REQUIRED turns a missing -std= into an error instead of a
# silent fallback to a newer one. CROSSZIG_CXX_STANDARD is 11 everywhere
# except where the target's standard library rules that out.
set_target_properties(crosszig_core crosszig_cli PROPERTIES
    CXX_STANDARD ${CROSSZIG_CXX_STANDARD}
    CXX_STANDARD_REQUIRED ON
    CXX_EXTENSIONS OFF
)

install(TARGETS crosszig_core crosszig_cli
    RUNTIME DESTINATION bin
    LIBRARY DESTINATION lib
    ARCHIVE DESTINATION lib
)
install(DIRECTORY include/ DESTINATION include)
