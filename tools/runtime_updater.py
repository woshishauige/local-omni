#!/usr/bin/env python
"""Runtime auto-updater for Qwen-Local: discover, prove, adopt, record — or refuse.

The rule this module implements, in the order it must be applied:

  1. discover   — the newest release of the track, with an asset for this platform;
  2. verify     — download it, check the publisher's digest, and run the *same* 20-item probe
                  that once caught a build whose output was garbage;
  3. compare    — a paired `llama-bench` against the installed build, two rounds each, interleaved;
  4. adopt      — only when correctness is unchanged and generation (or prompt speed) improved;
  5. record     — every decision, with numbers, appended to a JSONL so the history is auditable;
  6. roll back  — the previous build is never deleted, and a failed activation restores the config.

Never restart the owner's service on its own: a new runtime only takes effect at the next start,
and `--apply-restart` exists for the explicit case where that is wanted.

Usage:
  python tools/runtime_updater.py --track all --dry-run
  python tools/runtime_updater.py --track stock
  python tools/runtime_updater.py --track fork --candidate-tag prism-b10826-c9c34bc
  python tools/runtime_updater.py --track stock --apply-restart
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import zipfile

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = pathlib.Path(r"D:\AI\_bonsai-eval")
STAGING = BASE / "runtime-incoming"
STATE = BASE / "runtime-state.json"
LOG = BASE / "runtime-update-log.jsonl"
SUBSET = BASE / "mmlu" / "subset.json"
UA = {"user-agent": "Mozilla/5.0"}

TRACKS = {
    "stock": {
        "repo": "ggml-org/llama.cpp",
        "asset": re.compile(r"llama-b\d+-bin-win-(cuda-[0-9.]+|cpu)-x64\.zip$"),
        "prefer": "cuda-13.4",
        "config_const": "EXECUTABLE",
        "fallback_dir": "runtime-next",
        "profile": "qwen3.5-9b-uncensored",
    },
    "fork": {
        "repo": "PrismML-Eng/llama.cpp",
        "asset": re.compile(r"llama-prism-b\d+-\w+-bin-win-cuda-[0-9.]+-x64\.zip$"),
        "prefer": "cuda-13.3",
        "config_const": "PRISM_EXECUTABLE",
        "fallback_dir": "runtime-prism-10826",
        "profile": "qwen3.8-27b",
    },
}

# A candidate must match the installed build within this many items on the probe, and must not
# produce unparsable or garbage answers at all. Generation improvement is what buys adoption.
MAX_PROBE_REGRESSION = 2
MIN_TG_GAIN = 2.0
MIN_PP_GAIN = 3.0


# --------------------------------------------------------------------------- helpers


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def free_mib():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=30)
    try:
        return int(out.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def wait_for_card(need=8000, limit=1800):
    deadline = time.time() + limit
    while time.time() < deadline:
        try:
            sleeping = bool(httpx.get("http://127.0.0.1:8082/props", timeout=5,
                                      trust_env=False).json().get("is_sleeping"))
        except Exception:
            sleeping = True
        if sleeping and free_mib() >= need:
            return True
        time.sleep(20)
    return False


def build_version(exe: pathlib.Path) -> int:
    out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60)
    match = re.search(r"build (\d+)", (out.stdout or "") + (out.stderr or ""))
    return int(match.group(1)) if match else 0


def installed_exe(track: str) -> pathlib.Path:
    """What the app is actually configured to use, resolved from qwen_config itself."""
    sys.path.insert(0, str(ROOT))
    import qwen_config as config  # noqa: PLC0415 — imported lazily so importing this module is cheap
    return pathlib.Path(getattr(config, TRACKS[track]["config_const"]))


def github(path, params=None):
    response = httpx.get(f"https://api.github.com{path}", params=params, timeout=90, headers=UA,
                         trust_env=False)
    return response.json() if response.status_code == 200 else None


def newest_release(track: str, tag: str = ""):
    """Newest release of the track that ships a Windows asset, or one specific tag."""
    spec = TRACKS[track]
    path = (f"/repos/{spec['repo']}/releases/tags/{tag}" if tag
            else f"/repos/{spec['repo']}/releases")
    payload = github(path, None if tag else {"per_page": 8})
    releases = [payload] if tag and payload else (payload or [])
    if not isinstance(releases, list):
        return None
    for release in releases:
        assets = release.get("assets") or []
        windows = [asset for asset in assets if spec["asset"].search(asset.get("name", ""))]
        if not windows:
            continue
        # prefer the CUDA generation we already have runtime DLLs for
        windows.sort(key=lambda asset: (spec["prefer"] not in asset["name"], asset["name"]))
        asset = windows[0]
        release_tag = release.get("tag_name", "")
        match = re.search(r"b(\d+)", release_tag)
        return {"tag": release_tag, "build": int(match.group(1)) if match else 0,
                "published": str(release.get("published_at"))[:10],
                "asset": asset, "body": release.get("body") or ""}
    return None


def download(asset, target: pathlib.Path) -> bool:
    """Resume-capable download, then verify the publisher's digest."""
    expected = int(asset.get("size") or 0)
    digest = (asset.get("digest") or "").split(":")[-1]
    for prefix in ("https://gh-proxy.com/", ""):
        have = target.stat().st_size if target.exists() else 0
        if expected and have >= expected:
            break
        headers = {"user-agent": "Mozilla/5.0"}
        if have:
            headers["range"] = f"bytes={have}-"
        try:
            with httpx.stream("GET", prefix + asset["browser_download_url"], headers=headers,
                              timeout=300, follow_redirects=True, trust_env=False) as response:
                if response.status_code not in (200, 206):
                    log(f"    {prefix or 'direct'}: HTTP {response.status_code}")
                    continue
                mode = "ab" if have and response.status_code == 206 else "wb"
                with target.open(mode) as handle:
                    for chunk in response.iter_bytes(1 << 20):
                        handle.write(chunk)
            if expected and target.stat().st_size == expected:
                break
        except (httpx.HTTPError, OSError) as exc:
            log(f"    {prefix or 'direct'}: {type(exc).__name__}（已存 "
                f"{target.stat().st_size if target.exists() else 0} B，换路续传）")
    if not target.exists() or (expected and target.stat().st_size != expected):
        return False
    if digest:
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest:
            log(f"    digest 不符: {actual[:16]}… != {digest[:16]}…")
            return False
        log("    digest 校验通过")
    return True


def extract(zip_path: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as bundle:
        bundle.extractall(target)
    while not (target / "llama-server.exe").is_file() and any(p.is_dir() for p in target.iterdir()):
        inner = next(p for p in target.iterdir() if p.is_dir())
        for item in inner.iterdir():
            shutil.move(str(item), str(target / item.name))
        inner.rmdir()
    # the zips ship without the CUDA runtime; borrow the same-generation DLLs we already run
    for name in ("cublas64_13.dll", "cublasLt64_13.dll", "cudart64_13.dll", "libomp.dll"):
        source = ROOT / "runtime" / name
        if source.is_file() and not (target / name).is_file():
            shutil.copy2(source, target / name)
    return target / "llama-server.exe"


def profile_command(profile: str, port: int, spec: str = "ngram-mod"):
    sys.path.insert(0, str(ROOT))
    import qwen_config as config  # noqa: PLC0415
    settings = {**config.read_settings(), "model_profile": profile}
    command = [str(part) for part in config.server_command("text", settings=settings)]
    if "--port" in command:
        command[command.index("--port") + 1] = str(port)
    else:
        command += ["--port", str(port)]
    if "--spec-type" in command:
        command[command.index("--spec-type") + 1] = spec
    return command


LETTER = re.compile(r"^[\s*`\"'(]*([ABCD])(?![A-Za-z])")
THINK = ("<think", "The question asks", "Let's analyze", "Let me ", "We need", "To answer")


def probe(exe: pathlib.Path, profile: str, port: int, limit: int = 20):
    """20 MMLU items through the candidate, judged on answers, not just on not crashing."""
    command = profile_command(profile, port)
    command[0] = str(exe)
    log_path = STAGING / f"probe-{exe.parent.name}-{port}.log"
    with log_path.open("wb") as handle:
        process = subprocess.Popen(command, cwd=str(ROOT), stdout=handle,
                                   stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 600
        while time.time() < deadline:
            try:
                props = httpx.get(f"http://127.0.0.1:{port}/props", timeout=6,
                                  trust_env=False).json()
                if props.get("default_generation_settings", {}).get("n_ctx"):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(3)
        else:
            return {"error": "load timeout", "log": str(log_path)}
        items = json.loads(SUBSET.read_text(encoding="utf-8"))["items"][:limit]
        correct = unparsed = thinking = garbage = 0
        for item in items:
            prompt = ("以下是一道单项选择题。请只回答正确选项的字母（A/B/C/D），不要解释。\n\n"
                      f"{item['question']}\nA. {item['choices'][0]}\nB. {item['choices'][1]}\n"
                      f"C. {item['choices'][2]}\nD. {item['choices'][3]}")
            reply = httpx.post(f"http://127.0.0.1:{port}/v1/chat/completions", timeout=600,
                               trust_env=False, json={
                                   "messages": [{"role": "user", "content": prompt}],
                                   "temperature": 0.0, "max_tokens": 24,
                                   "chat_template_kwargs": {"enable_thinking": False,
                                                            "preserve_thinking": False}})
            text = ""
            if reply.status_code == 200:
                message = reply.json()["choices"][0]["message"]
                text = (message.get("content") or "") or (message.get("reasoning_content") or "")
            found = LETTER.match(text.strip())
            letter = found.group(1) if found else ""
            if letter:
                correct += int(letter == "ABCD"[item["answer"]])
            else:
                unparsed += 1
            thinking += int(text.lstrip().startswith("<think")
                            or any(marker in text[:60] for marker in THINK))
            garbage += int("////" in text or "\\\\" in text)
        return {"correct": correct, "unparsed": unparsed, "thinking": thinking,
                "garbage": garbage, "items": len(items), "log": str(log_path)}
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
        time.sleep(3)


def bench(exe_dir: pathlib.Path, profile: str, rounds: int = 2) -> dict:
    """llama-bench on the *profile's own* model and layer count — not a hardcoded one."""
    exe = exe_dir / "llama-bench.exe"
    if not exe.is_file():
        return {"error": "llama-bench missing"}
    reference = profile_command(profile, 0)
    model = reference[reference.index("-m") + 1]
    layers = reference[reference.index("-ngl") + 1] if "-ngl" in reference else "99"
    trend = {}
    for round_index in range(rounds):
        command = [str(exe), "-m", model, "-ngl", layers, "-fa", "on", "-t", "16",
                   "-p", "512", "-n", "128", "-r", "2", "-ctk", "q8_0", "-ctv", "q8_0", "-o", "json"]
        out = subprocess.run(command, capture_output=True, text=True, timeout=1800)
        if out.returncode != 0:
            return {"error": (out.stdout or out.stderr or "")[-160:]}
        rows = json.loads(out.stdout)
        for row in rows:
            key = "pp" if (row.get("n_prompt") or 0) else "tg" if (row.get("n_gen") or 0) else None
            if key:
                trend.setdefault(key, []).append(float(row.get("avg_ts") or 0))
    result = {key: sum(values) / len(values) for key, values in trend.items()}
    result["rounds"] = {key: [round(value, 2) for value in values]
                        for key, values in trend.items()}
    result["model"] = pathlib.Path(model).name
    result["ngl"] = layers
    return result


def bench_pair(old_dir: pathlib.Path, new_dir: pathlib.Path, profile: str, rounds: int = 2):
    """Interleaved A,B,B,A … so thermal drift cannot favour whichever build runs last.

    The first version measured both rounds of the installed build and then both rounds of the
    candidate; its very first decision (+11.2% prompt speed for a build nobody expected to help)
    was produced that way, which is exactly the kind of ordering bias this version removes.
    """
    order = []
    for index in range(rounds):
        order.extend([("old", old_dir), ("new", new_dir)] if index % 2 == 0
                     else [("new", new_dir), ("old", old_dir)])
    samples = {"old": {}, "new": {}}
    for label, directory in order:
        result = bench(directory, profile, rounds=1)
        if "error" in result:
            return {"old": {"error": result["error"]}, "new": {"error": result["error"]}}
        for key in ("pp", "tg"):
            if result.get(key):
                samples[label].setdefault(key, []).append(result[key])
    summary = {}
    for label in ("old", "new"):
        summary[label] = {key: sum(values) / len(values)
                          for key, values in samples[label].items()}
        summary[label]["rounds"] = samples[label]
    return summary


def decide(probe_new, probe_old, bench_new, bench_old):
    """The whole policy in one place, so it can be argued with and unit-tested."""
    reasons = []
    if not probe_new or "error" in probe_new:
        return False, f"候选探针失败: {(probe_new or {}).get('error', 'no result')}"
    if probe_new["unparsed"] or probe_new["garbage"]:
        return False, (f"候选输出异常：未解析 {probe_new['unparsed']}、垃圾 {probe_new['garbage']}"
                       f"（正确 {probe_new['correct']}/{probe_new['items']}）")
    if probe_old and "error" not in probe_old:
        if probe_new["correct"] < probe_old["correct"] - MAX_PROBE_REGRESSION:
            return False, (f"正确率下降过多：候选 {probe_new['correct']} vs 现役 "
                           f"{probe_old['correct']}（允许差 {MAX_PROBE_REGRESSION}）")
        reasons.append(f"正确率 {probe_new['correct']}/{probe_new['items']} vs "
                       f"{probe_old['correct']}/{probe_old['items']}")
    if not bench_new or not bench_old or "error" in bench_new or "error" in bench_old:
        return False, "基准数据缺失，无法证明提升"
    gains = {}
    for key in ("pp", "tg"):
        old, new = bench_old.get(key), bench_new.get(key)
        if old and new:
            gains[key] = (new - old) / old * 100
    if gains.get("tg", 0) >= MIN_TG_GAIN or gains.get("pp", 0) >= MIN_PP_GAIN:
        return True, (f"tg {gains.get('tg', 0):+.1f}% / pp {gains.get('pp', 0):+.1f}%"
                      f"（阈值 tg≥{MIN_TG_GAIN}% 或 pp≥{MIN_PP_GAIN}%）")
    return False, (f"提升不足：tg {gains.get('tg', 0):+.1f}% / pp {gains.get('pp', 0):+.1f}%"
                   f"（阈值 tg≥{MIN_TG_GAIN}% 或 pp≥{MIN_PP_GAIN}%）")


def record(entry):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except ValueError:
            state = {}
    state.setdefault(entry["track"], {})
    state[entry["track"]].update({
        "active_build": entry.get("active_build"),
        "active_dir": entry.get("active_dir"),
        "checked_at": entry["at"],
        "last_decision": entry["decision"],
        "last_reason": entry["reason"],
    })
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def activate(track: str, exe: pathlib.Path) -> tuple[bool, str]:
    """Point the app at another binary by editing the one constant, with a compile gate.

    The config file is the single source of truth for which build runs, so the switch is a
    one-line edit; the original text is kept so a failed verification can be undone exactly.
    """
    config_path = ROOT / "qwen_config.py"
    original = config_path.read_text(encoding="utf-8")
    name = TRACKS[track]["config_const"]
    pattern = re.compile(rf'^{name} = ROOT / "[^"]+"$', re.M)
    replacement = f'{name} = ROOT / "{exe.parent.name}/{exe.name}"'
    updated, count = pattern.subn(replacement, original)
    if count != 1:
        return False, f"配置里没找到唯一一条 {name} 定义（找到 {count} 条）"
    config_path.write_text(updated, encoding="utf-8")
    compile_check = subprocess.run([sys.executable, "-m", "py_compile", str(config_path)],
                                   capture_output=True, text=True, timeout=120)
    if compile_check.returncode != 0:
        config_path.write_text(original, encoding="utf-8")
        return False, "改完配置后 py_compile 失败，已还原"
    tests = subprocess.run([sys.executable, "-m", "unittest", "test_context_config"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    if tests.returncode != 0:
        config_path.write_text(original, encoding="utf-8")
        return False, "配置单测未通过，已还原"
    return True, replacement


def rollback(track: str) -> bool:
    """Go back to the build that was active before the last adoption, if known."""
    previous = ""
    if STATE.exists():
        try:
            previous = (json.loads(STATE.read_text(encoding="utf-8")).get(track) or {}).get(
                "previous_dir") or ""
        except ValueError:
            previous = ""
    candidates = [previous, TRACKS[track]["fallback_dir"]]
    for name in candidates:
        if not name:
            continue
        exe = ROOT / name / "llama-server.exe"
        if exe.is_file():
            ok, detail = activate(track, exe)
            log(f"  回退到 {name}: {'成功' if ok else detail}")
            return ok
    return False


# --------------------------------------------------------------------------- the run


def run_track(track: str, args) -> dict:
    spec = TRACKS[track]
    log(f"=== 轨道 {track}（{spec['repo']}，profile={spec['profile']}）")
    current = installed_exe(track)
    current_build = build_version(current)
    log(f"  现役: {current.parent.name} build {current_build}")

    release = newest_release(track, args.candidate_tag)
    if not release:
        return {"track": track, "decision": "skip", "reason": "取不到 release 信息"}
    log(f"  最新: {release['tag']}（{release['published']}）build {release['build']}")
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "track": track,
             "active_build": current_build, "active_dir": current.parent.name,
             "candidate": release["tag"], "candidate_build": release["build"],
             "published": release["published"], "asset": release["asset"]["name"]}
    if release["build"] <= current_build:
        log("  已是最新，无需更新")
        entry.update({"decision": "current",
                      "reason": f"最新 build {release['build']} ≤ 现役 {current_build}"})
        if not args.dry_run:
            record(entry)
        return entry

    asset = release["asset"]
    zip_path = STAGING / asset["name"]
    STAGING.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        log(f"  [dry-run] 会下载 {asset['name']}（{int(asset.get('size') or 0)/1e6:.1f} MB）"
            f" 到 {zip_path}")
        entry.update({"decision": "dry-run", "reason": "只报告，不动手"})
        return entry

    if not (zip_path.exists() and int(asset.get("size") or 0) == zip_path.stat().st_size):
        log(f"  下载 {asset['name']}（{int(asset.get('size') or 0)/1e6:.1f} MB）")
        if not download(asset, zip_path):
            entry.update({"decision": "error", "reason": "下载或 digest 校验失败"})
            record(entry)
            return entry
    staged = STAGING / zip_path.stem
    exe = extract(zip_path, staged)
    candidate_build = build_version(exe)
    if candidate_build != release["build"]:
        entry.update({"decision": "error", "reason": f"解压后版本不符：{candidate_build} != {release['build']}"})
        record(entry)
        return entry

    if not wait_for_card():
        entry.update({"decision": "error", "reason": "显卡一直没空出来"})
        record(entry)
        return entry

    log("  自校准探针：现役 20 题")
    probe_old = probe(current, spec["profile"], port=18099)
    log(f"    {probe_old}")
    log("  候选探针：20 题")
    probe_new = probe(exe, spec["profile"], port=18100)
    log(f"    {probe_new}")
    log("  配对基准（交替顺序 A,B,B,A）")
    pair = bench_pair(current.parent, staged, spec["profile"])
    bench_old, bench_new = pair["old"], pair["new"]
    log(f"    现役 {bench_old}")
    log(f"    候选 {bench_new}")
    adopt, reason = decide(probe_new, probe_old, bench_new, bench_old)
    entry.update({"probe_old": probe_old, "probe_new": probe_new,
                  "bench_old": bench_old, "bench_new": bench_new,
                  "decision": "adopt" if adopt else "keep", "reason": reason})
    log(f"  决定: {'升级' if adopt else '保持现役'} — {reason}")

    if adopt:
        target = ROOT / f"runtime-{track}-{candidate_build}"
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(staged), str(target))
        ok, detail = activate(track, target / "llama-server.exe")
        if not ok:
            entry.update({"decision": "rollback", "reason": f"激活失败：{detail}"})
            log(f"  激活失败（{detail}），保持原样")
        else:
            entry.update({"active_build": candidate_build, "active_dir": target.name,
                          "previous_dir": current.parent.name,
                          "activated": True, "restart_required": not args.apply_restart})
            log(f"  已切换到 {target.name}；{'已按要求重启' if args.apply_restart else '重启后生效'}")
            if args.apply_restart:
                restart_service(entry, track)
    record(entry)
    return entry


def restart_service(entry, track):
    """Only when asked: stop through the app's own script, start through its scheduled task."""
    log("  重启服务……")
    subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "qwen_service.py"), "stop"],
                   capture_output=True, text=True, timeout=300)
    time.sleep(10)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Start-ScheduledTask -TaskName QwenService"], capture_output=True, text=True,
                   timeout=120)
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
            client = httpx.Client(follow_redirects=False, timeout=30, trust_env=False)
            client.get(link)
            status = client.get("http://127.0.0.1:8081/local/status", timeout=20).json()
            if status.get("state") == "ready":
                entry.update({"restarted": True, "restart_status": status.get("model_profile")})
                log(f"  服务已就绪：{status.get('model_profile')}")
                return
        except (httpx.HTTPError, ValueError, OSError):
            pass
        time.sleep(5)
    entry.update({"restarted": False, "restart_status": "timeout"})
    log("  重启后 10 分钟未就绪——可执行 --rollback 回退")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--track", choices=["stock", "fork", "all"], default="all")
    parser.add_argument("--candidate-tag", default="", help="指定 tag（默认取最新）")
    parser.add_argument("--dry-run", action="store_true", help="只发现与报告，不下载不动配置")
    parser.add_argument("--apply-restart", action="store_true", help="升级后重启服务使其生效")
    parser.add_argument("--rollback", action="store_true", help="把指定轨道退回备份目录")
    args = parser.parse_args()

    tracks = ["stock", "fork"] if args.track == "all" else [args.track]
    if args.rollback:
        for track in tracks:
            ok = rollback(track)
            print(f"{track}: {'已回退' if ok else '回退失败（备份目录不在？）'}")
        return 0
    results = []
    for track in tracks:
        try:
            results.append(run_track(track, args))
        except Exception as exc:  # the module itself must not take the app down
            print(f"{track}: 模块异常 {type(exc).__name__}: {exc}")
            results.append({"track": track, "decision": "error", "reason": str(exc)})
    print("\n=== 汇总 ===")
    for entry in results:
        print(f"  {entry['track']:<6} {entry.get('decision'):<8} "
              f"{entry.get('active_dir', '')} → {entry.get('candidate', '')}  {entry['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
