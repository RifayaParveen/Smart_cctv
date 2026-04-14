# Smart CCTV — AI-Powered Motion Detection System
## ESP32-S3 N16R8 + OV3660 | Claude Vision AI | Cloudinary | WebM/Chrome

```
ESP32-S3 ──JPEG frames──▶ Cloud Backend (Railway) ──▶ Cloudinary (video)
                                   │
                                   ▼
                          Claude claude-3-5-sonnet-20241022 Vision API (AI labelling)
                                   │
                                   ▼
                          Dashboard (served by backend, open in Chrome)
```

---

## What Each Part Does

| File | Role |
|---|---|
| `firmware/smart_cctv.ino` | Runs on ESP32-S3. Detects motion, asks backend to analyze key frame with AI, uploads JPEG frames |
| `backend/main.py` | FastAPI cloud server. Calls Claude Vision, assembles WebM video, stores to Cloudinary |
| `backend/dashboard.html` | Dark surveillance dashboard. Auto-refreshes every 5 s. Plays WebM clips in Chrome |

---

## Step 1 — Free Cloud Accounts to Create (once)

### A. Cloudinary (free — 25 GB video storage)
1. Sign up at https://cloudinary.com/users/register_free
2. Dashboard → copy **`CLOUDINARY_URL`** (looks like `cloudinary://123:abc@yourcloud`)

### B. Anthropic Claude Vision (AI analysis)
1. Sign up at https://console.anthropic.com
2. Top-right → Settings → API Keys → copy **`ANTHROPIC_API_KEY`**
3. Used for Claude Vision AI — labels objects (person, dog, car, mailman, etc.)

> Anthropic offers $5 free credits for new accounts — enough for thousands of AI frame analyses.

### C. Railway (free backend hosting)
1. Sign up at https://railway.app with GitHub
2. New Project → Deploy from GitHub Repo (push `backend/` folder)
3. Variables tab → add:
   ```
   ANTHROPIC_API_KEY = your_key_here
   CLOUDINARY_URL    = cloudinary://...
   PORT              = 8000
   ```
4. Copy the deployment URL (e.g. `https://smart-cctv-abc.up.railway.app`)

> **Alternative free hosts:** Render.com (same process), Fly.io

---

## Step 2 — Deploy Backend

```
smart-cctv/
└── backend/
    ├── main.py          ← FastAPI app
    ├── dashboard.html   ← served at /
    ├── requirements.txt
    └── Procfile         ← web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

Push the `backend/` directory to GitHub, then connect to Railway.

Railway automatically installs `requirements.txt` and runs the `Procfile`.

> FFmpeg is available in Railway's default build image. If not, add `nixpacks.toml`:
> ```toml
> [phases.setup]
> nixPkgs = ["ffmpeg"]
> ```

---

## Step 3 — Flash ESP32

### Arduino IDE Settings
| Setting | Value |
|---|---|
| Board | ESP32S3 Dev Module |
| PSRAM | **OPI PSRAM** ← critical |
| Flash Mode | QIO |
| Flash Size | 16MB |
| Partition Scheme | Huge APP (3MB No OTA) |
| Upload Speed | 921600 |

### Edit firmware/smart_cctv.ino
```cpp
const char* WIFI_SSID     = "YourWiFiName";
const char* WIFI_PASSWORD = "YourWiFiPassword";
const char* BACKEND_URL   = "https://smart-cctv-abc.up.railway.app";  // ← Railway URL
```

### Required Libraries (Library Manager)
- **ArduinoJson** by Benoit Blanchon
- ESP32 Arduino Core ≥ 2.0.11 (includes `esp_camera`)

Flash once from any computer. After that the ESP32 runs forever with power only.

---

## Step 4 — Open Dashboard

Visit `https://your-railway-url.up.railway.app` in **Chrome**.

✅ Clips appear automatically as the ESP32 sends them.
✅ WebM video plays natively in Chrome (no plugin needed).
✅ AI label (dog / person / car / mailman / …) shown per clip.

---

## System Flow (per motion event)

```
1. ESP32 idle loop — fills 25-frame circular pre-buffer (~1 s)

2. Motion detected (JPEG size delta ≥ 8%)

3. ESP32 calls POST /api/motion_event → gets clip_id

4. ESP32 POSTs one key frame to POST /api/analyze_frame
   └─ Backend sends JPEG to Claude Vision
   └─ Claude returns { label: "dog", confidence: 0.92 }
   └─ ESP32 stores label in clipAiLabel

5. ESP32 records up to 8 s post-motion (240 frames)

6. ESP32 streams frames one-by-one to POST /api/upload_frame?clip_id=...

7. ESP32 calls POST /api/finalize_clip?clip_id=...
   └─ Backend runs FFmpeg: JPEG frames → WebM (VP9, Chrome-native)
   └─ Uploads WebM + thumbnail to Cloudinary
   └─ Dashboard auto-refreshes and shows clip

Total no-laptop path: ESP32 → Railway → Cloudinary → Chrome
```

---

## Motion Tuning (firmware)

```cpp
#define MOTION_THRESHOLD   8.0f   // % JPEG size change — lower = more sensitive
#define PRE_MOTION_FRAMES  25     // pre-buffer (~1 s)
#define POST_MOTION_FRAMES 60     // tail after motion stops (~2 s)
#define MAX_CLIP_FRAMES    240    // hard cap (~8 s)
```

---

## Video Format

- **Codec:** VP9 (WebM) — plays natively in Chrome, Edge, Firefox
- **Resolution:** 800×600 (SVGA from OV3660)
- **Frame rate:** ~30 fps
- **Storage:** Cloudinary free tier = 25 GB / 10 GB bandwidth per month

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| ESP32 loops "PSRAM not found" | Arduino IDE → Tools → PSRAM → **OPI PSRAM** |
| Camera init error 0x20004 | Check wiring; confirm XCLK=GPIO15 |
| HTTP 500 on finalize | FFmpeg not in PATH on host; add nixpacks.toml |
| Video won't play in Chrome | Check Cloudinary URL is `.webm`; use DevTools > Console |
| AI always returns "unknown" | Check ANTHROPIC_API_KEY env var on Railway |
