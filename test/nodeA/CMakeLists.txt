cmake_minimum_required(VERSION 3.10)
project(nodeA C)

set(CMAKE_C_STANDARD 99)
set(CMAKE_BUILD_TYPE Debug)

file(GLOB NODEA_SRC
    ${CMAKE_CURRENT_SOURCE_DIR}/src/*.c
)

set(SCP_CORE
    ${CMAKE_CURRENT_SOURCE_DIR}/../../scp.c
)

file(GLOB DATASTRUCT_SRC
    ${CMAKE_CURRENT_SOURCE_DIR}/../../lib/src/*.c
)

add_executable(nodeA
    ${NODEA_SRC}
    ${SCP_CORE}
    ${DATASTRUCT_SRC}
)

target_include_directories(nodeA PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/../../
    ${CMAKE_CURRENT_SOURCE_DIR}/../../lib/include
    /usr/local/include
)

link_directories(/usr/local/lib)

find_package(Threads REQUIRED)

target_link_libraries(nodeA PRIVATE
    Threads::Threads
    rt
    m
    noiseprotocol
    noisekeys
    noiseprotobufs
    sodium
)