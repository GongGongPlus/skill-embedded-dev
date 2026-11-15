# Cross-compilation for bare-metal Cortex-M with the GNU Arm Embedded toolchain.
# Used by examples/stm32-openocd; install the toolchain first and confirm it:
#   arm-none-eabi-gcc --version
set(CMAKE_SYSTEM_NAME Generic)
set(CMAKE_SYSTEM_PROCESSOR arm)

find_program(ARM_GCC arm-none-eabi-gcc REQUIRED)
find_program(ARM_OBJCOPY arm-none-eabi-objcopy REQUIRED)
find_program(ARM_SIZE arm-none-eabi-size REQUIRED)

set(CMAKE_C_COMPILER ${ARM_GCC})
set(CMAKE_ASM_COMPILER ${ARM_GCC})
set(CMAKE_OBJCOPY ${ARM_OBJCOPY} CACHE FILEPATH "")
set(CMAKE_SIZE ${ARM_SIZE} CACHE FILEPATH "")

set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
