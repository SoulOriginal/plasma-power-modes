# Measured: HP OMEN 17-ck1xxx (board 8A17)

Core i7-12700H (6P + 8E, 20 threads), RTX 3060 Laptop GPU, BIOS F.15.
Nobara 44 (Fedora) with Linux 7.2.11, Plasma 6.7.5, NVIDIA 595.104.02,
power-profiles-daemon 0.30, nvidia-powerd running. The external display hangs
off the RTX (HDMI), so the dGPU never reaches D3cold while docked.

Measured on 2026-10-22, one line per second: RAPL package watts (from the
widget's own sampler), average MHz, package temperature, governor, EPP,
`platform_profile`, RAPL PL1, `nvidia-smi` power, clocks, temperature,
utilisation and throttle reasons, fan RPM and `pwm1_enable`.

Loads: `7z b 2 -mmt20` (CPU), `glmark2 --off-screen` at 3840x2160 on the RTX
through PRIME offload (GPU), both at once with `stress-ng --cpu 20` (CPU+GPU),
and the same `glmark2` at 1920x1080 on the Intel iGPU.

## Scoreboard

| run | profile | keep-alive | governor | state | 7z MIPS | CPU W avg / max (7z) | glmark2 RTX alone | glmark2 CPU+GPU | GPU W avg / max (CPU+GPU) |
|---|---|---|---|---|---|---|---|---|---|
| normal | balanced | no | powersave | first run after boot | **77 895** | **80.7 / 89.2** | 5615 | 4309 | 97 / 109 |
| max (governor pinned) | performance (held) | yes | performance | warm | 57 563 | 53.0 / 88.4 | 5546 | 3967 | 99 / 118 |
| max | performance (held) | yes | powersave | warm | 68 406 | 55.2 / 85.4 | 5558 | 4362 | 101 / 119 |
| performance, no keep-alive | performance | no | powersave | warm | 69 567 | 57.3 / 85.8 | – | 4004 | 99 / 119 |
| balanced, governor pinned | balanced | yes | performance | warm | 62 365 | 58.5 / 86.1 | – | 4050 | 97 / 115 |
| balanced, kept | balanced | yes | powersave | warm | 65 238 | 53.2 / 87.2 | – | 4267 | 101 / 119 |
| normal again | balanced | no | powersave | warm | 67 979 | 57.3 / 87.6 | – | 4137 | 98 / 114 |
| performance, cold | performance | no | powersave | 8 min idle, 42 °C | 72 235 | 63.4 / 87.7 | – | 4162 | 101 / 118 |
| normal, cold | balanced | no | powersave | 8 min idle, 40 °C, GPU test first | 70 687 | 61.3 / 91.1 | 5640 | 4380 | 99 / 115 |

GPU alone: 113 to 118 W average, 128 to 134 W peaks (Dynamic Boost), 2000 to
2050 MHz, 65 to 68 °C, in every mode. The GPU is never the thing being capped.

## The CPU curve

Every run but the first looks the same, whatever the profile, the governor or
the keep-alive: 82 to 90 W and 3.4 to 3.5 GHz for a window, then a cut to
44 to 45 W and 2.3 to 2.7 GHz that lasts for the rest of the load. The window
is 20 to 28 s on a warm machine, 32 to 37 s after 8 minutes of idle. At the
cut the package is at 59 to 79 °C and goes *down* afterwards: this is not
die-temperature protection.

While capped (sampled every second): RAPL PL1 and PL2 read 115 W on both the
MSR and the MMIO interface, `scaling_max_freq` stays at 4.6 GHz, every
`Processor` cooling device and `intel_powerclamp` stay at 0, thermald is not
installed. Nothing in the OS imposes the limit: the embedded controller does,
over PECI, and Linux cannot see it. 45 W is the i7-12700H's nominal TDP
(`constraint_0_max_power_uw`).

The first run after boot sustained 87 W for the whole 60 s of 7z and then
87 W + 97 W (GPU) for 45 s at 90 °C. Nothing done since has brought that
back, 8-minute cool-downs included. Whether the EC latches a state after a
hot run or tracks a slow chassis sensor (`acpitz` rose from 51 to 72 °C right
before one cut) is not settled; only a reboot-then-measure would tell.

`platform_profile` read back what was written throughout: `performance`
while held, `balanced` otherwise. The 120 s fallback of the 16-wf0 was not
observed here; the cap arrives regardless of the keep-alive.

## What follows for the widget

* Max cannot lift the EC's 45 W cap, in any profile. The honest claim is:
  Max keeps the profile, the fans and Dynamic Boost where you put them, and
  the keep-alive is at worst redundant.
* Pinning the cpufreq governor to `performance` (tried here, not something
  the widget does) is worse under the cap: 57 563 against 68 406 MIPS under
  performance, 62 365 against 65 238 under balanced, and gains nothing
  before it.
* performance against balanced: on a cold machine performance cut at 37 s
  where balanced (first run) did not cut in 60 s; warm, both cut. Within
  noise for the CPU and the same for the GPU.
* Fans: boost ran the fans up at 90 °C (`pwm1_enable=0`, 4627/4781 RPM) and
  `pwm1_enable=2` put them back at once. Left at 0 after a session, they
  stayed at full speed until the next boot: `hp-wmi` (7.x) re-sends "max
  fans" every 90 s until 2 is written, and 8A17 is not in its Victus-S table,
  so 2 is the plain "max fans off" here rather than the request that stops
  the fans on 8BAB.
* `power-saver` is `quiet` (or `cool`) in `platform_profile` here: the
  firmware offers `cool quiet balanced performance`, no `low-power`.
* The iGPU: the panel is on the Intel GPU and OpenGL/Vulkan default to it
  (glmark2 1538 on Intel against 5615 on the RTX). A game on the wrong GPU
  shows the RTX at 15 W and 210 MHz.
