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

# Ensure Colab NVIDIA driver paths are properly accessible in LD_LIBRARY_PATH
cuda_paths = ["/usr/lib64-nvidia", "/usr/local/cuda/lib64", "/usr/lib/x86_64-linux-gnu"]
existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
paths_to_add = [p for p in cuda_paths if os.path.exists(p) and p not in existing_ld]
if paths_to_add:
    os.environ["LD_LIBRARY_PATH"] = ":".join(paths_to_add) + (f":{existing_ld}" if existing_ld else "")

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

def ensure_compatible_ffmpeg():
    # Test if current ffmpeg supports NVENC on the local Tesla T4
    test_cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=640x360:rate=24",
        "-c:v", "hevc_nvenc", "-f", "null", "-"
    ]
    res = subprocess.run(test_cmd, capture_output=True, text=True)
    if res.returncode == 0:
        print("✅ Current FFmpeg is 100% compatible with Tesla T4 NVENC driver!")
        return True

    print("⚡ Incompatible FFmpeg detected (NVENC API 13.1 vs driver 13.0). Restoring clean driver-matched Ubuntu FFmpeg...")
    try:
        subprocess.run("rm -f /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/bin/ffmpeg /usr/bin/ffprobe", shell=True)
        subprocess.run("apt-get update -qq && apt-get install -y -qq --reinstall ffmpeg", shell=True)
        res2 = subprocess.run(test_cmd, capture_output=True, text=True)
        if res2.returncode == 0:
            print("✅ Clean driver-matched FFmpeg restored and verified operational on Tesla T4!")
            return True
        else:
            print(f"⚠️ Notice after restore: {res2.stderr[:200]}")
    except Exception as e:
        print(f"⚠️ Error restoring system FFmpeg: {e}")
    return False

ensure_compatible_ffmpeg()

VMAF_FFMPEG_BIN = "ffmpeg"

def ensure_vmaf_engine():
    global VMAF_FFMPEG_BIN
    try:
        fchk = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
        if "libvmaf" in fchk.stdout:
            VMAF_FFMPEG_BIN = "ffmpeg"
            return True
    except Exception:
        pass

    vmaf_path = "/usr/local/bin/ffmpeg-vmaf"
    if os.path.exists(vmaf_path):
        VMAF_FFMPEG_BIN = vmaf_path
        return True

    print("⚡ Installing standalone VMAF quality benchmark engine (/usr/local/bin/ffmpeg-vmaf)...")
    try:
        setup_cmd = (
            "wget -q -c -O /tmp/vmaf_pkg.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz && "
            "tar -xJf /tmp/vmaf_pkg.tar.xz -C /tmp && "
            "cp /tmp/ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ffmpeg-vmaf && "
            "chmod +x /usr/local/bin/ffmpeg-vmaf && "
            "rm -rf /tmp/vmaf_pkg.tar.xz /tmp/ffmpeg-*-amd64-static"
        )
        subprocess.run(setup_cmd, shell=True, capture_output=True, text=True)
        if os.path.exists(vmaf_path):
            VMAF_FFMPEG_BIN = vmaf_path
            print("✅ VMAF benchmark engine ready!")
            return True
    except Exception:
        pass
    return False

ensure_vmaf_engine()

def probe_video(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate,codec_type,bit_rate:format=bit_rate,duration",
        "-of", "json", path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(res.stdout)
        v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
        a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
        fmt = data.get("format", {})
        fps_str = v.get("avg_frame_rate", "30/1")
        fps_eval = eval(fps_str) if "/" in fps_str else float(fps_str or 30)
        return {
            "codec": v.get("codec_name", "unknown"),
            "acodec": a.get("codec_name", "none"),
            "width": int(v.get("width", 0)),
            "height": int(v.get("height", 0)),
            "fps": max(1, int(round(fps_eval))),
            "duration": float(fmt.get("duration", 0)),
            "size_mb": round(os.path.getsize(path) / (1024 * 1024), 2)
        }
    except Exception:
        return {"width": 1920, "height": 1080, "fps": 30, "duration": 0, "size_mb": 0, "codec": "unknown", "acodec": "none"}

def probe_nvenc_flags():
    flags = []
    try:
        res = subprocess.run(["ffmpeg", "-h", "encoder=hevc_nvenc"], capture_output=True, text=True)
        out = res.stdout
        if "-spatial_aq" in out: flags += ["-spatial_aq", "1"]
        elif "-spatial-aq" in out: flags += ["-spatial-aq", "1"]

        if "-temporal_aq" in out: flags += ["-temporal_aq", "1"]
        elif "-temporal-aq" in out: flags += ["-temporal-aq", "1"]

        if "-aq-strength" in out: flags += ["-aq-strength", "7"]
        if "-b_ref_mode" in out: flags += ["-b_ref_mode", "middle"]
        elif "-b-ref-mode" in out: flags += ["-b-ref-mode", "middle"]
        if "-multipass" in out: flags += ["-multipass", "fullres"]
    except Exception:
        pass
    return flags

def get_vfr_flag():
    try:
        res = subprocess.run(["ffmpeg", "-h"], capture_output=True, text=True)
        if "-fps_mode" in res.stdout: return ["-fps_mode", "vfr"]
    except Exception:
        pass
    return ["-vsync", "vfr"]

NVENC_DYNAMIC_FLAGS = probe_nvenc_flags()
VFR_FLAG = get_vfr_flag()

def build_ffmpeg_cmd(in_path, out_path, target_k, max_v, bufsize, has_nvenc, fps=24, mode="VBR", preset="p7", multipass="fullres", safe_mode=False, copy_audio=True):
    vcodec = "hevc_nvenc" if has_nvenc else "libx264"
    gop = max(30, int(fps * 5))
    keyint_min = max(1, int(fps))

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

    if has_nvenc:
        if safe_mode:
            # ⚡ Robust standard 8-bit YUV420p NVENC
            cmd = [
                "ffmpeg", "-y", "-loglevel", "warning", "-progress", "-", "-hide_banner",
                "-i", in_path,
                "-map", "0:v:0",
                "-map", "0:a?",
                *VFR_FLAG,
                "-c:v", vcodec,
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-pix_fmt", "yuv420p",
                "-preset", "p5",
                *rc_opts,
            ]
        else:
            # 💎 Studio Quality 10-bit HEVC NVENC
            cmd = [
                "ffmpeg", "-y", "-loglevel", "warning", "-progress", "-", "-hide_banner",
                "-i", in_path,
                "-map", "0:v:0",
                "-map", "0:a?",
                *VFR_FLAG,
                "-c:v", vcodec,
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-profile:v", "main10",
                "-pix_fmt", "p010le",
                "-preset", preset,
                "-tune", "hq",
                *rc_opts,
                "-rc-lookahead", "32",
                "-bf", "3",
                "-g", str(gop),
                "-keyint_min", str(keyint_min),
                *NVENC_DYNAMIC_FLAGS,
                "-tag:v:0", "hvc1",
            ]
    else:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning", "-progress", "-", "-hide_banner",
            "-i", in_path,
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-b:v", f"{target_k}k",
            "-maxrate", f"{max_v}k",
            "-bufsize", f"{bufsize}k",
        ]

    if copy_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]

    cmd += [
        "-map_metadata", "-1",
        "-movflags", "+faststart",
        out_path
    ]
    return cmd

def compress_video(in_path, out_path, task, report_progress_fn):
    info = probe_video(in_path)
    target_k = int(task.get("target_bitrate_k", 1500))
    if info.get("fps", 30) > 45:
        target_k = int(target_k * 1.5)
        
    # EXACT MATCH with Video_compression.bat: MAX_V = TARGET * 2, BUFSIZE = MAX_V * 2
    max_v = target_k * 2
    bufsize = max_v * 2

    has_nvenc = ("NVIDIA" in GPU_NAME or "Tesla" in GPU_NAME)
    duration = info.get("duration", 0)
    mode = task.get("mode", "VBR")

    print(f"   📹 Input: {info.get('width')}x{info.get('height')} @ {info.get('fps')}fps, video={info.get('codec')}, audio={info.get('acodec')}, size={info.get('size_mb')}MB")

    def _run_cmd(ffmpeg_args):
        p_proc = subprocess.Popen(ffmpeg_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _fps = "0"
        _spd = "0x"
        _tme = "00:00:00"
        _pct = 0.0
        while True:
            line = p_proc.stdout.readline()
            if not line and p_proc.poll() is not None:
                break
            line = line.strip()
            if line.startswith("fps="):
                _fps = line.split("=")[1].strip()
            elif line.startswith("speed="):
                _spd = line.split("=")[1].strip()
            elif line.startswith("out_time="):
                _tme = line.split("=")[1].strip().split(".")[0]
                if duration > 0:
                    parts = _tme.split(":")
                    if len(parts) == 3:
                        try:
                            sec = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                            _pct = min(99.0, round((sec / duration) * 100.0, 1))
                        except Exception:
                            pass
            elif line.startswith("progress=continue"):
                report_progress_fn(_pct, _fps, _spd, _tme)
        err = p_proc.stderr.read()
        return p_proc.returncode, err

    # Try 1: Studio Quality 10-bit NVENC Pipeline
    cmd = build_ffmpeg_cmd(in_path, out_path, target_k, max_v, bufsize, has_nvenc, fps=info.get("fps", 30), mode=mode, preset="p7", multipass="fullres", safe_mode=False, copy_audio=True)
    print(f"   ▶ Studio Quality Hardware Pipeline (NVENC p7 10-bit, mode={mode}, {target_k}k target)...")
    rc, stderr_out = _run_cmd(cmd)

    # Try 2: Robust High-Speed GPU NVENC Pipeline (p5 8-bit, universal stream compatibility, still 100% on GPU!)
    if rc != 0 and has_nvenc:
        print(f"   ❌ Primary NVENC failed (code {rc}). Stderr output:")
        for line in stderr_out.splitlines()[-15:]:
            if line.strip():
                print(f"      {line.strip()}")
        print(f"   ⚡ Retrying with universal GPU NVENC (Tesla T4 p5 8-bit YUV420p)...")
        cmd = build_ffmpeg_cmd(in_path, out_path, target_k, max_v, bufsize, has_nvenc, fps=info.get("fps", 30), mode=mode, preset="p5", multipass=None, safe_mode=True, copy_audio=False)
        rc, stderr_out = _run_cmd(cmd)

    # Try 3: Ultra-compatible CPU Software Fallback (libx264)
    if rc != 0:
        print(f"   ❌ Secondary NVENC failed (code {rc}). Stderr output:")
        for line in stderr_out.splitlines()[-15:]:
            if line.strip():
                print(f"      {line.strip()}")
        print(f"   ⚠️ Falling back to CPU software encoder (libx264)...")
        cmd = build_ffmpeg_cmd(in_path, out_path, target_k, max_v, bufsize, has_nvenc=False, fps=info.get("fps", 30), mode=mode, safe_mode=True, copy_audio=False)
        rc, stderr_out = _run_cmd(cmd)

    if rc != 0:
        print(f"❌ FFmpeg exit code: {rc}")
        if stderr_out:
            print(f"   Error details: {stderr_out[-500:]}")
        return False

    return os.path.exists(out_path) and os.path.getsize(out_path) > 1000

def ensure_vmaf_model():
    model_path = "/tmp/vmaf_v0.6.1.json"
    if not os.path.exists(model_path):
        try:
            url = "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.json"
            subprocess.run(["wget", "-q", "-O", model_path, url], timeout=15)
        except Exception:
            pass
    return model_path if os.path.exists(model_path) else None

def calculate_vmaf(ref_path, comp_path, sample_sec=30):
    global VMAF_FFMPEG_BIN
    try:
        fchk = subprocess.run([VMAF_FFMPEG_BIN, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=5)
        if "libvmaf" not in fchk.stdout:
            print(f"   ℹ️ [VMAF] libvmaf filter not available in {VMAF_FFMPEG_BIN}. Skipping automated test.")
            return None

        print(f"   🎯 [VMAF] Running fast {sample_sec}s automated benchmark (subsample=4)...")
        t_start = time.time()
        vmaf_log = f"/tmp/vmaf_{int(time.time()*1000)}.json"

        # Direct, universal VMAF filter without deprecated scale2ref
        filter_str = f"[0:v]setpts=PTS-STARTPTS[d];[1:v]setpts=PTS-STARTPTS[r];[d][r]libvmaf=log_path='{vmaf_log}':log_fmt=json:n_subsample=4:n_threads=4"

        cmd = [
            VMAF_FFMPEG_BIN, "-y", "-nostdin", "-hide_banner",
            "-ss", "10", "-t", str(sample_sec), "-i", comp_path,
            "-ss", "10", "-t", str(sample_sec), "-i", ref_path,
            "-filter_complex", filter_str,
            "-f", "null", "-"
        ]

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=35)

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
        else:
            if res.stderr:
                err_lines = [l.strip() for l in res.stderr.splitlines() if "error" in l.lower() or "vmaf" in l.lower()]
                if err_lines:
                    print(f"   ⚠️ [VMAF] Note: {' '.join(err_lines[-2:])}")
    except subprocess.TimeoutExpired:
        print("   ⚠️ [VMAF] Benchmark timed out after 35s. Proceeding directly with upload.")
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

            # Fast Colab VMAF Benchmark (30s sample with subsampling = under 10s execution!)
            vmaf_val = calculate_vmaf(in_path, out_path, sample_sec=30)

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
