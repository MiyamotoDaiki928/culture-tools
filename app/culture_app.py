"""culture-tools：フォルダ作成・合成・見比べを 1 つにまとめたアプリ

使い方:
    pythonw culture_app.py      （黒い画面を出さずに起動する。タスクバーのショートカットから呼ぶ）
    python  culture_app.py      （動作を確かめたいときは、こちらで起動するとメッセージが表示される）

ブラウザで画面を開き、上部の切り替えで「フォルダ作成」「撮影」「合成」「見比べ」を行き来する。
既に起動しているときは新しく起動せず、開いている画面をもう一度開く。
画面（タブ）をすべて閉じると数秒で終了する。合成の途中は、合成が終わるまで終了しない。
"""
from __future__ import annotations

import html
import json
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
for _d in ("stitch", "viewer", "folders", "capture"):          # 機能ごとのフォルダから読み込む
    sys.path.insert(0, str(APP_DIR.parent / _d))
import culture_stitch as cs                         # noqa: E402
import culture_viewer as cv                         # noqa: E402
import culture_folders as cf                        # noqa: E402
import culture_capture as cc                        # noqa: E402

APP = "culture-tools"
ROOT: Path = Path()                                # main() で、初回に選んだ写真のフォルダを入れる
STATE = APP_DIR / "app_state.json"         # 起動中のアプリの接続先（二重起動を防ぐため）
LOG = APP_DIR / "app.log"                  # 黒い画面がないので、メッセージはここに残す
CLOSE_GRACE = 8       # 最後の画面が閉じられてから終了するまでの秒数（画面の切り替えでは終了しない）
STALE = 180           # 画面からの合図がこの秒数届かなければ閉じられたとみなす（裏に回したタブは合図が間引かれるため長め）

TABS = [("folders", "フォルダ作成"), ("capture", "撮影"), ("stitch", "合成"), ("viewer", "見比べ")]

NAV_CSS = """
header.top .nav{display:flex;gap:2px;margin-left:20px}
header.top .nav a{position:relative;color:#a1a1aa;text-decoration:none;padding:6px 14px;border-radius:8px;font-size:14px;
  font-family:"Segoe UI","Yu Gothic UI","Meiryo",sans-serif;white-space:nowrap}
header.top .nav a:hover{color:#fafafa;background:rgba(255,255,255,.05)}
header.top .nav a.on{color:#fafafa;background:rgba(245,158,11,.16)}
header.top .nav .badge{display:inline-block;min-width:18px;margin-left:6px;padding:0 5px;border-radius:9px;background:#f59e0b;
  color:#111113;font-size:11px;font-weight:700;line-height:18px;text-align:center}
#quit{display:none !important}
@media (max-width:640px){header.top .name{display:none} header.top .nav{margin-left:4px} header.top .nav a{padding:6px 9px}}
#gone{display:none;position:fixed;inset:0;z-index:200;background:rgba(17,17,19,.94);align-items:center;justify-content:center;
  color:#fafafa;font-size:15px;font-family:"Segoe UI","Yu Gothic UI","Meiryo",sans-serif;text-align:center;line-height:1.8}
"""

# 画面ごとに 1 つの番号を持ち、数秒ごとに「開いている」と知らせる。閉じたときは「閉じた」と知らせる。
# アプリが終了したあとで古い画面に戻ったときは、開き直すよう案内する。
LIFE_JS = r"""
(() => {
  const T = '%TOKEN%', C = Math.random().toString(36).slice(2);
  const gone = () => { document.getElementById('gone').style.display = 'flex'; };
  const beat = () => fetch(`/ping?t=${T}&c=${C}`, {method: 'POST'}).then(r => { if (!r.ok) gone(); }).catch(gone);
  beat(); setInterval(beat, 5000);
  addEventListener('pagehide', () => navigator.sendBeacon(`/bye?t=${T}&c=${C}`));
  addEventListener('pageshow', ev => { if (ev.persisted) beat(); });
})();
"""


# ---------------------------------------------------------------- 合成待ちのフォルダ

_pending = {"t": 0.0, "ready": [], "odd": []}
_pending_lock = threading.Lock()


def pending(fresh: bool = False) -> tuple[list, list]:
    """合成待ち（IMG が決まった枚数〔545 は 6 枚、515 は 1 枚〕で合成画像がまだない）と、枚数が合わないフォルダ。30 秒間は前回の結果を使う。"""
    with _pending_lock:
        if fresh or time.monotonic() - _pending["t"] > 30:
            ready, odd = [], []
            for info in cs.find_targets([ROOT]):
                if info.probe is None:
                    continue
                done = re.compile(rf"^\d{{6}}_{info.probe}\.jpe?g$", re.IGNORECASE)
                if any(done.match(f.name) for f in info.path.iterdir() if f.is_file()):
                    continue
                (ready if len(info.raw_files) == info.need else odd).append(info)
            _pending.update(t=time.monotonic(), ready=ready, odd=odd)
        return _pending["ready"], _pending["odd"]


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


class Job:
    running = False
    done = 0
    total = 0
    current = ""
    results: list | None = None


def run_stitch() -> None:
    try:
        infos = cs.find_targets([ROOT])
        Job.total = sum(len(i.raw_files) == i.need for i in infos)
        results = []
        for info in infos:
            if len(info.raw_files) == info.need:
                Job.current = rel(info.path)
            r = cs.process(info, False)
            if len(info.raw_files) == info.need:
                Job.done += 1
            print(f"  [{r.status}] {info.path} " + " / ".join(r.messages))
            results.append(r)
        # 前回の合成で元画像を消さずに残したフォルダは、今回は「対象外（合成済み）」になる。
        # 撮り直したフォルダだけを合成し直したときにも、それらの元画像を結果の画面から削除できるよう前回の記録を引き継ぐ
        prev = {r.folder: r for r in last_results() if cs.deletable(r)}
        results = [prev[r.folder] if r.status == "skip" and r.folder in prev else r for r in results]
        results.sort(key=lambda r: cs.ORDER.index(r.status))
        if any(r.status in ("ok", "warn") for r in results):
            cs.save_manifest(results)
            (cs.REVIEW_DIR / "review.html").write_text(cs.render_page(results), encoding="utf-8")
        Job.results = results
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        Job.results = [cs.Result(str(ROOT), "error", messages=["合成の途中で問題が起きました。app.log を確認してください"])]
    finally:
        Job.running = False
        pending(fresh=True)


# ---------------------------------------------------------------- 画面

def brand(active: str, token: str) -> str:
    n = len(pending()[0])
    links = ""
    for k, label in TABS:
        on = ' class="on"' if k == active else ""
        badge = f'<span class="badge" title="合成待ちのフォルダ">{n}</span>' if k == "stitch" and n else ""
        if k == "capture" and cc.Session.active:
            badge = '<span class="badge" title="撮影モード中">●</span>'
        links += f'<a href="/{k}?t={token}"{on}>{label}{badge}</a>'
    return (f'<style>{NAV_CSS}</style>{cs.LOGO_SVG}<span class="name">culture-<b>tools</b></span>'
            f'<nav class="nav">{links}</nav>'
            '<div id="gone">culture-tools は終了しています。<br>タスクバーの culture-tools から開き直してください。</div>'
            f'<script>{LIFE_JS.replace("%TOKEN%", token)}</script>')


def unify(page: str, title: str) -> str:
    """各画面のタブ名とアイコンを culture-tools のものに揃える"""
    page = re.sub(r"<title>.*?</title>", f"<title>{title} | {APP}</title>", page, count=1)
    return page.replace(cv.FAVICON, cs.FAVICON).replace(cf.FAVICON, cs.FAVICON)


STITCH_JS = r"""
const T = document.body.dataset.token, $ = id => document.getElementById(id);
const post = (p, b) => fetch(p, {method: 'POST', headers: {'X-Token': T, 'Content-Type': 'application/json'}, body: JSON.stringify(b || {})});
async function poll() {
  const s = await (await fetch('/stitch/status?t=' + T)).json();
  if (!s.running) { location.reload(); return; }
  $('run').disabled = true;
  $('run').textContent = '合成しています…';
  $('msg').textContent = `${s.done} / ${s.total}　${s.current}`;
  $('prog').style.width = (s.total ? s.done / s.total * 100 : 0) + '%';
  setTimeout(poll, 800);
}
if ($('run')) $('run').onclick = async () => { await post('/stitch/run'); poll(); };
if (document.body.dataset.running === '1') poll();
if ($('last')) $('last').onclick = async () => { await post('/stitch/last'); location.reload(); };
"""

AGAIN_JS = r"""
document.getElementById('again').onclick = async () => {
  await fetch('/stitch/reset', {method: 'POST', headers: {'X-Token': token}});
  location.reload();
};
"""

# 「撮り直す」：元画像と合成画像をごみ箱へ移し、画面下のボタンからそのまま撮影モードを始められるようにする
RETAKE_JS = r"""
(() => {
  const go = document.getElementById('retake-go'), msg = document.getElementById('msg');
  const show = (targets) => {
    go.dataset.keys = JSON.stringify(targets.map(t => t.key));
    go.textContent = `撮り直しの撮影を始める（${targets.map(t => 'MEA-Probe ' + t.probe).join('、')}）`;
    go.style.display = targets.length ? '' : 'none';
  };
  document.querySelectorAll('button.redo').forEach(b => b.onclick = async () => {
    const sec = b.closest('section');
    const what = b.dataset.out === '1' ? '元画像と合成画像' : '元画像';
    if (!confirm(`${sec.querySelector('h2').textContent}\n\nこのフォルダの${what}をごみ箱へ移して、撮り直せるようにします。よろしいですか？`)) return;
    b.disabled = true; msg.textContent = '移動中...';
    try {
      const r = await (await fetch('/stitch/retake', {method: 'POST', headers: {'X-Token': token, 'Content-Type': 'application/json'},
                                                     body: JSON.stringify({id: Number(b.dataset.id)})})).json();
      const p = document.createElement('p');
      if (r.errors.length) {
        p.className = 'fail'; b.disabled = false;
        p.textContent = `${r.moved} 個を移動、${r.errors.length} 個は移動できませんでした: ${r.errors.join(' / ')}`;
      } else {
        p.className = 'done';
        p.textContent = r.today ? `${r.moved} 個のファイルをごみ箱へ移しました。画面下の「撮り直しの撮影を始める」から撮影できます`
                                : `${r.moved} 個のファイルをごみ箱へ移しました。今日の DIV フォルダではないため撮影モードは使えません。このフォルダに撮り直した写真を直接入れてください`;
        sec.className = 'retake'; sec.querySelector('.st').textContent = '撮り直し';
        const box = sec.querySelector('input.pick'); if (box) { box.checked = false; box.disabled = true; box.parentElement.remove(); }
        b.remove(); refresh();
      }
      sec.insertBefore(p, sec.querySelector('ul').nextSibling);
      show(r.targets); msg.textContent = '';
    } catch (e) { msg.textContent = 'エラー: ' + e.message; b.disabled = false; }
  });
  go.onclick = async () => {
    const r = await (await fetch('/capture/start', {method: 'POST', headers: {'X-Token': token, 'Content-Type': 'application/json'},
                                                   body: JSON.stringify({keys: JSON.parse(go.dataset.keys || '[]')})})).json();
    if (r.error) alert(r.error);
    location.href = '/capture?t=' + token;
  };
})();
"""


def retake_targets() -> list[dict]:
    """「撮り直す」を押したフォルダのうち、今日の DIV フォルダで撮影モードを使えるもの（撮る順は結果の画面の並び順）"""
    today = {Path(t["path"]): t for t in cc.today_targets(ROOT, cf.scan(ROOT))}
    out: list[dict] = []
    for r in Job.results or []:
        t = today.get(Path(r.folder))
        if r.status == "retake" and t and all(o["key"] != t["key"] for o in out):
            out.append({"key": t["key"], "probe": t["probe"]})
    return out


def last_results() -> list:
    """前回の合成の記録のうち、元画像がまだ残っていて削除できるもの"""
    try:
        res = [cs.Result(**r) for r in json.loads(cs.MANIFEST.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return []
    return res if any(cs.deletable(r) for r in res) else []


def stitch_page(token: str) -> str:
    if Job.results is not None and not Job.running:
        targets = retake_targets()
        keys = json.dumps([t["key"] for t in targets], ensure_ascii=False)
        names = "、".join(f"MEA-Probe {t['probe']}" for t in targets)
        hide = "" if targets else ' style="display:none"'
        go = (f'<button id="retake-go" data-keys="{html.escape(keys)}"{hide}>'
              f'撮り直しの撮影を始める（{names}）</button>')
        page = cs.render_page(Job.results, token, brand=brand("stitch", token),
                              extra_bar=go + '<button class="sub" id="again">合成待ちを確認し直す</button>',
                              extra_js=AGAIN_JS + RETAKE_JS, folder_label=lambda f: rel(Path(f)), retake=True)
        return unify(page, "合成結果の確認")
    ready, odd = pending(fresh=not Job.running)
    rows = "".join(f'<li>{rel(i.path)}　{i.kind}電極{"（1 枚・合成なし）" if i.need == 1 else ""}</li>' for i in ready)
    odd_rows = "".join(f'<li>{rel(i.path)}　{i.kind}電極：IMG ファイルが {len(i.raw_files)} 枚（{i.need} 枚で合成します）</li>'
                       for i in odd)
    body = (f'<section class="ok"><h2>合成待ちのフォルダ（{len(ready)}）</h2><ul>{rows}</ul></section>' if ready else
            '<section class="skip"><h2>合成前の写真が入ったフォルダはありません</h2>'
            '<ul><li>撮影した写真（IMG_数字.JPG。545電極は 6 枚、515電極は 1 枚）を DIV フォルダに入れると、ここに表示されます</li></ul></section>')
    if odd:
        body += (f'<section class="warn"><h2>枚数が合わないため合成しないフォルダ（{len(odd)}）</h2><ul>{odd_rows}</ul>'
                 '</section>')
    last = last_results()
    if last:
        n = sum(cs.deletable(r) for r in last)
        body += (f'<section><h2>前回の合成で、元画像がまだ残っているフォルダ（{n}）</h2>'
                 '<ul><li>結果を開くと、合成画像を確かめてから元画像をごみ箱へ移動できます</li></ul>'
                 '<button class="btn-line" id="last">前回の結果を開く</button></section>')
    label = f"合成を開始（{len(ready)} フォルダ）" if ready else "合成するフォルダがありません"
    return (f'<!doctype html><html lang="ja"><meta charset="utf-8"><title>合成 | {APP}</title>'
            f'<link rel="icon" type="image/svg+xml" href="{cs.FAVICON}"><style>{cs.PAGE_CSS}'
            '#prog-wrap{flex:1;height:4px;background:rgba(255,255,255,.08);border-radius:2px;overflow:hidden;max-width:320px}'
            '#prog{height:100%;width:0;background:#f59e0b;transition:width .3s}'
            '.btn-line{margin-top:4px;background:transparent;color:#fafafa;border:1px solid rgba(255,255,255,.15);'
            'border-radius:8px;padding:6px 14px;font:inherit;cursor:pointer}.btn-line:hover{border-color:rgba(255,255,255,.3)}'
            f'</style><body data-token="{token}" data-running="{1 if Job.running else 0}">'
            f'<header class="top">{brand("stitch", token)}</header><main><h1>合成</h1>'
            '<p>合成前の写真（IMG_数字.JPG。545電極は 6 枚、515電極は 1 枚）が入った DIV フォルダを写真のフォルダの中から探し、まとめて合成します。'
            '515電極の写真は 1 枚なので合成はせず、電極を検出して傾きを補正し、正方形に切り出します。'
            '合成が終わると、この画面に結果が表示されます。</p>'
            f'{body}</main><div id="bar"><button id="run"{"" if ready else " disabled"}>{label}</button>'
            '<div id="prog-wrap"><div id="prog"></div></div><span id="msg"></span></div>'
            f'<script>{STITCH_JS}</script></body></html>')


def simple_page(active: str, title: str, token: str, body: str) -> str:
    return (f'<!doctype html><html lang="ja"><meta charset="utf-8"><title>{title} | {APP}</title>'
            f'<link rel="icon" type="image/svg+xml" href="{cs.FAVICON}"><style>{cs.PAGE_CSS}</style>'
            f'<body data-token="{token}"><header class="top">{brand(active, token)}</header><main>{body}</main>'

            f'<script>{STITCH_JS}</script></body></html>')


# ---------------------------------------------------------------- 配信

def serve() -> None:
    token = secrets.token_urlsafe(16)
    clients: dict[str, float] = {}              # 開いている画面ごとの、最後に合図が届いた時刻
    quiet_since = [time.monotonic() + 60 - CLOSE_GRACE]   # 起動直後はブラウザが開くまで 1 分待つ
    life = threading.Lock()
    viewer = {"entries": []}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self) -> bool:
            q = parse_qs(urlparse(self.path).query).get("t", [None])[0]
            return secrets.compare_digest(self.headers.get("X-Token") or q or "", token)

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", cache=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _html(self, page: str):
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

        def _json(self, obj):
            self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _file(self, path: Path):
            if not path.exists():
                return self._send(404, b"not found", "text/plain")
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)

        def do_GET(self):
            if not self._ok():
                return self._send(403, b"forbidden", "text/plain")
            u = urlparse(self.path)
            parts = u.path.strip("/").split("/")
            try:
                if parts == [""]:                           # 合成待ちがあれば「合成」、なければ「フォルダ作成」から
                    self.send_response(302)
                    self.send_header("Location", f"/{'stitch' if pending()[0] else 'folders'}?t={token}")
                    self.end_headers()
                    return
                if parts == ["alive"]:
                    return self._json({"app": APP})
                if parts == ["folders"]:
                    return self._html(unify(cf.render_page(cf.scan(ROOT), token, brand("folders", token), ROOT), "フォルダ作成"))
                if parts == ["capture"]:
                    return self._html(cc.render_page(ROOT, cf.scan(ROOT), token, brand("capture", token), cs.FAVICON))
                if parts == ["capture", "status"]:
                    return self._json(cc.Session.status())
                if len(parts) == 3 and parts[:2] == ["capture", "thumb"]:
                    from urllib.parse import unquote
                    data = cc.thumb(unquote(parts[2]))
                    return self._send(200, data, "image/jpeg", cache=True) if data else self._send(404, b"", "text/plain")
                if parts == ["stitch"]:
                    return self._html(stitch_page(token))
                if parts == ["stitch", "status"]:
                    return self._json({"running": Job.running, "done": Job.done, "total": Job.total, "current": Job.current})
                if parts == ["missing"]:                     # 見比べに出ない画像と、その理由・表示されるようにする方法
                    return self._html(cv.render_missing(cv.diagnose(ROOT, viewer["entries"])))
                if parts == ["viewer"]:
                    entries, warns = cv.scan(ROOT)
                    viewer["entries"] = entries
                    if not entries:                          # 初めて使う人向けに、どの形にすれば表示されるかを案内する
                        return self._html(simple_page("viewer", "見比べ", token,
                                                      f"<style>{cv.MISSING_CSS}</style><h1>見比べ</h1>"
                                                      "<p>見比べられる合成画像が見つかりませんでした。</p>"
                                                      + cv.render_missing(cv.diagnose(ROOT, entries))))
                    return self._html(unify(cv.render_page(entries, cv.group(entries), warns, token,
                                                           brand("viewer", token)), "見比べ"))
                if len(parts) == 2 and parts[1].isdigit():
                    i = int(parts[1])
                    if parts[0] == "scale" and i < len(viewer["entries"]):   # 見比べのスケールバーの長さ
                        return self._json(cv.scale_info(viewer["entries"][i]))
                    if parts[0] == "img" and i < len(viewer["entries"]):     # 見比べの画像
                        e = viewer["entries"][i]
                        if parse_qs(u.query).get("q", ["m"])[0] == "f":
                            return self._file(Path(e["path"]))
                        return self._send(200, cv.medium_image(i, e), "image/jpeg", cache=True)
                    res = Job.results or []
                    if parts[0] in ("preview", "full") and i < len(res):         # 合成結果の確認ページの画像
                        r = res[i]
                        if parts[0] == "preview" and r.preview:
                            return self._file(cs.REVIEW_DIR / r.preview)
                        if parts[0] == "full" and r.output:
                            return self._file(Path(r.output))
                self._send(404, b"not found", "text/plain")
            except ConnectionError:
                pass                                          # 画面側が読み込みを取りやめた

        def do_POST(self):
            if not self._ok():
                return self._send(403, b"forbidden", "text/plain")
            u = urlparse(self.path)
            path = u.path
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            c = parse_qs(u.query).get("c", [None])[0]
            if path == "/ping":
                if c:
                    with life:
                        clients[c] = time.monotonic()
                return self._json({})
            if path == "/bye":
                with life:
                    if clients.pop(c, None) is not None:
                        quiet_since[0] = time.monotonic()
                return self._json({})
            if path == "/quit":
                return self._json({})                   # 「終了」ボタンはなくしたので何もしない
            if path == "/rename":                             # 以前の形の播種フォルダ名を新しい形に変える
                r = cf.rename_old(ROOT)
                pending(fresh=True)
                return self._json(r)
            if path == "/create":                             # フォルダ作成
                results = cf.create(ROOT, body)
                pending(fresh=True)
                return self._json({"results": results, "seeds": cf.scan(ROOT)})
            if path == "/open":
                p = Path(body.get("path", ""))
                if p.is_dir() and ROOT.resolve() in p.resolve().parents:
                    import os
                    if sys.platform == "win32":
                        os.startfile(p)                       # noqa: S606
                return self._json({})
            if path == "/capture/inbox":                     # 取り込み元フォルダを選ぶ
                p = cc.pick_folder()
                if p is None:
                    return self._json({})
                if ROOT.resolve() == p.resolve() or ROOT.resolve() in p.resolve().parents:
                    return self._json({"error": "写真のフォルダの外のフォルダを選んでください"})
                cc.save_inbox(p)
                return self._json({})
            if path == "/capture/start":
                err = cc.start(ROOT, cf.scan(ROOT), body.get("keys", []))
                if not err and not Job.running:
                    Job.results = None          # 撮影を始めたら、前回の合成結果の画面は閉じる（撮影後に「合成」を開くと合成待ちが出る）
                return self._json({"error": err})
            if path == "/capture/next":
                cc.Session.next()
                pending(fresh=True)
                return self._json({})
            if path == "/capture/undo":
                return self._json({"error": cc.Session.undo()})
            if path == "/capture/end":
                cc.Session.stop("撮影モードを途中で中断しました")
                pending(fresh=True)
                return self._json({})
            if path == "/stitch/run":
                with lock:
                    if not Job.running:
                        Job.running, Job.done, Job.total, Job.current, Job.results = True, 0, 0, "", None
                        threading.Thread(target=run_stitch, daemon=True).start()
                return self._json({})
            if path == "/stitch/reset":
                if not Job.running:
                    Job.results = None
                return self._json({})
            if path == "/stitch/retake":                     # 合成結果の確認ページの「撮り直す」
                res = Job.results or []
                i = body.get("id")
                if Job.running or not (isinstance(i, int) and 0 <= i < len(res)) or not cs.retakable(res[i]):
                    return self._json({"moved": 0, "errors": ["撮り直しの対象にできません"], "today": False,
                                       "targets": retake_targets()})
                with lock:
                    r = res[i]
                    moved, errors = cs.trash_for_retake(r)
                    print(f"  撮り直しのため {moved} 個をごみ箱へ移動: {r.folder}")
                    if not errors:
                        r.status = "retake"
                        r.messages.append("撮り直しのため、元画像と合成画像をごみ箱へ移しました")
                    cs.save_manifest(res)
                pending(fresh=True)
                targets = retake_targets()
                today = any(Path(t["path"]) == Path(r.folder) for t in cc.today_targets(ROOT, cf.scan(ROOT)))
                return self._json({"moved": moved, "errors": errors, "today": today, "targets": targets})
            if path == "/stitch/last":
                if not Job.running:
                    Job.results = last_results() or None
                return self._json({})
            if path == "/delete":                             # 合成結果の確認ページの「元画像をごみ箱へ移動」
                out = []
                res = Job.results or []
                with lock:
                    for i in body.get("ids", []):
                        if not (isinstance(i, int) and 0 <= i < len(res)) or not cs.deletable(res[i]):
                            continue
                        moved, errors = cs.trash_raw_files(res[i])
                        if not errors:
                            res[i].status = "deleted"
                        out.append({"id": i, "moved": moved, "errors": errors})
                    cs.save_manifest(res)
                return self._json({"results": out})
            self._send(404, b"not found", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={token}"
    STATE.write_text(json.dumps({"port": port, "token": token}), encoding="utf-8")
    print(f"{APP} を起動しました: {url}")
    if "--no-open" not in sys.argv:
        webbrowser.open(url)

    def watchdog():
        while True:
            time.sleep(1)
            now = time.monotonic()
            with life:
                for k, t in list(clients.items()):
                    if now - t > STALE:
                        del clients[k]
                        quiet_since[0] = now
                if clients or Job.running:
                    continue
                if now - quiet_since[0] > CLOSE_GRACE:
                    print("画面がすべて閉じられたので終了します。")
                    server.shutdown()
                    return
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            if json.loads(STATE.read_text(encoding="utf-8")).get("token") == token:
                STATE.unlink()
        except (OSError, ValueError):
            pass


def already_running() -> bool:
    """起動中のアプリがあれば、その画面を開いて True を返す"""
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{st['port']}"
        with urllib.request.urlopen(f"{url}/alive?t={st['token']}", timeout=1.5) as r:
            if json.loads(r.read()).get("app") != APP:
                return False
        webbrowser.open(f"{url}/?t={st['token']}")
        return True
    except (OSError, ValueError, KeyError):
        return False


def notify(msg: str) -> None:
    """黒い画面がなくても気づけるよう、Windows の通知ダイアログで知らせる"""
    print(msg)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, APP, 0x10)


def ask(msg: str) -> bool:
    """「はい」「いいえ」で答えてもらう"""
    print(msg)
    if sys.platform == "win32":
        import ctypes
        return ctypes.windll.user32.MessageBoxW(0, msg, APP, 0x4 | 0x30) == 6      # はい/いいえ・注意アイコン
    return True


def choose_root(prev: Path | None) -> Path | None:
    """写真のフォルダ（YYMMDD播種(Probe 番号) のフォルダを入れる場所）を選んでもらい、app\\config.json に覚える"""
    if prev is not None and not ask(
            f"写真のフォルダが見つかりません。\n{prev}\n\n"
            "Google ドライブなど、フォルダのある場所が使える状態か確認してください。\n\n"
            "別のフォルダを選び直しますか？"):
        return None
    import tkinter as tk
    from tkinter import filedialog
    w = tk.Tk()
    w.withdraw()
    w.attributes("-topmost", True)
    while True:
        picked = filedialog.askdirectory(parent=w, mustexist=True,
                                         title="写真のフォルダ（YYMMDD播種(Probe 番号) のフォルダを入れる場所）を選んでください")
        if not picked:
            w.destroy()
            return None
        root = Path(picked)
        seeded = any(d.is_dir() and re.match(r"^\d{6}播種", d.name) for d in root.iterdir())
        if seeded or ask(f"{root}\n\nこのフォルダには「YYMMDD播種(Probe 番号)」の形のフォルダがありません。"
                         "このフォルダを写真のフォルダとして使いますか？"):
            break
    w.destroy()
    cs.save_root(root)
    print(f"写真のフォルダを設定しました: {root}")
    return root


def main() -> None:
    global ROOT
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if sys.stdout is None:                              # pythonw で起動したときはメッセージをファイルに残す
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.replace(LOG.with_suffix(".old.log"))       # 1MB を超えたら古いほうへ回し、増え続けないようにする
        sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)
    print(f"---- {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if already_running():
        return
    if args:                                            # 動作確認用：写真のフォルダを指定できる
        ROOT = Path(args[0])
        if not ROOT.is_dir():
            notify(f"写真のフォルダが見つかりません。\n{ROOT}")
            sys.exit(1)
    else:
        saved = cs.load_root()
        if saved is not None and saved.is_dir():
            ROOT = saved
        else:
            picked = choose_root(saved)
            if picked is None:
                print("写真のフォルダが選ばれなかったので終了します。")
                return
            ROOT = picked
    serve()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        notify(f"{APP} を起動できませんでした。詳しくは次のファイルを確認してください。\n{LOG}")
        sys.exit(1)
