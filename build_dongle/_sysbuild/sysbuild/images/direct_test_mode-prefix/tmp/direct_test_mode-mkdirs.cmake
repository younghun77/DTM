# Distributed under the OSI-approved BSD 3-Clause License.  See accompanying
# file LICENSE.rst or https://cmake.org/licensing for details.

cmake_minimum_required(VERSION ${CMAKE_VERSION}) # this file comes with cmake

# If CMAKE_DISABLE_SOURCE_CHANGES is set to true and the source directory is an
# existing directory in our source tree, calling file(MAKE_DIRECTORY) on it
# would cause a fatal error, even though it would be a no-op.
if(NOT EXISTS "C:/Users/USER/direct_test_mode")
  file(MAKE_DIRECTORY "C:/Users/USER/direct_test_mode")
endif()
file(MAKE_DIRECTORY
  "C:/Users/USER/direct_test_mode/build_dongle/direct_test_mode"
  "C:/Users/USER/direct_test_mode/build_dongle/_sysbuild/sysbuild/images/direct_test_mode-prefix"
  "C:/Users/USER/direct_test_mode/build_dongle/_sysbuild/sysbuild/images/direct_test_mode-prefix/tmp"
  "C:/Users/USER/direct_test_mode/build_dongle/_sysbuild/sysbuild/images/direct_test_mode-prefix/src/direct_test_mode-stamp"
  "C:/Users/USER/direct_test_mode/build_dongle/_sysbuild/sysbuild/images/direct_test_mode-prefix/src"
  "C:/Users/USER/direct_test_mode/build_dongle/_sysbuild/sysbuild/images/direct_test_mode-prefix/src/direct_test_mode-stamp"
)

set(configSubDirs )
foreach(subDir IN LISTS configSubDirs)
    file(MAKE_DIRECTORY "C:/Users/USER/direct_test_mode/build_dongle/_sysbuild/sysbuild/images/direct_test_mode-prefix/src/direct_test_mode-stamp/${subDir}")
endforeach()
if(cfgdir)
  file(MAKE_DIRECTORY "C:/Users/USER/direct_test_mode/build_dongle/_sysbuild/sysbuild/images/direct_test_mode-prefix/src/direct_test_mode-stamp${cfgdir}") # cfgdir has leading slash
endif()
