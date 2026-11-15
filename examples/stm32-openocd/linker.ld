/* STM32F103C8T6: 64 KiB flash, 20 KiB SRAM. */
ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 64K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 20K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);

/* Reserve 1 KiB stack and 2 KiB heap; shrink these first if the build overflows. */
_min_stack = 0x400;
_min_heap  = 0x800;

SECTIONS
{
  .isr_vector :
  {
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  } > FLASH

  .text :
  {
    . = ALIGN(4);
    *(.text*)
    *(.rodata*)
    . = ALIGN(4);
    _etext = .;
  } > FLASH

  _sidata = LOADADDR(.data);

  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } > RAM AT > FLASH

  .bss :
  {
    . = ALIGN(4);
    _sbss = .;
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > RAM

  /* Fail the link early if RAM is over-committed. */
  ._stack_and_heap_check (NOLOAD) :
  {
    . = ALIGN(8);
    PROVIDE(__heap_start = .);
    . = . + _min_heap;
    PROVIDE(__heap_end = .);
  } > RAM
}
