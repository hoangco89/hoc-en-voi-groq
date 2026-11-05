# streamlit_app.py - Hỏi đáp với Groq: ô nhập tùy biến (bàn phím / micro), trả lời bằng chữ + giọng đọc
import io
import re
import tempfile
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components
from gtts import gTTS

st.set_page_config(page_title="Hỏi đáp với Groq AI")
st.header("Hỏi đáp với Groq AI")

# ---------- Cổng mật khẩu (chỉ bật nếu có APP_PASSWORD trong Secrets) ----------
pw_required = st.secrets.get("APP_PASSWORD")
if pw_required and not st.session_state.get("ok"):
    pw = st.text_input("Mật khẩu", type="password")
    if pw:
        if pw == pw_required:
            st.session_state.ok = True
            st.rerun()
        else:
            st.error("Sai mật khẩu.")
    st.stop()

# ---------- Cấu hình ----------
API_KEY = st.secrets.get("GROQ_API_KEY")
MODEL = st.secrets.get("GROQ_MODEL", "qwen/qwen3.8-27b")

if not API_KEY:
    st.error("Thiếu GROQ_API_KEY trong Secrets.")
    st.stop()

GROQ = "https://api.groq.com/openai/v1"
# tên hiển thị: (mã cho giọng đọc gTTS, mã cho nhận diện giọng nói của trình duyệt)
LANGS = {
    "Tiếng Việt": ("vi", "vi-VN"),
    "English": ("en", "en-US"),
    "Français": ("fr", "fr-FR"),
    "Dansk": ("da", "da-DK"),
}

if "history" not in st.session_state:
    st.session_state.history = []

# ---------- Ô nhập tùy biến (HTML/JS chạy trong trình duyệt) ----------
COMPONENT_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --fg:#31333f; --bg:#ffffff; --bg2:#f0f2f6; --pri:#ff4b4b; }
  @media (prefers-color-scheme: dark) { :root { --fg:#fafafa; --bg:#0e1117; --bg2:#262730; } }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: transparent; color: var(--fg);
    font-family: "Source Sans Pro", system-ui, -apple-system, sans-serif; }
  #wrap { padding: 4px 2px 6px; }
  #chipRow { display: none; align-items: center; height: 38px; }
  .chip { display: none; align-items: center; gap: 6px; padding: 5px 12px; border-radius: 999px;
    background: var(--bg2); border: 1px solid rgba(128,128,128,.4); font-size: 15px;
    cursor: pointer; user-select: none; -webkit-user-select: none; }
  .chip.show { display: inline-flex; animation: pop .28s ease-out; }
  .chip.live { border-color: #dc2626; }
  .chip.live .em { animation: pulse 1s infinite; }
  .chip .x { opacity: .6; margin-left: 4px; padding: 0 2px; }
  @keyframes pop { from { transform: translateY(26px); opacity: 0; } to { transform: none; opacity: 1; } }
  @keyframes pulse { 50% { opacity: .3; } }
  #frame { display: flex; align-items: flex-end; gap: 8px; min-height: 66px;
    border: 1px solid rgba(128,128,128,.5); border-radius: 14px; padding: 8px 8px 8px 12px; background: var(--bg); }
  #left { flex: 1; min-width: 0; display: flex; align-items: center; align-self: stretch; }
  #hint { font-size: 15px; line-height: 1.8; }
  .emo { border: 0; background: var(--bg2); border-radius: 8px; font-size: 18px; padding: 0 7px;
    cursor: pointer; line-height: 1.5; vertical-align: baseline; }
  #txt { display: none; width: 100%; border: 0; outline: 0; resize: none; background: transparent;
    color: var(--fg); font: inherit; font-size: 16px; line-height: 1.4; max-height: 120px; padding: 4px 0; }
  #send { flex: none; width: 42px; height: 42px; border: 0; border-radius: 50%; background: var(--pri);
    color: #fff; font-size: 18px; cursor: pointer; }
  #send:disabled { opacity: .35; cursor: not-allowed; }
</style>
</head>
<body>
<div id="wrap">
  <div id="chipRow">
    <span id="chip" class="chip"><span class="em" id="chipEm"></span><span id="chipTxt"></span><span class="x" id="chipX">✕</span></span>
  </div>
  <div id="frame">
    <div id="left">
      <div id="hint">Bấm <button class="emo" id="btnKb" type="button">⌨️</button>, hoặc bấm <button class="emo" id="btnMic" type="button">🎤</button> để hỏi rồi gửi</div>
      <textarea id="txt" rows="1"></textarea>
    </div>
    <button id="send" type="button" disabled>𖤂</button>
  </div>
</div>

<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var lang = "vi-VN", mode = null, rec = null, listening = false, base = "", hadError = false;
  var autoSecs = 0, silenceTimer = null, manualStop = false;
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var isMobile = /iPhone|iPad|Android/i.test(navigator.userAgent);
  var ERR = {
    "not-allowed": "Chưa cấp quyền micro",
    "service-not-allowed": "Chưa bật nhận diện giọng nói (iPhone: bật Đọc chính tả trong Cài đặt)",
    "no-speech": "Không nghe thấy gì · bấm để thử lại",
    "audio-capture": "Không tìm thấy micro",
    "network": "Lỗi mạng khi nhận diện giọng nói",
    "language-not-supported": "Ngôn ngữ này chưa được hỗ trợ"
  };

  function post(type, data) {
    window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type: type }, data || {}), "*");
  }
  function fit() {
    post("streamlit:setFrameHeight", { height: Math.ceil($("wrap").getBoundingClientRect().height) + 8 });
  }
  new ResizeObserver(fit).observe($("wrap"));

  function autosize() {
    var t = $("txt");
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, 120) + "px";
  }
  function updateSend() { $("send").disabled = !$("txt").value.trim(); }
  function setStatus(s) { $("chipTxt").textContent = s; }

  /* ----- nhận diện giọng nói (không thu âm, chỉ chuyển thành chữ) ----- */
  function killRec() {
    clearTimeout(silenceTimer);
    if (rec) {
      var r = rec; rec = null;
      r.onresult = null; r.onerror = null; r.onend = null;
      try { r.abort(); } catch (e) {}
    }
    listening = false;
    $("chip").classList.remove("live");
  }
  function armTimer() {
    clearTimeout(silenceTimer);
    if (autoSecs > 0 && mode === "mic") {
      silenceTimer = setTimeout(function () {
        if ($("txt").value.trim()) doSend();
      }, autoSecs * 1000);
    }
  }
  function startRec() {
    if (!SR) {
      setStatus("Trình duyệt không hỗ trợ nhận diện giọng nói · hãy dùng ⌨️");
      return;
    }
    killRec();
    hadError = false;
    manualStop = false;
    base = $("txt").value.trim() ? $("txt").value.trim() + " " : "";
    rec = new SR();
    rec.lang = lang;
    rec.interimResults = true;
    rec.continuous = true;
    rec.onstart = function () {
      listening = true;
      $("chip").classList.add("live");
      setStatus(autoSecs > 0 ? "Đang nghe... (tự gửi khi bạn ngừng nói)" : "Đang nghe... (bấm để dừng)");
      $("txt").placeholder = "Hãy nói...";
    };
    rec.onresult = function (e) {
      var s = "";
      for (var i = 0; i < e.results.length; i++) s += e.results[i][0].transcript;
      $("txt").value = base + s;
      autosize(); updateSend();
      armTimer();
    };
    rec.onerror = function (e) { hadError = true; setStatus(ERR[e.error] || ("Lỗi micro: " + e.error)); };
    rec.onend = function () {
      listening = false;
      $("chip").classList.remove("live");
      if (!hadError && !manualStop && autoSecs > 0 && mode === "mic" && $("txt").value.trim()) {
        doSend();
        return;
      }
      if (!hadError) setStatus("Đã dừng · bấm để nói tiếp");
    };
    try { rec.start(); } catch (e) {}
  }

  /* ----- chuyển chế độ ----- */
  function setMode(m) {
    mode = m;
    var chip = $("chip");
    if (!m) {
      killRec();
      $("hint").style.display = "";
      $("txt").style.display = "none";
      $("txt").value = "";
      $("chipRow").style.display = "none";
      chip.classList.remove("show");
      autosize(); updateSend(); fit();
      return;
    }
    $("hint").style.display = "none";
    $("txt").style.display = "block";
    $("chipRow").style.display = "flex";
    chip.classList.remove("show"); void chip.offsetWidth; chip.classList.add("show");
    if (m === "kb") {
      killRec();
      $("chipEm").textContent = "⌨️";
      setStatus("Gõ / dán văn bản");
      $("txt").placeholder = "Gõ hoặc dán câu hỏi...";
      $("txt").focus();
    } else {
      $("chipEm").textContent = "🎤";
      startRec();
    }
    fit();
  }

  function doSend() {
    var text = $("txt").value.trim();
    if (!text) return;
    killRec();
    post("streamlit:setComponentValue", {
      value: { text: text, id: Date.now() + "-" + Math.random() },
      dataType: "json"
    });
    setMode(null);  // emoji trở về vị trí cũ
  }

  $("btnKb").addEventListener("click", function () { setMode("kb"); });
  $("btnMic").addEventListener("click", function () { setMode("mic"); });
  $("chip").addEventListener("click", function (e) {
    if (e.target.id === "chipX") { setMode(null); return; }
    if (mode === "mic") { if (listening && rec) { manualStop = true; clearTimeout(silenceTimer); rec.stop(); } else { startRec(); } }
    else if (mode === "kb") { $("txt").focus(); }
  });
  $("send").addEventListener("click", doSend);
  $("txt").addEventListener("input", function () { autosize(); updateSend(); });
  $("txt").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !isMobile) { e.preventDefault(); doSend(); }
  });

  /* ----- nhận dữ liệu từ Streamlit ----- */
  window.addEventListener("message", function (e) {
    var d = e.data;
    if (!d || d.type !== "streamlit:render") return;
    var a = d.args || {};
    if (typeof a.auto_secs === "number") autoSecs = a.auto_secs;
    if (a.lang && a.lang !== lang) {
      lang = a.lang;
      if (mode === "mic" && listening) startRec();
    }
    var t = d.theme;
    if (t) {
      var s = document.documentElement.style;
      s.setProperty("--fg", t.textColor);
      s.setProperty("--bg", t.backgroundColor);
      s.setProperty("--bg2", t.secondaryBackgroundColor);
      s.setProperty("--pri", t.primaryColor);
    }
    fit();
  });
  post("streamlit:componentReady", { apiVersion: 1 });
  setMode(null);
})();
</script>
</body>
</html>
"""

_comp_dir = Path(tempfile.gettempdir()) / "groq_voice_input"
_comp_dir.mkdir(exist_ok=True)
(_comp_dir / "index.html").write_text(COMPONENT_HTML, encoding="utf-8")
voice_input = components.declare_component("groq_voice_input", path=str(_comp_dir))


# ---------- Các hàm ----------
def ask_groq(history):
    r = requests.post(
        f"{GROQ}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "Bạn là trợ lý hữu ích. Trả lời ngắn gọn, rõ ràng, bằng ngôn ngữ của người hỏi."},
                *history,
            ],
        },
        timeout=60,
    )
    body = r.json()
    if not r.ok:
        raise RuntimeError(body.get("error", {}).get("message", "Lỗi từ Groq."))
    return body["choices"][0]["message"]["content"]


def text_to_speech(text, lang):
    """Chữ -> file mp3 bằng gTTS."""
    clean = re.sub(r"[*#`_>~]", "", text)
    buf = io.BytesIO()
    gTTS(clean, lang=lang).write_to_fp(buf)
    return buf.getvalue()


# ---------- Giao diện ----------
CHAT_HEIGHT = 320  # chiều cao khung chat (px), chỉnh theo màn hình của bạn
AUTO_SEND_SECS = 2.5  # số giây im lặng trước khi tự gửi (khi dùng micro)

c1, c2 = st.columns(2)
lang_name = c1.selectbox(":red[Ngôn ngữ]", list(LANGS))
lang, bcp = LANGS[lang_name]
read_aloud = c2.toggle("Đọc câu trả lời", value=True)
auto_send = st.toggle("Micro: tự gửi khi ngừng nói", value=True)

# Khung chat có chiều cao cố định, nội dung dài sẽ cuộn bên trong khung
chat_box = st.container(height=CHAT_HEIGHT, border=True)

# Ô nhập tùy biến nằm ngay dưới khung chat
sent = voice_input(lang=bcp, auto_secs=AUTO_SEND_SECS if auto_send else 0, key="voice_input")

with chat_box:
    for m in st.session_state.history:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

# Mỗi lần bấm gửi, ô nhập trả về {text, id}; dùng id để không xử lý lại cùng một câu
question = None
if sent and sent.get("id") != st.session_state.get("last_id"):
    st.session_state.last_id = sent.get("id")
    question = (sent.get("text") or "").strip() or None

if question:
    st.session_state.history.append({"role": "user", "content": question})
    with chat_box:
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("Đang suy nghĩ..."):
                try:
                    reply = ask_groq(st.session_state.history)
                    st.markdown(reply)
                    st.session_state.history.append({"role": "assistant", "content": reply})
                    if read_aloud:
                        try:
                            st.audio(text_to_speech(reply, lang), format="audio/mp3", autoplay=True)
                        except Exception as e:
                            st.caption(f"Không đọc được câu trả lời: {e}")
                except Exception as e:
                    st.error(f"Lỗi: {e}")
                    st.session_state.history.pop()

