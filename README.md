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

To avoid a port conflict with another container, set `H12FAN_WEB_PORT` in `.env` (for example, `H12FAN_WEB_PORT=8088`) and run `docker compose up -d` to recreate the container with the new mapping. Then open **http://localhost:8088**. The default host port is 8080; the container's internal port remains 8080.

To view it from another machine, set `H12FAN_LISTEN_ADDRESS` in `.env` to the Docker host's trusted LAN address. Optionally protect access with `H12FAN_WEB_PASSWORD`. Use a TLS reverse proxy or an SSH tunnel when accessing the dashboard beyond a trusted network; HTTP Basic authentication alone does not encrypt traffic.

## Connect your motherboard

Edit `.env`:

```dotenv
H12FAN_DEMO=false
IPMI_HOST=192.168.1.100
IPMI_USER=fancontroller
IPMI_PASSWORD=your-bmc-password
H12FAN_WEB_PASSWORD=use-a-long-unique-dashboard-password
H12FAN_LISTEN_ADDRESS=127.0.0.1
H12FAN_WEB_PORT=8080
IPMI_PORT=623
IPMI_INTERFACE=lanplus
IPMI_CIPHER=3
```

Then recreate the container:

```sh
docker compose up -d
```

Web UI authentication is optional in both demo and live mode. Leave `H12FAN_WEB_PASSWORD` blank or unset for no login. Set any non-empty password to enable login as **admin**; there is no minimum password length. Anyone who can reach the dashboard can view and change its settings when authentication is disabled. BMC credentials remain required for live IPMI access and are separate from the optional dashboard password. The BMC account needs permission to read sensors/chassis status and execute OEM fan commands and sensor-threshold writes. Check your BMC's account permissions; a read-only account cannot control fans. The host needs network access to the BMC's IPMI UDP port (normally 623). `IPMI_INTERFACE` defaults to `lanplus` (IPMI 2.0), with cipher suite 3, matching the confirmed working H12 connection. Set `IPMI_INTERFACE=lan` to use the older IPMI LAN interface; `IPMI_CIPHER` applies only to `lanplus`. There is no automatic protocol fallback.

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

## Troubleshooting IPMI connections

The confirmed working connection for this H12 uses `lanplus` with cipher suite 3:

```dotenv
IPMI_INTERFACE=lanplus
IPMI_CIPHER=3
```

These are now the app defaults. If you already have `.env` settings from an earlier version, explicitly change `IPMI_CIPHER=17` to `IPMI_CIPHER=3` and any `IPMI_INTERFACE=lan` to `IPMI_INTERFACE=lanplus`. Existing environment values override new defaults. Apply the changes with `docker compose up -d --build`.

Match the connection options used by your successful host command. In ipmitool, specifying `-H` without `-I` defaults to **lan**, whereas this app defaults to **lanplus**. A working host command such as `ipmitool -H BMC -U USER -P PASSWORD sdr type Temperature` therefore tests a different protocol from the app's default.

To match that command, set this in `.env` and recreate the container:

```dotenv
IPMI_INTERFACE=lan
```

```sh
docker compose up -d --build
```

`lan` uses the older IPMI protocol without payload encryption. `lanplus` supports encryption and stronger authentication through its selected cipher suite. Keep `lanplus` when it works for your BMC. See the [ipmitool interface documentation](https://github.com/ipmitool/ipmitool/blob/master/doc/ipmitool.1.in) and [default-interface selection](https://github.com/ipmitool/ipmitool/blob/master/lib/ipmi_main.c).

To test temperature reads from **inside the container** using the app's actual connection settings and credentials:

```sh
docker compose exec h12fan python -c 'from h12fan.ipmi import IPMI; print(IPMI().run("sdr", "type", "Temperature"))'
```

The app first reads `chassis power status`, then `sensor list`, and only reads/writes OEM fan control when automatic control is enabled. A successful temperature-only command does not test those other operations. Errors now show the failing operation, interface/cipher, exit status, and ipmitool's error details with the configured username/password redacted. Timeouts identify the operation and time limit. The original generic error did not establish whether connectivity, authentication, privileges, or command support caused the failure.

## Configure control

1. Leave automatic fan control disabled while verifying live telemetry.
2. Choose reference temperature sensors for **Zone 1 (FAN1–FAN5, raw ID 0)** and **Zone 2 (FANA/FANB, raw ID 1)**. Sensor names come from the BMC; only headers actually present on your board appear in telemetry. The zone labels follow the requested grouping; verify the physical wiring and firmware mapping on your server.
3. Choose one sensor per zone, or select several to use their hottest reading. IPMI sensors only: OS-only drive temperatures are not collected in this first version.
4. Edit temperature steps and duty percentages. The graph previews changes immediately. The first duty applies below the first step, and the final duty applies above the final step, until the critical override requests 100%.
5. Set hysteresis and critical overrides appropriate for the hardware. Curves must have strictly increasing temperatures and non-decreasing duty. Duty values are integer percentages from 10–100; the minimum does not guarantee a particular fan will spin reliably.
6. Enable automatic fan control and Apply. No hardware acknowledgement checkbox is required.

The graph distinguishes the edited curve, cooling hysteresis, and the last live applied operating point. The live point uses the saved configuration's controlling temperature and the BMC-reported duty until edits are applied. Each zone card shows **Live duty**, polled directly from the BMC whenever the server is powered on, including during startup settling and with automatic control disabled. During control, the requested target is shown separately. Duty read-back confirms what the BMC reports; actual fan RPM is shown separately. Powered-off, disconnected, or failed duty reads show no current percentage rather than retaining an old value. A BMC may override requested duty for its own reasons.

### Raw-command profile

The application implements the following Supermicro command profile, including the duty-cycle commands confirmed for this server:

| Operation | IPMI command suffix |
| --- | --- |
| Read chassis power | `chassis power status` |
| Read sensor readings and thresholds | `sensor list` |
| Read fan mode | `raw 0x30 0x45 0x00` |
| Enter Full/manual mode | `raw 0x30 0x45 0x01 0x01` |
| Return to firmware Standard mode | `raw 0x30 0x45 0x01 0x00` |
| Read zone duty | `raw 0x30 0x70 0x66 0x00 ZONE` |
| Write Zone 0 duty | `raw 0x30 0x70 0x66 0x01 0x00 DUTY` |
| Write Zone 1 duty | `raw 0x30 0x70 0x66 0x01 0x01 DUTY` |
| Write a reported lower threshold | `sensor thresh FAN lnr VALUE` (or `lcr` / `lnc`) |

Zone and duty are sent as hexadecimal bytes. For example, 50% is `0x32`, and 100% is `0x64`. The controller reads back mode, duty and threshold writes. It seeds both zones at 100% before lowering either on takeover, detects fan-mode resets, and checks duty read-back on every control cycle.

The board manual documents dual fan zones and IPMI monitoring but does not establish this OEM raw-command contract. Supermicro cautions that raw fan commands can vary and recommends its own utilities for some systems. Reference documentation:

- [H12SSL motherboard manual](https://www.supermicro.com/manuals/motherboard/EPYC7000/MNL-2314.pdf)
- [Supermicro FAQ on raw fan commands](https://www.supermicro.com/en/support/faqs/faq.php?faq=39476)
- [ipmitool manual and command definitions](https://github.com/ipmitool/ipmitool/blob/master/doc/ipmitool.1.in)

## Lower fan thresholds

Every configured fan starts with desired values **400 / 800 / 1000 RPM**, equivalent to:

```sh
ipmitool sensor thresh FAN1 lower 400 800 1000
```

The order is **lower non-recoverable (LNR), lower critical (LCR), lower non-critical (LNC)**. Change values individually, or copy FAN1's desired values to all configured fans. Save configuration before using **Apply now**, which writes the saved values to the reported threshold slots and verifies the effective result. A fan with LNR/LCR values and an unreported LNC remains available; the LNC input is disabled and that slot is skipped. Fans without an RPM reading remain unavailable.

Automatic restoration is independent of temperature-based fan control and is off initially:

1. Establish the actual factory thresholds for your firmware and fans. **Record defaults** explicitly records the current BMC values as the known factory triplet for that fan. Do this only when those readings are known factory values. Do not record your existing custom values as factory defaults. This first version does not ship with an assumed H12 factory threshold table.
2. Enable Auto restore for that fan and Apply configuration.
3. At startup, after power-on/BMC reconnection, and periodically, the service checks the thresholds. It writes only when the current triplet matches the recorded factory values and differs from the desired triplet.
4. Unknown differences are displayed, preserving possible manual changes. Missing fans and unavailable thresholds are skipped. Empty fan headers do not block zone temperature control. A numeric zero RPM is flagged in telemetry; it cannot by itself distinguish an unused header from a stalled fan. Leave automatic threshold restoration off for unused headers. Failed writes are logged and retried at the next check.

Some sensors quantise RPM thresholds. The service reads linear fan conversion factors from an `ipmitool sdr dump` in temporary storage, caching them for five minutes, and computes the representable target before comparison or writing. For example, a sensor with 140 RPM steps represents requested 400 / 800 RPM as 420 / 840 RPM. The dashboard shows the actual triplet and effective target; `—` means a slot is unreported and skipped. No guessed percentage tolerance is used. If conversion metadata is missing or unsupported, comparison remains strict. A read-back that differs from the effective target remains a failure. Targets that collapse to equal thresholds after rounding or exceed the raw sensor range are rejected.

Known factory baselines can contain unreported slots (stored as `null`). Capturing a partial baseline and automatically restoring its reported thresholds is supported. Matching already-rounded targets does not cause repeated writes. Threshold checks wait until the server is on and the startup settling period has elapsed. If settings survive a reboot, no write is needed. Recording defaults is available per fan so different fans can have different baselines.

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

The named Docker volume `h12fan-data` stores `/data/config.json`, including curves, desired thresholds and explicitly recorded factory baselines. Demo configuration is isolated in `/data/demo/config.json`; enabling curves in demo mode never enables live control. Existing configurations automatically discard obsolete FAN6/FAN7 entries and update the original default zone label while preserving curves and remaining fan thresholds. Preserve or back up this volume. The Activity log records successful app-issued duty changes with the raw zone ID (0 or 1), previous and new percentages, and the reason. Temperature-driven changes include the hottest selected reference sensor and its temperature. Safety changes for takeover, threshold operations, and fault recovery are also labelled. Unchanged polls, unchanged safety writes, and passive BMC changes do not produce duty-change entries; failed writes remain errors rather than being logged as successful changes. If a safety write succeeds while the previous duty cannot be read, the old value is shown as unknown. The latest 100 activity events are saved atomically to `/data/activity.json` as they occur and loaded on startup. History survives container restarts, rebuilds, and recreation while the named volume is retained. Demo history is kept separately in `/data/demo/activity.json`. Removing the volume (for example, `docker compose down -v`) removes both settings and history. Existing in-memory history from versions before v0.1.2 cannot be recovered after those containers stop. If history storage fails, the Activity log reports the problem and keeps new events in memory while cooling continues; subsequent events retry saving. Credentials are configured at container startup; restart/recreate the container to change them.

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
