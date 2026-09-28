# Rule tweaks that have to happen after the platform module has already set the
# link rules, which is exactly what CMAKE_USER_MAKE_RULES_OVERRIDE_<LANG> is
# for. Only the MSVC ABI target pulls this in.
#
# Targeting that ABI makes CMake build lld-link style link lines, and two
# things in them do not survive contact with zig:
#
#   - `/version:<major>.<minor>`. zig's linker insists on three components and
#     rejects two of them with "InvalidVersion", so a trailing `.0` is added.
#   - `-nostartfiles -nostdlib`. CMake passes both and then lists the import
#     libraries by hand, but on this ABI they also drop the CRT itself. lld
#     then falls back to the wide GUI startup object and a console program
#     fails to link with "undefined symbol: wWinMainCRTStartup".

foreach(_rule CMAKE_C_LINK_EXECUTABLE CMAKE_CXX_LINK_EXECUTABLE
              CMAKE_C_CREATE_SHARED_LIBRARY CMAKE_CXX_CREATE_SHARED_LIBRARY
              CMAKE_C_CREATE_SHARED_MODULE CMAKE_CXX_CREATE_SHARED_MODULE)
    if(DEFINED ${_rule})
        string(REPLACE "/version:<TARGET_VERSION_MAJOR>.<TARGET_VERSION_MINOR>"
                       "/version:<TARGET_VERSION_MAJOR>.<TARGET_VERSION_MINOR>.0"
                       ${_rule} "${${_rule}}")
        string(REPLACE " -nostartfiles -nostdlib" "" ${_rule} "${${_rule}}")
    endif()
endforeach()
