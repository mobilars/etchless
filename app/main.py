import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .pipeline import process_job, GcodeParams

app = FastAPI(title="etchless")

STATIC = Path(__file__).parent.parent / "static"


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/version")
def version():
    import os
    return {"sha": os.environ.get("BUILD_SHA", "dev"),
            "date": os.environ.get("BUILD_DATE", "unknown")}


@app.post("/api/process")
async def api_process(
    copper: UploadFile | None = File(None),
    drill: UploadFile | None = File(None),
    outline: UploadFile | None = File(None),
    tool_mode: str = Form("endmill"),          # endmill | vbit
    tool_dia: float = Form(0.2),
    vbit_angle: float = Form(30.0),
    vbit_tip: float = Form(0.1),
    iso_depth: float = Form(0.05),
    iso_passes: int = Form(1),
    iso_overlap: float = Form(0.3),
    strategy: str = Form("contour"),           # contour | voronoi | rubout
    copper_margin: float = Form(0.0),
    spot_drills: bool = Form(False),
    spot_depth: float = Form(0.1),
    drill_depth: float = Form(2.0),
    cutout_tool_dia: float = Form(1.0),
    board_thickness: float = Form(1.6),
    cutout_pass_depth: float = Form(0.6),
    tab_count: int = Form(4),
    tab_width: float = Form(2.0),
    mirror: bool = Form(False),
    safe_z: float = Form(2.0),
    feed_xy: float = Form(120.0),
    feed_z: float = Form(60.0),
    rpm: float = Form(10000.0),
    max_segment: float = Form(1.0),
):
    tmp = Path(tempfile.mkdtemp(prefix="g2g_"))
    try:
        paths = {}
        for name, up in (("copper", copper), ("drill", drill), ("outline", outline)):
            if up is not None and up.filename:
                dest = tmp / f"{name}_{Path(up.filename).name}"
                with dest.open("wb") as f:
                    shutil.copyfileobj(up.file, f)
                paths[name] = str(dest)
        if not paths:
            return JSONResponse({"error": "upload at least one file"}, status_code=400)

        res = process_job(
            copper_path=paths.get("copper"),
            drill_path=paths.get("drill"),
            outline_path=paths.get("outline"),
            tool_dia=None if tool_mode == "vbit" else tool_dia,
            vbit_angle=vbit_angle if tool_mode == "vbit" else None,
            vbit_tip=vbit_tip,
            iso_depth=iso_depth, iso_passes=iso_passes, iso_overlap=iso_overlap,
            strategy=strategy, copper_margin=copper_margin,
            spot_drills=spot_drills, spot_depth=spot_depth,
            drill_depth=drill_depth,
            cutout_tool_dia=cutout_tool_dia, board_thickness=board_thickness,
            cutout_pass_depth=cutout_pass_depth,
            tab_count=tab_count, tab_width=tab_width,
            mirror=mirror,
            gcode_params=GcodeParams(safe_z=safe_z, feed_xy=feed_xy, feed_z=feed_z,
                                     rpm=rpm, max_segment=max_segment),
        )
        return asdict(res)
    except Exception as e:
        text = str(e)
        which = next((label for key, label in (("copper_", "copper layer"),
                                               ("drill_", "drill file"),
                                               ("outline_", "board outline"))
                      if key in text), None)
        msg = f"Could not parse the {which}: {text}" if which else f"{type(e).__name__}: {text}"
        if "excellon statement" in text.lower() or "gerber" in text.lower() or which:
            uploaded = {n: Path(u.filename).name for n, u in
                        (("copper layer", copper), ("drill file", drill), ("board outline", outline))
                        if u is not None and u.filename}
            bad_name = uploaded.get(which, "")
            if bad_name.lower().endswith((".ngc", ".nc", ".gcode", ".tap")):
                msg = (f"The {which} you uploaded ({bad_name}) looks like G-code output "
                       f"from another CAM tool, not a CAD export. Upload what your EDA tool "
                       f"exports instead - KiCad: *.drl for drills, *-F_Cu.gbr for copper, "
                       f"*-Edge_Cuts.gbr for the outline.")
        return JSONResponse({"error": msg}, status_code=422)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


SAMPLE = Path(__file__).parent.parent / "sample"


@app.get("/api/sample")
def api_sample(strategy: str = "rubout"):
    """Process the bundled sample board with default settings (demo/smoke test)."""
    try:
        res = process_job(
            copper_path=str(SAMPLE / "test-F_Cu.gbr"),
            drill_path=str(SAMPLE / "test.drl"),
            outline_path=str(SAMPLE / "test-Edge_Cuts.gbr"),
            tool_dia=0.2, iso_passes=2, strategy=strategy,
        )
        return asdict(res)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=422)


class NoCacheStaticFiles(StaticFiles):
    """Static files with forced revalidation, so a redeploy can never leave the
    browser mixing cached-old JS with fresh HTML."""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", NoCacheStaticFiles(directory=str(STATIC), html=True), name="static")
