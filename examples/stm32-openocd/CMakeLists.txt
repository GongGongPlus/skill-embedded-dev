cmake_minimum_required(VERSION 3.20)
project(embedded_dev_stm32_blinky C ASM)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

# Pass the toolchain file when configuring:
#   cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/arm-none-eabi.cmake
if(NOT CMAKE_CROSSCOMPILING)
  message(FATAL_ERROR "This example needs cmake/arm-none-eabi.cmake (arm-none-eabi-gcc must be on PATH)")
endif()

set(MCU_FLAGS -mcpu=cortex-m3 -mthumb -mfloat-abi=soft -ffunction-sections -fdata-sections)
add_compile_options(${MCU_FLAGS} -Wall -Wextra -Werror=implicit-function-declaration)
add_link_options(${MCU_FLAGS} -T${CMAKE_SOURCE_DIR}/linker.ld
  -Wl,--gc-sections -Wl,-Map=${CMAKE_BINARY_DIR}/firmware.map -Wl,--print-memory-usage -nostartfiles)

set(STARTUP ${CMAKE_SOURCE_DIR}/startup_stm32f103c8.s)

add_executable(firmware
  src/main.c
  ${STARTUP}
)

set_target_properties(firmware PROPERTIES
  OUTPUT_NAME firmware
  SUFFIX ".elf"
)

add_custom_command(TARGET firmware POST_BUILD
  COMMAND ${CMAKE_OBJCOPY} -O ihex $<TARGET_FILE:firmware> ${CMAKE_BINARY_DIR}/firmware.hex
  COMMAND ${CMAKE_OBJCOPY} -O binary $<TARGET_FILE:firmware> ${CMAKE_BINARY_DIR}/firmware.bin
  COMMENT "Generating firmware.hex and firmware.bin"
)

# size report after every build: flash + RAM must stay inside the linker values
add_custom_command(TARGET firmware POST_BUILD
  COMMAND ${CMAKE_SIZE} $<TARGET_FILE:firmware>
  VERBATIM
)
