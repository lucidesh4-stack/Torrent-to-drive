# ==============================================================================
# 🚀 CLOUDFLOW REMOTE GPU WORKER (Google Colab Tesla T4 Dual-NVENC Sidecar)
# ==============================================================================
import os, sys, time, json, subprocess, requests, threading

CLOUDFLOW_URL = os.getenv("CLOUDFLOW_URL", "https://lucidesh4-cloudflow.hf.space")
WORKER_SECRET = os.getenv("STREAMLY_GPU_SECRET", "cloudflow_t4_gpu")
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))

print("=" * 75)
print(f"🚀 CLOUDFLOW REMOTE GPU WORKER ({MAX_CONCURRENT_JOBS}-PARALLEL DUAL NVENC PIPELINE)")
print("=" * 75)
print(f"🔗 Target Cloudflow Instance : {CLOUDFLOW_URL}")
print(f"⚡ Max Concurrent Encoders   : {MAX_CONCURRENT_JOBS}")

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
print(f"🎮 Hardware Accelerator      : {GPU_NAME}")
if "NVIDIA" not in GPU_NAME and "Tesla" not in GPU_NAME:
    print("⚠️ WARNING: No NVIDIA GPU detected! Make sure you selected T4 GPU in Colab:")
    print("   Runtime -> Change runtime type -> Hardware accelerator -> T4 GPU")

def ensure_vmaf_ffmpeg():
    try:
        fchk = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
        if "libvmaf" in fchk.stdout:
            return True
    except Exception:
        pass

    print("⚡ Installing enhanced FFmpeg with native libvmaf + NVENC...")
    try:
        cmd = "curl -fsSL https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz | tar -xJ --strip-components=2 -C /usr/local/bin/ --wildcards '*/bin/ffmpeg' '*/bin/ffprobe'"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if res.returncode == 0:
            print("✅ Upgraded FFmpeg with native libvmaf & NVENC successfully!")
            return True
        else:
            print(f"⚠️ FFmpeg upgrade notice: {res.stderr[:200]}")
    except Exception as e:
        print(f"⚠️ Notice: {e}")
    return False

ensure_vmaf_ffmpeg()

def probe_nvenc_capabilities():
    caps = {
        "spatial_aq": None,
        "temporal_aq": None,
        "aq_strength": False,
        "b_ref_mode": None,
        "multipass": False,
    }
    try:
        res = subprocess.run(["ffmpeg", "-h", "encoder=hevc_nvenc"], capture_output=True, text=True)
        out = res.stdout
        if "-spatial-aq" in out: caps["spatial_aq"] = "-spatial-aq"
        elif "-spatial_aq" in out: caps["spatial_aq"] = "-spatial_aq"

        if "-temporal-aq" in out: caps["temporal_aq"] = "-temporal-aq"
        elif "-temporal_aq" in out: caps["temporal_aq"] = "-temporal_aq"

        if "-aq-strength" in out: caps["aq_strength"] = True
        
        if "-b_ref_mode" in out: caps["b_ref_mode"] = "-b_ref_mode"
        elif "-b-ref-mode" in out: caps["b_ref_mode"] = "-b-ref-mode"

        if "-multipass" in out: caps["multipass"] = True
    except Exception:
        pass
    return caps

NVENC_CAPS = probe_nvenc_capabilities()

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

def build_ffmpeg_cmd(in_path, out_path, target_k, max_v, bufsize, has_nvenc, fps=30, mode="VBR", preset="p7", multipass="fullres", safe_mode=False, copy_audio=True):
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
        
        # Exact rate-control matching original Video_compression.bat or Enhanced 95+ VMAF
        if mode == "VMAF95_ENHANCED":
            rc_opts = [
                "-rc", "vbr",
                "-cq", "25",
                "-b:v", f"{target_k}k",
                "-maxrate", f"{int(target_k * 2.2)}k",
                "-bufsize", f"{int(target_k * 4.4)}k",
                "-qmin", "18",
                "-qmax", "33",
            ]
        elif mode == "CQ":
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
            "-preset", preset,               # p7 highest quality (exact match with bat script!)
            "-tune", "hq",                   # High visual quality tuning
            *rc_opts,
            "-rc-lookahead", "32",           # 32-frame forward bit distribution
            "-bf", "3",                      # 3 B-frames
            "-g", str(gop),
            "-keyint_min", str(keyint_min),
            "-tag:v:0", "hvc1",              # Apple / iOS hardware decoding tag
        ]

        if not safe_mode:
            if NVENC_CAPS.get("multipass") and multipass:
                cmd += ["-multipass", multipass]

            if NVENC_CAPS.get("spatial_aq"):
                cmd += [NVENC_CAPS["spatial_aq"], "1"]
                if NVENC_CAPS.get("aq_strength"):
                    cmd += ["-aq-strength", "7"]

            if NVENC_CAPS.get("temporal_aq"):
                cmd += [NVENC_CAPS["temporal_aq"], "1"]

            if NVENC_CAPS.get("b_ref_mode"):
                cmd += [NVENC_CAPS["b_ref_mode"], "middle"]
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
    
    # Adaptive targets based on resolution (Exact match with Video_compression.bat)
    pixels = info.get("width", 1920) * info.get("height", 1080)
    if pixels >= 6000000:       target_k = max(target_k, 6000)
    elif pixels >= 3500000:     target_k = max(target_k, 3000)
    elif pixels >= 1500000:     target_k = max(target_k, 1500)
    
    if info.get("fps", 30) > 45:
        target_k = int(target_k * 1.5)
        
    # EXACT MATCH with Video_compression.bat: MAX_V = TARGET * 2, BUFSIZE = MAX_V * 2
    max_v = target_k * 2
    bufsize = max_v * 2

    has_nvenc = ("NVIDIA" in GPU_NAME or "Tesla" in GPU_NAME)
    duration = info.get("duration", 0)
    mode = task.get("mode", "VBR")

    # Try 1: Exact batch script match (p7, fullres, main10, copy audio)
    cmd = build_ffmpeg_cmd(in_path, out_path, target_k, max_v, bufsize, has_nvenc, fps=info.get("fps", 30), mode=mode, preset="p7", multipass="fullres", copy_audio=True)
    print(f"   ▶ Studio Quality Hardware Pipeline (NVDEC + NVENC p7 fullres 10-bit, mode={mode}, {target_k}k target, max={max_v}k)...")
    
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
    
    if p.returncode != 0:
        needs_aac = any(msg in stderr_out for msg in ["Could not find tag", "incompatible", "muxer does not support", "tag not found"])
        needs_safe_mode = any(msg in stderr_out.lower() for msg in ["unrecognized option", "option not found", "error splitting the argument list"])
        needs_preset_fallback = ("preset" in stderr_out.lower() or "multipass" in stderr_out.lower() or "p7" in stderr_out or needs_safe_mode)
        preset_to_use = "p5" if needs_preset_fallback else "p7"
        mp_to_use = "qres" if needs_preset_fallback else "fullres"

        print(f"   ⚠️ Retrying with adaptive fallback (audio={'aac' if needs_aac else 'copy'}, preset={preset_to_use}, safe_mode={needs_safe_mode})...")
        cmd = build_ffmpeg_cmd(in_path, out_path, target_k, max_v, bufsize, has_nvenc, fps=info.get("fps", 30), mode=mode, preset=preset_to_use, multipass=mp_to_use, safe_mode=needs_safe_mode, copy_audio=(not needs_aac))
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

def calculate_vmaf(ref_path, comp_path, sample_sec=60):
    try:
        fchk = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
        if "libvmaf" not in fchk.stdout:
            print("   ℹ️ [VMAF] libvmaf filter not available in current FFmpeg build. Skipping automated test.")
            return None

        print(f"   🎯 [VMAF] Running fast automated {sample_sec}s VMAF benchmark on Colab...")
        t_start = time.time()
        vmaf_log = f"/tmp/vmaf_{int(time.time()*1000)}.json"

        # Exact filter string matching Video_compression/VMAF_Test.ps1
        filter_str = (
            f"[0:v]setpts=PTS-STARTPTS,format=yuv420p10le[d];"
            f"[1:v]setpts=PTS-STARTPTS,format=yuv420p10le[rr];"
            f"[rr][d]scale2ref=flags=bicubic[r][d2];"
            f"[d2][r]libvmaf=log_path='{vmaf_log}':log_fmt=json:n_threads=4"
        )

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", "10", "-t", str(sample_sec), "-i", comp_path,
            "-ss", "10", "-t", str(sample_sec), "-i", ref_path,
            "-filter_complex", filter_str,
            "-f", "null", "-"
        ]

        subprocess.run(cmd, capture_output=True, text=True)

        if os.path.exists(vmaf_log):
            with open(vmaf_log, "r") as f:
                vdata = json.load(f)
            try: os.remove(vmaf_log)
            except Exception: pass

            score = None
            if "pooled_metrics" in vdata and "vmaf" in vdata["pooled_metrics"]:
                score = round(float(vdata["pooled_metrics"]["vmaf"].get("mean", 0)), 2)
            elif "VMAF score" in vdata:
                score = round(float(vdata["VMAF score"]), 2)

            if score and score > 0:
                elapsed = time.time() - t_start
                verdict = "Visually Lossless (95+)" if score >= 95 else ("High Quality (90+)" if score >= 90 else "Noticeable Compression")
                print(f"   🏆 [VMAF] Score: {score} / 100 ({verdict}) in {elapsed:.1f}s")
                return score
    except Exception as e:
        print(f"   ⚠️ [VMAF] Benchmark notice: {e}")
    return None

# ==============================================================================
# 🔄 PARALLEL JOB WORKER
# ==============================================================================
active_jobs = set()
jobs_lock = threading.Lock()

def process_single_task(task):
    task_id = task["task_id"]
    filename = task.get("filename", "video.mp4")
    with jobs_lock:
        active_jobs.add(task_id)
        current_active = len(active_jobs)

    print(f"\n📥 [NEW JOB ({current_active}/{MAX_CONCURRENT_JOBS})] Task {task_id}: {filename} ({task.get('mode', 'VBR')} Mode)")

    session = requests.Session()
    source_url = task["source_url"]
    if source_url.startswith("/"):
        source_url = f"{CLOUDFLOW_URL}{source_url}"

    in_path = f"/tmp/input_{task_id}.mp4"
    out_path = f"/tmp/compressed_{task_id}.mp4"

    try:
        print(f"   [{task_id}] Downloading source video from Cloudflow...")
        t_start = time.time()
        with session.get(source_url, stream=True, timeout=180) as r:
            r.raise_for_status()
            with open(in_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    f.write(chunk)

        orig_mb = os.path.getsize(in_path) / (1024 * 1024)
        print(f"   [{task_id}] Downloaded {orig_mb:.1f} MB. Starting 10-bit HEVC encode on {GPU_NAME}...")

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
            print(f"✅ [{task_id}] Finished! {orig_mb:.1f} MB -> {new_mb:.1f} MB ({saved_pct:.1f}% saved in {elapsed:.1f}s)")

            # Fast Colab VMAF Benchmark
            vmaf_val = calculate_vmaf(in_path, out_path, sample_sec=60)

            print(f"🚀 [{task_id}] Uploading compressed video back to Cloudflow...")
            with open(out_path, "rb") as f:
                files = {"file": (f"compressed_{filename}", f, "video/mp4")}
                form_data = {
                    "task_id": task_id,
                    "secret": WORKER_SECRET,
                    "orig_mb": orig_mb,
                    "new_mb": new_mb
                }
                if vmaf_val is not None:
                    form_data["vmaf"] = vmaf_val
                session.post(f"{CLOUDFLOW_URL}/api/gpu/complete", data=form_data, files=files, timeout=300)
            print(f"🎉 [{task_id}] Task completed and saved in Temp Cloud!\n")
        else:
            print(f"❌ [{task_id}] Compression failed or output empty.\n")

    except Exception as e:
        print(f"❌ [{task_id}] Error in worker thread: {e}")
    finally:
        for p in [in_path, out_path]:
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass
        with jobs_lock:
            active_jobs.discard(task_id)

def run_worker_loop():
    print(f"\n🟢 Colab GPU Worker is ACTIVE! Listening for up to {MAX_CONCURRENT_JOBS} simultaneous tasks from Cloudflow...\n")
    session = requests.Session()

    while True:
        try:
            with jobs_lock:
                current_active = len(active_jobs)
                can_accept_more = current_active < MAX_CONCURRENT_JOBS

            poll_payload = {
                "gpu_name": GPU_NAME,
                "secret": WORKER_SECRET,
                "heartbeat_only": not can_accept_more,
                "info": {"active_jobs": current_active, "max_jobs": MAX_CONCURRENT_JOBS}
            }

            poll_resp = session.post(
                f"{CLOUDFLOW_URL}/api/gpu/poll",
                json=poll_payload,
                timeout=15
            )

            if poll_resp.status_code == 200:
                data = poll_resp.json()
                task = data.get("task")
                if task:
                    # Spawn independent worker thread for this task
                    t = threading.Thread(target=process_single_task, args=(task,), daemon=True)
                    t.start()
                    time.sleep(0.3)
                    continue # immediately check if second parallel slot can be filled!

        except Exception:
            pass

        time.sleep(2.0)

if __name__ == "__main__":
    run_worker_loop()
