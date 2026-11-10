# On-Demand SDN Slicing in ComNetsEmu

Project for *Networking (mod. 2)*, University of Trento — A.Y. 2025/26
Author: Alessandro Cazzaniga

Network slices that can be **activated and deactivated at runtime** from a CLI,
a web GUI or a REST API, on top of a single Ryu SDN controller. Every slice is
described by a template that identifies its **flows**, its **topology** (the
switch path it is pinned to) and the **percentage of link capacity** it is
guaranteed on that path. The controller enforces the templates with OpenFlow
1.3 rules and Open vSwitch HTB queues, and rejects activations that would
over-commit a link (admission control).

---

## 1. Topology

```
 h1 video client ─┐                                   ┌─ h4 video server
 h2 robot / PLC  ─┤    A1 20 Mb/s 10 ms   A2 20 Mb/s  │
 h3 IoT gateway  ─┴─[s1]──────────[s2]──────────[s4]──┴─ h5 control server
                     │  edge       core        edge│
                     │                             │
                     └──B1 10 Mb/s 2 ms─[s3]─B2────┘
                                         │ core
                                   M1 10 Mb/s 1 ms
                                         │
                                       [s5]── h6 MEC server
                                        edge
```

| element | role |
|---|---|
| `s1`, `s4`, `s5` | **edge** switches: hosts attach here; every uplink carries an HTB QoS with one queue per slice crossing it |
| `s2`, `s3` | **core** switches: emulate link capacity and propagation delay (netem) |
| path A `s1-s2-s4` | high capacity (20 Mbit/s), high latency (RTT ≈ 20 ms) |
| path B `s1-s3-s4` | low capacity (10 Mbit/s), low latency (RTT ≈ 4 ms) |
| branch `s3-s5` | edge-cloud (MEC) site with server `h6` |

Two disjoint paths between the same edges make the *topology* dimension of a
slice meaningful: a slice is not only a share of bandwidth, it is also the
choice of where its traffic flows.

Best-effort traffic (anything not claimed by an active slice) is routed on the
least-cost path (`cost` in `network.json`), so by default h1–h5 talk over
path A and reach h6 through s3.

## 2. Slice templates

`config/slices.json` contains the slice catalogue:

| slice | type | flows | path | share |
|---|---|---|---|---|
| `video` | eMBB | UDP between h1 ↔ h4 | s1-s2-s4 | 60 % (12 Mbit/s) |
| `control` | URLLC | all IP between h2 ↔ h5 | s1-s3-s4 | 30 % (3 Mbit/s) |
| `iot` | mMTC | all IP between h3 ↔ h6 | s1-s3-s5 | 50 % (5 Mbit/s) |
| `backup` | bulk | TCP port 5201 between h1 ↔ h4 | s1-s3-s4 | 40 % (4 Mbit/s) |

```json
"control": {
  "label": "Industrial control",
  "type": "URLLC",
  "queue": 2,
  "color": "#eb6834",
  "endpoints": { "clients": ["h2"], "servers": ["h5"] },
  "traffic": { "proto": "any" },
  "path": ["s1", "s3", "s4"],
  "share": 30
}
```

* `endpoints` + `traffic` identify the flows (`proto` is `any`, `tcp`, `udp`
  or `icmp`; `port` optionally restricts to one server port).
* `path` pins the slice to an explicit sequence of switches.
* `share` is the percentage of **every** link on the path that the slice gets:
  guaranteed (HTB `min-rate`) and capped (HTB `max-rate`).
* `queue` is the OVS queue id used on the edge uplinks.

The templates are validated at start-up (path follows existing links,
endpoints sit on the first/last switch, unique queue ids, share within the
budget, port only with TCP/UDP…); every problem is reported at once.

`video` and `backup` serve the same pair of hosts: the slice is selected by
the **service** (UDP vs TCP 5201), so the same two hosts use two different
paths at the same time.

## 3. How it works

```
           CLI (slicectl / Mininet "slice")        Web GUI
                         │      REST (northbound)     │
                         └──────────┬─────────────────┘
                                    ▼
            ┌───────────────── Ryu: slice_controller.py ─────────────────┐
            │ admission control · rule computation · flow/port stats     │
            └──────────────┬─────────────────────────────────────────────┘
                           │ OpenFlow 1.3 (southbound)
                           ▼
        Open vSwitch  ── edge: set_queue → HTB queues   core: netem
```

**Single source of truth.** `slicing/model.py` reads the two JSON files and
derives everything the data plane and the control plane must agree on: port
numbers (Mininet is told to use exactly those), uplinks, least-cost routes,
the per-port queue plan and the link reservation. No MAC/port table is
written by hand; changing the topology only means editing `network.json`.

**Data plane provisioning** (`network/topology.py`, at start-up):

* core switch ports facing other switches: `tc netem delay <d> rate <bw>`;
* edge uplinks: an OVS `linux-htb` QoS, `max-rate` = link capacity, with
  queue 0 for best effort (guaranteed `min_share` = 10 %, may borrow up to
  the whole link) and one queue per slice crossing that link
  (`min-rate = max-rate = share × capacity`).

Every direction of every slice crosses exactly one edge uplink first, so the
queues police both directions; each core switch adds the delay of the link it
transmits on (expected RTT: path A ≈ 20 ms, path B ≈ 4 ms, h3–h6 ≈ 3 ms; measured in the VM: ≈ 21 ms and ≈ 5 ms).

**Control plane** (`controller/slice_controller.py`):

* fully proactive: when a switch connects it receives best-effort routes for
  IPv4 and for ARP (ARP requests are routed towards the target, never
  flooded, so the looped topology has no broadcast storms);
* `activate` installs, on every switch of the path and for both directions,
  higher-priority rules matching the slice flows, with `set_queue` on the
  edge uplinks; rules are pushed downstream-first;
* `deactivate` removes the slice with **one** cookie-masked `OFPFC_DELETE`
  per switch: the traffic immediately falls back to best effort;
* rule cookies encode the slice queue id and whether the rule is an
  *ingress* rule; flow statistics are requested for ingress rules only, so
  each slice packet is counted exactly once;
* when a switch reconnects, best-effort routes and every active slice are
  re-installed.

**Admission control.** A slice is admitted only if, on every link of its
path, the sum of the shares of the active slices stays within
`100 − min_share` (90 %). With `control` and `iot` active, link B1 is 80 %
reserved, so `backup` (40 %) is rejected with an explicit message; after
deactivating `iot` it is accepted. The templates may therefore over-book a
link, as long as the *active* slices never do.

Rule priorities: best effort 10, slice "all traffic" 100, slice with
protocol 200, slice with protocol and port 300; unmatched packets are
dropped.

## 4. Running it

Requirements: the ComNetsEmu virtual machine (Ubuntu 20.04, Mininet, Open
vSwitch, Ryu 4.34, iperf). No extra packages are needed for the demo;
`matplotlib` is only used to draw the experiment plots.

```bash
# inside the VM
git clone <this repository> ondemand-slicing && cd ondemand-slicing
./run.sh
```

`run.sh` cleans previous state, starts Ryu in the background (log in
`controller.log`), waits for the REST API, then opens the Mininet CLI. Leaving
the CLI stops everything. `./clean.sh` resets Mininet/OVS by hand if a run
was interrupted.

### Mininet CLI

```
slicing> slice list
slicing> slice on control
slicing> h2 ping -c 5 h5          # RTT drops from ~20 ms to ~4 ms
slicing> slice on video
slicing> iperfudp 15M h1 h4         # UDP h1->h4 is the video slice: capped at 12 Mbit/s
slicing> slice on backup           # rejected if control + iot are active
slicing> slice stats
slicing> slice off control
slicing> slice reset
```

### Standalone CLI (from a second terminal)

```bash
./cli/slicectl.py list
./cli/slicectl.py on video control
./cli/slicectl.py stats --watch
./cli/slicectl.py status           # switches, active slices, recent events
./cli/slicectl.py off video
```

### Web GUI

Open `http://<vm-ip>:8080/` (e.g. `http://192.168.56.2:8080/` with the
default host-only network). It shows the topology with the active slices drawn
on their links and the live load of each link, a toggle for every slice,
the reservation of each link against the admission limit, live per-slice
throughput, and the controller event log. Rejected activations are shown
with the controller's reason.

### REST API

| method | path | description |
|---|---|---|
| GET | `/api/topology` | switches, hosts, links |
| GET | `/api/slices` | catalogue, state, link reservation |
| POST | `/api/slices/<name>/activate` | 200, 404 unknown slice, 409 rejected |
| POST | `/api/slices/<name>/deactivate` | 200 / 404 |
| POST | `/api/reset` | deactivate every slice |
| GET | `/api/stats` | Mbit/s per slice and per link direction |
| GET | `/api/status` | connected switches, active slices, event log |

```bash
curl -X POST http://127.0.0.1:8080/api/slices/video/activate
```

## 5. Experiments

```bash
./run.sh --experiments
```

runs three scripted experiments (about 2 minutes) and writes CSV/JSON, a
Markdown summary and plots to `results/<timestamp>/`. Each experiment
switches a slice on and off **while traffic is running**:

| | traffic | toggled slice | what it shows |
|---|---|---|---|
| E1 | video UDP 15 Mbit/s h1→h4 + best-effort UDP flood 20 Mbit/s h3→h4 | `video` | without the slice video and flood split path A evenly; with it the video gets its guaranteed 60 % (12 Mbit/s, 11.6 of iperf payload) and the flood only the rest |
| E2 | bulk TCP h3→h4 fills path A, ping h2→h5 every 200 ms | `control` | queueing delay on the congested path vs. the dedicated low-latency path |
| E3 | activation requests + TCP h1→h4 on ports 5201 and 5003 | `control`, `iot`, `backup` | admission control, and per-service steering of the same host pair |

If matplotlib is not installed for root, plot later with
`python3 experiments/plot_results.py results/<timestamp>`.

## 6. Tests

The model, the rule generation and the controller logic are covered by unit
tests that run without Mininet or Ryu (`tests/fake_ryu.py` stands in for the
Ryu API and enforces the OpenFlow match prerequisites checked by OVS):

```bash
python3 -m unittest discover -s tests -v
```

`tests/flowsim.py` walks packets through the generated flow tables; the tests
check full connectivity for **every admissible combination** of active
slices, the path and queue of each slice, cookie-based removal, admission
control, reconnection and throughput measurement.

## 7. Project structure

```
config/
  network.json          infrastructure: switches, links (bw, delay, cost), hosts
  slices.json           slice templates
slicing/
  model.py              validation, port numbering, routing, queue plan, admission control
  rules.py              abstract OpenFlow rules (best effort + slices)
  client.py             REST client shared by the CLIs
controller/
  slice_controller.py   Ryu app: rule installation, REST API, statistics, GUI hosting
network/
  topology.py           Mininet network, netem/HTB provisioning, CLI with `slice` command
cli/slicectl.py         standalone command line client
gui/                    web interface (HTML/CSS/vanilla JS, served by Ryu)
experiments/            automated measurements and plots
tests/                  unit tests and flow-table simulator
run.sh, clean.sh
```

## 8. Troubleshooting

* **`pingall` fails right after start** — check `./cli/slicectl.py status`:
  all five switches must be connected. Look at `controller.log`.
* **`Address already in use` on 6633/8080** — a previous controller is still
  running: `./clean.sh`.
* **GUI not reachable from the host** — use the VM's host-only address
  (`ip -4 addr` inside the VM), or forward port 8080.
* **Throughput higher than the share** — the QoS is applied at start-up by
  `topology.py`; make sure it was not interrupted (`sudo ovs-vsctl list qos`
  should show five entries).
