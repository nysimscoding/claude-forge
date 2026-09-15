#!/usr/bin/env python3
"""
dualsub — 영상/오디오/유튜브에서 이중자막(영문+한글) 생성.

파이프라인: 오디오 확보 → <24MB 청크 분할 → Groq whisper-large-v3 전사(증분·retry-after 준수)
            → 세그먼트 병합 → Groq LLM으로 EN→KO 번역(증분) → 이중 VTT/SRT/TXT 출력.

사용:
  python3 dualsub.py <video|audio|youtube-url> [--outdir DIR] [--name NAME]
                     [--src en] [--to ko] [--model whisper-large-v3]
                     [--llm openai/gpt-oss-120b] [--no-translate] [--chunk-sec 3000]

키: GROQ_API_KEY 환경변수, 없으면 ~/.config/watch/.env 에서 로드.
증분 산출물은 <outdir>/.dualsub/ 아래에 캐시되어 중단 시 재개된다.
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

GROQ = "https://api.groq.com/openai/v1"
MAX_BYTES = 24 * 1024 * 1024

def log(*a): print("[dualsub]", *a, file=sys.stderr, flush=True)

def load_key():
    k = os.environ.get("GROQ_API_KEY", "").strip()
    if k: return k
    env = Path.home() / ".config/watch/.env"
    if env.exists():
        for ln in env.read_text().splitlines():
            if ln.startswith("GROQ_API_KEY="):
                return ln.split("=", 1)[1].strip()
    sys.exit("GROQ_API_KEY 없음 — 환경변수 또는 ~/.config/watch/.env 에 설정하세요.")

def sh(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"명령 실패: {' '.join(cmd[:3])}...\n{r.stderr[:400]}")
    return r

def is_url(s): return s.startswith("http://") or s.startswith("https://")

def ensure_audio(src, work):
    """입력을 64k mono mp3로 정규화. URL이면 yt-dlp로 오디오 다운로드."""
    out = work / "audio.mp3"
    if out.exists() and out.stat().st_size > 0:
        return out
    if is_url(src):
        log("유튜브 오디오 다운로드…")
        sh(["yt-dlp", "--no-playlist", "-x", "--audio-format", "mp3",
            "--audio-quality", "64K", "-o", str(work / "audio.%(ext)s"), src])
        if not out.exists():
            cand = list(work.glob("audio.*"))
            if cand: cand[0].rename(out)
    else:
        log("ffmpeg 오디오 추출(64k mono)…")
        sh(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-b:a", "64k", str(out)])
    if not out.exists(): sys.exit("오디오 확보 실패")
    return out

def split_chunks(audio, work, chunk_sec):
    chunks = sorted(work.glob("chunk_*.mp3"))
    if chunks: return chunks
    log(f"{chunk_sec}s 청크 분할…")
    sh(["ffmpeg", "-y", "-i", str(audio), "-f", "segment",
        "-segment_time", str(chunk_sec), "-c", "copy", str(work / "chunk_%03d.mp3")])
    return sorted(work.glob("chunk_*.mp3"))

def http_post_multipart(url, key, fields, files):
    boundary = "----dualsub" + str(int(time.time()))
    body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, path in files.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{Path(path).name}\"\r\nContent-Type: audio/mpeg\r\n\r\n".encode()
        body += Path(path).read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {key}",
        "User-Agent": "curl/8.7.1",
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    return urllib.request.urlopen(req, timeout=600)

def transcribe(chunks, work, key, model, chunk_sec):
    segs = []
    for ch in chunks:
        idx = int(re.search(r"chunk_(\d+)", ch.name).group(1))
        offset = idx * chunk_sec
        cache = work / f"{ch.stem}.json"
        if cache.exists():
            segs += json.loads(cache.read_text())["segments"]; continue
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = http_post_multipart(f"{GROQ}/audio/transcriptions", key,
                    {"model": model, "response_format": "verbose_json", "language": "en", "temperature": "0"},
                    {"file": str(ch)})
                d = json.loads(resp.read())
                cs = [{"start": s.get("start", 0)+offset, "end": s.get("end", 0)+offset,
                       "text": s.get("text", "").strip()} for s in d.get("segments", [])]
                cache.write_text(json.dumps({"segments": cs}, ensure_ascii=False))
                segs += cs
                log(f"  {ch.name}: {len(cs)} segs")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    ra = int(e.headers.get("retry-after", "60") or "60")
                    log(f"  429 rate limit — {ra+15}s 대기 (attempt {attempt})")
                    if attempt >= 40: sys.exit("429 과다 — 재실행 시 재개")
                    time.sleep(ra + 15)
                else:
                    log(f"  HTTP {e.code}: {e.read()[:200]}")
                    if attempt >= 5: sys.exit("전사 실패")
                    time.sleep(15)
    return sorted(segs, key=lambda s: s["start"])

REASONING_MODELS = re.compile(r"gpt-oss|qwen3|deepseek-r1|reasoning", re.I)

def groq_chat(key, model, messages, temperature=0.2, max_tokens=8192):
    """(content, finish_reason)를 반환. reasoning 모델은 추론 토큰을 최소화한다.

    max_completion_tokens를 명시하지 않으면 reasoning 모델이 추론에 출력 예산을
    모두 써서 본문이 중간에 잘린다(finish_reason=length).
    """
    payload = {"model": model, "messages": messages, "temperature": temperature,
               "max_completion_tokens": max_tokens}
    if REASONING_MODELS.search(model):
        payload["reasoning_effort"] = "low"
    req = urllib.request.Request(f"{GROQ}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "User-Agent": "curl/8.7.1",
                 "Content-Type": "application/json"})
    for attempt in range(1, 41):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=180).read())
            ch = d["choices"][0]
            return (ch["message"].get("content") or ""), ch.get("finish_reason")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = int(e.headers.get("retry-after", "30") or "30")
                log(f"  번역 429 — {ra+5}s 대기"); time.sleep(ra + 5)
            else:
                raise
    sys.exit("번역 429 과다")

def parse_numbered(text, n):
    """번호 매긴 응답을 1..n 인덱스 맵으로 파싱한다."""
    lines = {}
    for ln in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.*)", ln)
        if m:
            k, v = int(m.group(1)), m.group(2).strip()
            if 1 <= k <= n and v: lines[k] = v
    return lines

def translate_block(key, llm, lang_name, block):
    """한 배치를 번역한다. 누락분은 배치를 절반씩 줄여 재시도한다.

    반환: (번역 리스트, 끝내 실패한 인덱스 리스트)
    """
    out = [""] * len(block)
    pending = [j for j, s in enumerate(block) if s.strip()]
    size = len(block)
    for attempt in range(4):
        if not pending: break
        truncated = False
        for k in range(0, len(pending), size):
            grp = pending[k:k+size]
            numbered = "\n".join(f"{n+1}. {block[j]}" for n, j in enumerate(grp))
            prompt = (f"Translate these subtitle lines to natural {lang_name}. "
                      f"Keep technical terms (code, tool names) as-is. "
                      f"Return EXACTLY {len(grp)} numbered lines, same order, "
                      f"no preamble, no extra text.\n\n{numbered}")
            content, finish = groq_chat(key, llm, [{"role": "user", "content": prompt}])
            got = parse_numbered(content, len(grp))
            for n, j in enumerate(grp):
                if got.get(n+1): out[j] = got[n+1]
            truncated = truncated or finish == "length"
        pending = [j for j in pending if not out[j].strip()]
        if pending:
            size = max(1, size // 2)
            why = "응답 잘림" if truncated else "파싱 누락"
            log(f"  {why} {len(pending)}건 — 배치 {size}로 재시도")
    return out, pending

def translate(segs, work, key, llm, to_lang, batch=40):
    texts = [s["text"] for s in segs]
    cache = work / "ko.json"
    done = json.loads(cache.read_text()) if cache.exists() else {}
    lang_name = {"ko": "Korean", "en": "English", "ja": "Japanese"}.get(to_lang, to_lang)

    def cached_ok(i, block):
        arr = done.get(str(i))
        return (arr is not None and len(arr) == len(block)
                and all(t.strip() for s, t in zip(block, arr) if s.strip()))

    failed = 0
    for i in range(0, len(texts), batch):
        block = texts[i:i+batch]
        if cached_ok(i, block): continue
        out, pending = translate_block(key, llm, lang_name, block)
        done[str(i)] = out
        cache.write_text(json.dumps(done, ensure_ascii=False))
        failed += len(pending)
        tail = f" — 미해결 {len(pending)}건" if pending else ""
        log(f"  번역 {i+len(block)}/{len(texts)}{tail}")
    if failed:
        log(f"경고: {failed}개 자막을 번역하지 못해 해당 줄은 원문을 유지합니다.")
    ko = []
    for i in range(0, len(texts), batch):
        block = texts[i:i+batch]
        arr = done.get(str(i), [])
        arr = arr + [""] * (len(block) - len(arr))
        ko += [t if t.strip() else s for s, t in zip(block, arr)]
    return ko

def ts_vtt(sec):
    h=int(sec//3600); m=int(sec%3600//60); s=sec%60; return f"{h:02d}:{m:02d}:{s:06.3f}"
def ts_srt(sec):
    h=int(sec//3600); m=int(sec%3600//60); s=int(sec%60); ms=int((sec-int(sec))*1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
def ts_txt(sec):
    h=int(sec//3600); m=int(sec%3600//60); s=int(sec%60); return f"{h:01d}:{m:02d}:{s:02d}"

def write_outputs(segs, ko, outdir, name, translate_on):
    en_vtt = outdir / f"{name}.en.vtt"; ko_vtt = outdir / f"{name}.ko.vtt"
    dual_vtt = outdir / f"{name}.dual.vtt"; dual_srt = outdir / f"{name}.dual.srt"
    en_txt = outdir / f"{name}.en.txt"
    with open(en_vtt,"w") as fe, open(en_txt,"w") as ft:
        fe.write("WEBVTT\n\n")
        for i,s in enumerate(segs,1):
            if not s["text"]: continue
            fe.write(f"{i}\n{ts_vtt(s['start'])} --> {ts_vtt(s['end'])}\n{s['text']}\n\n")
            ft.write(f"[{ts_txt(s['start'])}] {s['text']}\n")
    outs = [en_vtt, en_txt]
    if translate_on:
        with open(ko_vtt,"w") as fk, open(dual_vtt,"w") as fd, open(dual_srt,"w") as fs:
            fk.write("WEBVTT\n\n"); fd.write("WEBVTT\n\n")
            for i,(s,k) in enumerate(zip(segs,ko),1):
                if not s["text"]: continue
                fk.write(f"{i}\n{ts_vtt(s['start'])} --> {ts_vtt(s['end'])}\n{k}\n\n")
                fd.write(f"{i}\n{ts_vtt(s['start'])} --> {ts_vtt(s['end'])}\n{s['text']}\n{k}\n\n")
                fs.write(f"{i}\n{ts_srt(s['start'])} --> {ts_srt(s['end'])}\n{s['text']}\n{k}\n\n")
        outs += [ko_vtt, dual_vtt, dual_srt]
    return outs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--name", default=None)
    ap.add_argument("--to", default="ko")
    ap.add_argument("--model", default="whisper-large-v3")
    ap.add_argument("--llm", default="openai/gpt-oss-120b")
    ap.add_argument("--no-translate", action="store_true")
    ap.add_argument("--chunk-sec", type=int, default=3000)
    a = ap.parse_args()

    outdir = Path(a.outdir); outdir.mkdir(parents=True, exist_ok=True)
    name = a.name or (re.sub(r"[^\w.-]", "_", Path(a.input).stem)[:60] if not is_url(a.input) else "video")
    work = outdir / ".dualsub" / name; work.mkdir(parents=True, exist_ok=True)
    key = load_key()

    audio = ensure_audio(a.input, work)
    chunks = split_chunks(audio, work, a.chunk_sec)
    log(f"{len(chunks)} 청크 전사 시작…")
    segs = transcribe(chunks, work, key, a.model, a.chunk_sec)
    log(f"전사 완료: {len(segs)} 세그먼트")
    ko = []
    if not a.no_translate:
        log(f"EN→{a.to} 번역…")
        ko = translate(segs, work, key, a.llm, a.to)
    outs = write_outputs(segs, ko, outdir, name, not a.no_translate)
    log("완료. 산출물:")
    for o in outs: print(o)

if __name__ == "__main__":
    main()
