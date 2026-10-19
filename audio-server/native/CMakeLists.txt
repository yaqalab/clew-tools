cmake_minimum_required(VERSION 3.20)
# The version is written once, in tools/VERSION (the release script and the installer read it too).
file(READ ${CMAKE_CURRENT_SOURCE_DIR}/../../VERSION CLEW_VERSION)
string(STRIP "${CLEW_VERSION}" CLEW_VERSION)
if(NOT CLEW_VERSION MATCHES "^([0-9]+)\\.([0-9]+)\\.([0-9]+)$")
  message(FATAL_ERROR "tools/VERSION is not MAJOR.MINOR.PATCH: ${CLEW_VERSION}")
endif()
set(CLEW_VERSION_MAJOR ${CMAKE_MATCH_1})
set(CLEW_VERSION_MINOR ${CMAKE_MATCH_2})
set(CLEW_VERSION_PATCH ${CMAKE_MATCH_3})
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/../../VERSION)
project(clew_audio_server VERSION ${CLEW_VERSION} LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Ninja learns each file's headers from MSVC's "Note: including file:" lines, written in the
# language Visual Studio is installed in. CMake reads that line without /utf-8; this project
# compiles with it. In French the line holds a narrow no-break space, which comes out as "?"
# without /utf-8 and as itself with it, so the prefix never matched: no header was tracked, and
# a changed struct left stale objects behind that corrupted the heap at run time (2026-10-16).
# Read the line again the way this project compiles.
if(MSVC AND CMAKE_GENERATOR MATCHES "Ninja")
  set(probe_dir ${CMAKE_BINARY_DIR}/CMakeFiles/showincludes)
  file(MAKE_DIRECTORY ${probe_dir})
  file(WRITE ${probe_dir}/probe.cpp "#include <cstddef>\n")
  execute_process(
    COMMAND ${CMAKE_CXX_COMPILER} /nologo /utf-8 /showIncludes /c probe.cpp
    WORKING_DIRECTORY ${probe_dir}
    OUTPUT_VARIABLE probe_out ERROR_VARIABLE probe_out)
  if(probe_out MATCHES "\n([^\r\n]*:) +[A-Za-z]:[^\r\n]*cstddef")
    set(CMAKE_CL_SHOWINCLUDES_PREFIX "${CMAKE_MATCH_1} ")
  endif()
endif()

if(MSVC)
  # Static C++ runtime: the .exe needs no Visual C++ redistributable.
  set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
  add_compile_options(/W4 /permissive- /utf-8 /sdl /guard:cf /EHsc /Zc:__cplusplus /Qspectre-)
  add_link_options(/guard:cf /DYNAMICBASE /HIGHENTROPYVA /NXCOMPAT /CETCOMPAT)
  add_compile_definitions(WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE _WIN32_WINNT=0x0A00 WINVER=0x0A00)
else()
  add_compile_options(-Wall -Wextra -fstack-protector-strong -D_FORTIFY_SOURCE=2)
endif()

# The portable core: the HTTP server, links, files, mp3 tags, settings, conversion cache.
add_library(clew_core STATIC
  src/core/convert_cache.cpp
  src/core/dictation.cpp
  src/core/files.cpp
  src/core/folders.cpp
  src/core/headless.cpp
  src/core/json.cpp
  src/core/mp3.cpp
  src/core/server.cpp
  src/core/service.cpp
  src/core/settings.cpp
  src/core/sha256.cpp
  src/core/text.cpp
  src/core/update.cpp
)
configure_file(res/version.h.in ${CMAKE_BINARY_DIR}/generated/version.h @ONLY)
target_include_directories(clew_core PUBLIC src/core ${CMAKE_BINARY_DIR}/generated)

# What each system supplies (platform.h).
if(WIN32)
  add_library(clew_platform STATIC src/win/platform_win.cpp src/win/mf_convert.cpp src/win/update_win.cpp)
  target_link_libraries(clew_platform PUBLIC ws2_32 bcrypt crypt32 mfplat mfreadwrite mfuuid wmcodecdspuuid
    ole32 shell32 advapi32 winhttp wintrust)
else()
  # macOS and Linux: compatible, not built or tested yet (README, "Other systems").
  add_library(clew_platform STATIC src/posix/platform_posix.cpp)
  find_package(Threads REQUIRED)
  target_link_libraries(clew_platform PUBLIC Threads::Threads)
endif()
target_link_libraries(clew_core PUBLIC clew_platform)
target_link_libraries(clew_platform PUBLIC clew_core)

if(WIN32)
  add_executable(clew-audio-server WIN32
    src/win/main_win.cpp
    src/win/tray_win.cpp
    src/win/strings.cpp
    res/app.rc
  )
  target_link_libraries(clew-audio-server PRIVATE clew_core clew_platform comctl32 user32 gdi32 shlwapi)
  # The version resource reads the generated version.h.
  target_include_directories(clew-audio-server PRIVATE ${CMAKE_BINARY_DIR}/generated)
  # The manifest is a resource (app.rc), not a step after linking: that step rewrote the fresh exe
  # while an antivirus scan held it open, and failed.
  target_link_options(clew-audio-server PRIVATE /MANIFEST:NO)
  # The resource compiler's includes are not tracked: a changed icon alone must rebuild the resources.
  file(GLOB CLEW_ICONS CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/res/icons/*.ico)
  set_source_files_properties(res/app.rc PROPERTIES OBJECT_DEPENDS "${CLEW_ICONS};${CMAKE_CURRENT_SOURCE_DIR}/res/resource.h;${CMAKE_CURRENT_SOURCE_DIR}/res/app.manifest")
else()
  add_executable(clew-audio-server src/posix/main_posix.cpp)
  target_link_libraries(clew-audio-server PRIVATE clew_core clew_platform)
endif()

enable_testing()
add_executable(unit_tests test/unit_tests.cpp)
target_link_libraries(unit_tests PRIVATE clew_core clew_platform)
add_test(NAME unit_tests COMMAND unit_tests)
if(WIN32)
  # The updater against a stand-in for GitHub (test/test_update.py signs and runs it).
  add_executable(update_check test/update_check.cpp)
  target_link_libraries(update_check PRIVATE clew_core clew_platform)
endif()
