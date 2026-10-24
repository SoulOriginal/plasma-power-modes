# hp-wmi-charge

Battery charge control for the HP OMEN 16-wf0 (board 8BAB). It adds the
kernel's standard `charge_behaviour` property to the battery, with two modes:
`auto` (charge as usual) and `inhibit-charge` (do not charge on AC). The
widget's Battery care switch builds a charge limit on top of it.

The module is the kernel's own `hp-wmi` driver with about 150 lines added.
Nothing else in the driver changes, so power profiles, fans, hotkeys and the
widget's Max mode behave exactly as with the stock module.

## What the firmware offers

HP's WMI command `0x2b` reaches two ACPI methods in the BIOS (SSDT, BIOS F.29):
`GBCC` reads the state and `SBCC` sets it. `SBCC` takes a mode in bits 15:8 of
its first data word and sets flags in the embedded controller:

| Mode | Controller flags | Meaning | Used here |
|---|---|---|---|
| `0x00`, `0x04` | all cleared | charge as usual | `auto` |
| `0x05` | `SBIS`, `WPBS` | do not charge on AC | `inhibit-charge` |
| `0x02` | `WACL`, `WPBS` | run from the battery on AC | no |
| `0x06`, `0x07` | controller command `0x45 0x20`, `WTSM` | looks like ship mode | never |

`GBCC` returns a status byte: 0 auto, 1 charging, 2 discharging, 4 to 6
charging held, 0xff no battery. The driver asks the firmware on every read
rather than caching what it last wrote.

Measured on AC at 40 %: `inhibit-charge` turned "Charging" into "Not
charging" within 5 s, and `auto` turned it back within 5 s.

The same command exists on other HP boards (Spectre 8C17 and 8C15, Victus
8DCD), where `charge_behaviour` patches for `hp-wmi` were posted to the
platform-driver-x86 list in 2026.

## Checks before anything changes

Each check below has to pass, otherwise the stock driver stays and the
battery simply has no `charge_behaviour`:

1. The board is 8BAB (DMI).
2. The ACPI methods `\_SB.WMID.GBCC` and `\_SB.WMID.SBCC` exist.
3. The first status read returns a known value.
4. The source matches the running kernel. `prepare-source.sh` takes the
   upstream `hp-wmi.c` for the kernel's version (from kernel.org, or `cache/`),
   builds it unchanged and compares its `srcversion` with the kernel's own
   `hp-wmi` module. A distribution that patched the driver, or a version that
   cannot be matched, stops the build here.
5. The patch applies to that file without fuzz.

DKMS runs the same script before every build, so after a kernel update the
module is rebuilt only if all of this still holds. It needs Linux 6.15 or
newer; the patch applies cleanly to 6.15 through 7.2-rc1.

## Install

Needs `dkms`, `curl` and the headers for the running kernel.

```sh
./install.sh      # build through DKMS, swap the module, keep the power profile
./uninstall.sh    # charging back to auto, stock hp-wmi back
```

Without DKMS, for one boot:

```sh
./build.sh                  # as your user
sudo ./test-read.sh         # load it and show what the firmware reports
sudo ./test-write.sh        # on AC: 20 s of inhibit-charge, then auto
```

The module is not signed. With Secure Boot off it loads and marks the kernel
as tainted; with Secure Boot on it needs to be signed with your own key.

`hp-wmi-charge.patch` is the whole change, made against Linux 6.18.49.
