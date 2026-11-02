#pragma once

// Symbol visibility of the C++ SDK shared library (libr2kernel_cpp).
//
// The library is compiled with hidden default visibility; only declarations
// marked R2KERNEL_CPP_API form its exported ABI. The SDK build defines
// R2KERNEL_CPP_BUILDING. Consumers include the headers unchanged.

#if defined(_WIN32) || defined(__CYGWIN__)
#if defined(R2KERNEL_CPP_BUILDING)
#define R2KERNEL_CPP_API __declspec(dllexport)
#else
#define R2KERNEL_CPP_API __declspec(dllimport)
#endif
#elif defined(__GNUC__) || defined(__clang__)
#define R2KERNEL_CPP_API __attribute__((visibility("default")))
#else
#define R2KERNEL_CPP_API
#endif
