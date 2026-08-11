# etchless

Web-based CAM for milling PCBs on a CNC: upload a copper Gerber, an Excellon drill
file, and an edge-cuts Gerber, get back verified G-code for **isolation milling**,
**drilling**, and **board cutout** — with a toolpath preview rendered from the *same*
parsed geometry that generates the G-code, so what you see is what the machine runs.

Built on [gerbonara](https://gerbolyze.gitlab.io/gerbonara/) (Gerber/Excellon parsing)
and [shapely](https://shapely.readthedocs.io/) (polygon union/offset).

## Run locally (Windows, Linux, macOS)

```
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8321
```

Open http://localhost:8321. "Try sample board" processes the bundled sample.

## Run in Docker / Kubernetes

```
docker build -t etchless .
docker run -p 8000:8000 etchless
```

Kubernetes manifests are in `k8s/` (Deployment + Service + Ingress — set your own
host and image). The app is stateless; uploads are processed in a temp dir and
discarded.

## Features

- Full RS-274X support via gerbonara: apertures, macros, arcs, regions,
  dark/clear polarity (pours with clearances render correctly).
- Isolation milling with three strategies:
  - **Contour passes** — N offset passes hugging the copper, configurable overlap.
  - **Voronoi midline** — one cut equidistant between neighboring nets; every net
    keeps maximum copper, no floating slivers, shortest milling time.
  - **Rubout** — clear ALL open copper, nothing floating remains.
- End mill or V-bit (effective diameter computed from V angle, tip diameter, and
  cut depth).
- Copper margin: grow all copper by X mm before cutting, as a guard band against
  over-cutting (warns if the margin closes a gap between different nets).
- Warnings when copper features sit closer together than the tool diameter
  (potential short), when rubout leaves unreachable slivers, and when a voronoi
  midline is so tight the tool nibbles both neighbors.
- Drilling: holes grouped per diameter, nearest-neighbor ordered, `M0` tool-change
  pause between sizes.
- Optional drill spotting: while the isolation tool is still in the spindle, a
  small divot (configurable depth, default 0.1 mm) is milled at every hole center
  so the drill bit doesn't wander on bare copper.
- Board cutout from the edge-cuts layer: multi-depth passes, holding tabs on the
  final pass.
- Mirror option for bottom-side milling; origin moved to board lower-left.
- **Built-in G-code sender with autoleveling** (Chrome/Edge, via Web Serial):
  connect to a GRBL board straight from the browser tab, jog, zero, probe a grid
  over exactly the isolation area (`G38.2`), and stream any generated file with
  the probed surface applied to every cutting Z (bilinear interpolation).
  Hold / resume / STOP (soft reset) during the run. The server never touches the
  serial port — the browser does, so this works against the k8s deployment too.
- `Max segment` splits long moves so leveling (built-in or an external sender
  like OpenCNCPilot or Candle) can follow the surface — **do use autoleveling**:
  copper is 35 µm thin and no board is that flat.
- G-code is plain GRBL-safe metric (`G21 G90 G94`), one file per operation.

## API

`POST /api/process` — multipart form: files `copper`, `drill`, `outline` (each
optional, at least one required) plus tool/feed parameters (see `app/main.py`).
Returns JSON with preview geometry, warnings, stats, and the G-code texts.

`GET /api/sample` — processes the bundled sample board with defaults.

`GET /healthz` — liveness.
