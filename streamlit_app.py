# -*- coding: utf-8 -*-
"""
Tự học tiếng Anh từ Shorts YouTube với Groq AI
Triển khai trên Streamlit Cloud. Ưu tiên iPhone (Safari), chạy tốt trên laptop.

Secrets cần có (Streamlit Cloud -> App settings -> Secrets):
    APP_PASSWORD = "mat-ma-cua-ban"
    GROQ_API_KEY = "gsk_..."
"""

import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from groq import Groq

# ----------------------------------------------------------------------------
# CẤU HÌNH
# ----------------------------------------------------------------------------
KENH_FILE = "cac_kenh.json"
CHAT_MODEL = "qwen/qwen3.8-27b"   # đổi nếu Groq ngừng model này
STT_MODEL = "whisper-large-v3-turbo"     # nhận dạng giọng nói
MAX_CONTEXT_CHARS = 12000                # giới hạn độ dài phụ đề gửi cho Groq
NUM_VIDEOS = 3

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

st.set_page_config(
    page_title="Tự học tiếng Anh từ Shorts",
    page_icon="🎧",
    layout="centered",
    initial_sidebar_state="expanded",
)

# ----------------------------------------------------------------------------
# TRẠNG THÁI
# ----------------------------------------------------------------------------
def init_state():
    defaults = {
        "auth": False,
        "kenh_v": 0,              # đổi key của bảng kênh sau khi sửa
        "sel_id": None,           # id kênh đang được chọn (chỉ 1)
        "del_ids": [],            # id kênh đang được tích Xóa
        "videos": [],             # [{id, title}]
        "videos_kind": "",        # "short" | "video"
        "videos_channel": "",
        "transcripts": {},        # video_id -> text
        "tr_err": {},             # video_id -> lỗi lấy phụ đề
        "summaries": {},          # video_id -> {"en": .., "vi": ..}
        "read_result": {},        # video_id -> nhận xét đọc
        "gq": {},                 # video_id -> câu hỏi hiện tại của Groq
        "gq_result": {},          # video_id -> nhận xét câu trả lời
        "chat": {},               # video_id -> [{"q","en","vi"}]
        "rec_n": 0,               # đếm để xóa các ô ghi âm/nhập sau khi gửi
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


init_state()

# ----------------------------------------------------------------------------
# MẬT MÃ
# ----------------------------------------------------------------------------
def check_password() -> bool:
    if st.session_state.auth:
        return True
    st.title("🎧 Tự học tiếng Anh từ Shorts")
    expected = st.secrets.get("APP_PASSWORD", "")
    if not expected:
        st.error("Chưa đặt APP_PASSWORD trong Secrets của Streamlit.")
        return False
    pw = st.text_input("Nhập mật mã để tiếp tục", type="password")
    if st.button("Vào"):
        if hmac.compare_digest(pw.encode(), expected.encode()):
            st.session_state.auth = True
            st.rerun()
        else:
            st.error("Sai mật mã.")
    return False


# ----------------------------------------------------------------------------
# DANH SÁCH KÊNH (cac_kenh.json)
# ----------------------------------------------------------------------------
def load_channels() -> list:
    if not os.path.exists(KENH_FILE):
        save_channels([])  # tạo tệp mới
        return []
    try:
        with open(KENH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_channels(channels: list):
    with open(KENH_FILE, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------------
# YOUTUBE (không cần API key)
# ----------------------------------------------------------------------------
def http_get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": "CONSENT=YES+1; SOCS=CAI",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_video_id(url: str):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{11})", url)
    return m.group(1) if m else None


def parse_feed(xml_text: str):
    root = ET.fromstring(xml_text)
    title = root.findtext("atom:title", default="", namespaces=NS)
    items = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=NS)
        ttl = entry.findtext("atom:title", default="", namespaces=NS)
        if vid:
            items.append({"id": vid, "title": ttl or vid})
    return title, items


def fetch_feed(url: str):
    """Trả về (tên_feed, danh_sách_video). 404 -> ('', [])."""
    try:
        return parse_feed(http_get(url))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "", []
        raise


def channel_from_link(link: str):
    """Từ link video (hoặc chính id kênh UC...) -> (channel_id, tên kênh)."""
    link = link.strip()
    if re.fullmatch(r"UC[\w-]{22}", link):
        channel_id = link
    else:
        vid = extract_video_id(link)
        if not vid:
            raise ValueError("Không nhận ra link video YouTube.")
        page = http_get(f"https://www.youtube.com/watch?v={vid}")
        m = (
            re.search(r'itemprop="channelId" content="(UC[\w-]{22})"', page)
            or re.search(r'"externalChannelId":"(UC[\w-]{22})"', page)
            or re.search(r'"channelId":"(UC[\w-]{22})"', page)
        )
        if not m:
            raise ValueError("Không tìm thấy id kênh trong trang video.")
        channel_id = m.group(1)

    name = channel_id
    try:
        feed_title, _ = fetch_feed(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        )
        if feed_title:
            name = feed_title
    except Exception:
        pass
    return channel_id, name


def try_feed(url: str, label: str, log: list):
    """Đọc RSS, thử lại 1 lần (YouTube thỉnh thoảng trả 404/5xx tạm thời)."""
    for _ in range(2):
        try:
            _, items = parse_feed(http_get(url))
            log.append(f"{label}: OK, {len(items)} mục")
            return items
        except urllib.error.HTTPError as e:
            log.append(f"{label}: HTTP {e.code}")
        except Exception as e:
            log.append(f"{label}: {type(e).__name__}: {str(e)[:120]}")
        time.sleep(1)
    return []


def video_title(vid: str) -> str:
    try:
        u = urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}", safe="")
        data = json.loads(http_get(f"https://www.youtube.com/oembed?format=json&url={u}"))
        return data.get("title") or vid
    except Exception:
        return vid


def scrape_tab(channel_id: str, tab: str, n: int, log: list):
    """Phương án dự phòng: đọc trang /shorts hoặc /videos của kênh."""
    try:
        html = http_get(f"https://www.youtube.com/channel/{channel_id}/{tab}")
    except Exception as e:
        log.append(f"trang /{tab}: {type(e).__name__}: {str(e)[:120]}")
        return []
    ids = list(dict.fromkeys(re.findall(r'"videoId":"([\w-]{11})"', html)))[:n]
    log.append(f"trang /{tab}: tìm thấy {len(ids)} id")
    return [{"id": i, "title": video_title(i)} for i in ids]


def latest_shorts_or_videos(channel_id: str, n: int = NUM_VIDEOS):
    """n Shorts mới nhất; nếu kênh không có Shorts thì n video mới nhất.
    Trả về (danh_sách, loại, nhật_ký)."""
    log = []
    items = try_feed(
        f"https://www.youtube.com/feeds/videos.xml?playlist_id=UUSH{channel_id[2:]}",
        "RSS Shorts", log,
    )
    if items:
        return items[:n], "short", log
    items = scrape_tab(channel_id, "shorts", n, log)
    if items:
        return items, "short", log
    items = try_feed(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        "RSS kênh", log,
    )
    if items:
        return items[:n], "video", log
    return scrape_tab(channel_id, "videos", n, log), "video", log


def get_transcript(video_id: str):
    """Trả về (văn bản, lỗi). Ưu tiên phụ đề tiếng Anh; nếu không có thì dịch sang Anh."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception as e:
        return "", f"Chưa cài youtube-transcript-api>=1.0 ({e})"
    try:
        kwargs = {}
        proxy = st.secrets.get("PROXY_URL", "")  # tùy chọn, xem hướng dẫn
        if proxy:
            from youtube_transcript_api.proxies import GenericProxyConfig

            kwargs["proxy_config"] = GenericProxyConfig(http_url=proxy, https_url=proxy)
        api = YouTubeTranscriptApi(**kwargs)
        tlist = api.list(video_id)
        try:
            t = tlist.find_transcript(["en", "en-US", "en-GB"])  # thủ công hoặc tự động
        except Exception:
            t = next(iter(tlist))  # ngôn ngữ bất kỳ
            if not t.language_code.startswith("en") and t.is_translatable:
                t = t.translate("en")
        fetched = t.fetch()
        text = " ".join(sn.text for sn in fetched).strip()
        return text, ("" if text else "Phụ đề rỗng.")
    except Exception as e:
        return "", f"{type(e).__name__}: {str(e).strip()[:300]}"


# ----------------------------------------------------------------------------
# GROQ
# ----------------------------------------------------------------------------
@st.cache_resource
def groq_client() -> Groq:
    return Groq(api_key=st.secrets["GROQ_API_KEY"])


def groq_chat(messages, temperature=0.4, json_mode=False) -> str:
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    r = groq_client().chat.completions.create(
        model=CHAT_MODEL, messages=messages, temperature=temperature, **kwargs
    )
    return r.choices[0].message.content.strip()


def groq_json(messages, temperature=0.3) -> dict:
    raw = groq_chat(messages, temperature=temperature, json_mode=True)
    try:
        return json.loads(raw)
    except Exception:
        return {"en": raw, "vi": ""}


def transcribe(audio_bytes: bytes, filename: str = "speech.wav") -> str:
    r = groq_client().audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=STT_MODEL,
        language="en",
    )
    return (getattr(r, "text", None) or str(r)).strip()


SYSTEM_TUTOR = (
    "Bạn là gia sư tiếng Anh thân thiện cho người Việt (trình độ A2-B1). "
    "Nhận xét bằng tiếng Việt, ví dụ và câu mẫu bằng tiếng Anh đơn giản. "
    "Ngắn gọn, cụ thể, khích lệ."
)


def lesson_context(video_id: str, title: str) -> str:
    text = st.session_state.transcripts.get(video_id, "")[:MAX_CONTEXT_CHARS]
    summ = st.session_state.summaries.get(video_id)
    ctx = f"Video title: {title}\n"
    if text:
        ctx += f"Transcript:\n{text}\n"
    if summ:
        ctx += f"English summary:\n{summ['en']}\n"
    return ctx


# ----------------------------------------------------------------------------
# THÀNH PHẦN GIAO DIỆN (HTML)
# ----------------------------------------------------------------------------
PLAYER_TEMPLATE = """
<style>
  body { margin:0; font-family:-apple-system,system-ui,sans-serif; }
  #wrap { max-width:__MAXW__px; margin:0 auto; }
  #box { position:relative; width:100%; aspect-ratio:__RATIO__; background:#000;
         border-radius:12px; overflow:hidden; }
  #box iframe { position:absolute; inset:0; width:100%; height:100%; border:0; }
  .bar { display:flex; flex-wrap:wrap; gap:6px; justify-content:center; margin-top:8px; }
  button { border:1px solid #bbb; background:#f6f6f6; border-radius:8px;
           padding:8px 10px; font-size:15px; }
  button.on { background:#ff4b4b; color:#fff; border-color:#ff4b4b; }
  @media (prefers-color-scheme: dark) {
    button { background:#2b2b2b; color:#eee; border-color:#555; }
  }
</style>
<div id="wrap">
  <div id="box">
    <iframe id="yt"
      src="https://www.youtube.com/embed/__VID__?playsinline=1&enablejsapi=1&rel=0&modestbranding=1&cc_load_policy=__CC__&cc_lang_pref=en&hl=en"
      allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>
  </div>
  <div class="bar" id="speeds"></div>
  <div class="bar">
    <button onclick="seekBy(-5)">⏪ 5s</button>
    <button onclick="replay()">↺ Nghe lại</button>
    <button onclick="seekBy(5)">5s ⏩</button>
  </div>
</div>
<script>
  var f=document.getElementById('yt'), cur=0, rates=[0.5,0.75,1,1.25];
  function cmd(fn,args){
    f.contentWindow.postMessage(JSON.stringify({event:'command',func:fn,args:args||[]}),'*');
  }
  function listen(){
    f.contentWindow.postMessage(JSON.stringify({event:'listening',id:1}),'*');
  }
  f.addEventListener('load',function(){ listen(); setTimeout(listen,800); setTimeout(listen,2000); });
  window.addEventListener('message',function(e){
    try{
      var d=(typeof e.data==='string')?JSON.parse(e.data):e.data;
      if(d && d.event==='infoDelivery' && d.info && typeof d.info.currentTime==='number'){ cur=d.info.currentTime; }
    }catch(x){}
  });
  var bar=document.getElementById('speeds');
  rates.forEach(function(r){
    var b=document.createElement('button'); b.textContent=r+'x'; b.id='r'+r;
    if(r===1) b.className='on';
    b.onclick=function(){
      cmd('setPlaybackRate',[r]);
      rates.forEach(function(x){document.getElementById('r'+x).className=(x===r?'on':'');});
    };
    bar.appendChild(b);
  });
  function seekBy(s){ cmd('seekTo',[Math.max(0,cur+s),true]); }
  function replay(){ cmd('seekTo',[0,true]); cmd('playVideo'); }
</script>
"""


def render_player(video_id: str, kind: str, captions: bool):
    short = kind == "short"
    html = (
        PLAYER_TEMPLATE.replace("__VID__", video_id)
        .replace("__CC__", "1" if captions else "0")
        .replace("__MAXW__", "300" if short else "720")
        .replace("__RATIO__", "9/16" if short else "16/9")
    )
    components.html(html, height=640 if short else 470)


def speak_buttons(text: str, lang: str = "en-US"):
    """Đọc to bằng giọng của trình duyệt (hoạt động trên iPhone vì bấm nút)."""
    safe = json.dumps(text).replace("</", "<\\/")
    html = """
    <style>
      button{border:1px solid #bbb;background:#f6f6f6;border-radius:8px;
             padding:8px 12px;font-size:15px;margin-right:6px;}
      @media (prefers-color-scheme: dark){button{background:#2b2b2b;color:#eee;border-color:#555;}}
    </style>
    <button onclick="say(1)">🔊 Nghe</button>
    <button onclick="say(0.7)">🐢 Chậm</button>
    <button onclick="speechSynthesis.cancel()">⏹</button>
    <script>
      var T=__TEXT__;
      function say(rate){
        speechSynthesis.cancel();
        var u=new SpeechSynthesisUtterance(T);
        u.lang='__LANG__'; u.rate=rate; speechSynthesis.speak(u);
      }
    </script>
    """.replace("__TEXT__", safe).replace("__LANG__", lang)
    components.html(html, height=50)


# ----------------------------------------------------------------------------
# SIDEBAR
# ----------------------------------------------------------------------------
def sidebar():
    st.sidebar.header("📺 Kênh học tiếng Anh")
    channels = load_channels()

    # --- Thêm kênh (bắt buộc nếu chưa có kênh nào) ---
    if not channels:
        st.sidebar.info(
            "Chưa có kênh nào. Dán 1 link video YouTube của kênh bạn muốn học."
        )
    with st.sidebar.expander("➕ Thêm kênh", expanded=not channels):
        link = st.text_input("Link 1 video YouTube (hoặc id kênh UC...)", key="add_link")
        if st.button("Thêm kênh"):
            try:
                with st.spinner("Đang lấy id kênh..."):
                    cid, name = channel_from_link(link)
                if any(c["id"] == cid for c in channels):
                    st.warning("Kênh này đã có trong danh sách.")
                else:
                    channels.append({"name": name, "id": cid})
                    save_channels(channels)
                    st.session_state.kenh_v += 1
                    st.rerun()
            except Exception as e:
                st.error(f"Lỗi: {e}")

    if not channels:
        return

    # --- Bảng chọn kênh (chỉ cho chọn 1 kênh) ---
    sel_id = st.session_state.sel_id
    del_ids = set(st.session_state.del_ids)
    df = pd.DataFrame(
        [
            {
                "Chọn": c["id"] == sel_id,
                "Tên kênh": c["name"],
                "Id của kênh": c["id"],
                "Xóa": c["id"] in del_ids,
            }
            for c in channels
        ]
    )
    edited = st.sidebar.data_editor(
        df,
        hide_index=True,
        disabled=["Tên kênh", "Id của kênh"],
        column_config={
            "Chọn": st.column_config.CheckboxColumn("Chọn", width="small"),
            "Xóa": st.column_config.CheckboxColumn("Xóa", width="small"),
        },
        key=f"kenh_editor_{st.session_state.kenh_v}",
    )

    # ghi nhớ các ô Xóa
    st.session_state.del_ids = list(edited.loc[edited["Xóa"], "Id của kênh"])

    # nếu vừa tích thêm một kênh khác -> chỉ giữ kênh mới, bỏ chọn kênh cũ
    checked = list(edited.loc[edited["Chọn"], "Id của kênh"])
    newly = [i for i in checked if i != sel_id]
    if newly:
        st.session_state.sel_id = newly[-1]
        st.session_state.kenh_v += 1  # vẽ lại bảng với đúng 1 kênh được chọn
        st.rerun()
    if not checked:
        st.session_state.sel_id = None

    c1, c2 = st.sidebar.columns(2)
    find_clicked = c1.button("🔎 Tìm video")
    del_clicked = c2.button("🗑 Xóa kênh")

    if del_clicked:
        to_del = set(st.session_state.del_ids)
        if to_del:
            save_channels([c for c in channels if c["id"] not in to_del])
            if st.session_state.sel_id in to_del:
                st.session_state.sel_id = None
            st.session_state.del_ids = []
            st.session_state.kenh_v += 1
            st.rerun()
        else:
            st.sidebar.warning("Hãy tích cột Xóa ở kênh muốn xóa.")

    if find_clicked:
        chosen = next((c for c in channels if c["id"] == st.session_state.sel_id), None)
        if chosen is None:
            st.sidebar.warning("Hãy tích chọn một kênh.")
        else:
            head = f"Kênh: {chosen['name']} ({chosen['id']})"
            try:
                with st.spinner("Đang tìm Shorts/video mới nhất..."):
                    vids, kind, log = latest_shorts_or_videos(chosen["id"])
                st.session_state.search_log = [head] + log
                if vids:
                    st.session_state.search_failed = False
                    st.session_state.videos = vids
                    st.session_state.videos_kind = kind
                    st.session_state.videos_channel = chosen["name"]
                    st.session_state.pop("picked_vid", None)
                else:
                    st.session_state.search_failed = True
            except Exception as e:
                st.session_state.search_log = [head, f"Ngoại lệ: {type(e).__name__}: {e}"]
                st.session_state.search_failed = True

    if st.session_state.get("search_failed"):
        st.sidebar.error("Không lấy được video nào từ kênh này.")
        with st.sidebar.expander("🔍 Chi tiết lỗi", expanded=True):
            st.code("\n".join(st.session_state.get("search_log", [])), language=None)

    # --- Chọn 1 short/video ---
    if st.session_state.videos:
        st.sidebar.divider()
        label = "Shorts" if st.session_state.videos_kind == "short" else "Video"
        st.sidebar.subheader(f"{label} mới nhất – {st.session_state.videos_channel}")
        ids = [v["id"] for v in st.session_state.videos]
        titles = {v["id"]: v["title"] for v in st.session_state.videos}
        st.sidebar.radio(
            "Chọn một bài",
            ids,    # là list 3 id video/short mới nhất
            index=None,
            format_func=lambda i: titles[i],
            key="picked_vid",
        )

        if len(ids)>0:
            st.sidebar.write("---")
            st.sidebar.write(":red[Nếu muốn mở trực tiếp:]")
            for j in range(len(ids)):
                st.sidebar.write("https://www.youtube.com/watch?v="+ids[j])


# ----------------------------------------------------------------------------
# PHẦN HỌC VỚI GROQ (màn hình chính, dưới iframe)
# ----------------------------------------------------------------------------
def audio_or_text_input(prefix: str, placeholder: str):
    """Ô ghi âm + ô gõ chữ. Trả về (audio_bytes | None, text)."""
    n = st.session_state.rec_n
    audio = st.audio_input("🎤 Nói", key=f"{prefix}_audio_{n}")
    text = st.text_area(
        "…hoặc gõ chữ", key=f"{prefix}_text_{n}", placeholder=placeholder, height=80
    )
    return (audio.getvalue() if audio else None), text.strip()


def get_user_input(audio_bytes, text) -> str:
    if audio_bytes:
        with st.spinner("Đang nhận dạng giọng nói..."):
            return transcribe(audio_bytes)
    return text


def tab_summary(vid, title):
    if not st.session_state.transcripts.get(vid):
        err = st.session_state.tr_err.get(vid, "")
        if any(k in err for k in ("ParseError", "no element", "PoToken")):
            st.warning(
                "YouTube hiện không cho máy chủ tải phụ đề tự động (cần mã PoToken), "
                "nên app chưa có lời thoại cho Groq. Nút CC trong khung video vẫn bật được. "
                "Chọn một cách dưới đây để có lời thoại:"
            )
        else:
            st.warning("Không lấy được phụ đề tự động. Chọn một cách dưới đây:")
            if err:
                st.code(err, language=None)
        if st.button("🔄 Thử lấy phụ đề tự động lại", key=f"retry_{vid}"):
            st.session_state.tr_err.pop(vid, None)
            st.session_state.transcripts.pop(vid, None)
            st.rerun()

        with st.expander("🎤 Cách 1: ghi âm lời thoại bằng micro", expanded=True):
            st.caption(
                "Phát video bằng loa ngoài, rồi ghi âm ở đây (nơi yên tĩnh, ghi hết video). "
                "Trên iPhone, bật mic có thể làm video tạm dừng; nếu vậy hãy dùng cách 2 hoặc 3."
            )
            rec = st.audio_input("Ghi âm lời thoại", key=f"trrec_{vid}")
            if st.button("Chuyển thành chữ", key=f"trrecbtn_{vid}"):
                if not rec:
                    st.warning("Hãy ghi âm trước.")
                else:
                    with st.spinner("Groq đang nhận dạng..."):
                        txt = transcribe(rec.getvalue())
                    if txt:
                        st.session_state.transcripts[vid] = txt
                        st.rerun()
                    st.error("Không nghe được nội dung. Thử ghi lại gần loa hơn.")

        with st.expander("📁 Cách 2: tải tệp âm thanh/video lên"):
            up = st.file_uploader(
                "Tệp mp3, m4a, wav, mp4, webm (tối đa khoảng 25MB)",
                type=["mp3", "m4a", "wav", "mp4", "webm", "ogg"],
                key=f"trup_{vid}",
            )
            if st.button("Chuyển thành chữ", key=f"trupbtn_{vid}") and up:
                with st.spinner("Groq đang nhận dạng..."):
                    txt = transcribe(up.getvalue(), up.name)
                if txt:
                    st.session_state.transcripts[vid] = txt
                    st.rerun()
                st.error("Không nghe được nội dung trong tệp.")

        with st.expander("📋 Cách 3: dán lời thoại"):
            pasted = st.text_area("Dán lời thoại tiếng Anh", key=f"paste_{vid}", height=120)
            if st.button("Dùng nội dung này", key=f"usepaste_{vid}") and pasted.strip():
                st.session_state.transcripts[vid] = pasted.strip()
                st.rerun()
        return

    with st.expander("Xem phụ đề/lời thoại gốc"):
        st.write(st.session_state.transcripts[vid])

    if st.button("📝 Groq tóm tắt nội dung", key=f"sum_{vid}"):
        with st.spinner("Groq đang tóm tắt..."):
            res = groq_json(
                [
                    {"role": "system", "content": SYSTEM_TUTOR},
                    {
                        "role": "user",
                        "content": (
                            lesson_context(vid, title)
                            + "\nTóm tắt nội dung video bằng 4-6 câu tiếng Anh đơn giản (B1), "
                            "mỗi câu ngắn dễ đọc to. Sau đó dịch sang tiếng Việt. "
                            'Trả về JSON: {"en": "...", "vi": "..."}'
                        ),
                    },
                ]
            )
        st.session_state.summaries[vid] = {
            "en": str(res.get("en", "")),
            "vi": str(res.get("vi", "")),
        }

    summ = st.session_state.summaries.get(vid)
    if summ:
        st.markdown("**🇬🇧 English**")
        st.write(summ["en"])
        speak_buttons(summ["en"])
        st.markdown("**🇻🇳 Tiếng Việt**")
        st.write(summ["vi"])


def tab_read(vid, title):
    summ = st.session_state.summaries.get(vid)
    if not summ:
        st.info("Hãy bấm tóm tắt ở tab 📝 trước để có văn bản tập đọc.")
        return
    st.markdown("**Văn bản để đọc theo:**")
    st.write(summ["en"])
    speak_buttons(summ["en"])

    n = st.session_state.rec_n
    audio = st.audio_input("🎤 Đọc to đoạn văn trên", key=f"read_audio_{n}")
    if st.button("✅ Groq chấm điểm bài đọc", key=f"readbtn_{vid}"):
        if not audio:
            st.warning("Hãy ghi âm trước.")
            return
        heard = get_user_input(audio.getvalue(), "")
        with st.spinner("Groq đang đánh giá..."):
            fb = groq_chat(
                [
                    {"role": "system", "content": SYSTEM_TUTOR},
                    {
                        "role": "user",
                        "content": (
                            f"Văn bản gốc:\n{summ['en']}\n\n"
                            f"Máy nhận dạng giọng nói nghe được:\n{heard}\n\n"
                            "Hãy so sánh và đánh giá việc đọc: điểm /10, từ/cụm bị sót hoặc đọc sai "
                            "(nghi ngờ phát âm sai), 2-3 lời khuyên cụ thể để cải thiện. "
                            "Lưu ý: bạn chỉ có kết quả nhận dạng bằng chữ nên không đánh giá được "
                            "ngữ điệu; hãy nói rõ giới hạn này một câu."
                        ),
                    },
                ]
            )
        st.session_state.read_result[vid] = (heard, fb)
        st.session_state.rec_n += 1
        st.rerun()

    if vid in st.session_state.read_result:
        heard, fb = st.session_state.read_result[vid]
        with st.expander("Máy nghe được bạn nói", expanded=False):
            st.write(heard)
        st.markdown(fb)


def tab_groq_asks(vid, title):
    if st.button("❓ Groq đặt câu hỏi", key=f"gqbtn_{vid}"):
        prev = st.session_state.gq.get(vid, "")
        with st.spinner("Groq đang nghĩ câu hỏi..."):
            q = groq_chat(
                [
                    {"role": "system", "content": SYSTEM_TUTOR},
                    {
                        "role": "user",
                        "content": (
                            lesson_context(vid, title)
                            + "\nHãy hỏi MỘT câu hỏi tiếng Anh ngắn, đơn giản về nội dung bài "
                            "để người học trả lời bằng lời. "
                            + (f"Không lặp lại câu này: {prev}. " if prev else "")
                            + "Chỉ trả về câu hỏi."
                        ),
                    },
                ],
                temperature=0.8,
            )
        st.session_state.gq[vid] = q
        st.session_state.gq_result.pop(vid, None)
        st.session_state.rec_n += 1
        st.rerun()

    q = st.session_state.gq.get(vid)
    if not q:
        return
    st.markdown(f"**Groq hỏi:** {q}")
    speak_buttons(q)

    audio_bytes, text = audio_or_text_input("gqa", "Gõ câu trả lời bằng tiếng Anh")
    if st.button("📨 Gửi câu trả lời", key=f"gqsend_{vid}"):
        if not audio_bytes and not text:
            st.warning("Hãy nói hoặc gõ câu trả lời.")
            return
        answer = get_user_input(audio_bytes, text)
        with st.spinner("Groq đang đánh giá..."):
            fb = groq_chat(
                [
                    {"role": "system", "content": SYSTEM_TUTOR},
                    {
                        "role": "user",
                        "content": (
                            lesson_context(vid, title)
                            + f"\nCâu hỏi: {q}\nCâu trả lời của người học: {answer}\n\n"
                            "Đánh giá: (1) nội dung có đúng với bài không, (2) lỗi ngữ pháp/từ vựng, "
                            "(3) một câu trả lời mẫu tốt hơn bằng tiếng Anh, (4) điểm /10."
                        ),
                    },
                ]
            )
        st.session_state.gq_result[vid] = (answer, fb)
        st.session_state.rec_n += 1
        st.rerun()

    if vid in st.session_state.gq_result:
        answer, fb = st.session_state.gq_result[vid]
        st.caption(f"Bạn trả lời: {answer}")
        st.markdown(fb)


def tab_ask_groq(vid, title):
    audio_bytes, text = audio_or_text_input("ask", "Gõ câu hỏi về nội dung bài (Anh hoặc Việt)")
    if st.button("📨 Hỏi Groq", key=f"asksend_{vid}"):
        if not audio_bytes and not text:
            st.warning("Hãy nói hoặc gõ câu hỏi.")
        else:
            question = get_user_input(audio_bytes, text)
            history = st.session_state.chat.setdefault(vid, [])
            msgs = [
                {
                    "role": "system",
                    "content": SYSTEM_TUTOR
                    + " Trả lời câu hỏi dựa trên nội dung bài bên dưới. "
                    'Trả về JSON: {"en": "câu trả lời tiếng Anh đơn giản", '
                    '"vi": "bản dịch tiếng Việt"}.\n\n' + lesson_context(vid, title),
                }
            ]
            for h in history[-4:]:
                msgs.append({"role": "user", "content": h["q"]})
                msgs.append({"role": "assistant", "content": h["en"]})
            msgs.append({"role": "user", "content": question})
            with st.spinner("Groq đang trả lời..."):
                res = groq_json(msgs)
            history.append(
                {"q": question, "en": str(res.get("en", "")), "vi": str(res.get("vi", ""))}
            )
            st.session_state.rec_n += 1
            st.rerun()

    for i, h in enumerate(reversed(st.session_state.chat.get(vid, []))):
        with st.chat_message("user"):
            st.write(h["q"])
        with st.chat_message("assistant"):
            st.write(h["en"])
            if h["vi"]:
                st.caption(h["vi"])
            if i == 0:
                speak_buttons(h["en"])


# ----------------------------------------------------------------------------
# MÀN HÌNH CHÍNH
# ----------------------------------------------------------------------------
def main_screen():
    vid = st.session_state.get("picked_vid")
    if not vid:
        st.title("🎧 Tự học tiếng Anh từ Shorts")
        st.info(
            "👈 Mở thanh bên: chọn kênh → bấm **Tìm video** → chọn một bài. "
            "(Trên iPhone, bấm mũi tên ở góc trên bên trái để mở thanh bên.)"
        )
        return

    title = next((v["title"] for v in st.session_state.videos if v["id"] == vid), vid)
    kind = st.session_state.videos_kind

    # --- iframe ở trên cùng ---
    captions = st.session_state.get("cc_on", False)
    render_player(vid, kind, captions)

    c1, c2 = st.columns([1, 2])
    c1.toggle("Phụ đề (CC)", key="cc_on", help="Bật/tắt sẽ tải lại video")
    with c2:
        cols = st.columns(3)
        for i, col in enumerate(cols, start=1):
            col.checkbox(f"Nghe lần {i}", key=f"listen_{vid}_{i}")
    st.caption(f"▶ {title}")

    # Lấy phụ đề một lần cho mỗi video
    if vid not in st.session_state.transcripts and vid not in st.session_state.tr_err:
        with st.spinner("Đang lấy phụ đề..."):
            text, err = get_transcript(vid)
        if text:
            st.session_state.transcripts[vid] = text
        else:
            st.session_state.tr_err[vid] = err or "Không rõ lỗi."

    # --- Các hoạt động tự học với Groq ---
    t1, t2, t3, t4 = st.tabs(["📝 Tóm tắt", "📖 Đọc theo", "❓ Groq hỏi", "💬 Hỏi Groq"])
    with t1:
        tab_summary(vid, title)
    with t2:
        tab_read(vid, title)
    with t3:
        tab_groq_asks(vid, title)
    with t4:
        tab_ask_groq(vid, title)


# ----------------------------------------------------------------------------
# CHẠY
# ----------------------------------------------------------------------------
if check_password():
    if not st.secrets.get("GROQ_API_KEY"):
        st.error("Chưa đặt GROQ_API_KEY trong Secrets của Streamlit.")
        st.stop()
    sidebar()
    main_screen()
