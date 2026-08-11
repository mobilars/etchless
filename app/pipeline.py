"""Gerber/Excellon -> shapely geometry -> isolation/drill/outline toolpaths -> G-code."""

import math
from dataclasses import dataclass, field

import shapely
from shapely.geometry import LineString, MultiPoint, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union, substring, linemerge
from shapely import affinity, voronoi_polygons, STRtree

from gerbonara import GerberFile, ExcellonFile, utils
from gerbonara import graphic_primitives as gp

MM = utils.MM

ARC_MAX_ERROR = 0.005  # mm, arc flattening tolerance
QUAD_SEGS = 24         # circle approximation quality for buffers


# ---------------------------------------------------------------- geometry

def _prim_to_shapely(prim):
    if isinstance(prim, gp.Circle):
        return Point(prim.x, prim.y).buffer(prim.r, quad_segs=QUAD_SEGS)
    if isinstance(prim, gp.Line):
        if prim.width <= 0:
            return None
        ls = LineString([(prim.x1, prim.y1), (prim.x2, prim.y2)])
        if ls.length == 0:
            return Point(prim.x1, prim.y1).buffer(prim.width / 2, quad_segs=QUAD_SEGS)
        return ls.buffer(prim.width / 2, cap_style="round", quad_segs=QUAD_SEGS)
    if isinstance(prim, gp.Rectangle):
        b = box(prim.x - prim.w / 2, prim.y - prim.h / 2,
                prim.x + prim.w / 2, prim.y + prim.h / 2)
        if prim.rotation:
            b = affinity.rotate(b, prim.rotation, origin=(prim.x, prim.y), use_radians=True)
        return b
    if isinstance(prim, (gp.Arc, gp.ArcPoly)):
        ap = prim.to_arc_poly() if isinstance(prim, gp.Arc) else prim
        ap = ap.approximate_arcs(max_error=ARC_MAX_ERROR)
        pts = list(ap.outline)
        if len(pts) < 3:
            return None
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        return poly
    return None


def gerber_to_geometry(gerber: GerberFile):
    """Union all objects of a gerber layer into one (Multi)Polygon,
    honoring dark/clear polarity in file order."""
    acc = Polygon()
    run, run_dark = [], True

    def flush():
        nonlocal acc, run
        if not run:
            return
        merged = unary_union(run)
        acc = acc.union(merged) if run_dark else acc.difference(merged)
        run = []

    for obj in gerber.objects:
        for prim in obj.to_primitives(unit=MM):
            g = _prim_to_shapely(prim)
            if g is None or g.is_empty:
                continue
            dark = bool(prim.polarity_dark)
            if dark != run_dark:
                flush()
                run_dark = dark
            run.append(g)
    flush()
    if not acc.is_valid:
        acc = acc.buffer(0)
    return acc


def _as_polygons(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def _rings(geom):
    """All boundary rings of a (multi)polygon as LineStrings."""
    rings = []
    for poly in _as_polygons(geom):
        rings.append(LineString(poly.exterior.coords))
        for hole in poly.interiors:
            rings.append(LineString(hole.coords))
    return rings


# ---------------------------------------------------------------- toolpaths

def vbit_effective_diameter(angle_deg: float, tip_diameter: float, depth: float) -> float:
    return tip_diameter + 2 * depth * math.tan(math.radians(angle_deg) / 2)


def isolation_toolpaths(copper, tool_dia: float, n_passes: int = 1, overlap: float = 0.3):
    """Offset contours around the copper. Returns (paths, warnings)."""
    warnings = []
    islands = _as_polygons(copper)
    paths = []
    for i in range(n_passes):
        offset = tool_dia / 2 + i * tool_dia * (1 - overlap)
        buffered = copper.buffer(offset, quad_segs=QUAD_SEGS // 2)
        if i == 0 and len(_as_polygons(buffered)) < len(islands):
            warnings.append(
                f"Some copper features are closer together than the tool diameter "
                f"({tool_dia:.3f} mm): their isolation contours merged and the gap "
                f"between them will NOT be milled. Check for shorts or use a smaller tool.")
        paths.extend(_rings(buffered))
    return paths, warnings


def rubout_toolpaths(copper, region, tool_dia: float, overlap: float = 0.3):
    """Clear ALL copper inside `region` that isn't part of the layer artwork.

    The allowed tool-center area is `region` minus copper grown by the tool
    radius; successive inward offsets of that area sweep the whole open zone.
    Returns (paths, warnings)."""
    warnings = []
    r = tool_dia / 2
    center_area = region.difference(copper.buffer(r, quad_segs=QUAD_SEGS // 2))
    step = tool_dia * (1 - overlap)
    paths, cur = [], center_area
    while not cur.is_empty:
        paths.extend(_rings(cur))
        cur = cur.buffer(-step, quad_segs=QUAD_SEGS // 4)

    # anything the tool could not reach (gaps narrower than the tool)?
    cleared = unary_union([p.buffer(r + 1e-3, quad_segs=8) for p in paths]) if paths else Polygon()
    remaining = region.difference(copper).difference(cleared)
    slivers = [g for g in _as_polygons(remaining) if g.area > 0.005]
    if slivers:
        warnings.append(
            f"{len(slivers)} copper sliver(s) totalling {sum(g.area for g in slivers):.2f} mm² "
            f"are narrower than the tool ({tool_dia:.3f} mm) and cannot be cleared - "
            "they will remain as floating copper. Use a smaller tool to remove them.")
    return paths, warnings


def voronoi_toolpaths(copper, region, tool_dia: float, densify: float = 0.15):
    """Cut along the Voronoi midlines between copper islands: one cut per gap,
    equidistant from the neighbors, every bit of copper stays attached to its
    nearest net. Returns (paths, warnings)."""
    warnings = []
    islands = _as_polygons(copper)
    if len(islands) < 2:
        warnings.append("Voronoi mode needs at least two copper islands - "
                        "fell back to a single contour pass.")
        return isolation_toolpaths(copper, tool_dia, 1, 0.0)[0], warnings

    # seed points: densified island boundaries, tagged with their island
    seed_pts, seed_island = [], []
    for idx, isl in enumerate(islands):
        for ring in [isl.exterior] + list(isl.interiors):
            for xy in shapely.segmentize(LineString(ring.coords), densify).coords:
                seed_pts.append(Point(xy))
                seed_island.append(idx)

    edges = voronoi_polygons(MultiPoint(seed_pts), extend_to=region.buffer(1.0),
                             only_edges=True)
    # keep only edges that separate seeds of DIFFERENT islands: each voronoi
    # edge is equidistant from its two generating seeds, so probe the midpoint
    tree = STRtree(seed_pts)
    keep = []
    for e in getattr(edges, "geoms", [edges]):
        mid = e.interpolate(0.5, normalized=True)
        d = mid.distance(seed_pts[tree.nearest(mid)])
        near = {seed_island[i] for i in tree.query(mid.buffer(d + 1e-6))
                if mid.distance(seed_pts[i]) <= d + 1e-6}
        if len(near) >= 2:
            keep.append(e)

    paths = []
    if keep:
        clipped = linemerge(keep).intersection(region)
        paths = [g for g in getattr(clipped, "geoms", [clipped])
                 if isinstance(g, LineString) and g.length > 0.05]

    # where the midline runs closer to copper than the tool radius, the cut
    # will bite the copper on both sides (still isolates, but narrows traces)
    r = tool_dia / 2
    tight = 0.0
    for path in paths:
        pts = shapely.points(list(shapely.segmentize(path, 0.2).coords))
        d = shapely.distance(pts, copper)
        tight += 0.2 * int((d < r * 0.98).sum())
    if tight > 0.2:
        warnings.append(
            f"~{tight:.1f} mm of the voronoi midline runs closer to copper than the tool "
            f"radius ({r:.3f} mm): the cut will nibble both neighbors there. "
            "Isolation still works, but traces get narrower - consider a smaller tool.")
    return paths, warnings


def board_outline(edge_geom, stroke_width: float):
    """Board polygon from the stroked edge-cuts loop (largest polygon's exterior,
    shrunk by half the stroke width so the edge is the drawn centerline)."""
    polys = _as_polygons(edge_geom)
    if not polys:
        return None
    biggest = max(polys, key=lambda p: p.area)
    board = Polygon(biggest.exterior.coords)
    if stroke_width > 0:
        board = board.buffer(-stroke_width / 2, quad_segs=QUAD_SEGS // 2)
    return board


def outline_toolpath(board: Polygon, tool_dia: float, tab_count: int, tab_width: float):
    """Cut path around the board. Returns (full_ring, final_pass_segments)."""
    ring = LineString(board.buffer(tool_dia / 2, quad_segs=QUAD_SEGS // 2).exterior.coords)
    if tab_count <= 0 or tab_width <= 0:
        return ring, [ring]
    L = ring.length
    segments = []
    for i in range(tab_count):
        start = (i * L / tab_count) + tab_width / 2
        end = ((i + 1) * L / tab_count) - tab_width / 2
        if end > start:
            segments.append(substring(ring, start, end))
    return ring, segments


def order_nearest(items, keyfn):
    """Greedy nearest-neighbor ordering. keyfn(item) -> (x, y) start point."""
    remaining = list(items)
    out = []
    pos = (0.0, 0.0)
    while remaining:
        nxt = min(remaining, key=lambda it: (keyfn(it)[0] - pos[0]) ** 2 + (keyfn(it)[1] - pos[1]) ** 2)
        remaining.remove(nxt)
        out.append(nxt)
        pos = keyfn(nxt)
    return out


# ---------------------------------------------------------------- G-code

@dataclass
class GcodeParams:
    safe_z: float = 2.0          # rapid clearance above stock, mm
    feed_xy: float = 120.0       # mm/min
    feed_z: float = 60.0         # mm/min plunge
    rpm: float = 10000.0
    max_segment: float = 1.0     # mm; long moves are split for autoleveling


def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _emit_path(lines, path: LineString, depth: float, p: GcodeParams):
    coords = list(shapely.segmentize(path, p.max_segment).coords)
    x0, y0 = coords[0][0], coords[0][1]
    lines.append(f"G0 X{_fmt(x0)} Y{_fmt(y0)}")
    lines.append(f"G1 Z{_fmt(depth)} F{_fmt(p.feed_z)}")
    for x, y in coords[1:]:
        lines.append(f"G1 X{_fmt(x)} Y{_fmt(y)} F{_fmt(p.feed_xy)}")
    lines.append(f"G0 Z{_fmt(p.safe_z)}")


def _header(p: GcodeParams, comment: str):
    return [f"({comment})", "G21 G90 G94", f"G0 Z{_fmt(p.safe_z)}",
            f"M3 S{_fmt(p.rpm)}", "G4 P2"]


def _footer(p: GcodeParams):
    return [f"G0 Z{_fmt(p.safe_z)}", "M5", "G0 X0 Y0", "M2", ""]


def gcode_isolation(paths, depth: float, p: GcodeParams) -> str:
    lines = _header(p, f"isolation milling, depth {depth} mm")
    for path in order_nearest(paths, lambda ls: ls.coords[0]):
        _emit_path(lines, path, -abs(depth), p)
    lines += _footer(p)
    return "\n".join(lines)


def gcode_drill(drills, p: GcodeParams, depth: float) -> str:
    """drills: list of (x, y, dia). One M0 tool-change pause per diameter."""
    lines = _header(p, f"drilling, depth {depth} mm")
    by_dia = {}
    for x, y, d in drills:
        by_dia.setdefault(round(d, 3), []).append((x, y))
    first = True
    for dia in sorted(by_dia):
        if not first:
            lines += ["M5", f"G0 Z{_fmt(max(p.safe_z, 20))}",
                      f"(change tool: {dia} mm drill)", "M0",
                      f"M3 S{_fmt(p.rpm)}", "G4 P2"]
        first = False
        lines.append(f"(tool diameter {dia} mm, {len(by_dia[dia])} holes)")
        for x, y in order_nearest(by_dia[dia], lambda pt: pt):
            lines.append(f"G0 X{_fmt(x)} Y{_fmt(y)}")
            lines.append(f"G1 Z{_fmt(-abs(depth))} F{_fmt(p.feed_z)}")
            lines.append(f"G0 Z{_fmt(p.safe_z)}")
    lines += _footer(p)
    return "\n".join(lines)


def gcode_outline(full_ring, final_segments, total_depth: float, pass_depth: float,
                  p: GcodeParams) -> str:
    lines = _header(p, f"board outline, total depth {round(total_depth, 3)} mm")
    depth = 0.0
    while depth < total_depth - 1e-9:
        depth = min(depth + pass_depth, total_depth)
        last = depth >= total_depth - 1e-9
        for path in (final_segments if last else [full_ring]):
            _emit_path(lines, path, -depth, p)
    lines += _footer(p)
    return "\n".join(lines)


# ---------------------------------------------------------------- top level

@dataclass
class JobResult:
    units: str = "mm"
    bbox: tuple = None
    copper: list = field(default_factory=list)      # preview polygons
    board: list = field(default_factory=list)       # board edge rings
    isolation: list = field(default_factory=list)   # toolpath polylines
    outline: list = field(default_factory=list)     # outline cut polylines
    drills: list = field(default_factory=list)      # {x, y, dia}
    warnings: list = field(default_factory=list)
    gcode: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


def _poly_preview(geom, tol=0.005):
    out = []
    for poly in _as_polygons(geom):
        poly = poly.simplify(tol)
        out.append({"ext": [[round(x, 4), round(y, 4)] for x, y in poly.exterior.coords],
                    "holes": [[[round(x, 4), round(y, 4)] for x, y in h.coords]
                              for h in poly.interiors]})
    return out


def _line_preview(lines_, tol=0.005):
    return [[[round(x, 4), round(y, 4)] for x, y in ls.simplify(tol).coords] for ls in lines_]


def process_job(copper_path=None, drill_path=None, outline_path=None, *,
                tool_dia=None, vbit_angle=None, vbit_tip=0.1,
                iso_depth=0.05, iso_passes=1, iso_overlap=0.3,
                strategy="contour", copper_margin=0.0,
                drill_depth=2.0,
                cutout_tool_dia=1.0, board_thickness=1.6, cutout_pass_depth=0.6,
                cutout_overshoot=0.1, tab_count=4, tab_width=2.0,
                mirror=False, zero_lower_left=True,
                gcode_params: GcodeParams = None) -> JobResult:
    p = gcode_params or GcodeParams()
    res = JobResult()

    if tool_dia is None and vbit_angle:
        tool_dia = vbit_effective_diameter(vbit_angle, vbit_tip, iso_depth)
        res.stats["vbit_effective_diameter"] = round(tool_dia, 4)
    tool_dia = tool_dia or 0.2

    copper = gerber_to_geometry(GerberFile.open(copper_path)) if copper_path else Polygon()

    board = None
    if outline_path:
        edge_file = GerberFile.open(outline_path)
        stroke = 0.0
        for obj in edge_file.objects:
            ap = getattr(obj, "aperture", None)
            if ap is not None and getattr(ap, "diameter", None):
                stroke = ap.diameter
                break
        board = board_outline(gerber_to_geometry(edge_file), stroke)

    drills = []
    if drill_path:
        exc = ExcellonFile.open(drill_path)
        for flash in exc.drills():
            f = flash.converted(MM)
            drills.append((f.x, f.y, f.tool.diameter))

    # ---- transform: mirror (for bottom-side milling) then move origin
    geoms = {"copper": copper, "board": board}
    ref = board if board is not None else copper
    minx, miny, maxx, maxy = ref.bounds

    def xform(g):
        if g is None or g.is_empty:
            return g
        if mirror:
            g = affinity.scale(g, xfact=-1, yfact=1, origin=((minx + maxx) / 2, 0))
        if zero_lower_left:
            g = affinity.translate(g, xoff=-minx, yoff=-miny)
        return g

    copper = xform(copper)
    board = xform(board) if board is not None else None
    pts = [xform(Point(x, y)) for x, y, _ in drills]
    drills = [(pt.x, pt.y, d) for pt, (_, _, d) in zip(pts, drills)]

    # ---- toolpaths
    if not copper.is_empty:
        work = copper
        if copper_margin > 0:
            work = copper.buffer(copper_margin, quad_segs=QUAD_SEGS // 2)
            lost = len(_as_polygons(copper)) - len(_as_polygons(work))
            if lost > 0:
                res.warnings.append(
                    f"The copper margin ({copper_margin} mm) merged {lost} pair(s) of "
                    "copper features - the gap between them will NOT be cut and they "
                    "will stay connected. Reduce the margin or redesign the clearance.")
            res.stats["copper_margin"] = copper_margin
        region = board if board is not None else box(*copper.bounds).buffer(1.0)
        if strategy == "rubout":
            iso_paths, warns = rubout_toolpaths(work, region, tool_dia, iso_overlap)
        elif strategy == "voronoi":
            iso_paths, warns = voronoi_toolpaths(work, region, tool_dia)
        else:
            iso_paths, warns = isolation_toolpaths(work, tool_dia, iso_passes, iso_overlap)
        res.warnings += warns
        res.stats["strategy"] = strategy
        res.gcode["isolation"] = gcode_isolation(iso_paths, iso_depth, p)
        res.isolation = _line_preview(iso_paths)
        res.stats["isolation_paths"] = len(iso_paths)
        res.stats["isolation_length_mm"] = round(sum(l.length for l in iso_paths), 1)

    if drills:
        res.gcode["drill"] = gcode_drill(drills, p, drill_depth)
        res.drills = [{"x": round(x, 4), "y": round(y, 4), "dia": d} for x, y, d in drills]
        if board is not None:
            inset = board.buffer(1e-6)
            outside = [1 for x, y, _ in drills if not inset.contains(Point(x, y))]
            if outside:
                res.warnings.append(f"{len(outside)} drill hit(s) fall outside the board "
                                    "outline - check that drill and gerber files match.")

    if board is not None:
        full, segs = outline_toolpath(board, cutout_tool_dia, tab_count, tab_width)
        res.gcode["outline"] = gcode_outline(full, segs, board_thickness + cutout_overshoot,
                                             cutout_pass_depth, p)
        res.outline = _line_preview([full])
        res.board = _line_preview([LineString(bp.exterior.coords) for bp in _as_polygons(board)])

    res.copper = _poly_preview(copper)
    everything = [g for g in [copper, board] if g is not None and not g.is_empty]
    if everything:
        b = unary_union(everything).bounds
        res.bbox = [round(v, 3) for v in b]
    res.stats["tool_diameter"] = round(tool_dia, 4)
    res.stats["drill_count"] = len(drills)
    return res
