"""
Smart CCTV – Cloud Backend
FastAPI app deployable to Railway / Render / Fly.io (free tier)

Environment variables required:
  ROBOFLOW_API_KEY  – free at roboflow.com (no credit card)
  CLOUDINARY_URL    – e.g. cloudinary://api_key:api_secret@cloud_name
                       (from Cloudinary dashboard → free 25 GB)

AI detection uses Roboflow's free hosted COCO model:
  - Detects: person, dog, cat, car, bird, bicycle, motorcycle, truck, etc.
  - Free tier: 10,000 API calls/month (plenty for home CCTV)
  - No credit card required

Deploy steps (Railway):
  1. Push this folder to GitHub
  2. railway new → connect repo → set env vars → deploy
  3. Copy the railway URL into firmware BACKEND_URL

Endpoints used by ESP32:
  POST /api/motion_event      → register clip, returns { clip_id }
  POST /api/analyze_frame     → Roboflow object detection, returns { label, confidence, description }
  POST /api/upload_frame      → receive one JPEG frame
  POST /api/finalize_clip     → encode MJPEG → WebM, upload to Cloudinary
  GET  /api/clips             → list all clips (for dashboard)
  GET  /                      → serve dashboard HTML
"""

import os, io, time, uuid, json, base64, tempfile, subprocess, logging
from pathlib import Path
from datetime import datetime
from typing import Optional

import httpx
import cloudinary, cloudinary.uploader
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cctv")

# ── Config from env ───────────────────────────────────────────────────────────
ROBOFLOW_API_KEY = os.environ["ROBOFLOW_API_KEY"]
cloudinary.config(cloudinary_url=os.environ["CLOUDINARY_URL"])

# Roboflow hosted COCO model — detects 80 common object classes, completely free
ROBOFLOW_MODEL_URL = (
    "https://detect.roboflow.com/coco/13"
    f"?api_key={ROBOFLOW_API_KEY}&confidence=40&overlap=30"
)

# Map COCO class names → friendly security labels
COCO_LABEL_MAP = {
    "person":       "person",
    "dog":          "dog",
    "cat":          "cat",
    "car":          "car",
    "truck":        "car",
    "bus":          "car",
    "motorcycle":   "motorcycle",
    "bicycle":      "bicycle",
    "bird":         "bird",
    "horse":        "animal",
    "cow":          "animal",
    "sheep":        "animal",
    "backpack":     "person",   # person likely carrying it
    "handbag":      "person",
    "suitcase":     "delivery",
    "umbrella":     "person",
}

# In-memory clip registry (persists for the lifetime of the process)
# For production, swap with a small SQLite / Supabase table.
clips: dict[str, dict] = {}       # clip_id → metadata
frame_store: dict[str, list] = {} # clip_id → list of JPEG bytes (temp)

FRAME_TEMP_DIR = Path(tempfile.gettempdir()) / "cctv_frames"
FRAME_TEMP_DIR.mkdir(exist_ok=True)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Smart CCTV Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── Dashboard HTML (inline — no static dir needed) ───────────────────────────
DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"

# ═════════════════════════════════════════════════════════════════════════════
# ESP32 Endpoints
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/api/motion_event")
async def motion_event(request: Request):
    """ESP32 calls this first to register a clip and get a clip_id."""
    body = await request.json()
    clip_id = "clip_" + uuid.uuid4().hex[:8]
    clips[clip_id] = {
        "clip_id":     clip_id,
        "device_id":   body.get("device_id", "esp32"),
        "frame_count": body.get("frame_count", 0),
        "ai_label":    body.get("ai_label", "analyzing..."),
        "duration_ms": body.get("duration_ms", 0),
        "timestamp":   datetime.utcnow().isoformat() + "Z",
        "status":      "receiving",
        "video_url":   None,
        "thumbnail_url": None,
    }
    frame_store[clip_id] = []
    log.info(f"New clip registered: {clip_id} | label={clips[clip_id]['ai_label']}")
    return {"clip_id": clip_id}


@app.post("/api/analyze_frame")
async def analyze_frame(request: Request):
    """
    ESP32 POSTs a raw JPEG → Roboflow COCO object detection (free, no credit card).
    Returns { label, confidence, description, detections }
    """
    jpeg_bytes = await request.body()
    if len(jpeg_bytes) < 1000:
        return {"label": "unknown", "confidence": 0.0, "description": "Frame too small"}

    try:
        # Roboflow expects base64-encoded image in the POST body
        b64 = base64.b64encode(jpeg_bytes).decode("utf-8")

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                ROBOFLOW_MODEL_URL,
                content=b64,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()

        predictions = data.get("predictions", [])

        if not predictions:
            return {
                "label":       "motion",
                "confidence":  0.5,
                "description": "Movement detected, no object identified",
                "detections":  [],
            }

        # Pick the highest-confidence detection
        best = max(predictions, key=lambda p: p.get("confidence", 0))
        raw_class  = best.get("class", "unknown").lower()
        confidence = round(best.get("confidence", 0), 2)

        # Map COCO class → friendly label
        label = COCO_LABEL_MAP.get(raw_class, raw_class)

        # Build a short description from all detected objects
        unique = list(dict.fromkeys(
            COCO_LABEL_MAP.get(p["class"].lower(), p["class"].lower())
            for p in predictions
        ))
        desc = f"Detected: {', '.join(unique)}" if unique else "Motion detected"

        result = {
            "label":       label,
            "confidence":  confidence,
            "description": desc,
            "detections":  [
                {
                    "class":      p.get("class"),
                    "label":      COCO_LABEL_MAP.get(p.get("class","").lower(), p.get("class","")),
                    "confidence": round(p.get("confidence", 0), 2),
                }
                for p in predictions
            ],
        }
        log.info(f"Roboflow result: {result['label']} ({result['confidence']})")
        return result

    except Exception as e:
        log.error(f"analyze_frame error: {e}")
        return {"label": "unknown", "confidence": 0.0, "description": str(e)}


@app.post("/api/upload_frame")
async def upload_frame(
    request: Request,
    clip_id: str = Query(...),
    frame:   int = Query(0),
    total:   int = Query(1),
    ts:      int = Query(0),
    label:   str = Query("unknown"),
):
    """ESP32 streams one JPEG frame at a time."""
    jpeg = await request.body()
    if clip_id not in frame_store:
        frame_store[clip_id] = []
        clips[clip_id] = {
            "clip_id": clip_id, "device_id": "esp32",
            "frame_count": total, "ai_label": label,
            "duration_ms": 0,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "status": "receiving", "video_url": None, "thumbnail_url": None,
        }

    # Save frame to temp dir (indexed)
    frame_path = FRAME_TEMP_DIR / f"{clip_id}_{frame:04d}.jpg"
    frame_path.write_bytes(jpeg)
    frame_store[clip_id].append(str(frame_path))

    # Update label if ESP32 sent it
    if label != "unknown":
        clips[clip_id]["ai_label"] = label

    log.info(f"Frame {frame}/{total} for {clip_id} ({len(jpeg)} bytes)")
    return {"ok": True, "frame": frame}


@app.post("/api/finalize_clip")
async def finalize_clip(clip_id: str = Query(...)):
    """
    Assembles all JPEG frames into a WebM video using FFmpeg,
    then uploads to Cloudinary. WebM plays natively in Chrome.
    """
    if clip_id not in clips:
        raise HTTPException(404, "clip_id not found")

    frames = sorted(FRAME_TEMP_DIR.glob(f"{clip_id}_*.jpg"))
    if not frames:
        raise HTTPException(400, "No frames received")

    clips[clip_id]["status"] = "encoding"
    log.info(f"Encoding {len(frames)} frames for {clip_id}")

    # Write FFmpeg input list
    list_path = FRAME_TEMP_DIR / f"{clip_id}_list.txt"
    with open(list_path, "w") as f:
        for fp in frames:
            f.write(f"file '{fp}'\n")
            f.write("duration 0.033\n")  # ~30 fps

    out_path = FRAME_TEMP_DIR / f"{clip_id}.webm"

    try:
        # FFmpeg: JPEG frames → WebM (VP9 + Opus, Chrome-compatible)
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c:v", "libvpx-vp9",
            "-b:v", "500k",
            "-vf", "scale=800:600",
            "-an",           # no audio
            "-r", "30",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            log.error(f"FFmpeg error: {result.stderr.decode()}")
            # Fallback: try libvpx (VP8)
            cmd[cmd.index("libvpx-vp9")] = "libvpx"
            result2 = subprocess.run(cmd, capture_output=True, timeout=120)
            if result2.returncode != 0:
                raise RuntimeError(result2.stderr.decode())

        # Upload to Cloudinary
        log.info(f"Uploading {out_path.name} to Cloudinary")
        upload = cloudinary.uploader.upload(
            str(out_path),
            resource_type="video",
            folder="smart_cctv",
            public_id=clip_id,
            overwrite=True,
        )
        video_url = upload["secure_url"]

        # Thumbnail (first frame)
        thumb_upload = cloudinary.uploader.upload(
            str(frames[0]),
            folder="smart_cctv/thumbs",
            public_id=clip_id + "_thumb",
            overwrite=True,
        )
        thumb_url = thumb_upload["secure_url"]

        clips[clip_id].update({
            "status":        "ready",
            "video_url":     video_url,
            "thumbnail_url": thumb_url,
            "frame_count":   len(frames),
            "duration_ms":   len(frames) * 33,
        })
        log.info(f"Clip ready: {video_url}")

    except Exception as e:
        log.error(f"finalize_clip error: {e}")
        clips[clip_id]["status"] = "error"
        clips[clip_id]["error"]  = str(e)
        raise HTTPException(500, str(e))

    finally:
        # Cleanup temp files
        for fp in frames:
            try: fp.unlink()
            except: pass
        try: list_path.unlink()
        except: pass
        try: out_path.unlink()
        except: pass

    return {"ok": True, "video_url": video_url, "thumbnail_url": thumb_url}


# ═════════════════════════════════════════════════════════════════════════════
# Dashboard Endpoints
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/api/clips")
async def list_clips(limit: int = 50):
    """Return clips sorted newest-first."""
    sorted_clips = sorted(
        clips.values(),
        key=lambda c: c["timestamp"],
        reverse=True,
    )
    return {"clips": sorted_clips[:limit], "total": len(clips)}


@app.get("/api/clip/{clip_id}")
async def get_clip(clip_id: str):
    if clip_id not in clips:
        raise HTTPException(404, "Not found")
    return clips[clip_id]


@app.get("/api/stats")
async def stats():
    total    = len(clips)
    by_label: dict[str, int] = {}
    for c in clips.values():
        lbl = c.get("ai_label", "unknown")
        by_label[lbl] = by_label.get(lbl, 0) + 1
    recent = sorted(clips.values(), key=lambda c: c["timestamp"], reverse=True)[:5]
    return {
        "total_clips": total,
        "by_label":    by_label,
        "recent":      recent,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the dashboard."""
    if DASHBOARD_HTML.exists():
        return HTMLResponse(content=DASHBOARD_HTML.read_text())
    return HTMLResponse("<h1>Dashboard not found. Deploy dashboard.html alongside main.py</h1>")


@app.get("/health")
async def health():
    return {"status": "ok", "clips": len(clips)}
