"""本地网页服务：python -m app.server"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import asr
from .bili import Bili, BiliError, parse_link
from .llm import LLM, fmt_ts, transcript_text

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("BILIAI_DATA") or (ROOT / "data"))
VIDEOS_DIR = DATA / "videos"
CONFIG_FILE = ROOT / "config.json"
STATIC = Path(__file__).resolve().parent / "static"
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------- 配置 ----------------
def load_dotenv():
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))


DEFAULT_CONFIG = {
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-chat",
    "sessdata": "",
    "asr_engine": "auto",
    "asr_model": "",
    "hf_mirror": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass
    apply_env(cfg)


_HF_DIRECT: Optional[bool] = None


def hf_direct_ok() -> bool:
    """huggingface.co 能直连就不用镜像：hf-mirror.com 对中国大陆以外的访问会 308 跳回
    huggingface.co，而 huggingface_hub 不跟随跨域跳转，结果模型下载失败。"""
    global _HF_DIRECT
    if _HF_DIRECT is None:
        import requests
        try:
            r = requests.head("https://huggingface.co/api/models/mlx-community/whisper-large-v3-turbo",
                              timeout=4, allow_redirects=False)
            _HF_DIRECT = r.status_code < 400
        except Exception:
            _HF_DIRECT = False
    return _HF_DIRECT


def apply_env(cfg: dict):
    # 注意：huggingface_hub 在 import 时就读取 HF_ENDPOINT，所以这里的改动只对启动后首次加载生效
    if os.environ.get("HF_ENDPOINT_USER"):
        return
    if cfg.get("hf_mirror") and not hf_direct_ok():
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    else:
        os.environ.pop("HF_ENDPOINT", None)


def api_key(cfg: dict) -> str:
    return cfg.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY", "")


def sessdata(cfg: dict) -> str:
    return cfg.get("sessdata") or os.environ.get("BILI_SESSDATA", "")


def make_llm(cfg: dict) -> LLM:
    return LLM(api_key(cfg), cfg.get("deepseek_base_url"), cfg.get("deepseek_model"))


load_dotenv()
if os.environ.get("HF_ENDPOINT"):
    os.environ["HF_ENDPOINT_USER"] = "1"  # 用户自己设置过镜像，就不覆盖
apply_env(load_config())


# ---------------- 存储 ----------------
VIDEOS: dict[str, dict] = {}
LOCK = threading.RLock()
RUNNING: set[str] = set()


def vpath(vid: str) -> Path:
    return VIDEOS_DIR / f"{vid}.json"


def save(v: dict):
    with LOCK:
        tmp = vpath(v["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
        tmp.replace(vpath(v["id"]))


def load_all():
    for f in VIDEOS_DIR.glob("*.json"):
        try:
            v = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if v.get("status") not in ("done", "error"):
            v["status"] = "error"
            v["message"] = "上次处理被中断，点「重新处理」再试"
        VIDEOS[v["id"]] = v


load_all()


def update(v: dict, persist: bool = False, **kw):
    with LOCK:
        v.update(kw)
        v["updated_at"] = time.time()
        if persist:
            save(v)


# ---------------- 处理流程 ----------------
def process(vid: str, force_asr: bool = False, only_summary: bool = False):
    v = VIDEOS[vid]
    cfg = load_config()
    try:
        if not only_summary:
            bili = Bili(sessdata(cfg))
            update(v, status="info", message="获取视频信息…", progress=None, error=None)
            info = bili.video_info(v["bvid"], v["page"])
            update(v, persist=True, **info)

            segs, src = None, ""
            if not force_asr:
                update(v, status="subtitle", message="查找字幕…")
                segs, src = bili.subtitles(info["bvid"], info["cid"])
            if segs:
                kind, _, label = src.partition(":")
                update(v, persist=True, transcript=segs, source_type=kind, source_label=label)
            else:
                reason = src or "已选择强制本地转写"
                engine = asr.pick_engine(cfg.get("asr_engine", "auto"))
                update(v, status="download", message=f"{reason}；改为下载音频本地转写…", progress=0)
                with tempfile.TemporaryDirectory() as td:
                    path = os.path.join(td, "audio.m4a")
                    bili.download_audio(info["bvid"], info["cid"], path,
                                        progress=lambda p: update(v, progress=p))
                    msg = "本地语音转写中（首次使用需要下载模型，可能要几分钟）…"
                    update(v, status="asr", message=msg, progress=0, asr_started=time.time())
                    segs, name = asr.transcribe(path, engine, cfg.get("asr_model", ""),
                                                progress=lambda p: update(v, progress=p))
                if not segs:
                    raise RuntimeError("转写结果为空（视频可能没有人声）")
                update(v, persist=True, transcript=segs, source_type="asr", source_label=name, subtitle_reason=reason)

        update(v, status="summarizing", message="正在生成总结…", progress=None, summary="")
        llm = make_llm(cfg)
        last = [0.0]

        def on_text(t):
            v["summary"] = t
            if time.time() - last[0] > 2:
                last[0] = time.time()
                save(v)

        summary = llm.summarize(v, on_text, on_status=lambda m: update(v, message=m))
        update(v, persist=True, status="done", message="", summary=summary, progress=None)
    except (BiliError, RuntimeError) as e:
        update(v, persist=True, status="error", message=str(e))
    except Exception as e:
        traceback.print_exc()
        update(v, persist=True, status="error", message=f"{type(e).__name__}: {e}")
    finally:
        RUNNING.discard(vid)


def start(vid: str, **kw):
    if vid in RUNNING:
        return
    RUNNING.add(vid)
    threading.Thread(target=process, args=(vid,), kwargs=kw, daemon=True).start()


# ---------------- API ----------------
app = FastAPI(title="B站视频AI助手")


class AddReq(BaseModel):
    url: str
    force_asr: bool = False


class ChatReq(BaseModel):
    message: str


class ConfigReq(BaseModel):
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = None
    deepseek_model: Optional[str] = None
    sessdata: Optional[str] = None
    asr_engine: Optional[str] = None
    asr_model: Optional[str] = None
    hf_mirror: Optional[bool] = None


def mask(s: str) -> str:
    return (s[:4] + "…" + s[-4:]) if len(s) > 10 else ("已设置" if s else "")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def get_config():
    cfg = load_config()
    key = api_key(cfg)
    try:
        engine = asr.pick_engine(cfg.get("asr_engine", "auto"))
    except RuntimeError:
        engine = "未安装"
    return {
        "deepseek_key_set": bool(key),
        "deepseek_key_source": "设置页" if cfg.get("deepseek_api_key") else ("环境变量 DEEPSEEK_API_KEY" if key else ""),
        "deepseek_key_mask": mask(key),
        "deepseek_base_url": cfg["deepseek_base_url"],
        "deepseek_model": cfg["deepseek_model"],
        "sessdata_set": bool(sessdata(cfg)),
        "sessdata_mask": mask(sessdata(cfg)),
        "asr_engine": cfg["asr_engine"],
        "asr_engine_active": engine,
        "asr_model": cfg["asr_model"],
        "asr_default_model": asr.DEFAULT_MLX_MODEL if engine == "mlx" else asr.DEFAULT_FW_MODEL,
        "hf_mirror": cfg["hf_mirror"],
    }


@app.post("/api/config")
def set_config(req: ConfigReq):
    cfg = load_config()
    for k, val in req.dict().items():
        if val is None:
            continue
        cfg[k] = val.strip() if isinstance(val, str) else val
    save_config(cfg)
    return get_config()


@app.get("/api/check-bili")
def check_bili():
    b = Bili(sessdata(load_config()))
    return {"logged_in": b.is_logged_in()}


@app.get("/api/videos")
def list_videos():
    items = sorted(VIDEOS.values(), key=lambda v: v.get("created_at", 0), reverse=True)
    return [{k: v.get(k) for k in ("id", "title", "owner", "pic", "status", "duration", "created_at")} for v in items]


@app.post("/api/videos")
def add_video(req: AddReq):
    try:
        bvid, page = parse_link(req.url)
    except BiliError as e:
        raise HTTPException(400, str(e))
    vid = f"{bvid}_p{page}"
    with LOCK:
        v = VIDEOS.get(vid)
        if v and not req.force_asr and v.get("status") in ("done",) :
            return {"id": vid, "existing": True}
        if not v:
            v = {"id": vid, "bvid": bvid, "page": page, "title": bvid, "created_at": time.time(),
                 "chat": [], "summary": "", "transcript": []}
            VIDEOS[vid] = v
        update(v, status="queued", message="排队中…", error=None)
    start(vid, force_asr=req.force_asr)
    return {"id": vid, "existing": False}


def get_v(vid: str) -> dict:
    v = VIDEOS.get(vid)
    if not v:
        raise HTTPException(404, "找不到这个视频")
    return v


@app.get("/api/videos/{vid}")
def get_video(vid: str, light: bool = False):
    v = get_v(vid)
    with LOCK:
        d = dict(v)
    d["running"] = vid in RUNNING
    if light:
        d.pop("transcript", None)
        d["transcript_len"] = len(v.get("transcript") or [])
    return d


@app.post("/api/videos/{vid}/retry")
def retry(vid: str, force_asr: bool = False):
    v = get_v(vid)
    update(v, status="queued", message="排队中…")
    start(vid, force_asr=force_asr)
    return {"ok": True}


@app.post("/api/videos/{vid}/resummarize")
def resummarize(vid: str):
    v = get_v(vid)
    if not v.get("transcript"):
        raise HTTPException(400, "还没有文字稿")
    update(v, status="queued", message="排队中…")
    start(vid, only_summary=True)
    return {"ok": True}


@app.delete("/api/videos/{vid}")
def delete_video(vid: str):
    get_v(vid)
    with LOCK:
        VIDEOS.pop(vid, None)
        vpath(vid).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/videos/{vid}/chat/clear")
def clear_chat(vid: str):
    v = get_v(vid)
    update(v, persist=True, chat=[])
    return {"ok": True}


@app.post("/api/videos/{vid}/chat")
def chat(vid: str, req: ChatReq):
    v = get_v(vid)
    q = req.message.strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    if not v.get("transcript"):
        raise HTTPException(400, "视频还没处理完，暂时不能提问")
    try:
        llm = make_llm(load_config())
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    history = list(v.get("chat") or [])

    def gen():
        out = ""
        try:
            for t in llm.chat(v, history, q):
                out += t
                yield t
        except Exception as e:
            err = f"\n\n> 出错了：{type(e).__name__}: {e}"
            out += err
            yield err
        finally:
            with LOCK:
                v.setdefault("chat", []).extend([
                    {"role": "user", "content": q, "ts": time.time()},
                    {"role": "assistant", "content": out, "ts": time.time()},
                ])
                save(v)

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/videos/{vid}/export.md")
def export_md(vid: str):
    v = get_v(vid)
    url = f"https://www.bilibili.com/video/{v['bvid']}" + (f"?p={v['page']}" if v.get("page", 1) > 1 else "")
    parts = [f"# {v.get('title')}", "", f"- UP主：{v.get('owner', '')}", f"- 链接：{url}",
             f"- 时长：{fmt_ts(v.get('duration') or 0)}", f"- 文字稿来源：{v.get('source_label', '')}", "",
             v.get("summary") or "", ""]
    if v.get("chat"):
        parts += ["## 问答记录", ""]
        for m in v["chat"]:
            parts += [("**问：** " if m["role"] == "user" else "**答：** ") + m["content"], ""]
    parts += ["## 文字稿", "", "```", transcript_text(v.get("transcript") or []), "```"]
    name = f"{v['bvid']}.md"
    return PlainTextResponse("\n".join(parts), media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import uvicorn

    port = int(os.environ.get("BILIAI_PORT", "8765"))
    print(f"\n  B站视频AI助手已启动：http://127.0.0.1:{port}\n  关闭本窗口即可退出。\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
