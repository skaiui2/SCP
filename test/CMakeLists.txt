cmake_minimum_required(VERSION 3.12)
project(scp_tests C)
set(CMAKE_C_STANDARD 99)
set(SCP_TEST_LOG_INTERVAL_MS 100 CACHE STRING "Protocol state log interval in milliseconds")
set(SCP_TEST_TICK_SCALE 1000 CACHE STRING "Virtual clock ticks per millisecond (1 or 1000)")
if(NOT SCP_TEST_TICK_SCALE MATCHES "^(1|1000)$")
  message(FATAL_ERROR "Set SCP_TEST_TICK_SCALE to 1 or 1000")
endif()
option(SCP_TEST_TRANSFER "Build generated-data transfer test" ON)
if(SCP_TEST_TRANSFER)
  set(SCP_DATASTRUCT
    ../lib/src/hashmap.c ../lib/src/link_list.c
    ../lib/src/queue.c ../lib/src/rbtree.c)
  function(scp_test_target target)
    add_executable(${target} ${ARGN} ${SCP_DATASTRUCT})
    if(target STREQUAL "scp_udp")
      set(tick_scale 1)
    else()
      set(tick_scale ${SCP_TEST_TICK_SCALE})
    endif()
    math(EXPR dump_interval "${SCP_TEST_LOG_INTERVAL_MS} * ${tick_scale}")
    target_compile_definitions(${target} PRIVATE SCP_DUMP_SS=1
      SCP_TICK_SCALE=${tick_scale}U SCP_DUMP_INTERVAL_MS=${dump_interval}U)
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
  scp_test_target(test_handshake virtual/case_handshake.c virtual/vnet.c common/transfer.c ../scp.c)
  scp_test_target(test_pacing_startup virtual/case_pacing_startup.c)
  scp_test_target(test_network virtual/case_network.c virtual/vnet.c common/transfer.c)
  enable_testing()
  find_package(Python3 COMPONENTS Interpreter REQUIRED)
  foreach(drops 1 2)
    add_test(NAME handshake_ack_loss_${drops} COMMAND test_handshake ${drops})
    set_tests_properties(handshake_ack_loss_${drops} PROPERTIES LABELS virtual TIMEOUT 30)
  endforeach()
    add_test(NAME udp_transfer COMMAND ${Python3_EXECUTABLE}
      ${CMAKE_CURRENT_SOURCE_DIR}/run.py $<TARGET_FILE:scp_udp>
      --no-plot --bytes 8388625 --out ${CMAKE_CURRENT_BINARY_DIR}/results/udp)
    set_tests_properties(udp_transfer PROPERTIES TIMEOUT 120 RUN_SERIAL TRUE LABELS real)
    foreach(mode prob pacing prob-prob prob-aimd)
      add_test(NAME udp_${mode} COMMAND ${Python3_EXECUTABLE}
        ${CMAKE_CURRENT_SOURCE_DIR}/run.py $<TARGET_FILE:scp_udp>
        --no-plot --bytes 8388625 --mode ${mode} --seconds 3
        --out ${CMAKE_CURRENT_BINARY_DIR}/results/udp-${mode})
      set_tests_properties(udp_${mode} PROPERTIES TIMEOUT 120 RUN_SERIAL TRUE LABELS real)
    endforeach()
  add_test(NAME udp_file COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/run.py $<TARGET_FILE:scp_udp>
    --file-test --bytes 8388625 --no-plot
    --out ${CMAKE_CURRENT_BINARY_DIR}/results/udp-file)
  set_tests_properties(udp_file PROPERTIES TIMEOUT 120 RUN_SERIAL TRUE LABELS real)
  foreach(case lifecycle close pacing_startup network)
    if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
      target_compile_options(test_${case} PRIVATE -UNDEBUG)
    endif()
    add_test(NAME ${case} COMMAND test_${case})
    set_tests_properties(${case} PROPERTIES LABELS virtual)
  endforeach()
  file(MAKE_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/results/smoke
    ${CMAKE_CURRENT_BINARY_DIR}/results/loss
    ${CMAKE_CURRENT_BINARY_DIR}/results/prob
    ${CMAKE_CURRENT_BINARY_DIR}/results/fixed-feedback)
  add_test(NAME transfer_smoke COMMAND scp_virtual --bytes 8388625)
  set_tests_properties(transfer_smoke PROPERTIES
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/results/smoke LABELS virtual)
  add_test(NAME transfer_loss COMMAND scp_virtual --bytes 8388625 --loss-ppm 1000 --seed 7)
  set_tests_properties(transfer_loss PROPERTIES
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/results/loss LABELS virtual)
  add_test(NAME transfer_fixed_feedback COMMAND scp_virtual --bytes 8388625 --mbps 0 --delay-ms 50)
  set_tests_properties(transfer_fixed_feedback PROPERTIES
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/results/fixed-feedback LABELS virtual)
  add_test(NAME transfer_prob COMMAND scp_virtual --bytes 8388625 --cc 0)
  set_tests_properties(transfer_prob PROPERTIES
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/results/prob LABELS virtual TIMEOUT 120)
endif()
