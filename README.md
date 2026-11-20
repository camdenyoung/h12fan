# H12Fan

A Docker-ready web dashboard and network IPMI fan controller for the Supermicro H12SSL-i. Run it in a Proxmox VM, on another server, or on a Raspberry Pi with a 64-bit OS. The container communicates directly with the BMC over LAN; no host IPMI device, privileged container, or Proxmox API access is needed.

## First run

Install Docker Engine and the Docker Compose plugin on the machine that will run the controller:

```sh
git clone https://github.com/camdenyoung/h12fan.git
cd h12fan
cp .env.example .env
docker compose up -d --build
```

Open **http://localhost:8080** on that machine. The initial deployment uses **demo mode**, with simulated sensor readings and controls to test power transitions and a BMC settings reset. No IPMI traffic is sent in demo mode.

To view it from another machine, set `H12FAN_LISTEN_ADDRESS` in `.env` to the Docker host's trusted LAN address. Protect access with `H12FAN_WEB_PASSWORD`. Use a TLS reverse proxy or an SSH tunnel when accessing the dashboard beyond a trusted network; HTTP Basic authentication alone does not encrypt traffic.

## Connect your motherboard

Edit `.env`:

```dotenv
H12FAN_DEMO=false
IPMI_HOST=192.168.1.100
IPMI_USER=fancontroller
IPMI_PASSWORD=your-bmc-password
H12FAN_WEB_PASSWORD=use-a-long-unique-dashboard-password
H12FAN_LISTEN_ADDRESS=127.0.0.1
IPMI_PORT=623
IPMI_CIPHER=17
```

Then recreate the container:

```sh
docker compose up -d
```

Dashboard login is **admin** with the configured `H12FAN_WEB_PASSWORD`. Live mode requires a dashboard password of at least 16 characters. BMC credentials and dashboard credentials are separate. The BMC account needs permission to read sensors/chassis status and execute OEM fan commands and sensor-threshold writes. Check your BMC's account permissions; a read-only account cannot control fans. The host needs network access to the BMC's IPMI UDP port (normally 623). Cipher suite 17 is the default; `IPMI_CIPHER` can be changed for your firmware.

Environment credentials are visible to administrators of the Docker host. The application also supports `IPMI_HOST_FILE`, `IPMI_USER_FILE`, `IPMI_PASSWORD_FILE`, and `H12FAN_WEB_PASSWORD_FILE`; a `_FILE` setting takes precedence over its environment value. To use Docker secrets, add an override such as:

```yaml
# compose.secrets.yaml
services:
  h12fan:
    environment:
      IPMI_PASSWORD_FILE: /run/secrets/ipmi_password
      H12FAN_WEB_PASSWORD_FILE: /run/secrets/web_password
    secrets:
      - ipmi_password
      - web_password
secrets:
  ipmi_password:
    file: ./secrets/ipmi_password
  web_password:
    file: ./secrets/web_password
```

Create the two secret files yourself, restrict access, and run `docker compose -f compose.yaml -f compose.secrets.yaml up -d --build`. Secret files are excluded from Git and the Docker build context. Credentials never appear in dashboard API responses or the persisted fan configuration. IPMI passwords are passed to `ipmitool` through its environment, not command-line arguments.

## Configure control

1. Leave automatic fan control disabled while verifying live telemetry.
2. Choose reference temperature sensors for **Zone 1 (FAN1–FAN7, raw ID 0)** and **Zone 2 (FANA/FANB, raw ID 1)**. Sensor names come from the BMC; only headers actually present on your board appear in telemetry. The zone labels follow the requested grouping; verify the physical wiring and firmware mapping on your server.
3. Choose one sensor per zone, or select several to use their hottest reading. IPMI sensors only: OS-only drive temperatures are not collected in this first version.
4. Edit temperature steps and duty percentages. The graph previews changes immediately. The first duty applies below the first step, and the final duty applies above the final step, until the critical override requests 100%.
5. Set hysteresis and critical overrides appropriate for the hardware. Curves must have strictly increasing temperatures and non-decreasing duty. Duty values are integer percentages from 10–100; the minimum does not guarantee a particular fan will spin reliably.
6. Verify the raw-command profile below for your BMC firmware, select the hardware confirmation checkbox, enable control, and Apply.

The graph distinguishes the edited curve, cooling hysteresis, and the last live applied operating point. The applied point continues to represent the saved configuration until edits are applied. Duty read-back confirms what the BMC reports; actual fan RPM is shown separately. A BMC may override requested duty for its own reasons.

### Raw-command profile

The application implements this fixed Supermicro-style profile; it is **not yet physically verified on your motherboard**:

| Operation | IPMI command suffix |
| --- | --- |
| Read chassis power | `chassis power status` |
| Read sensor readings and thresholds | `sensor list` |
| Read fan mode | `raw 0x30 0x45 0x00` |
| Enter Full/manual mode | `raw 0x30 0x45 0x01 0x01` |
| Return to firmware Standard mode | `raw 0x30 0x45 0x01 0x00` |
| Read zone duty | `raw 0x30 0x70 0x66 0x00 ZONE` |
| Write zone duty | `raw 0x30 0x70 0x66 0x01 ZONE DUTY` |
| Write lower thresholds | `sensor thresh FAN lower LNR LCR LNC` |

Zone and duty are sent as hexadecimal bytes. For example, 50% is `0x32`, and 100% is `0x64`. The controller reads back mode, duty and threshold writes. It seeds both zones at 100% before lowering either on takeover, detects fan-mode resets, and checks duty read-back on every control cycle.

The board manual documents dual fan zones and IPMI monitoring but does not establish this OEM raw-command contract. Supermicro cautions that raw fan commands can vary and recommends its own utilities for some systems. Verify the profile before enabling control:

- [H12SSL motherboard manual](https://www.supermicro.com/manuals/motherboard/EPYC7000/MNL-2314.pdf)
- [Supermicro FAQ on raw fan commands](https://www.supermicro.com/en/support/faqs/faq.php?faq=39476)
- [ipmitool manual and command definitions](https://github.com/ipmitool/ipmitool/blob/master/doc/ipmitool.1.in)

## Lower fan thresholds

Every configured fan starts with desired values **400 / 800 / 1000 RPM**, equivalent to:

```sh
ipmitool sensor thresh FAN1 lower 400 800 1000
```

The order is **lower non-recoverable (LNR), lower critical (LCR), lower non-critical (LNC)**. Change values individually, or copy FAN1's desired values to all configured fans. Save configuration before using **Apply now**, which writes the saved values and verifies the result.

Automatic restoration is independent of temperature-based fan control and is off initially:

1. Establish the actual factory thresholds for your firmware and fans. **Record defaults** explicitly records the current BMC values as the known factory triplet for that fan. Do this only when those readings are known factory values. Do not record your existing custom values as factory defaults. This first version does not ship with an assumed H12 factory threshold table.
2. Enable Auto restore for that fan and Apply configuration.
3. At startup, after power-on/BMC reconnection, and periodically, the service checks the thresholds. It writes only when the current triplet matches the recorded factory values and differs from the desired triplet.
4. Unknown differences are displayed, preserving possible manual changes. Missing fans and unavailable thresholds are skipped. Failed writes are logged and retried at the next check.

Some sensors quantise RPM thresholds. A mismatching read-back is reported as a failure; use values representable by the sensor. Threshold checks wait until the server is on and the startup settling period has elapsed. If settings survive a reboot, no write is needed. Recording defaults is available per fan so different fans can have different baselines.

## Power state and failures

- **Powered off:** The BMC is reachable but chassis power is off. Fan control and threshold writes pause; previous telemetry is cleared.
- **Starting:** After detecting power-on or reconnecting, wait for the configured settling period before restoring thresholds and controlling fans.
- **Monitoring:** Read sensors while automatic duty control is disabled. Threshold restoration can remain enabled separately.
- **Controlling:** Apply each zone's curve to the hottest selected sensor, increasing promptly and lowering with hysteresis.
- **Fault:** Show the error and mark telemetry stale. If the controller owns manual mode, attempt 100% on both zones, then return to firmware Standard mode. A missing selected sensor prevents lowering either zone.
- **Unreachable:** Mark chassis power unknown, rather than assuming it is off. Retry polling.

Disabling control and graceful container shutdown return owned fan control to firmware **Standard** mode. This does not restore the firmware's previous fan mode. Mode writes must be acknowledged before the service reports a successful release.

**There is no guaranteed fallback after network loss, process crash, host failure, or forced termination.** The BMC may retain the last manual duty. Running this on an external device avoids tying its availability to a Proxmox VM, but does not eliminate network or controller failure. Test failure behaviour on your firmware and choose conservative curves. Run only one controller against a BMC; competing tools or containers can fight over settings. This first version does not configure a hardware watchdog.

## Persistence and operations

The named Docker volume `h12fan-data` stores `/data/config.json`, including curves, desired thresholds and explicitly recorded factory baselines. Demo configuration is isolated in `/data/demo/config.json`; enabling curves or confirming hardware in demo mode never enables live control. Preserve or back up this volume. The dashboard's last 100 activity events are held in memory and reset on restart. Credentials are configured at container startup; restart/recreate the container to change them.

```sh
docker compose logs -f
docker compose down
```

The image runs as UID/GID 10001, with a read-only root filesystem, no Linux capabilities and no privileged/device access. Named-volume ownership is prepared by the image; if replacing it with a bind mount, grant UID 10001 write access to that directory. The health check confirms that the web service responds; it does not claim the BMC or cooling is healthy.

Python's official multi-platform base and Debian's `ipmitool` support amd64 and arm64. The CI workflow tests the controller and builds both image architectures without publishing images. Locally build for the Docker host's architecture with Compose. To export a multi-platform image to your own registry, use Docker Buildx with `--platform linux/amd64,linux/arm64` and an appropriate registry tag.

## Development

Python 3.11+; no Python packages or JavaScript build step required. Live operation additionally requires `ipmitool` on PATH.

```sh
python3 -m unittest discover -s tests -v
H12FAN_DEMO=true python3 -m h12fan.server
```

Open http://127.0.0.1:8080. Default local configuration directory is `./data`; override with `H12FAN_DATA`. The container sets it to `/data`. Default local bind is loopback; override with `H12FAN_BIND`. The dashboard polls its API every three seconds; BMC polling defaults to five seconds. All IPMI operations and configuration changes are serialised in one process. Do not run multiple web workers.

This is an initial implementation. Automated tests cover simulated hardware and HTTP behaviour; real BMC command compatibility, sensor names, zone mapping, RPM quantisation and failure behaviour require hardware validation.
