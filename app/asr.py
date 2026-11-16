"""本地语音转写。Apple 芯片优先用 mlx-whisper（GPU 加速），否则用 faster-whisper（CPU）。"""
from __future__ import annotations

import platform
import threading
from typing import Callable, Optional

import numpy as np

SR = 16000
PROMPT = "以下是普通话的句子，使用简体中文和标点符号。"

DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_FW_MODEL = "small"

_model_cache: dict = {}
_lock = threading.Lock()  # 同一时间只跑一个转写任务，避免内存爆掉


def has_mlx() -> bool:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except Exception:
        return False


def has_faster_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def pick_engine(pref: str) -> str:
    if pref == "mlx" and has_mlx():
        return "mlx"
    if pref == "faster-whisper" and has_faster_whisper():
        return "faster-whisper"
    if has_mlx():
        return "mlx"
    if has_faster_whisper():
        return "faster-whisper"
    raise RuntimeError("没有安装语音转写引擎（mlx-whisper 或 faster-whisper），请重新运行 start.command 安装依赖")


def load_audio(path: str) -> np.ndarray:
    """用 PyAV 解码任意音频为 16k 单声道 float32，不依赖 ffmpeg 命令。"""
    import av

    out = []
    with av.open(path) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SR)
        for frame in container.decode(stream):
            for f in resampler.resample(frame):
                out.append(f.to_ndarray().reshape(-1))
        for f in resampler.resample(None):
            out.append(f.to_ndarray().reshape(-1))
    if not out:
        raise RuntimeError("音频解码结果为空")
    return np.concatenate(out).astype(np.float32) / 32768.0


def _dedupe(segs: list[dict]) -> list[dict]:
    """去掉 Whisper 在静音/音乐处常见的连续重复幻觉。"""
    res: list[dict] = []
    for s in segs:
        t = s["t"].strip()
        if not t:
            continue
        if len(res) >= 2 and res[-1]["t"] == t and res[-2]["t"] == t:
            continue
        res.append({"s": round(s["s"], 2), "e": round(s["e"], 2), "t": t})
    return res


def transcribe(path: str, engine_pref: str = "auto", model: str = "",
               progress: Optional[Callable[[float], None]] = None) -> tuple[list[dict], str]:
    engine = pick_engine(engine_pref)
    audio = load_audio(path)
    total = len(audio) / SR
    with _lock:
        if engine == "mlx":
            segs = _mlx(audio, model or DEFAULT_MLX_MODEL, total, progress)
            name = f"mlx-whisper {model or DEFAULT_MLX_MODEL.split('/')[-1]}"
        else:
            segs = _fw(audio, model or DEFAULT_FW_MODEL, total, progress)
            name = f"faster-whisper {model or DEFAULT_FW_MODEL}"
    return _dedupe(segs), name


def _mlx(audio, model, total, progress):
    import mlx_whisper

    chunk = 10 * 60 * SR  # 分 10 分钟一段跑，方便显示进度
    segs = []
    for start in range(0, len(audio), chunk):
        piece = audio[start:start + chunk]
        offset = start / SR
        r = mlx_whisper.transcribe(
            piece, path_or_hf_repo=model, language="zh", initial_prompt=PROMPT,
            condition_on_previous_text=False, verbose=None,
        )
        for s in r.get("segments", []):
            segs.append({"s": s["start"] + offset, "e": s["end"] + offset, "t": s["text"]})
        if progress:
            progress(min(1.0, (start + len(piece)) / len(audio)))
    return segs


def _fw(audio, model, total, progress):
    from faster_whisper import WhisperModel

    key = ("fw", model)
    if key not in _model_cache:
        _model_cache.clear()
        _model_cache[key] = WhisperModel(model, device="cpu", compute_type="int8")
    m = _model_cache[key]
    segments, _info = m.transcribe(
        audio, language="zh", initial_prompt=PROMPT, vad_filter=True,
        condition_on_previous_text=False, beam_size=5,
    )
    segs = []
    for s in segments:
        segs.append({"s": s.start, "e": s.end, "t": s.text})
        if progress and total:
            progress(min(1.0, s.end / total))
    return segs
