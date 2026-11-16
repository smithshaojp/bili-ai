"""DeepSeek（OpenAI 兼容接口）：总结与问答。"""
from __future__ import annotations

import re
from typing import Callable, Iterator, Optional

from openai import OpenAI

# 中文约 0.6~0.7 token/字，DeepSeek 上下文 128K，留足余量
DIRECT_LIMIT = 90_000      # 文字稿字数在此以内：整篇直接交给模型
CHUNK_CHARS = 30_000       # 超长时分段总结的每段字数
CHAT_CONTEXT_CHARS = 60_000  # 超长视频问答时检索出的原文上限


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def transcript_text(segs: list[dict]) -> str:
    """把字幕合并成带时间戳的行，每行约 15~25 秒，省 token 又保留定位。"""
    lines, buf, start = [], [], None
    for s in segs:
        if start is None:
            start = s["s"]
        buf.append(s["t"])
        if s["e"] - start >= 15 or sum(len(x) for x in buf) > 120:
            lines.append(f"[{fmt_ts(start)}] {' '.join(buf)}")
            buf, start = [], None
    if buf:
        lines.append(f"[{fmt_ts(start)}] {' '.join(buf)}")
    return "\n".join(lines)


SYSTEM_BASE = "你是一个擅长整理视频内容的中文助手。"

SUMMARY_PROMPT = """下面是一个 B站 视频的信息和带时间戳的文字稿（{source}）。
请用简体中文输出 Markdown 格式的总结，结构如下：

## 一句话总结
（用一句话说清这个视频讲了什么、结论是什么）

## 核心要点
（3~8 条，每条一句到两句，信息密度要高，必要时附时间戳）

## 分段大纲
（按话题而不是按固定时长划分，每段格式：`- [mm:ss] 小标题：一两句内容概括`）

## 值得注意
（UP 主给出的关键数据、观点、建议、争议点或易被忽略的细节；没有就写“无”）

要求：
- 时间戳必须来自文字稿里真实存在的时间，格式为 [mm:ss] 或 [h:mm:ss]。
- 文字稿可能有语音识别错别字，请根据上下文理解（例如同音字），但不要编造文字稿里没有的内容。
- 不要复述“这个视频”“UP主说”之类的废话，直接写内容。

【标题】{title}
【UP主】{owner}
【简介】{desc}

【文字稿】
{transcript}
"""

CHUNK_PROMPT = """下面是一个长视频《{title}》文字稿的第 {i}/{n} 部分。请用简体中文提炼这一部分的内容，输出：
- 按话题划分的分段笔记，每条格式 `- [mm:ss] 小标题：要点`（时间戳取自原文）
- 关键数据、观点、结论尽量保留细节
不要编造原文没有的内容。

【文字稿片段】
{transcript}
"""

FINAL_FROM_NOTES = """下面是一个长视频的分段笔记（已按顺序排列，时间戳来自原视频）。
请基于这些笔记，按要求输出最终总结。

{format_req}

【标题】{title}
【UP主】{owner}
【简介】{desc}

【分段笔记】
{notes}
"""

CHAT_SYSTEM = """你是用户的视频学习助手。用户正在看 B站 视频《{title}》（UP主：{owner}）。
下面是该视频{scope}，你要基于它回答用户的问题。

回答规则：
- 优先依据视频内容回答，引用具体内容时附上时间戳，格式 [mm:ss] 或 [h:mm:ss]，用户可以点击跳转。
- 如果视频里没有讲到，要明确说“视频里没有提到”，然后可以用你自己的知识补充，并标明“（视频外补充）”。
- 文字稿可能有语音识别错别字，按上下文理解。
- 用简体中文，回答简洁直接，用户追问再展开。

【视频总结】
{summary}

【文字稿】
{transcript}
"""


class LLM:
    def __init__(self, api_key: str, base_url: str, model: str):
        if not api_key:
            raise RuntimeError("没有配置 DeepSeek API Key：请设置环境变量 DEEPSEEK_API_KEY，或在网页右上角「设置」里填写")
        self.client = OpenAI(api_key=api_key, base_url=base_url or "https://api.deepseek.com")
        self.model = model or "deepseek-chat"

    def stream(self, messages: list[dict], temperature: float = 0.5) -> Iterator[str]:
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, stream=True, temperature=temperature,
        )
        for chunk in resp:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            text = getattr(delta, "content", None)
            if text:
                yield text

    def complete(self, prompt: str) -> str:
        return "".join(self.stream([{"role": "system", "content": SYSTEM_BASE},
                                    {"role": "user", "content": prompt}], temperature=0.3))

    # ---------- 总结 ----------
    def summarize(self, video: dict, on_text: Callable[[str], None],
                  on_status: Optional[Callable[[str], None]] = None) -> str:
        text = transcript_text(video["transcript"])
        source = {"cc": "UP主/人工字幕", "ai": "B站AI字幕", "asr": "本地语音识别"}.get(video.get("source_type"), "字幕")
        meta = dict(title=video["title"], owner=video.get("owner", ""), desc=(video.get("desc") or "")[:500])
        if len(text) <= DIRECT_LIMIT:
            prompt = SUMMARY_PROMPT.format(source=source, transcript=text, **meta)
        else:
            chunks = split_lines(text, CHUNK_CHARS)
            notes = []
            for i, c in enumerate(chunks, 1):
                if on_status:
                    on_status(f"视频较长，正在分段提炼 {i}/{len(chunks)}…")
                notes.append(self.complete(CHUNK_PROMPT.format(title=video["title"], i=i, n=len(chunks), transcript=c)))
            fmt = SUMMARY_PROMPT.split("【标题】")[0].split("：\n", 1)[1]
            prompt = FINAL_FROM_NOTES.format(format_req=fmt, notes="\n\n".join(notes), **meta)
        if on_status:
            on_status("正在生成总结…")
        out = ""
        for t in self.stream([{"role": "system", "content": SYSTEM_BASE},
                              {"role": "user", "content": prompt}], temperature=0.3):
            out += t
            on_text(out)
        return out

    # ---------- 问答 ----------
    def chat(self, video: dict, history: list[dict], question: str) -> Iterator[str]:
        text = transcript_text(video["transcript"])
        if len(text) <= DIRECT_LIMIT:
            scope, ctx = "的完整文字稿", text
        else:
            scope = "文字稿中与问题最相关的片段（视频很长，只节选了部分）"
            recent = " ".join(m["content"] for m in history[-2:] if m["role"] == "user")
            ctx = retrieve(text, question + " " + recent, CHAT_CONTEXT_CHARS)
        system = CHAT_SYSTEM.format(title=video["title"], owner=video.get("owner", ""), scope=scope,
                                    summary=video.get("summary") or "（暂无）", transcript=ctx)
        msgs = [{"role": "system", "content": system}]
        msgs += [{"role": m["role"], "content": m["content"]} for m in history[-20:]]
        msgs.append({"role": "user", "content": question})
        yield from self.stream(msgs)


def split_lines(text: str, size: int) -> list[str]:
    chunks, cur = [], []
    n = 0
    for line in text.split("\n"):
        if n + len(line) > size and cur:
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line)
        n += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def retrieve(text: str, query: str, limit: int) -> str:
    """超长视频问答用：按 2 字词重叠度挑出最相关的段落，保持原时间顺序。"""
    blocks = split_lines(text, 1500)
    q = _bigrams(query)
    scored = []
    for i, b in enumerate(blocks):
        bg = _bigrams(b)
        score = len(q & bg) / (len(q) ** 0.5 + 1) if q else 0
        scored.append((score, i))
    scored.sort(reverse=True)
    picked, total = set(), 0
    # 永远带上开头一段（通常是视频主旨）
    for _, i in [(0, 0)] + scored:
        if i in picked:
            continue
        if total + len(blocks[i]) > limit:
            continue
        picked.add(i)
        total += len(blocks[i])
    return "\n…\n".join(blocks[i] for i in sorted(picked))
