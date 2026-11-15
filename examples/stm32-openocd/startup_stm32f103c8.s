/* Minimal Cortex-M3 vector table and startup for the STM32F103 example.
 * Keep this file boring: no magic copies, no printf, no heap before main.
 * Symbols _sidata/_sdata/_edata/_sbss/_ebss come from linker.ld.
 */
  .syntax unified
  .cpu cortex-m3
  .thumb

  .section .isr_vector, "a", %progbits
  .type g_pfnVectors, %object
g_pfnVectors:
  .word _estack
  .word Reset_Handler
  .word NMI_Handler
  .word HardFault_Handler
  .word MemManage_Handler
  .word BusFault_Handler
  .word UsageFault_Handler
  .word 0
  .word 0
  .word 0
  .word 0
  .word SVC_Handler
  .word DebugMon_Handler
  .word 0
  .word PendSV_Handler
  .word SysTick_Handler
  .size g_pfnVectors, . - g_pfnVectors

  .section .text.Reset_Handler, "ax", %progbits
  .weak Reset_Handler
  .type Reset_Handler, %function
Reset_Handler:
  ldr   r0, =_estack
  mov   sp, r0
  /* Copy .data from flash to RAM. */
  ldr   r0, =_sdata
  ldr   r1, =_edata
  ldr   r2, =_sidata
  movs  r3, #0
copy_data_loop:
  cmp   r0, r1
  bcs   copy_data_done
  ldr   r4, [r2, r3]
  str   r4, [r0, r3]
  adds  r3, r3, #4
  adds  r0, r0, #4
  b     copy_data_loop
copy_data_done:
  /* Zero .bss. */
  ldr   r0, =_sbss
  ldr   r1, =_ebss
  movs  r3, #0
zero_bss_loop:
  cmp   r0, r1
  bcs   zero_bss_done
  str   r3, [r0]
  adds  r0, r0, #4
  b     zero_bss_loop
zero_bss_done:
  bl    SystemInit
  bl    main
  b     .

  .size Reset_Handler, . - Reset_Handler

  .section .text.Default_Handler, "ax", %progbits
  .type Default_Handler, %function
Default_Handler:
  /* Trap loop: attach a debugger and read PC/LR before changing anything. */
  b .
  .size Default_Handler, . - Default_Handler

  .weak NMI_Handler
  .thumb_set NMI_Handler, Default_Handler
  .weak HardFault_Handler
  .thumb_set HardFault_Handler, Default_Handler
  .weak MemManage_Handler
  .thumb_set MemManage_Handler, Default_Handler
  .weak BusFault_Handler
  .thumb_set BusFault_Handler, Default_Handler
  .weak UsageFault_Handler
  .thumb_set UsageFault_Handler, Default_Handler
  .weak SVC_Handler
  .thumb_set SVC_Handler, Default_Handler
  .weak DebugMon_Handler
  .thumb_set DebugMon_Handler, Default_Handler
  .weak PendSV_Handler
  .thumb_set PendSV_Handler, Default_Handler
  .weak SysTick_Handler
  .thumb_set SysTick_Handler, Default_Handler
