cmake_minimum_required(VERSION 3.20)

project(r2kernel_cpp VERSION 1.0.0 LANGUAGES CXX)

include(CTest)

set(
    R2KERNEL_CPP_RELEASE_VERSION
    "1.0.0"
    CACHE STRING
    "Full R2Kernel C++ SDK release version, including prerelease suffixes."
)

include(GNUInstallDirs)
include(CMakePackageConfigHelpers)

set(
    R2KERNEL_YAML_CPP_LIBRARY
    ""
    CACHE FILEPATH
    "Optional explicit yaml-cpp library path, used by cross builds."
)
set(
    R2KERNEL_YAML_CPP_INCLUDE_DIR
    ""
    CACHE PATH
    "Optional explicit yaml-cpp include directory, used by cross builds."
)

if(NOT R2KERNEL_YAML_CPP_LIBRARY STREQUAL "")
    if(NOT EXISTS "${R2KERNEL_YAML_CPP_LIBRARY}")
        message(FATAL_ERROR "yaml-cpp library not found: ${R2KERNEL_YAML_CPP_LIBRARY}")
    endif()
    set(R2KERNEL_YAML_CPP_TARGET "${R2KERNEL_YAML_CPP_LIBRARY}")
else()
    find_package(yaml-cpp REQUIRED)
    if(TARGET yaml-cpp::yaml-cpp)
        set(R2KERNEL_YAML_CPP_TARGET yaml-cpp::yaml-cpp)
    elseif(TARGET yaml-cpp)
        set(R2KERNEL_YAML_CPP_TARGET yaml-cpp)
    else()
        set(R2KERNEL_YAML_CPP_TARGET ${YAML_CPP_LIBRARIES})
        if(YAML_CPP_INCLUDE_DIR)
            set(R2KERNEL_YAML_CPP_INCLUDE_DIR "${YAML_CPP_INCLUDE_DIR}")
        endif()
    endif()
endif()

if(NOT R2KERNEL_YAML_CPP_INCLUDE_DIR STREQUAL "" AND NOT EXISTS "${R2KERNEL_YAML_CPP_INCLUDE_DIR}")
    message(FATAL_ERROR "yaml-cpp include directory not found: ${R2KERNEL_YAML_CPP_INCLUDE_DIR}")
endif()
set(
    R2KERNEL_RUST_CORE_DIR
    ""
    CACHE PATH
    "Directory containing the built Rust core shared library (libr2kernel_comm.*)."
)
set(
    R2KERNEL_RUST_CORE_INCLUDE_DIR
    ""
    CACHE PATH
    "Directory containing the Rust core C ABI headers (r2kernel_comm.h and r2kernel_comm_control.h)."
)

if(R2KERNEL_RUST_CORE_INCLUDE_DIR STREQUAL "")
    set(_rust_core_include_candidates
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
    )
    if(NOT R2KERNEL_RUST_CORE_DIR STREQUAL "")
        list(APPEND _rust_core_include_candidates
            "${R2KERNEL_RUST_CORE_DIR}/../include"
            "${R2KERNEL_RUST_CORE_DIR}/../../sdk/cpp/include"
            "${R2KERNEL_RUST_CORE_DIR}/../../runtime/core/include"
        )
    endif()
    foreach(_candidate IN LISTS _rust_core_include_candidates)
        if(EXISTS "${_candidate}/r2kernel_comm.h" AND EXISTS "${_candidate}/r2kernel_comm_control.h")
            set(R2KERNEL_RUST_CORE_INCLUDE_DIR "${_candidate}")
            break()
        endif()
    endforeach()
endif()

find_library(
    R2KERNEL_RUST_CORE_LIBRARY
    NAMES r2kernel_comm libr2kernel_comm
    PATHS "${R2KERNEL_RUST_CORE_DIR}"
    NO_DEFAULT_PATH
)

if(NOT R2KERNEL_RUST_CORE_LIBRARY)
    find_library(R2KERNEL_RUST_CORE_LIBRARY NAMES r2kernel_comm libr2kernel_comm)
endif()

if(NOT R2KERNEL_RUST_CORE_LIBRARY)
    message(
        FATAL_ERROR
        "R2Kernel runtime core library not found. Install R2Kernel and set R2KERNEL_RUST_CORE_DIR to <prefix>/runtime/lib, or to the directory that contains a separately built libr2kernel_comm."
    )
endif()

if(
    R2KERNEL_RUST_CORE_INCLUDE_DIR STREQUAL ""
    OR NOT EXISTS "${R2KERNEL_RUST_CORE_INCLUDE_DIR}/r2kernel_comm.h"
    OR NOT EXISTS "${R2KERNEL_RUST_CORE_INCLUDE_DIR}/r2kernel_comm_control.h"
)
    message(
        FATAL_ERROR
        "Rust core C ABI headers not found. Set R2KERNEL_RUST_CORE_INCLUDE_DIR to the directory containing r2kernel_comm.h and r2kernel_comm_control.h."
    )
endif()

find_package(Threads REQUIRED)
if(NOT TARGET r2kernel::comm)
    if(WIN32)
        add_library(r2kernel::comm UNKNOWN IMPORTED GLOBAL)
    else()
        add_library(r2kernel::comm SHARED IMPORTED GLOBAL)
    endif()
    set_target_properties(r2kernel::comm PROPERTIES IMPORTED_LOCATION "${R2KERNEL_RUST_CORE_LIBRARY}")
    if(UNIX AND NOT APPLE)
        # Rust cdylibs need not carry DT_SONAME. Link by basename so neither
        # this SDK nor its consumers acquire an absolute Core DT_NEEDED path.
        set_target_properties(r2kernel::comm PROPERTIES IMPORTED_NO_SONAME TRUE)
    endif()
endif()

# The SDK sources compile once, with hidden default visibility: only
# declarations marked R2KERNEL_CPP_API (include/r2kernel/export.hpp) are
# exported from the shared library. White-box tests link the same objects
# statically through r2kernel_cpp_internal.
add_library(r2kernel_cpp_objects OBJECT
    src/core/client.cpp
    src/core/control_requests.cpp
    src/core/control_registration.cpp
    src/core/control_shm_peer_reset.cpp
    src/core/inherited_fds.cpp
    src/core/json_value.cpp
    src/core/resource_broker_client.cpp
    src/core/sandbox_diagnostics.cpp
    src/wrapper/contract_loader.cpp
    src/wrapper/gateway_bound_control.cpp
    src/wrapper/prepared_codec.cpp
    src/wrapper/logger.cpp
    src/wrapper/manifest.cpp
    src/wrapper/subsystem.cpp
    src/wrapper/subsystem_config.cpp
    src/wrapper/subsystem_control_registration.cpp
    src/wrapper/subsystem_runtime_queries.cpp
    src/wrapper/subsystem_self_binding.cpp
    src/wrapper/subsystem_timing_headers.cpp
)
set_target_properties(r2kernel_cpp_objects PROPERTIES
    POSITION_INDEPENDENT_CODE ON
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN ON
)
target_compile_features(r2kernel_cpp_objects PUBLIC cxx_std_17)
target_compile_definitions(r2kernel_cpp_objects PRIVATE R2KERNEL_CPP_BUILDING)
# A static yaml-cpp (the release builds link libyaml-cpp.a) is compiled against
# with YAML_CPP_STATIC_DEFINE, as yaml-cpp's own CMake package adds for a static
# build. Without it yaml-cpp 0.8 headers give its classes default visibility,
# and the inline members the SDK instantiates are exported from
# libr2kernel_cpp, which --exclude-libs cannot hide (cross-audit RELEASE-112-01).
if(R2KERNEL_YAML_CPP_LIBRARY MATCHES "\\.a$")
    target_compile_definitions(r2kernel_cpp_objects PRIVATE YAML_CPP_STATIC_DEFINE)
endif()
target_include_directories(r2kernel_cpp_objects
    PUBLIC
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
        "${R2KERNEL_RUST_CORE_INCLUDE_DIR}"
    PRIVATE
        ${R2KERNEL_YAML_CPP_INCLUDE_DIR}
)
target_link_libraries(r2kernel_cpp_objects PUBLIC r2kernel::comm PRIVATE ${R2KERNEL_YAML_CPP_TARGET} Threads::Threads)
if(MSVC)
    target_compile_options(r2kernel_cpp_objects PRIVATE /W4)
else()
    target_compile_options(r2kernel_cpp_objects PRIVATE -Wall -Wextra -Wpedantic)
endif()

add_library(r2kernel_cpp SHARED $<TARGET_OBJECTS:r2kernel_cpp_objects>)
add_library(r2kernel::cpp ALIAS r2kernel_cpp)
# 1.0.0 is the ABI baseline: SOVERSION follows the major version, so a
# consumer records DT_NEEDED libr2kernel_cpp.so.1.
set_target_properties(r2kernel_cpp PROPERTIES
    EXPORT_NAME cpp
    VERSION ${PROJECT_VERSION}
    SOVERSION ${PROJECT_VERSION_MAJOR}
)
target_compile_features(r2kernel_cpp PUBLIC cxx_std_17)
target_include_directories(r2kernel_cpp
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<BUILD_INTERFACE:${R2KERNEL_RUST_CORE_INCLUDE_DIR}>
        $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
)
target_link_libraries(r2kernel_cpp PUBLIC r2kernel::comm PRIVATE ${R2KERNEL_YAML_CPP_TARGET} Threads::Threads)
# Only the SDK's own API is exported: a statically linked yaml-cpp (the release
# builds link libyaml-cpp.a) stays internal instead of joining the 1.x ABI
# (cross-audit RELEASE-112-01).
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_options(r2kernel_cpp PRIVATE "LINKER:--exclude-libs,ALL")
endif()

if(BUILD_TESTING)
    if(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND CMAKE_NM)
        add_test(NAME r2kernel_cpp_exports_test
            COMMAND "${CMAKE_COMMAND}" "-DLIBRARY=$<TARGET_FILE:r2kernel_cpp>" "-DNM=${CMAKE_NM}"
                    -P "${CMAKE_CURRENT_SOURCE_DIR}/tests/check_exports.cmake")
    endif()
    # Cases shared with the Python SDK tests (tests/test_sdk_parity_cases.py);
    # absent from a copy of this directory alone.
    set(_parity_cases "${CMAKE_CURRENT_SOURCE_DIR}/../testdata/parity")
    # Golden Control wire fixtures shared with the backend and the Python SDK
    # tests (tests/test_wire_contract_fixtures.py); absent from a copy of this directory alone.
    set(_wire_fixtures "${CMAKE_CURRENT_SOURCE_DIR}/../testdata/wire")
    # The public spec tree beside the SDKs, the installed standard dictionary of
    # the tests that need one; absent from a copy of this directory alone.
    set(_spec_dictionary "${CMAKE_CURRENT_SOURCE_DIR}/../../spec")
    # Test-only static copy of the SDK objects, so white-box tests can reach
    # internal (non-exported) declarations. Never installed.
    add_library(r2kernel_cpp_internal STATIC $<TARGET_OBJECTS:r2kernel_cpp_objects>)
    target_compile_features(r2kernel_cpp_internal PUBLIC cxx_std_17)
    target_compile_definitions(r2kernel_cpp_internal INTERFACE R2KERNEL_CPP_BUILDING)
    target_include_directories(r2kernel_cpp_internal PUBLIC
        "${CMAKE_CURRENT_SOURCE_DIR}/include" "${R2KERNEL_RUST_CORE_INCLUDE_DIR}")
    target_link_libraries(r2kernel_cpp_internal PUBLIC r2kernel::comm ${R2KERNEL_YAML_CPP_TARGET} Threads::Threads)
    # Offline Subsystem regressions sharing tests/subsystem_offline_fixture.hpp.
    foreach(_offline_test IN ITEMS subsystem_lifecycle subsystem_registration subsystem_parity raw_control_metadata
            event_registration_rollback)
        add_executable(r2kernel_cpp_${_offline_test}_test tests/${_offline_test}_test.cpp)
        target_include_directories(r2kernel_cpp_${_offline_test}_test PRIVATE ${R2KERNEL_YAML_CPP_INCLUDE_DIR})
        target_link_libraries(r2kernel_cpp_${_offline_test}_test PRIVATE r2kernel_cpp ${R2KERNEL_YAML_CPP_TARGET})
        add_test(NAME r2kernel_cpp_${_offline_test}_test COMMAND r2kernel_cpp_${_offline_test}_test)
    endforeach()
    if(UNIX)
        find_package(Threads REQUIRED)
        add_executable(r2kernel_cpp_control_result_test tests/control_result_test.cpp src/core/control_requests.cpp src/core/control_registration.cpp
            src/core/control_shm_peer_reset.cpp)
        target_compile_features(r2kernel_cpp_control_result_test PRIVATE cxx_std_17)
        target_compile_options(r2kernel_cpp_control_result_test PRIVATE -UNDEBUG)
        target_include_directories(r2kernel_cpp_control_result_test PRIVATE include "${R2KERNEL_RUST_CORE_INCLUDE_DIR}" ${R2KERNEL_YAML_CPP_INCLUDE_DIR})
        target_link_libraries(r2kernel_cpp_control_result_test PRIVATE "${R2KERNEL_RUST_CORE_LIBRARY}" ${R2KERNEL_YAML_CPP_TARGET} Threads::Threads)
        add_test(NAME r2kernel_cpp_control_result_test COMMAND r2kernel_cpp_control_result_test)
        set(_control_tests control_shm control_prepared control_session control_requests event_receive_growth)
        if(EXISTS "${_parity_cases}")
            list(APPEND _control_tests control_local_policy)
        endif()
        if(EXISTS "${_wire_fixtures}")
            list(APPEND _control_tests wire_contract_fixtures)
        endif()
        foreach(_control_test IN LISTS _control_tests)
            # Tests that include client.cpp also need its cold companion units.
            add_executable(r2kernel_cpp_${_control_test}_test tests/${_control_test}_test.cpp src/core/control_requests.cpp
                src/core/control_registration.cpp src/core/control_shm_peer_reset.cpp)
            target_compile_features(r2kernel_cpp_${_control_test}_test PRIVATE cxx_std_17)
            target_compile_options(r2kernel_cpp_${_control_test}_test PRIVATE -UNDEBUG)
            target_include_directories(r2kernel_cpp_${_control_test}_test PRIVATE include "${R2KERNEL_RUST_CORE_INCLUDE_DIR}" ${R2KERNEL_YAML_CPP_INCLUDE_DIR})
            target_link_libraries(r2kernel_cpp_${_control_test}_test PRIVATE "${R2KERNEL_RUST_CORE_LIBRARY}" ${R2KERNEL_YAML_CPP_TARGET} Threads::Threads)
            add_test(NAME r2kernel_cpp_${_control_test}_test COMMAND r2kernel_cpp_${_control_test}_test)
        endforeach()
        if(EXISTS "${_parity_cases}")
            target_compile_definitions(r2kernel_cpp_control_local_policy_test PRIVATE
                R2KERNEL_SDK_PARITY_DIR="${_parity_cases}")
        endif()
        if(EXISTS "${_wire_fixtures}")
            target_compile_definitions(r2kernel_cpp_wire_contract_fixtures_test PRIVATE
                R2KERNEL_SDK_WIRE_DIR="${_wire_fixtures}")
        endif()
        # Explicit live fixture: requires a running backend, so it is not a CTest.
        add_executable(r2kernel_cpp_raw_control_runtime_fixture tests/raw_control_runtime_fixture.cpp)
        target_link_libraries(r2kernel_cpp_raw_control_runtime_fixture PRIVATE r2kernel_cpp)
    endif()
    add_executable(r2kernel_cpp_prepared_codec_test tests/prepared_codec_test.cpp)
    target_link_libraries(r2kernel_cpp_prepared_codec_test PRIVATE r2kernel_cpp "${R2KERNEL_RUST_CORE_LIBRARY}")
    add_test(NAME r2kernel_cpp_prepared_codec_test COMMAND r2kernel_cpp_prepared_codec_test)
    add_executable(r2kernel_cpp_control_envelope_test tests/control_envelope_test.cpp)
    target_link_libraries(r2kernel_cpp_control_envelope_test PRIVATE r2kernel_cpp "${R2KERNEL_RUST_CORE_LIBRARY}")
    add_test(NAME r2kernel_cpp_control_envelope_test COMMAND r2kernel_cpp_control_envelope_test)
    add_executable(r2kernel_cpp_source_topic_test tests/control_source_topic.cpp)
    target_compile_features(r2kernel_cpp_source_topic_test PRIVATE cxx_std_17)
    target_include_directories(r2kernel_cpp_source_topic_test PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/src/core")
    add_test(NAME r2kernel_cpp_source_topic_test COMMAND r2kernel_cpp_source_topic_test)
    if(UNIX)
        add_executable(r2kernel_cpp_inherited_fds_test tests/inherited_fds_test.cpp)
        target_link_libraries(r2kernel_cpp_inherited_fds_test PRIVATE r2kernel_cpp)
        add_test(NAME r2kernel_cpp_inherited_fds_test COMMAND r2kernel_cpp_inherited_fds_test)
        add_executable(r2kernel_cpp_resource_broker_test tests/resource_broker_test.cpp)
        target_link_libraries(r2kernel_cpp_resource_broker_test PRIVATE r2kernel_cpp Threads::Threads)
        add_test(NAME r2kernel_cpp_resource_broker_test COMMAND r2kernel_cpp_resource_broker_test)
        add_executable(r2kernel_cpp_subsystem_config_test tests/subsystem_config_test.cpp)
        target_link_libraries(r2kernel_cpp_subsystem_config_test PRIVATE r2kernel_cpp)
        add_test(NAME r2kernel_cpp_subsystem_config_test COMMAND r2kernel_cpp_subsystem_config_test)
        add_executable(r2kernel_cpp_gateway_bound_control_test tests/gateway_bound_control_test.cpp)
        target_link_libraries(r2kernel_cpp_gateway_bound_control_test PRIVATE r2kernel_cpp Threads::Threads)
        add_test(NAME r2kernel_cpp_gateway_bound_control_test COMMAND r2kernel_cpp_gateway_bound_control_test)
        add_executable(r2kernel_cpp_subsystem_bound_calls_test tests/subsystem_bound_calls_test.cpp)
        target_include_directories(r2kernel_cpp_subsystem_bound_calls_test PRIVATE ${R2KERNEL_YAML_CPP_INCLUDE_DIR})
        target_link_libraries(r2kernel_cpp_subsystem_bound_calls_test PRIVATE r2kernel_cpp ${R2KERNEL_YAML_CPP_TARGET} Threads::Threads)
        add_test(NAME r2kernel_cpp_subsystem_bound_calls_test COMMAND r2kernel_cpp_subsystem_bound_calls_test)
        add_executable(r2kernel_cpp_subsystem_manifest_test tests/subsystem_manifest_test.cpp)
        target_link_libraries(r2kernel_cpp_subsystem_manifest_test PRIVATE r2kernel_cpp)
        add_test(NAME r2kernel_cpp_subsystem_manifest_test COMMAND r2kernel_cpp_subsystem_manifest_test)
        if(EXISTS "${_parity_cases}")
            add_executable(r2kernel_cpp_sdk_parity_cases_test tests/sdk_parity_cases_test.cpp)
            target_include_directories(r2kernel_cpp_sdk_parity_cases_test PRIVATE ${R2KERNEL_YAML_CPP_INCLUDE_DIR})
            target_link_libraries(r2kernel_cpp_sdk_parity_cases_test PRIVATE r2kernel_cpp ${R2KERNEL_YAML_CPP_TARGET})
            target_compile_definitions(r2kernel_cpp_sdk_parity_cases_test PRIVATE
                R2KERNEL_SDK_PARITY_DIR="${_parity_cases}")
            if(EXISTS "${_spec_dictionary}")
                target_compile_definitions(r2kernel_cpp_sdk_parity_cases_test PRIVATE
                    R2KERNEL_SDK_SPEC_DIR="${_spec_dictionary}")
            endif()
            add_test(NAME r2kernel_cpp_sdk_parity_cases_test COMMAND r2kernel_cpp_sdk_parity_cases_test)
        endif()
        if(EXISTS "${_spec_dictionary}")
            # spatial_ports frames and the reserved standard IDs (Python tests/test_spatial_frame_ports.py).
            add_executable(r2kernel_cpp_spatial_frame_ports_test tests/spatial_frame_ports_test.cpp)
            target_include_directories(r2kernel_cpp_spatial_frame_ports_test PRIVATE ${R2KERNEL_YAML_CPP_INCLUDE_DIR})
            target_link_libraries(r2kernel_cpp_spatial_frame_ports_test PRIVATE r2kernel_cpp_internal)
            target_compile_definitions(r2kernel_cpp_spatial_frame_ports_test PRIVATE
                R2KERNEL_SDK_SPEC_DIR="${_spec_dictionary}")
            add_test(NAME r2kernel_cpp_spatial_frame_ports_test COMMAND r2kernel_cpp_spatial_frame_ports_test)
        endif()
        add_executable(r2kernel_cpp_subsystem_runtime_queries_test tests/subsystem_runtime_queries_test.cpp)
        target_link_libraries(r2kernel_cpp_subsystem_runtime_queries_test PRIVATE r2kernel_cpp Threads::Threads)
        add_test(NAME r2kernel_cpp_subsystem_runtime_queries_test COMMAND r2kernel_cpp_subsystem_runtime_queries_test)
        add_executable(r2kernel_cpp_subsystem_control_plane_test tests/subsystem_control_plane_test.cpp)
        target_include_directories(r2kernel_cpp_subsystem_control_plane_test PRIVATE ${R2KERNEL_YAML_CPP_INCLUDE_DIR})
        target_link_libraries(r2kernel_cpp_subsystem_control_plane_test PRIVATE r2kernel_cpp_internal)
        if(EXISTS "${_wire_fixtures}")
            target_compile_definitions(r2kernel_cpp_subsystem_control_plane_test PRIVATE
                R2KERNEL_SDK_WIRE_DIR="${_wire_fixtures}")
        endif()
        add_test(NAME r2kernel_cpp_subsystem_control_plane_test COMMAND r2kernel_cpp_subsystem_control_plane_test)
        add_executable(r2kernel_cpp_operation_lifetime_test tests/operation_lifetime_test.cpp)
        target_link_libraries(r2kernel_cpp_operation_lifetime_test PRIVATE r2kernel_cpp "${R2KERNEL_RUST_CORE_LIBRARY}" Threads::Threads)
        add_test(NAME r2kernel_cpp_operation_lifetime_test COMMAND r2kernel_cpp_operation_lifetime_test)
        set_tests_properties(r2kernel_cpp_operation_lifetime_test PROPERTIES TIMEOUT 25)
        if(EXISTS "${_wire_fixtures}")
            # Operation Event wire (SDAE) against the Python encoder's golden fixtures.
            add_executable(r2kernel_cpp_operation_event_wire_test tests/operation_event_wire_test.cpp)
            target_include_directories(r2kernel_cpp_operation_event_wire_test PRIVATE ${R2KERNEL_YAML_CPP_INCLUDE_DIR})
            target_link_libraries(r2kernel_cpp_operation_event_wire_test PRIVATE r2kernel_cpp "${R2KERNEL_RUST_CORE_LIBRARY}"
                ${R2KERNEL_YAML_CPP_TARGET} Threads::Threads)
            target_compile_definitions(r2kernel_cpp_operation_event_wire_test PRIVATE
                R2KERNEL_SDK_WIRE_DIR="${_wire_fixtures}")
            add_test(NAME r2kernel_cpp_operation_event_wire_test COMMAND r2kernel_cpp_operation_event_wire_test)
            set_tests_properties(r2kernel_cpp_operation_event_wire_test PROPERTIES TIMEOUT 25)
            # Encoded (JSON-free) Operation and Procedure payloads and the Python
            # provider's event sequences.
            add_executable(r2kernel_cpp_operation_typed_payload_test tests/operation_typed_payload_test.cpp)
            target_include_directories(r2kernel_cpp_operation_typed_payload_test PRIVATE ${R2KERNEL_YAML_CPP_INCLUDE_DIR})
            target_link_libraries(r2kernel_cpp_operation_typed_payload_test PRIVATE r2kernel_cpp "${R2KERNEL_RUST_CORE_LIBRARY}"
                ${R2KERNEL_YAML_CPP_TARGET} Threads::Threads)
            target_compile_definitions(r2kernel_cpp_operation_typed_payload_test PRIVATE
                R2KERNEL_SDK_WIRE_DIR="${_wire_fixtures}")
            add_test(NAME r2kernel_cpp_operation_typed_payload_test COMMAND r2kernel_cpp_operation_typed_payload_test)
            set_tests_properties(r2kernel_cpp_operation_typed_payload_test PROPERTIES TIMEOUT 60)
        endif()
        add_executable(r2kernel_cpp_runtime_contract_types_test tests/runtime_contract_types_test.cpp)
        target_link_libraries(r2kernel_cpp_runtime_contract_types_test PRIVATE r2kernel_cpp Threads::Threads)
        add_test(NAME r2kernel_cpp_runtime_contract_types_test COMMAND r2kernel_cpp_runtime_contract_types_test)
        add_executable(r2kernel_cpp_control_reregistration_test tests/control_reregistration_test.cpp)
        target_include_directories(r2kernel_cpp_control_reregistration_test PRIVATE ${R2KERNEL_YAML_CPP_INCLUDE_DIR})
        target_link_libraries(r2kernel_cpp_control_reregistration_test PRIVATE r2kernel_cpp "${R2KERNEL_RUST_CORE_LIBRARY}" Threads::Threads)
        add_test(NAME r2kernel_cpp_control_reregistration_test COMMAND r2kernel_cpp_control_reregistration_test)
        add_executable(r2kernel_cpp_best_effort_notification_test tests/best_effort_notification_test.cpp)
        target_compile_features(r2kernel_cpp_best_effort_notification_test PRIVATE cxx_std_17)
        target_link_libraries(r2kernel_cpp_best_effort_notification_test PRIVATE Threads::Threads)
        add_test(NAME r2kernel_cpp_best_effort_notification_test COMMAND r2kernel_cpp_best_effort_notification_test)
    endif()
    add_executable(r2kernel_cpp_callback_authority_test tests/callback_authority_test.cpp)
    target_link_libraries(r2kernel_cpp_callback_authority_test PRIVATE r2kernel_cpp_internal)
    add_test(NAME r2kernel_cpp_callback_authority_test COMMAND r2kernel_cpp_callback_authority_test)
endif()

option(R2KERNEL_CPP_BUILD_BENCHMARKS "Build the in-process SDK micro-benchmarks (not CTests)." OFF)
if(R2KERNEL_CPP_BUILD_BENCHMARKS)
    add_executable(r2kernel_cpp_codec_paths_benchmark benchmarks/codec_paths_benchmark.cpp)
    target_link_libraries(r2kernel_cpp_codec_paths_benchmark PRIVATE r2kernel_cpp "${R2KERNEL_RUST_CORE_LIBRARY}")
    add_executable(r2kernel_cpp_json_escape_benchmark benchmarks/json_escape_benchmark.cpp)
    target_compile_features(r2kernel_cpp_json_escape_benchmark PRIVATE cxx_std_17)
    add_executable(r2kernel_cpp_operation_event_payload_benchmark benchmarks/operation_event_payload_benchmark.cpp)
    target_link_libraries(r2kernel_cpp_operation_event_payload_benchmark PRIVATE r2kernel_cpp "${R2KERNEL_RUST_CORE_LIBRARY}")
    add_executable(r2kernel_cpp_procedure_payload_benchmark benchmarks/procedure_payload_benchmark.cpp)
    target_link_libraries(r2kernel_cpp_procedure_payload_benchmark PRIVATE r2kernel_cpp "${R2KERNEL_RUST_CORE_LIBRARY}")
endif()

if(APPLE)
    set_target_properties(r2kernel_cpp PROPERTIES INSTALL_RPATH "@loader_path;@loader_path/../lib")
elseif(UNIX)
    set_target_properties(r2kernel_cpp PROPERTIES INSTALL_RPATH "$ORIGIN:$ORIGIN/../lib")
endif()

install(TARGETS r2kernel_cpp
    EXPORT r2kernel_cppTargets
    ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
    LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
)

install(FILES "${R2KERNEL_RUST_CORE_LIBRARY}" DESTINATION ${CMAKE_INSTALL_LIBDIR})
install(DIRECTORY include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(
    FILES
        "${R2KERNEL_RUST_CORE_INCLUDE_DIR}/r2kernel_comm.h"
        "${R2KERNEL_RUST_CORE_INCLUDE_DIR}/r2kernel_comm_control.h"
    DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}
)

write_basic_package_version_file(
    "${CMAKE_CURRENT_BINARY_DIR}/r2kernel_cppConfigVersion.cmake"
    VERSION ${PROJECT_VERSION}
    COMPATIBILITY SameMajorVersion
)

get_filename_component(_core_library_name "${R2KERNEL_RUST_CORE_LIBRARY}" NAME)
set(R2KERNEL_CPP_CORE_LIBRARY "\${PACKAGE_PREFIX_DIR}/${CMAKE_INSTALL_LIBDIR}/${_core_library_name}")
configure_package_config_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/cmake/r2kernel_cppConfig.cmake.in"
    "${CMAKE_CURRENT_BINARY_DIR}/install/r2kernel_cppConfig.cmake"
    INSTALL_DESTINATION "${CMAKE_INSTALL_LIBDIR}/cmake/r2kernel_cpp"
)
set(R2KERNEL_CPP_CORE_LIBRARY "${R2KERNEL_RUST_CORE_LIBRARY}")
configure_package_config_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/cmake/r2kernel_cppConfig.cmake.in"
    "${CMAKE_CURRENT_BINARY_DIR}/r2kernel_cppConfig.cmake"
    INSTALL_DESTINATION "."
)

install(FILES
    "${CMAKE_CURRENT_BINARY_DIR}/install/r2kernel_cppConfig.cmake"
    "${CMAKE_CURRENT_BINARY_DIR}/r2kernel_cppConfigVersion.cmake"
    DESTINATION "${CMAKE_INSTALL_LIBDIR}/cmake/r2kernel_cpp"
)

install(EXPORT r2kernel_cppTargets
    FILE r2kernel_cppTargets.cmake
    NAMESPACE r2kernel::
    DESTINATION "${CMAKE_INSTALL_LIBDIR}/cmake/r2kernel_cpp"
)

# Source consumers can reuse this configured SDK build without recompiling its
# implementation in each consumer or installing a second copy.
export(EXPORT r2kernel_cppTargets
    FILE "${CMAKE_CURRENT_BINARY_DIR}/r2kernel_cppTargets.cmake"
    NAMESPACE r2kernel::
)
