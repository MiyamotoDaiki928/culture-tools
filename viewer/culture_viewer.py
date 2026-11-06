"""MEA-Probe 合成画像の見比べツール（culture-viewer）

使い方:
    python culture_viewer.py [フォルダ]    フォルダ省略時は 00_Culture_Media を読む

00_Culture_Media の「YYMMDD播種(番号)\\DIVn\\YYMMDD_番号.jpg」を集め、2 枚を左右に並べて
（または重ねて）見比べる画面をブラウザで開く。画像は読むだけで、vault 内のファイルは変更しない。
画面を閉じるか「終了」を押すと、このツールも終了する。
"""
from __future__ import annotations

import datetime as dt
import io
import html
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps

import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "stitch"))   # 合成（culture_stitch.py）の部品を使う
import culture_stitch as cs

MEDIUM = 2400                              # 通常表示に使う縮小画像の長辺。これより拡大すると原寸を読む

SEED_RE = re.compile(r"^(\d{6})播種\((\d+)\)$")
DIV_RE = re.compile(r"^DIV(\d+)$", re.IGNORECASE)
IMG_RE = re.compile(r"^(\d{6})_(\d+)\.jpe?g$", re.IGNORECASE)
LOOKS_LIKE = re.compile(r"^\d+_\d+\.jpe?g$", re.IGNORECASE)   # 合成画像らしいが名前の形式が違うもの


# ---------------------------------------------------------------- 合成画像を集める

def scan(root: Path) -> tuple[list[dict], list[str]]:
    entries, warns = [], []
    for seed_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        m = SEED_RE.match(seed_dir.name)
        if not m:
            continue                       # 旧形式（Probe が 2 つのフォルダ）などは対象外
        seed = dt.datetime.strptime(m.group(1), "%y%m%d").date()
        probe = int(m.group(2))
        for div_dir in sorted(p for p in seed_dir.iterdir() if p.is_dir()):
            d = DIV_RE.match(div_dir.name)
            if not d:
                continue
            div = int(d.group(1))
            expected = seed + dt.timedelta(days=div)
            found = []
            for f in div_dir.iterdir():
                if not f.is_file():
                    continue
                n = IMG_RE.match(f.name)
                if n:
                    found.append((f, n))
                elif LOOKS_LIKE.match(f.name):
                    warns.append(f"{seed_dir.name}\\{div_dir.name}\\{f.name}：名前の形式が違うため表示していません")
            for f, n in found:
                date = dt.datetime.strptime(n.group(1), "%y%m%d").date()
                if int(n.group(2)) != probe:
                    warns.append(f"{seed_dir.name}\\{div_dir.name}\\{f.name}：Probe 番号がフォルダ名と違います")
                    continue
                if date != expected:
                    warns.append(f"{seed_dir.name}\\{div_dir.name}\\{f.name}：日付が播種日＋DIV（{expected:%y%m%d}）と違います")
                st = f.stat()
                entries.append({
                    "sample": f"{seed:%y%m%d}_{probe}",
                    "sampleLabel": f"{seed:%y%m%d}播種({probe})",
                    "seed": seed.isoformat(), "probe": probe, "div": div,
                    "date": date.isoformat(), "name": f.name,
                    "path": str(f), "mtime": st.st_mtime, "size": st.st_size,
                })
            if len(found) > 1:
                warns.append(f"{seed_dir.name}\\{div_dir.name}：合成画像が {len(found)} 枚あります")
    return entries, warns


def group(entries: list[dict]) -> list[dict]:
    samples: dict[str, dict] = {}
    for i, e in enumerate(entries):
        s = samples.setdefault(e["sample"], {"key": e["sample"], "label": e["sampleLabel"],
                                              "probe": e["probe"], "seed": e["seed"], "ids": []})
        s["ids"].append(i)
    for s in samples.values():
        s["ids"].sort(key=lambda i: entries[i]["div"])
    # 新しい播種から。同じ日の播種は番号の小さい順（文字として並べると 12 が 5 より前に来るため数で比べる）
    return sorted(samples.values(), key=lambda s: (s["seed"], -s["probe"]), reverse=True)


# ---------------------------------------------------------------- 表示用の縮小画像

# 縮小画像はツールを開いている間だけメモリに持ち、ディスクには保存しない（終了すると消える）
_cache: dict[tuple, bytes] = {}
_locks: dict[tuple, threading.Lock] = {}
_locks_guard = threading.Lock()


def medium_image(i: int, e: dict) -> bytes:
    i = (e["path"], e["mtime"], e["size"])          # 画像ごとに 1 つ（一覧を読み直しても取り違えない）
    if i in _cache:
        return _cache[i]
    with _locks_guard:
        lock = _locks.setdefault(i, threading.Lock())
    with lock:                                       # 同じ画像を同時に 2 回作らない
        if i not in _cache:
            with Image.open(e["path"]) as im:
                im.draft("RGB", (MEDIUM, MEDIUM))    # JPEG を縮小しながら読み込んで速くする
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((MEDIUM, MEDIUM), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=88)
            _cache[i] = buf.getvalue()
        return _cache[i]

# ---------------------------------------------------------------- 画面

# culture-viewer のロゴ（culture-stitcher のロゴの色を反転したもの。デスクトップのアイコンと同じ図案）
LOGO = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32' fill='none'>"
        "<circle cx='16' cy='16' r='15' fill='#f59e0b'/>"
        + "".join(f"<circle cx='{x}' cy='{y}' r='2' fill='#111113'/>"
                  for y in (10, 16, 22) for x in (10, 16, 22) if (x, y) != (22, 10))
        + "<circle cx='22' cy='10' r='2.6' fill='#fafafa'/></svg>")
FAVICON = "data:image/svg+xml," + LOGO.replace("#", "%23")


CSS = """
:root{--bg:#18181b;--panel:#111113;--line:rgba(255,255,255,.08);--ink:#fafafa;--quiet:#a1a1aa;
  --accent:#f59e0b;--accent2:#fbbf24;--split:50%}
html,body{height:100%;margin:0}
body{display:flex;flex-direction:column;background:var(--bg);color:var(--ink);
  font-family:"Segoe UI","Yu Gothic UI","Meiryo",sans-serif;font-size:13px;overflow:hidden}
header.top{flex:none;height:52px;display:flex;align-items:center;gap:10px;padding:0 16px;background:var(--panel);
  border-bottom:1px solid var(--line)}
header.top svg{width:30px;height:30px;flex:none}
header.top .name{font-weight:600;font-size:15px;letter-spacing:-.01em;white-space:nowrap}
header.top .name b{color:var(--accent);font-weight:600}
.tools{margin-left:auto;display:flex;gap:8px;align-items:center}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button,.btn{background:transparent;color:var(--quiet);border:0;padding:6px 12px;cursor:pointer;font:inherit}
.seg button+button{border-left:1px solid var(--line)}
.seg button.on{background:rgba(245,158,11,.16);color:var(--ink)}
.btn{border:1px solid var(--line);border-radius:8px;color:var(--ink)}
.btn:hover,.seg button:hover{color:var(--ink)}
.btn.warn{border-color:rgba(245,158,11,.5);color:var(--accent2)}
#ctrl{flex:none;display:grid;grid-template-columns:1fr auto 1fr;gap:16px;align-items:center;padding:8px 16px;
  background:var(--panel);border-bottom:1px solid var(--line)}
.pc{display:flex;gap:6px;align-items:center;min-width:0}
#cB{justify-content:flex-end}
.pc .tag{font-weight:700;color:var(--accent);margin-right:2px}
select{background:#1f1f23;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 6px;font:inherit;
  min-width:0;max-width:16em}
.pc{position:relative}
.pc .btn{padding:4px 10px}
.pc .btn:disabled{opacity:.25;cursor:default}
.pick{display:inline-flex;align-items:baseline;gap:6px;background:#1f1f23;border:1px solid var(--line);border-radius:8px;
  color:var(--ink);padding:5px 12px;font:inherit;font-size:14px;cursor:pointer;white-space:nowrap}
.pick:hover{border-color:rgba(255,255,255,.25)}
.pick b{font-weight:600}
.pick .q{color:var(--quiet);font-size:12px}
.pick .sep{width:1px;height:14px;background:rgba(255,255,255,.18);align-self:center;margin:0 4px}
.pick .caret{color:var(--quiet);font-size:11px;margin-left:2px}
.pop{display:none;position:absolute;top:calc(100% + 8px);z-index:30;width:max-content;max-width:min(860px,calc(100vw - 32px));
  max-height:calc(100vh - 130px);overflow:auto;background:#1f1f23;border:1px solid var(--line);border-radius:10px;
  padding:8px 16px 12px;box-shadow:0 12px 32px rgba(0,0,0,.5)}
#popA{left:0} #popB{right:0}
.pop.open{display:block}
.ph{display:flex;justify-content:space-between;gap:24px;color:var(--quiet);font-size:12px;padding:4px 0 8px;
  border-bottom:1px solid var(--line)}
.lg i{display:inline-block;vertical-align:middle;margin:0 4px 0 16px}
.lg .dot{width:6px;height:6px;border-radius:3px;background:var(--accent)}
.lg .box{width:10px;height:10px;border:1px dashed var(--quiet);border-radius:3px}
.row{display:flex;gap:16px;align-items:center;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.rl{width:88px;flex:none;display:flex;flex-direction:column}
.rl b{font-size:14px;font-weight:600}
.rl span{color:var(--quiet);font-size:11px}
.chips{display:flex;flex-wrap:wrap;gap:4px}
.chip{position:relative;display:flex;flex-direction:column;align-items:center;min-width:50px;padding:4px 6px;
  background:transparent;border:1px solid var(--line);border-radius:6px;color:var(--ink);font:inherit;cursor:pointer;line-height:1.25}
.chip b{font-size:12px;font-weight:600}
.chip small{font-size:10.5px;color:var(--quiet)}
.chip:hover{border-color:rgba(255,255,255,.3)}
.chip.cur{background:rgba(245,158,11,.2);border-color:var(--accent)}
.chip.oth{border:1px dashed var(--quiet)}
.chip.same::after{content:'';position:absolute;top:3px;right:3px;width:6px;height:6px;border-radius:3px;background:var(--accent)}
#mid{text-align:center;white-space:nowrap}
#rel{color:var(--quiet)}
#rel b{color:var(--ink);font-weight:600}
#stage{flex:1;position:relative;display:flex;gap:4px;background:#0b0b0c;min-height:0}
.vp{position:relative;flex:1;overflow:hidden;cursor:grab;touch-action:none}
.vp.drag{cursor:grabbing}
.vp img{position:absolute;left:0;top:0;transform-origin:0 0;user-select:none;-webkit-user-drag:none}
#stage.ov{gap:0}
#stage.ov .vp{position:absolute;inset:0}
#stage.ov #vB{clip-path:inset(0 0 0 var(--split))}
#divider{display:none;position:absolute;top:0;bottom:0;left:var(--split);width:0;border-left:2px solid var(--accent);
  z-index:5;cursor:ew-resize}
#divider::before{content:'';position:absolute;top:0;bottom:0;left:-12px;width:24px}
#divider::after{content:'';position:absolute;top:50%;left:-7px;width:12px;height:36px;border-radius:6px;
  background:var(--accent);transform:translateY(-50%)}
#stage.ov #divider{display:block}
#hint{color:var(--quiet);font-size:11px;margin-top:2px}
.busy{color:var(--accent2);font-size:12px;visibility:hidden;white-space:nowrap}
.busy.on{visibility:visible}
#warns{display:none;position:absolute;right:16px;top:56px;z-index:20;max-width:560px;background:#1f1f23;
  border:1px solid var(--line);border-radius:8px;padding:8px 16px;font-size:12px}
#warns.open{display:block}
#warns li{margin:4px 0}
#done{display:none;position:fixed;inset:0;background:var(--bg);align-items:center;justify-content:center;
  font-size:15px;z-index:50}
"""

JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const WHEEL = 0.0012, PINCH = 0.02, TOUCH = 2;     // 拡大の速さ: マウスホイール、タッチパッドのピンチ、タッチ画面のピンチ
const T = D.token, E = D.entries, S = D.samples;
const sidx = Object.fromEntries(S.map((s, i) => [s.key, i]));
const $ = id => document.getElementById(id);
const other = p => p === 'A' ? 'B' : 'A';
const P = {A: {id: null}, B: {id: null}};
const V = {A: {cx: .5, cy: .5, z: 1}, B: {cx: .5, cy: .5, z: 1}};
let sync = true, mode = 'side';

const ymd = iso => iso.replaceAll('-', '/');
const url = (id, q) => `/img/${id}?q=${q}&t=${T}`;

const md = iso => `${+iso.slice(5, 7)}/${+iso.slice(8, 10)}`;
const dish = e => `MEA-Probe ${e.probe}`;

function updateUI() {
  for (const p of ['A', 'B']) {
    const e = E[P[p].id], ids = S[sidx[e.sample]].ids, k = ids.indexOf(P[p].id);
    $('pick' + p).innerHTML = `<b>${dish(e)}</b><span class="q">${md(e.seed)}播種</span><span class="sep"></span>`
      + `<b>DIV${e.div}</b><span class="q">${md(e.date)}</span><span class="caret">▾</span>`;
    $('prev' + p).disabled = k <= 0;
    $('next' + p).disabled = k >= ids.length - 1;
    if ($('pop' + p).classList.contains('open')) renderPop(p);
  }
  const a = E[P.A.id], b = E[P.B.id];
  let t;
  if (a.sample === b.sample) {
    const d = Math.abs(b.div - a.div);
    t = d ? `同じMEA-Probe　DIV${a.div} と DIV${b.div}（<b>${d} 日差</b>）` : '同じ画像';
  } else if (a.date === b.date) {
    t = `<b>同じ日</b>に撮影（${md(a.date)}）　DIV${a.div} と DIV${b.div}`;
  } else {
    const d = Math.round(Math.abs(Date.parse(b.date) - Date.parse(a.date)) / 864e5);
    t = `別のMEA-Probe　撮影日の差 ${d} 日`;
  }
  $('rel').innerHTML = t;
}

function renderPop(p) {
  const q = other(p), cur = P[p].id, oid = P[q].id, od = E[oid].date;
  $('pop' + p).innerHTML = `<div class="ph"><span>${p} に表示するMEA-Probe と日付を選ぶ</span>`
    + `<span class="lg"><i class="dot"></i>${q} と同じ日<i class="box"></i>${q} で表示中</span></div>`
    + S.map(s => `<div class="row"><div class="rl"><b>MEA-Probe ${s.probe}</b><span>${md(s.seed)} 播種</span></div><div class="chips">`
      + s.ids.map(j => `<button class="chip${j === cur ? ' cur' : ''}${j === oid ? ' oth' : ''}${E[j].date === od && j !== oid ? ' same' : ''}"`
        + ` data-id="${j}" title="${E[j].date.replaceAll('-', '/')} 撮影"><b>DIV${E[j].div}</b><small>${md(E[j].date)}</small></button>`).join('')
      + '</div></div>').join('');
}

function setEntry(p, id) {
  P[p] = {id, iw: 0, ih: 0, full: false, wantFull: false};
  $('busy' + p).textContent = '読み込み中…';
  updateUI();
  load(p, 'm');
  const ids = S[sidx[E[id].sample]].ids, k = ids.indexOf(id);
  for (const j of [ids[k - 1], ids[k + 1]]) if (j != null) new Image().src = url(j, 'm');   // 前後を先読み
}

function load(p, q) {
  const id = P[p].id, vp = $('v' + p);
  if (q === 'm') $('busy' + p).classList.add('on');
  const im = new Image();
  im.onload = () => {
    if (P[p].id !== id) return;
    $('i' + p).src = im.src;
    Object.assign(P[p], {iw: im.naturalWidth, ih: im.naturalHeight, full: q === 'f'});
    $('busy' + p).classList.remove('on');
    render(p);
  };
  im.onerror = () => { $('busy' + p).textContent = '読み込めませんでした'; $('busy' + p).classList.add('on'); };
  im.src = url(id, q);
}

function geom(p) {
  const vp = $('v' + p), pp = P[p], st = V[p];
  const vw = vp.clientWidth, vh = vp.clientHeight, fit = Math.min(vw / pp.iw, vh / pp.ih), s = fit * st.z;
  return {vw, vh, fit, s, tx: vw / 2 - st.cx * pp.iw * s, ty: vh / 2 - st.cy * pp.ih * s};
}

function render(p) {
  const pp = P[p];
  if (!pp.iw) return;
  const g = geom(p), img = $('i' + p);
  img.style.width = pp.iw + 'px';
  img.style.height = pp.ih + 'px';
  img.style.transform = `translate(${g.tx}px,${g.ty}px) scale(${g.s})`;
  if (!pp.full && !pp.wantFull && g.s > 1.05) { pp.wantFull = true; load(p, 'f'); }   // 拡大したら原寸を読む
}

function clamp(st) {
  st.z = Math.min(40, Math.max(1, st.z));
  if (st.z === 1) { st.cx = st.cy = .5; return; }
  st.cx = Math.min(1, Math.max(0, st.cx));
  st.cy = Math.min(1, Math.max(0, st.cy));
}

function changed(p) {
  clamp(V[p]);
  if (sync || mode === 'ov') Object.assign(V[other(p)], V[p]);
  render('A'); render('B');
}

function point(p, ev) {
  const r = $('v' + p).getBoundingClientRect();
  return [ev.clientX - r.left, ev.clientY - r.top];
}

function bind(p) {
  const vp = $('v' + p);
  const zoomAt = (mx, my, f) => {
    const g = geom(p), st = V[p], pp = P[p];
    const u = (mx - g.tx) / (g.s * pp.iw), v = (my - g.ty) / (g.s * pp.ih);
    st.z = Math.min(40, Math.max(1, st.z * f));
    const s2 = g.fit * st.z;
    st.cx = u - (mx - g.vw / 2) / (s2 * pp.iw);
    st.cy = v - (my - g.vh / 2) / (s2 * pp.ih);
    changed(p);
  };
  vp.addEventListener('wheel', ev => {
    ev.preventDefault();
    if (!P[p].iw) return;
    const [mx, my] = point(p, ev);
    const dy = ev.deltaY * (ev.deltaMode === 1 ? 16 : 1);
    zoomAt(mx, my, Math.exp(-dy * (ev.ctrlKey ? PINCH : WHEEL)));   // ctrl 付き = タッチパッドのピンチ
  }, {passive: false});
  const pts = new Map();
  let last = null, pinch = null;
  vp.addEventListener('pointerdown', ev => {
    vp.setPointerCapture(ev.pointerId);
    pts.set(ev.pointerId, [ev.clientX, ev.clientY]);
    if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = Math.hypot(a[0] - b[0], a[1] - b[1]); last = null; }
    else { last = [ev.clientX, ev.clientY]; vp.classList.add('drag'); }
  });
  vp.addEventListener('pointermove', ev => {
    if (!pts.has(ev.pointerId) || !P[p].iw) return;
    pts.set(ev.pointerId, [ev.clientX, ev.clientY]);
    if (pts.size === 2 && pinch) {                                   // タッチ画面の 2 本指ピンチ
      const [a, b] = [...pts.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      const r = $('v' + p).getBoundingClientRect();
      zoomAt((a[0] + b[0]) / 2 - r.left, (a[1] + b[1]) / 2 - r.top, Math.pow(d / pinch, TOUCH));
      pinch = d;
      return;
    }
    if (!last) return;
    const g = geom(p), st = V[p];
    st.cx -= (ev.clientX - last[0]) / (g.s * P[p].iw);
    st.cy -= (ev.clientY - last[1]) / (g.s * P[p].ih);
    last = [ev.clientX, ev.clientY];
    changed(p);
  });
  const up = ev => { pts.delete(ev.pointerId); pinch = null; last = null; vp.classList.remove('drag'); };
  vp.addEventListener('pointerup', up);
  vp.addEventListener('pointercancel', up);
  vp.addEventListener('dblclick', () => { Object.assign(V[p], {cx: .5, cy: .5, z: 1}); changed(p); });

  $('pick' + p).addEventListener('click', ev => {
    ev.stopPropagation();
    $('pop' + other(p)).classList.remove('open');
    if ($('pop' + p).classList.toggle('open')) renderPop(p);
  });
  $('pop' + p).addEventListener('click', ev => {
    ev.stopPropagation();
    const c = ev.target.closest('.chip');
    if (!c) return;
    $('pop' + p).classList.remove('open');
    setEntry(p, +c.dataset.id);
  });
  $('prev' + p).addEventListener('click', () => step(p, -1));
  $('next' + p).addEventListener('click', () => step(p, 1));
}

function step(p, dir) {
  const ids = S[sidx[E[P[p].id].sample]].ids, j = ids[ids.indexOf(P[p].id) + dir];
  if (j != null) setEntry(p, j);
}

function setMode(m) {
  mode = m;
  $('stage').classList.toggle('ov', m === 'ov');
  $('mSide').classList.toggle('on', m === 'side');
  $('mOv').classList.toggle('on', m === 'ov');
  $('sync').disabled = m === 'ov';
  if (m === 'ov') Object.assign(V.B, V.A);
  requestAnimationFrame(() => { render('A'); render('B'); });
}

function init() {
  // 最初は、いちばん新しい撮影日の画像（A）と、同じサンプルの 1 つ前の日（B）を並べる
  const a = E.reduce((b, e, i) => (e.date > E[b].date ? i : b), 0);
  const ids = S[sidx[E[a].sample]].ids, k = ids.indexOf(a);
  let b = ids[k - 1] ?? ids[k + 1];
  if (b == null) b = E.findIndex((e, i) => i !== a && e.date === E[a].date);
  if (b < 0 || b == null) b = E.findIndex((e, i) => i !== a);
  if (b < 0) b = a;
  P.A.id = a; P.B.id = b;
  setEntry('A', a); setEntry('B', b);
  bind('A'); bind('B');
  $('mSide').onclick = () => setMode('side');
  $('mOv').onclick = () => setMode('ov');
  $('sync').onclick = () => { sync = !sync; $('sync').classList.toggle('on', sync); if (sync) changed('A'); };
  $('swap').onclick = () => { const a = P.A.id, b = P.B.id; setEntry('A', b); setEntry('B', a); };
  $('fit').onclick = () => { for (const p of ['A', 'B']) Object.assign(V[p], {cx: .5, cy: .5, z: 1}); render('A'); render('B'); };
  if ($('warnBtn')) $('warnBtn').onclick = () => $('warns').classList.toggle('open');
  const dv = $('divider'), stage = $('stage');
  dv.addEventListener('pointerdown', ev => {
    dv.setPointerCapture(ev.pointerId);
    const move = e => {
      const r = stage.getBoundingClientRect();
      stage.style.setProperty('--split', Math.min(100, Math.max(0, (e.clientX - r.left) / r.width * 100)) + '%');
    };
    dv.addEventListener('pointermove', move);
    dv.addEventListener('pointerup', () => dv.removeEventListener('pointermove', move), {once: true});
  });
  addEventListener('resize', () => { render('A'); render('B'); });
  const closePops = () => { $('popA').classList.remove('open'); $('popB').classList.remove('open'); };
  document.addEventListener('click', closePops);
  $('stage').addEventListener('pointerdown', closePops);
  addEventListener('keydown', ev => {
    if (ev.key === 'Escape') { $('popA').classList.remove('open'); $('popB').classList.remove('open'); return; }
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') { step(ev.shiftKey ? 'A' : 'B', ev.key === 'ArrowRight' ? 1 : -1); ev.preventDefault(); }
  });
  const post = path => fetch(path, {method: 'POST', headers: {'X-Token': T}}).catch(() => {});
  post('/ping'); const beat = setInterval(() => post('/ping'), 5000);
  $('quit').onclick = async () => {
    clearInterval(beat); await post('/quit');
    $('done').style.display = 'flex'; window.close();
  };
}
init();
"""


def render_page(entries: list[dict], samples: list[dict], warns: list[str], token: str,
                brand: str | None = None) -> str:
    pub = [{k: e[k] for k in ("sample", "sampleLabel", "probe", "seed", "div", "date", "name")} for e in entries]
    data = json.dumps({"token": token, "entries": pub, "samples": samples}, ensure_ascii=False)
    data = data.replace("</", "<\\/")
    warn_btn = (f'<button class="btn warn" id="warnBtn">注意 {len(warns)} 件</button>' if warns else "")
    tools = ('<div class="tools">'
             '<div class="seg"><button id="mSide" class="on">並べる</button><button id="mOv">重ねる</button></div>'
             '<div class="seg"><button id="sync" class="on" title="片方を拡大・移動すると、もう片方も同じ位置に動く">拡大を連動</button></div>'
             '<button class="btn" id="fit" title="ダブルクリックでも全体表示に戻る">全体表示</button>'
             '<button class="btn" id="swap">左右を入れ替え</button>'
             f'{warn_btn}<button class="btn" id="quit">終了</button></div>')
    brand = brand or (LOGO.replace("<svg ", "<svg aria-hidden='true' ")
                      + '<span class="name">culture-<b>viewer</b></span>')
    header = f'<header class="top">{brand}{tools}</header>'
    panel = lambda p: (f'<div class="pc" id="c{p}"><span class="tag">{p}</span>'
                       f'<button class="btn" id="prev{p}" title="前の日">◀</button>'
                       f'<button class="pick" id="pick{p}" title="MEA-Probe と日付を選ぶ"></button>'
                       f'<button class="btn" id="next{p}" title="次の日">▶</button><span class="busy" id="busy{p}">読み込み中…</span>'
                       f'<div class="pop" id="pop{p}"></div></div>')
    vp = lambda p: f'<div class="vp" id="v{p}"><img id="i{p}" alt=""></div>'
    warn_list = "".join(f"<li>{html.escape(w)}</li>" for w in warns)
    return (f'<!doctype html><html lang="ja"><meta charset="utf-8"><title>見比べ | culture-viewer</title>'
            f'<link rel="icon" type="image/svg+xml" href="{FAVICON}"><style>{CSS}</style><body>'
            f'{header}<div id="warns"><ul>{warn_list}</ul></div>'
            f'<div id="ctrl">{panel("A")}<div id="mid"><div id="rel"></div><div id="hint">ホイール・ピンチで拡大　ドラッグで移動　ダブルクリックで全体　←→ で B の日付を送る（Shift で A）</div></div>{panel("B")}</div>'
            f'<div id="stage">{vp("A")}{vp("B")}<div id="divider"></div>'
            f'<div id="done">終了しました。このタブは閉じて構いません。</div>'
            f'<script id="data" type="application/json">{data}</script><script>{JS}</script></body></html>')


# ---------------------------------------------------------------- 配信

def serve(entries: list[dict], samples: list[dict], warns: list[str], open_browser: bool) -> None:
    token = secrets.token_urlsafe(16)
    page = render_page(entries, samples, warns, token).encode("utf-8")
    last_ping = [time.monotonic() + 60]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self) -> bool:
            q = parse_qs(urlparse(self.path).query).get("t", [None])[0]
            return secrets.compare_digest(self.headers.get("X-Token") or q or "", token)

        def _send(self, code, body: bytes, ctype: str, cache: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path):
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)

        def do_GET(self):
            if not self._ok():
                return self._send(403, b"forbidden", "text/plain")
            u = urlparse(self.path)
            parts = u.path.strip("/").split("/")
            if parts == [""]:
                return self._send(200, page, "text/html; charset=utf-8")
            if len(parts) == 2 and parts[0] == "img" and parts[1].isdigit() and int(parts[1]) < len(entries):
                e = entries[int(parts[1])]
                q = parse_qs(u.query).get("q", ["m"])[0]
                try:
                    if q == "f":
                        return self._send_file(Path(e["path"]))
                    return self._send(200, medium_image(int(parts[1]), e), "image/jpeg", cache=True)
                except (ConnectionError, OSError) as ex:
                    if isinstance(ex, ConnectionError):
                        return                           # 画面側が読み込みを取りやめた
                    print(f"  読み込めませんでした: {e['path']}（{ex}）")
                    return self._send(500, b"error", "text/plain")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._ok():
                return self._send(403, b"forbidden", "text/plain")
            path = urlparse(self.path).path
            if path == "/ping":
                last_ping[0] = time.monotonic()
                return self._send(200, b"{}", "application/json")
            if path == "/quit":
                self._send(200, b"{}", "application/json")
                threading.Thread(target=server.shutdown, daemon=True).start()
                return
            self._send(404, b"not found", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    print(f"見比べ画面を開きました（合成画像 {len(entries)} 枚、サンプル {len(samples)} 件）。")
    print("画面の「終了」を押すか画面を閉じると、この黒い画面も自動で閉じます。")
    print(f"画面が開かない場合はこのアドレスを開いてください: {url}")
    if open_browser:
        webbrowser.open(url)

        def watchdog():
            while True:
                time.sleep(2)
                if time.monotonic() - last_ping[0] > cs.PAGE_TIMEOUT:
                    print("見比べ画面が閉じられたので終了します。")
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
    print("合成画像を探しています…")
    entries, warns = scan(root)
    for w in warns:
        print(f"  注意: {w}")
    if not entries:
        print("見比べられる合成画像が見つかりませんでした。")
        sys.exit(3)
    serve(entries, group(entries), warns, "--no-open" not in sys.argv)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
