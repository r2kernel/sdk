# The shared SDK library exports its own API only: no symbol of a statically
# linked archive such as yaml-cpp (cross-audit RELEASE-112-01).
# Usage: cmake -DLIBRARY=<libr2kernel_cpp.so> -DNM=<nm> -P check_exports.cmake
execute_process(
    COMMAND "${NM}" -DC --defined-only "${LIBRARY}"
    OUTPUT_VARIABLE symbols
    ERROR_VARIABLE errors
    RESULT_VARIABLE result
)
if(NOT result EQUAL 0)
    message(FATAL_ERROR "nm failed on ${LIBRARY}: ${errors}")
endif()
string(FIND "${symbols}" "r2kernel::" own)
if(own EQUAL -1)
    message(FATAL_ERROR "${LIBRARY} exports no r2kernel symbol; the check would prove nothing")
endif()
string(REGEX MATCHALL "[^\n]*YAML::[^\n]*" leaked "${symbols}")
list(LENGTH leaked count)
if(count GREATER 0)
    list(GET leaked 0 first)
    message(FATAL_ERROR "${LIBRARY} exports ${count} yaml-cpp symbols, for example: ${first}")
endif()
message(STATUS "${LIBRARY} exports no yaml-cpp symbol")
