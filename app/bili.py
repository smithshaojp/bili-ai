"""B站接口：解析链接、视频信息、字幕、音频下载。"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from typing import Callable, Optional

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


class BiliError(Exception):
    pass


def parse_link(text: str) -> tuple[str, int]:
    """从链接/BV号/av号中解析出 (bvid 或 'av123', 分P序号)。"""
    text = text.strip()
    if "b23.tv" in text or "bili2233.cn" in text:
        m = re.search(r"https?://[^\s]+", text)
        if not m:
            raise BiliError("无法识别短链接")
        try:
            r = requests.get(m.group(0), headers={"User-Agent": UA}, allow_redirects=True, timeout=15)
            text = r.url
        except requests.RequestException as e:
            raise BiliError(f"短链接解析失败：{e}")
    page = 1
    m = re.search(r"[?&]p=(\d+)", text)
    if m:
        page = max(1, int(m.group(1)))
    m = re.search(r"(BV[0-9A-Za-z]{10})", text)
    if m:
        return m.group(1), page
    m = re.search(r"av(\d+)", text, re.I)
    if m:
        return "av" + m.group(1), page
    raise BiliError("没有在输入中找到 BV 号或 av 号")


class Bili:
    def __init__(self, sessdata: str = ""):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        sessdata = (sessdata or "").strip()
        if sessdata.startswith("SESSDATA="):
            sessdata = sessdata[len("SESSDATA="):]
        if sessdata:
            self.s.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
        self._wbi: Optional[tuple[str, str, float]] = None
        self._buvid_done = False

    # ---------- 基础 ----------
    def _ensure_buvid(self):
        if self._buvid_done:
            return
        self._buvid_done = True
        try:
            d = self.s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=10).json()["data"]
            self.s.cookies.set("buvid3", d["b_3"], domain=".bilibili.com")
            self.s.cookies.set("buvid4", d["b_4"], domain=".bilibili.com")
        except Exception:
            pass

    def _get(self, url: str, params: dict | None = None, wbi: bool = False) -> dict:
        self._ensure_buvid()
        params = dict(params or {})
        if wbi:
            params = self._sign(params)
        try:
            r = self.s.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            raise BiliError(f"网络请求失败：{e}")
        if r.status_code == 412:
            raise BiliError("被 B站 风控拦截（412），稍等几分钟再试，或在设置里填写 SESSDATA")
        try:
            j = r.json()
        except ValueError:
            raise BiliError(f"B站返回了非 JSON 内容（HTTP {r.status_code}）")
        if j.get("code") != 0:
            raise BiliError(f"B站接口错误 {j.get('code')}：{j.get('message')}")
        return j.get("data") or {}

    def _wbi_keys(self) -> tuple[str, str]:
        if self._wbi and time.time() - self._wbi[2] < 3600:
            return self._wbi[0], self._wbi[1]
        r = self.s.get("https://api.bilibili.com/x/web-interface/nav", timeout=15).json()
        img = r["data"]["wbi_img"]
        stem = lambda u: u.rsplit("/", 1)[1].split(".")[0]
        self._wbi = (stem(img["img_url"]), stem(img["sub_url"]), time.time())
        return self._wbi[0], self._wbi[1]

    def _sign(self, params: dict) -> dict:
        img_key, sub_key = self._wbi_keys()
        orig = img_key + sub_key
        mixin = "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        params["wts"] = round(time.time())
        params = dict(sorted(params.items()))
        params = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
        query = urllib.parse.urlencode(params)
        params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
        return params

    # ---------- 业务 ----------
    def is_logged_in(self) -> bool:
        try:
            d = self.s.get("https://api.bilibili.com/x/web-interface/nav", timeout=10).json()
            return bool(d.get("data", {}).get("isLogin"))
        except Exception:
            return False

    def video_info(self, vid: str, page: int = 1) -> dict:
        params = {"aid": vid[2:]} if vid.lower().startswith("av") else {"bvid": vid}
        d = self._get("https://api.bilibili.com/x/web-interface/view", params)
        pages = d.get("pages") or []
        if not pages:
            raise BiliError("视频没有分P信息")
        if page > len(pages):
            page = 1
        p = pages[page - 1]
        title = d["title"]
        if len(pages) > 1:
            title = f"{title}（P{page} {p.get('part', '')}）"
        return {
            "bvid": d["bvid"],
            "aid": d["aid"],
            "cid": p["cid"],
            "page": page,
            "page_count": len(pages),
            "title": title,
            "owner": (d.get("owner") or {}).get("name", ""),
            "desc": d.get("desc", ""),
            "pic": d.get("pic", ""),
            "duration": p.get("duration") or d.get("duration", 0),
            "pubdate": d.get("pubdate", 0),
        }

    def subtitles(self, bvid: str, cid: int) -> tuple[Optional[list], str]:
        """返回 (字幕段列表, 来源说明)。没有可用字幕返回 (None, 原因)。"""
        last_reason = "该视频没有字幕"
        for attempt in range(2):  # B站偶尔返回空 subtitle_url，重试一次
            d = self._get("https://api.bilibili.com/x/player/wbi/v2", {"bvid": bvid, "cid": cid}, wbi=True)
            subs = ((d.get("subtitle") or {}).get("subtitles")) or []
            if not subs:
                if not d.get("login_mid"):
                    last_reason = "未登录：B站的 AI 字幕需要登录才能获取（请在设置里填写 SESSDATA）"
                break

            def rank(s):
                lan = s.get("lan", "")
                is_ai = lan.startswith("ai-")
                is_zh = "zh" in lan
                return (0 if is_zh else 1, 1 if is_ai else 0)

            subs.sort(key=rank)
            for s in subs:
                url = s.get("subtitle_url") or ""
                if not url:
                    continue
                if url.startswith("//"):
                    url = "https:" + url
                try:
                    body = self.s.get(url, timeout=15).json().get("body") or []
                except Exception:
                    continue
                segs = [
                    {"s": float(b["from"]), "e": float(b["to"]), "t": str(b["content"]).strip()}
                    for b in body
                    if str(b.get("content", "")).strip()
                ]
                if segs:
                    src = "ai" if s.get("lan", "").startswith("ai-") else "cc"
                    return segs, f"{src}:{s.get('lan_doc') or s.get('lan')}"
            last_reason = "字幕地址为空（可能 SESSDATA 失效）"
            time.sleep(1)
        return None, last_reason

    def download_audio(self, bvid: str, cid: int, dest: str,
                       progress: Optional[Callable[[float], None]] = None) -> str:
        d = self._get(
            "https://api.bilibili.com/x/player/wbi/playurl",
            {"bvid": bvid, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 0},
            wbi=True,
        )
        urls: list[str] = []
        dash = d.get("dash") or {}
        audios = dash.get("audio") or []
        if audios:
            a = sorted(audios, key=lambda x: x.get("bandwidth", 0))[0]  # 最低码率，转写够用且下载快
            urls = [a.get("baseUrl") or a.get("base_url")] + (a.get("backupUrl") or a.get("backup_url") or [])
        elif d.get("durl"):
            durl = d["durl"][0]
            urls = [durl["url"]] + (durl.get("backup_url") or [])
        urls = [u for u in urls if u]
        if not urls:
            raise BiliError("没有拿到音频地址")
        last_err: Exception | None = None
        for u in urls:
            try:
                with self.s.get(u, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length") or 0)
                    done = 0
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(256 * 1024):
                            f.write(chunk)
                            done += len(chunk)
                            if progress and total:
                                progress(done / total)
                return dest
            except Exception as e:
                last_err = e
        raise BiliError(f"音频下载失败：{last_err}")
