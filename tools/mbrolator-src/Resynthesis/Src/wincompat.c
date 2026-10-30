/* Minimal POSIX drand48() for MinGW-w64 builds of MBROLATOR. */
#include <stdint.h>

static uint64_t rand48_state = 0x1234abcd330eULL;

double drand48(void)
{
	rand48_state = (rand48_state * 0x5DEECE66DULL + 0xBULL) & 0xFFFFFFFFFFFFULL;
	return (double)rand48_state / 281474976710656.0;
}
