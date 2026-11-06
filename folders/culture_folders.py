"""撮影前のフォルダ作成ツール（culture-folders）

使い方:
    python culture_folders.py [フォルダ]    フォルダ省略時は 00_Culture_Media を使う

ブラウザで画面を開き、次の 2 つをボタン 1 つで作る。
  ・今日撮影するMEA-Probe の DIV フォルダ（YYMMDD播種(番号)\\DIVn。n は播種日からの日数で自動計算）
  ・新しく播種したMEA-Probe のフォルダ（YYMMDD播種(番号)\\DIV0）
既にあるフォルダは作り直さず、ファイルの移動や削除は一切しない。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "stitch"))   # 合成（culture_stitch.py）の部品を使う
import culture_stitch as cs

RECENT_COUNT = 6      # 新しく播種した順に、この件数を最初から一覧に出す（残りは「以前のMEA-Probe も表示」で出す）

SEED_RE = re.compile(r"^(\d{6})播種\((\d+)\)$")
DIV_RE = re.compile(r"^DIV(\d+)$", re.IGNORECASE)

# culture-folders のロゴ（culture-stitcher と同じ円に、フォルダを足す「＋」）
LOGO = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32' fill='none'>"
        "<circle cx='16' cy='16' r='14' fill='#111113'/>"
        "<circle cx='16' cy='16' r='14' fill='#f59e0b' fill-opacity='.14' stroke='#f59e0b' stroke-width='2.2'/>"
        "<path d='M16 9.5V22.5M9.5 16H22.5' stroke='#fafafa' stroke-width='2.8' stroke-linecap='round'/>"
        "<circle cx='22' cy='10' r='2.6' fill='#fbbf24'/></svg>")
FAVICON = "data:image/svg+xml," + LOGO.replace("#", "%23")


def scan(root: Path) -> list[dict]:
    seeds = []
    for d in root.iterdir():
        m = SEED_RE.match(d.name) if d.is_dir() else None
        if not m:
            continue                                   # 旧形式（Probe が 2 つのフォルダ）などは対象外
        divs = sorted(int(x.group(1)) for x in (DIV_RE.match(c.name) for c in d.iterdir() if c.is_dir()) if x)
        seed = dt.datetime.strptime(m.group(1), "%y%m%d").date()
        seeds.append({"key": d.name, "seed": seed.isoformat(), "probe": int(m.group(2)), "divs": divs})
    return sorted(seeds, key=lambda s: (s["seed"], -s["probe"]), reverse=True)   # 新しい播種から、同じ日は番号の小さい順


def create(root: Path, body: dict) -> list[dict]:
    out = []
    shoot = dt.date.fromisoformat(body.get("shoot") or dt.date.today().isoformat())
    known = {s["key"] for s in scan(root)}
    for key in body.get("keys", []):
        if key not in known:
            out.append({"ok": False, "label": key, "msg": "このMEA-Probe のフォルダが見つかりません"})
            continue
        seed = dt.datetime.strptime(SEED_RE.match(key).group(1), "%y%m%d").date()
        div = (shoot - seed).days
        if div < 0:
            out.append({"ok": False, "label": key, "msg": "撮影日が播種日より前です"})
            continue
        out.append(make(root / key / f"DIV{div}", f"{key}\\DIV{div}"))
    new = body.get("new") or {}
    if new.get("probes"):
        date = dt.date.fromisoformat(new["date"])
        for n in new["probes"]:
            if not (isinstance(n, int) and 0 < n < 1000):
                continue
            key = f"{date:%y%m%d}播種({n})"
            out.append(make(root / key / "DIV0", f"{key}\\DIV0"))
    return out


def make(path: Path, label: str) -> dict:
    if path.exists():
        return {"ok": True, "label": label, "path": str(path), "msg": "既にあります（そのまま使えます）"}
    try:
        path.mkdir(parents=True)
    except OSError as e:
        return {"ok": False, "label": label, "msg": f"作れませんでした（{e}）"}
    print(f"  作成しました: {path}")
    return {"ok": True, "label": label, "path": str(path), "msg": "作成しました", "made": True}


CSS = """
:root{--bg:#18181b;--panel:#111113;--card:#1f1f23;--line:rgba(255,255,255,.08);--ink:#fafafa;--quiet:#a1a1aa;
  --accent:#f59e0b;--accent2:#fbbf24;--good:#4ade80;--bad:#f87171}
html,body{margin:0;background:var(--bg);color:var(--ink);font-family:"Segoe UI","Yu Gothic UI","Meiryo",sans-serif;font-size:14px}
header.top{position:sticky;top:0;z-index:5;height:52px;display:flex;align-items:center;gap:10px;padding:0 24px;
  background:var(--panel);border-bottom:1px solid var(--line)}
header.top svg{width:30px;height:30px;flex:none}
header.top .name{font-weight:600;font-size:15px}
header.top .name b{color:var(--accent);font-weight:600}
header .tools{margin-left:auto}
main{max-width:760px;margin:0 auto;padding:24px 24px 120px}
h2{font-size:15px;margin:28px 0 8px;display:flex;align-items:center;gap:12px}
h2 .sub{font-weight:400;color:var(--quiet);font-size:13px}
.date{display:inline-flex;align-items:center;gap:8px;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px;
  padding:5px 10px;font:inherit;cursor:pointer}
.date:hover{border-color:rgba(255,255,255,.25)}
.date .caret{color:var(--quiet);font-size:11px}
#cal{display:none;position:absolute;z-index:40;width:280px;background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:12px;box-shadow:0 12px 32px rgba(0,0,0,.5)}
#cal.open{display:block}
.cal-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;font-weight:600}
.cal-h button{background:none;border:0;color:var(--quiet);font:inherit;font-size:18px;line-height:1;cursor:pointer;padding:4px 10px;border-radius:6px}
.cal-h button:hover{color:var(--ink);background:rgba(255,255,255,.06)}
.cal-g{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}
.cal-g .w{color:var(--quiet);font-size:11px;text-align:center;padding:4px 0}
.cal-g .d{background:none;border:0;color:var(--ink);font:inherit;font-size:13px;height:32px;border-radius:6px;cursor:pointer}
.cal-g .d:hover{background:rgba(255,255,255,.06)}
.cal-g .d.out{color:#52525b}
.cal-g .d.today{box-shadow:inset 0 0 0 1px var(--quiet)}
.cal-g .d.sel{background:var(--accent);color:#111113;font-weight:600;box-shadow:none}
.cal-f{display:flex;justify-content:flex-end;margin-top:8px}
.cal-f .btn{padding:4px 12px;font-size:13px}
.new .hint code{font-family:Consolas,monospace;color:var(--ink);font-size:13px}
input[type=text]{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px;
  padding:5px 8px;font:inherit;color-scheme:dark}
.list{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
label.row{display:grid;grid-template-columns:28px 80px 1fr auto;align-items:center;gap:8px;padding:10px 16px;
  border-bottom:1px solid var(--line);cursor:pointer}
label.row:last-child{border-bottom:0}
label.row:hover{background:rgba(255,255,255,.03)}
label.row.off{cursor:default;opacity:.45}
label.row input{transform:scale(1.25);accent-color:var(--accent)}
.dish{font-weight:600;font-size:15px}
.seed{color:var(--quiet);font-size:12px}
.prev{color:var(--quiet);font-size:12px}
.div{font-weight:600;white-space:nowrap}
.div small{font-weight:400;color:var(--quiet);margin-left:6px}
.more{display:block;margin:8px 0 0;background:none;border:0;color:var(--quiet);font:inherit;font-size:13px;cursor:pointer;padding:0}
.more:hover{color:var(--ink)}
.new{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.new .hint{color:var(--quiet);font-size:12px}
.btn{background:transparent;border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:6px 12px;font:inherit;cursor:pointer}
.btn:hover{border-color:rgba(255,255,255,.25)}
#bar{position:fixed;left:0;right:0;bottom:0;background:var(--panel);border-top:1px solid var(--line);padding:12px 24px;
  display:flex;justify-content:center}
#go{background:var(--accent);color:#111113;border:0;border-radius:8px;padding:10px 24px;font:inherit;font-weight:600;cursor:pointer}
#go:disabled{background:#3f3f46;color:var(--quiet);cursor:default}
#res{margin-top:24px}
.r{display:flex;gap:12px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}
.r .m{min-width:11em}
.r.ok .m{color:var(--good)} .r.ng .m{color:var(--bad)} .r.ex .m{color:var(--quiet)}
.r .p{flex:1;font-family:Consolas,monospace;font-size:13px}
#done{display:none;position:fixed;inset:0;background:var(--bg);align-items:center;justify-content:center;font-size:15px;z-index:50}
"""

JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const T = D.token, $ = id => document.getElementById(id);
let seeds = D.seeds, showAll = false;
const picked = new Set();
const md = iso => `${+iso.slice(5, 7)}/${+iso.slice(8, 10)}`;
const days = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / 864e5);
const WD = '日月火水木金土';
const isoOf = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const dateOf = iso => new Date(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10));
const dayLabel = iso => { const d = dateOf(iso); return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}（${WD[d.getDay()]}）`; };
let calFor = null, calMonth = null;
function drawCal() {
  const sel = calFor.dataset.value, first = new Date(calMonth.getFullYear(), calMonth.getMonth(), 1);
  const n = Math.ceil((first.getDay() + new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate()) / 7) * 7;
  let cells = '';
  for (let i = 0; i < n; i++) {
    const d = new Date(first.getFullYear(), first.getMonth(), 1 - first.getDay() + i), iso = isoOf(d);
    cells += `<button class="d${d.getMonth() !== first.getMonth() ? ' out' : ''}${iso === D.today ? ' today' : ''}${iso === sel ? ' sel' : ''}" data-iso="${iso}">${d.getDate()}</button>`;
  }
  $('cal').innerHTML = `<div class="cal-h"><button data-mv="-1" title="前の月">‹</button><span>${first.getFullYear()}年 ${first.getMonth() + 1}月</span>`
    + `<button data-mv="1" title="次の月">›</button></div><div class="cal-g">${[...WD].map(w => `<span class="w">${w}</span>`).join('')}${cells}</div>`
    + `<div class="cal-f"><button class="btn" data-iso="${D.today}">今日</button></div>`;
}
function openCal(btn) {
  if (calFor === btn && $('cal').classList.contains('open')) { closeCal(); return; }
  calFor = btn; const d = dateOf(btn.dataset.value); calMonth = new Date(d.getFullYear(), d.getMonth(), 1);
  drawCal();
  const r = btn.getBoundingClientRect();
  $('cal').style.left = `${Math.min(r.left, innerWidth - 300) + scrollX}px`;
  $('cal').style.top = `${r.bottom + 6 + scrollY}px`;
  $('cal').classList.add('open');
}
function closeCal() { $('cal').classList.remove('open'); calFor = null; }
$('cal').addEventListener('click', ev => {
  ev.stopPropagation();
  const b = ev.target.closest('button');
  if (!b) return;
  if (b.dataset.mv) { calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + +b.dataset.mv, 1); drawCal(); return; }
  if (b.dataset.iso) { calFor.dataset.value = b.dataset.iso; closeCal(); render(); }
});
for (const id of ['shoot', 'seedDate']) $(id).addEventListener('click', ev => { ev.stopPropagation(); openCal($(id)); });
document.addEventListener('click', closeCal);
document.addEventListener('keydown', ev => { if (ev.key === 'Escape') closeCal(); });
const post = (path, body) => fetch(path, {method: 'POST', headers: {'X-Token': T, 'Content-Type': 'application/json'},
  body: JSON.stringify(body || {})}).then(r => r.json());

function render() {
  const shoot = $('shoot').dataset.value;
  $('shootLabel').textContent = shoot === D.today ? '今日' : `${md(shoot)}に`;
  const list = showAll ? seeds : seeds.slice(0, D.recent);
  $('list').innerHTML = list.length ? list.map(s => {
    const d = days(s.seed, shoot), has = s.divs.includes(d), off = d < 0 || has;
    if (off) picked.delete(s.key);
    const last = s.divs.length ? `前回 DIV${s.divs.at(-1)}` : 'まだ DIV フォルダなし';
    const right = d < 0 ? '播種前' : `DIV${d}<small>${has ? '作成済み' : 'を作成'}</small>`;
    return `<label class="row${off ? ' off' : ''}"><input type="checkbox" data-key="${s.key}"${picked.has(s.key) ? ' checked' : ''}${off ? ' disabled' : ''}>`
      + `<span class="dish">MEA-Probe ${s.probe}</span><span><span class="seed">${md(s.seed)} 播種</span>　<span class="prev">${last}</span></span>`
      + `<span class="div">${right}</span></label>`;
  }).join('') : '<div style="padding:12px 16px;color:var(--quiet)">最近播種したMEA-Probe はありません</div>';
  const older = Math.max(0, seeds.length - D.recent);
  $('more').style.display = older ? '' : 'none';
  $('more').textContent = showAll ? '最近のMEA-Probe だけ表示' : `以前のMEA-Probe も表示（${older} 件）`;
  const probes = /^\d+$/.test($('probes').value) && +$('probes').value > 0 ? [+$('probes').value] : [];
  const sd = $('seedDate').dataset.value, yymmdd = sd.slice(2, 4) + sd.slice(5, 7) + sd.slice(8, 10);
  $('newHint').innerHTML = probes.length
    ? `作成するフォルダ：<code>${yymmdd}播種(${probes[0]})\\DIV0</code>`
    : 'Probe 番号を入れると、作成するフォルダ名がここに表示されます';
  for (const id of ['shoot', 'seedDate']) $(id).innerHTML = `${dayLabel($(id).dataset.value)}<span class="caret">▾</span>`;
  const n = picked.size + probes.length;
  $('go').disabled = n === 0;
  $('go').textContent = n ? `フォルダを作成（${n} 件）` : '作成するMEA-Probe を選んでください';
}

$('list').addEventListener('change', ev => {
  const k = ev.target.dataset.key;
  if (k) { ev.target.checked ? picked.add(k) : picked.delete(k); render(); }
});
$('probes').addEventListener('input', () => { $('probes').value = $('probes').value.replace(/\D/g, ''); render(); });
$('more').onclick = () => { showAll = !showAll; render(); };

$('go').onclick = async () => {
  const probes = /^\d+$/.test($('probes').value) && +$('probes').value > 0 ? [+$('probes').value] : [];
  const res = await post('/create', {shoot: $('shoot').dataset.value, keys: [...picked],
                                      new: probes.length ? {date: $('seedDate').dataset.value, probes} : null});
  seeds = res.seeds; picked.clear(); $('probes').value = '';
  $('res').innerHTML = '<h2>結果</h2>' + res.results.map((r, i) =>
    `<div class="r ${!r.ok ? 'ng' : r.made ? 'ok' : 'ex'}"><span class="m">${r.msg}</span><span class="p">${r.label}</span>`
    + (r.path ? `<button class="btn" data-copy="${i}">パスをコピー</button><button class="btn" data-open="${i}">開く</button>` : '')
    + '</div>').join('');
  $('res').onclick = async ev => {
    const c = ev.target.dataset.copy, o = ev.target.dataset.open;
    if (c != null) { await navigator.clipboard.writeText(res.results[c].path); ev.target.textContent = 'コピーしました'; }
    if (o != null) post('/open', {path: res.results[o].path});
  };
  render();
};

post('/ping'); const beat = setInterval(() => post('/ping'), 5000);
$('quit').onclick = async () => { clearInterval(beat); await post('/quit').catch(() => {}); $('done').style.display = 'flex'; window.close(); };
render();
"""


def render_page(seeds: list[dict], token: str, brand: str | None = None) -> str:
    brand = brand or LOGO + '<span class="name">culture-<b>folders</b></span>'
    today = dt.date.today().isoformat()
    data = json.dumps({"token": token, "today": today, "recent": RECENT_COUNT, "seeds": seeds}, ensure_ascii=False)
    data = data.replace("</", "<\\/")
    return (f'<!doctype html><html lang="ja"><meta charset="utf-8"><title>フォルダ作成 | culture-folders</title>'
            f'<link rel="icon" type="image/svg+xml" href="{FAVICON}"><style>{CSS}</style><body>'
            f'<header class="top">{brand}'
            f'<div class="tools"><button class="btn" id="quit">終了</button></div></header><main>'
            f'<h2><span><span id="shootLabel">今日</span>撮影するMEA-Probe</span>'
            f'<span class="sub">撮影日 <button class="date" id="shoot" data-value="{today}"></button></span></h2>'
            f'<div class="list" id="list"></div><button class="more" id="more"></button>'
            f'<h2>新しく播種したMEA-Probe</h2>'
            f'<div class="new"><span>播種日 <button class="date" id="seedDate" data-value="{today}"></button></span>'
            f'<span>Probe 番号 <input type="text" id="probes" size="6" inputmode="numeric" maxlength="3" placeholder="例：5"></span>'
            f'<span class="hint" id="newHint"></span></div>'
            f'<div id="res"></div></main><div id="bar"><button id="go"></button></div>'
            f'<div id="cal"></div><div id="done">終了しました。このタブは閉じて構いません。</div>'
            f'<script id="data" type="application/json">{data}</script><script>{JS}</script></body></html>')


def serve(root: Path, open_browser: bool) -> None:
    token = secrets.token_urlsafe(16)
    last_ping = [time.monotonic() + 60]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self) -> bool:
            q = parse_qs(urlparse(self.path).query).get("t", [None])[0]
            return secrets.compare_digest(self.headers.get("X-Token") or q or "", token)

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def do_GET(self):
            if not self._ok():
                return self._send(403, b"forbidden", "text/plain")
            if urlparse(self.path).path == "/":
                return self._send(200, render_page(scan(root), token).encode("utf-8"), "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._ok():
                return self._send(403, b"forbidden", "text/plain")
            path = urlparse(self.path).path
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if path == "/ping":
                last_ping[0] = time.monotonic()
                return self._json({})
            if path == "/create":
                results = create(root, body)
                return self._json({"results": results, "seeds": scan(root)})
            if path == "/open":
                p = Path(body.get("path", ""))
                # 00_Culture_Media の中のフォルダだけをエクスプローラーで開く
                if p.is_dir() and root.resolve() in p.resolve().parents:
                    if sys.platform == "win32":
                        os.startfile(p)                 # noqa: S606
                    else:
                        subprocess.Popen(["xdg-open", str(p)])
                return self._json({})
            if path == "/quit":
                self._json({})
                threading.Thread(target=server.shutdown, daemon=True).start()
                return
            self._send(404, b"not found", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    print("フォルダ作成の画面を開きました。画面を閉じるか「終了」を押すと、この黒い画面も自動で閉じます。")
    print(f"画面が開かない場合はこのアドレスを開いてください: {url}")
    if open_browser:
        webbrowser.open(url)

        def watchdog():
            while True:
                time.sleep(2)
                if time.monotonic() - last_ping[0] > cs.PAGE_TIMEOUT:
                    server.shutdown()
                    return
        threading.Thread(target=watchdog, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = Path(args[0]) if args else cs.load_root()
    if root is None:
        print("写真のフォルダが設定されていません。culture-tools を一度起動して選ぶか、フォルダを指定してください。")
        sys.exit(1)
    if not root.is_dir():
        print(f"フォルダが見つかりません: {root}")
        sys.exit(1)
    serve(root, "--no-open" not in sys.argv)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
