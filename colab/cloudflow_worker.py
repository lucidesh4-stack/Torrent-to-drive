# ==============================================================================
# 🚀 CLOUDFLOW REMOTE GPU WORKER (Google Colab T4 NVENC Sidecar)
# ==============================================================================
import os, sys, time, json, subprocess, requests

CLOUDFLOW_URL = os.getenv("CLOUDFLOW_URL", "https://lucidesh4-cloudflow.hf.space")
WORKER_SECRET = os.getenv("STREAMLY_GPU_SECRET", "cloudflow_t4_gpu")

print("=" * 70)
print("🚀 CLOUDFLOW REMOTE GPU WORKER FOR GOOGLE COLAB")
print("=" * 70)
print(f"🔗 Target Cloudflow Instance: {CLOUDFLOW_URL}")

def get_gpu_name():
    try:
        res = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True)
        name = res.stdout.strip()
        if name:
            return name
    except Exception:
        pass
    return "CPU / No GPU Detected"

GPU_NAME = get_gpu_name()
print(f"⚡ Hardware Accelerator: {GPU_NAME}")
if "NVIDIA" not in GPU_NAME and "Tesla" not in GPU_NAME:
    print("⚠️ WARNING: No NVIDIA GPU detected! Make sure you selected T4 GPU in Colab:")
    print("   Runtime -> Change runtime type -> Hardware accelerator -> T4 GPU")

def probe_video(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate,bit_rate:format=bit_rate,duration",
        "-of", "json", path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(res.stdout)
        v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
        fmt = data.get("format", {})
        fps_str = v.get("avg_frame_rate", "30/1")
        fps_eval = eval(fps_str) if "/" in fps_str else float(fps_str or 30)
        return {
            "codec": v.get("codec_name", "unknown"),
            "width": int(v.get("width", 0)),
            "height": int(v.get("height", 0)),
            "fps": max(1, int(round(fps_eval))),
            "duration": float(fmt.get("duration", 0)),
            "size_mb": round(os.path.getsize(path) / (1024 * 1024), 2)
        }
    except Exception:
        return {"width": 1920, "height": 1080, "fps": 30, "duration": 0, "size_mb": 0}

def build_ffmpeg_cmd(in_path, out_path, target_k, max_v, bufsize, has_nvenc, fps=30, mode="VBR", preset="p7", multipass="fullres", copy_audio=True):
    vcodec = "hevc_nvenc" if has_nvenc else "libx264"
    cmd = ["ffmpeg", "-y", "-hide_banner"]
    
    # ⚡ Hardware NVDEC decoding on GPU VRAM (bypasses Colab CPU bottleneck!)
    if has_nvenc:
        cmd += ["-hwaccel", "cuda"]
        
    cmd += [
        "-progress", "-",
        "-i", in_path,
        "-c:v", vcodec
    ]
    
    if has_nvenc:
        gop = max(30, int(fps * 5)) # 5-second GOP matching batch script
        keyint_min = int(fps)
        
        # Exact rate-control matching your batch script
        if mode == "CQ":
            rc_opts = [
                "-rc", "vbr",
                "-cq", "30",
                "-b:v", f"{target_k}k",
                "-maxrate", f"{max_v}k",
                "-bufsize", f"{bufsize}k",
            ]
        else:
            rc_opts = [
                "-rc", "vbr",
                "-b:v", f"{target_k}k",
                "-maxrate", f"{max_v}k",
                "-bufsize", f"{bufsize}k",
                "-qmin", "22",
                "-qmax", "38",
            ]

        cmd += [
            "-profile:v", "main10",          # 10-bit HEVC color depth (forced for quality)
            "-pix_fmt", "p010le",
            "-preset", preset,               # p7 highest quality (exact match with your bat script!)
            "-tune", "hq",                   # High visual quality tuning
            *rc_opts,
            "-multipass", multipass,         # 2-pass fullres macroblock analysis
            "-spatial_aq", "1",              # Edge & fine texture adaptive quantization
            "-temporal_aq", "1",             # Motion-based quantization (0 bits for static frames)
            "-aq-strength", "7",             # Optimal strength matching bat script
            "-rc-lookahead", "32",           # 32-frame forward bit distribution
            "-bf", "3",                      # 3 B-frames
            "-b_ref_mode", "middle",         # B-frame middle reference
            "-g", str(gop),
            "-keyint_min", str(keyint_min),
            "-tag:v:0", "hvc1",              # Apple / iOS hardware decoding tag
        ]
    else:
        cmd += [
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-b:v", f"{target_k}k",
            "-maxrate", f"{max_v}k",
            "-bufsize", f"{bufsize}k",
        ]
        
    if copy_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
        
    cmd += ["-movflags", "+faststart", out_path]
    return cmd

def compress_video(in_path, out_path, task, report_progress_fn):
    info = probe_video(in_path)
    target_k = int(task.get("target_bitrate_k", 1500))
    
    # Adaptive targets based on resolution
    pixels = info.get("width", 1920) * info.get("height", 1080)
    if pixels >= 6000000:       target_k = max(target_k, 4500)
    elif pixels >= 3500000:     target_k = max(target_k, 2500)
    elif pixels >= 1500000:     target_k = max(target_k, 1500)
    
    if info.get("fps", 30) > 45:
        target_k = int(target_k * 1.3)
        
    max_v = int(target_k * 2.5)
    bufsize = int(target_k * 3.5)

    has_nvenc = ("NVIDIA" in GPU_NAME or "Tesla" in GPU_NAME)
    duration = info.get("duration", 0)

    mode = task.get("mode", "VBR")

    # Try 1: Exact batch script match (p7, fullres, main10, copy audio)
    cmd = build_ffmpeg_cmd(in_path, out_path, target_k, max_v, bufsize, has_nvenc, fps=info.get("fps", 30), mode=mode, preset="p7", multipass="fullres", copy_audio=True)
    print(f"   ▶ Studio Quality Hardware Pipeline (NVDEC + NVENC p7 fullres 10-bit, mode={mode}, {target_k}k target)...")
    
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    fps_val = "0"
    speed_val = "0x"
    time_val = "00:00:00"
    pct = 0.0

    while True:
        line = p.stdout.readline()
        if not line and p.poll() is not None:
            break
        line = line.strip()
        if line.startswith("fps="):
            fps_val = line.split("=")[1].strip()
        elif line.startswith("speed="):
            speed_val = line.split("=")[1].strip()
        elif line.startswith("out_time="):
            time_val = line.split("=")[1].strip().split(".")[0]
            if duration > 0:
                parts = time_val.split(":")
                if len(parts) == 3:
                    try:
                        sec = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                        pct = min(99.0, round((sec / duration) * 100.0, 1))
                    except Exception:
                        pass
        elif line.startswith("progress=continue"):
            report_progress_fn(pct, fps_val, speed_val, time_val)

    stderr_out = p.stderr.read()
    
    # If audio copy failed (e.g. PCM / incompatible in MP4), retry with AAC transcode
    if p.returncode != 0:
        needs_aac = any(msg in stderr_out for msg in ["Could not find tag", "incompatible", "muxer does not support", "tag not found"])
        needs_preset_fallback = ("preset" in stderr_out.lower() or "multipass" in stderr_out.lower() or "p7" in stderr_out)
        preset_to_use = "p5" if needs_preset_fallback else "p7"
        mp_to_use = "qres" if needs_preset_fallback else "fullres"

        print(f"   ⚠️ Retrying with adaptive fallback (audio={'aac' if needs_aac else 'copy'}, preset={preset_to_use})...")
        cmd = build_ffmpeg_cmd(in_path, out_path, target_k, max_v, bufsize, has_nvenc, fps=info.get("fps", 30), mode=mode, preset=preset_to_use, multipass=mp_to_use, copy_audio=(not needs_aac))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while True:
            line = p.stdout.readline()
            if not line and p.poll() is not None:
                break
            line = line.strip()
            if line.startswith("fps="):
                fps_val = line.split("=")[1].strip()
            elif line.startswith("speed="):
                speed_val = line.split("=")[1].strip()
            elif line.startswith("out_time="):
                time_val = line.split("=")[1].strip().split(".")[0]
            elif line.startswith("progress=continue"):
                report_progress_fn(pct, fps_val, speed_val, time_val)
        stderr_out = p.stderr.read()

    if p.returncode != 0:
        print(f"❌ FFmpeg exit code: {p.returncode}")
        if stderr_out:
            print(f"   Error details: {stderr_out[-500:]}")
        return False

    return os.path.exists(out_path) and os.path.getsize(out_path) > 1000

def run_worker_loop():
    print("\n🟢 Colab GPU Worker is ACTIVE! Waiting for tasks from Cloudflow...\n")
    session = requests.Session()

    while True:
        try:
            poll_resp = session.post(
                f"{CLOUDFLOW_URL}/api/gpu/poll",
                json={"gpu_name": GPU_NAME, "secret": WORKER_SECRET},
                timeout=15
            )
            if poll_resp.status_code == 200:
                data = poll_resp.json()
                task = data.get("task")
                if task:
                    task_id = task["task_id"]
                    filename = task.get("filename", "video.mp4")
                    print(f"\n📥 [NEW JOB] Task {task_id}: {filename} ({task.get('mode', 'VBR')} Mode)")

                    source_url = task["source_url"]
                    if source_url.startswith("/"):
                        source_url = f"{CLOUDFLOW_URL}{source_url}"

                    in_path = f"/tmp/input_{task_id}.mp4"
                    out_path = f"/tmp/compressed_{task_id}.mp4"

                    print(f"   Downloading source video from Cloudflow...")
                    t_start = time.time()
                    with session.get(source_url, stream=True, timeout=180) as r:
                        r.raise_for_status()
                        with open(in_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=1024*1024):
                                f.write(chunk)

                    orig_mb = os.path.getsize(in_path) / (1024 * 1024)
                    print(f"   Downloaded {orig_mb:.1f} MB. Starting 10-bit HEVC encode on {GPU_NAME}...")

                    def _report(pct, fps, speed, cur_time):
                        try:
                            session.post(
                                f"{CLOUDFLOW_URL}/api/gpu/progress",
                                json={"task_id": task_id, "secret": WORKER_SECRET, "progress": pct, "fps": fps, "speed_x": speed, "time_str": cur_time},
                                timeout=5
                            )
                        except Exception:
                            pass

                    ok = compress_video(in_path, out_path, task, _report)
                    if ok and os.path.exists(out_path):
                        new_mb = os.path.getsize(out_path) / (1024 * 1024)
                        saved_pct = ((orig_mb - new_mb) / orig_mb) * 100.0 if orig_mb > 0 else 0
                        elapsed = time.time() - t_start
                        print(f"✅ Finished! {orig_mb:.1f} MB -> {new_mb:.1f} MB ({saved_pct:.1f}% saved in {elapsed:.1f}s)")

                        print("🚀 Uploading compressed video back to Cloudflow...")
                        with open(out_path, "rb") as f:
                            files = {"file": (f"compressed_{filename}", f, "video/mp4")}
                            form_data = {
                                "task_id": task_id,
                                "secret": WORKER_SECRET,
                                "orig_mb": orig_mb,
                                "new_mb": new_mb
                            }
                            session.post(f"{CLOUDFLOW_URL}/api/gpu/complete", data=form_data, files=files, timeout=300)
                        print("🎉 Task completed and saved in Temp Cloud!\n")
                    else:
                        print("❌ Compression failed or output empty.\n")

                    for p in [in_path, out_path]:
                        if os.path.exists(p):
                            try: os.remove(p)
                            except Exception: pass

        except Exception as e:
            pass

        time.sleep(2.5)

if __name__ == "__main__":
    run_worker_loop()
