/* Bare-metal STM32F103C8T6 blink without HAL, CMSIS, or vendor headers.
 * Purpose: a reproducible target for the CLI smoke test and for fault drills.
 * PC13 = on-board LED (active low on the Blue Pill).
 */
#include <stdint.h>

#define REG32(addr) (*(volatile uint32_t *)(addr))

#define RCC_APB2ENR (REG32(0x40021018u))
#define GPIOC_CRH   (REG32(0x40011004u))
#define GPIOC_ODR   (REG32(0x4001100Cu))

#define SYST_CSR (REG32(0xE000E010u))
#define SYST_RVR (REG32(0xE000E014u))
#define SYST_CVR (REG32(0xE000E018u))

#define RCC_IOPCEN (1u << 4)

volatile uint32_t g_ms;

void SysTick_Handler(void)
{
    g_ms++;
}

/* Called from Reset_Handler before main. Real projects configure clocks here. */
void SystemInit(void)
{
}

static void delay_ms(uint32_t ms)
{
    uint32_t start = g_ms;
    while ((g_ms - start) < ms) {
        /* busy wait; SysTick keeps g_ms moving */
    }
}

int main(void)
{
    RCC_APB2ENR |= RCC_IOPCEN;

    /* PC13: output push-pull, 2 MHz (CNF13=00, MODE13=10 -> bits 20..23 = 0x2). */
    GPIOC_CRH = (GPIOC_CRH & ~(0xFu << 20)) | (0x2u << 20);

    /* SysTick from the 8 MHz reset clock (HSI), 1 kHz. */
    SYST_RVR = (8000000u / 1000u) - 1u;
    SYST_CVR = 0u;
    SYST_CSR = 0x7u; /* ENABLE | TICKINT | CLKSOURCE(processor clock) */

    for (;;) {
        GPIOC_ODR ^= (1u << 13); /* active low: on when bit is 0 */
        delay_ms(500u);
    }
}
