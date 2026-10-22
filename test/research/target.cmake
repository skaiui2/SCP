set(SCP_RESEARCH_GAIN_Q16 "" CACHE STRING "Fixed Q16 gain; zero uses the native AIMD policy")
set(SCP_RESEARCH_CA "0" CACHE STRING "Start in CA (1) or slow start (0)")
if(NOT SCP_RESEARCH_GAIN_Q16 MATCHES "^[0-9]+$" OR
   SCP_RESEARCH_GAIN_Q16 GREATER 524288)
  message(FATAL_ERROR "Set SCP_RESEARCH_GAIN_Q16 to an integer in [0,524288]")
endif()
if(NOT SCP_RESEARCH_CA MATCHES "^[01]$")
  message(FATAL_ERROR "Set SCP_RESEARCH_CA to 0 or 1")
endif()
scp_test_target(scp_research research/transfer_main.c research/observe.c
  virtual/vnet.c common/transfer.c ../scp.c)
target_compile_definitions(scp_research PRIVATE
  SCP_RESEARCH_GAIN_Q16=${SCP_RESEARCH_GAIN_Q16}U
  SCP_RESEARCH_CA=${SCP_RESEARCH_CA}
  SCP_RECV_LIMIT=1073741824U SEND_WIN_INIT=1073741824U
  RECV_WIN_INIT=1073741824U CWND_WIN_MAX=2147483647U)
