# The sample project builds twice over: once as a library that holds the actual
# logic, and once as a thin command line binary on top of it.

add_library(crosszig_core STATIC
    log_parser.cpp
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

add_executable(crosszig_cli
    main.cpp
)

target_link_libraries(crosszig_cli PRIVATE crosszig::core)

set_target_properties(crosszig_cli PROPERTIES OUTPUT_NAME crosszig)

install(TARGETS crosszig_core crosszig_cli
    RUNTIME DESTINATION bin
    LIBRARY DESTINATION lib
    ARCHIVE DESTINATION lib
)
install(DIRECTORY include/ DESTINATION include)
