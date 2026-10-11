cmake_minimum_required(VERSION 3.12)
project(scp_tests C)
set(CMAKE_C_STANDARD 99)
option(SCP_TEST_TRANSFER "Build generated-data transfer test" ON)
if(SCP_TEST_TRANSFER)
  set(SCP_DATASTRUCT
    ../lib/src/hashmap.c ../lib/src/link_list.c
    ../lib/src/queue.c ../lib/src/rbtree.c)
  function(scp_test_target target)
    add_executable(${target} ${ARGN} ${SCP_DATASTRUCT})
    target_include_directories(${target} PRIVATE .. ../lib/include)
    if(WIN32)
      target_include_directories(${target} PRIVATE virtual/compat)
      target_link_libraries(${target} PRIVATE ws2_32)
    else()
      target_link_libraries(${target} PRIVATE m)
    endif()
  endfunction()
  scp_test_target(scp_virtual virtual/case_transfer.c virtual/vnet.c common/transfer.c ../scp.c)
  scp_test_target(scp_udp common/udp_main.c common/udp_runner.c
    common/real_clock.c common/transfer.c ../scp.c)
  scp_test_target(test_lifecycle virtual/case_lifecycle.c common/transfer.c)
  scp_test_target(test_close virtual/case_close.c virtual/vnet.c common/transfer.c ../scp.c)
  enable_testing()
  find_package(Python3 COMPONENTS Interpreter REQUIRED)
    add_test(NAME udp_transfer COMMAND ${Python3_EXECUTABLE}
      ${CMAKE_CURRENT_SOURCE_DIR}/common/check_udp.py $<TARGET_FILE:scp_udp>
      --bytes 8388625 --out ${CMAKE_CURRENT_BINARY_DIR}/results/udp)
    set_tests_properties(udp_transfer PROPERTIES TIMEOUT 120 RUN_SERIAL TRUE)
    foreach(mode prob pacing prob-prob prob-aimd)
      add_test(NAME udp_${mode} COMMAND ${Python3_EXECUTABLE}
        ${CMAKE_CURRENT_SOURCE_DIR}/common/check_udp.py $<TARGET_FILE:scp_udp>
        --bytes 8388625 --mode ${mode} --seconds 3
        --out ${CMAKE_CURRENT_BINARY_DIR}/results/udp-${mode})
      set_tests_properties(udp_${mode} PROPERTIES TIMEOUT 120 RUN_SERIAL TRUE)
    endforeach()
  foreach(case lifecycle close)
    if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
      target_compile_options(test_${case} PRIVATE -UNDEBUG)
    endif()
    add_test(NAME ${case} COMMAND test_${case})
  endforeach()
  file(MAKE_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/results/smoke
    ${CMAKE_CURRENT_BINARY_DIR}/results/loss)
  add_test(NAME transfer_smoke COMMAND scp_virtual --bytes 8388625)
  set_tests_properties(transfer_smoke PROPERTIES
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/results/smoke)
  add_test(NAME transfer_loss COMMAND scp_virtual --bytes 8388625 --loss-ppm 1000 --seed 7)
  set_tests_properties(transfer_loss PROPERTIES
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/results/loss)
endif()
