"""撮影モード：EOS Utility の保存先を 1 か所に固定したまま、届いた写真を MEA-Probe ごとの DIV フォルダへ振り分ける

しくみ
  ・EOS Utility の保存先は「取り込み元フォルダ」1 か所に固定しておく。
  ・culture-tools の「撮影」画面で、今日撮る MEA-Probe を撮る順に選んで「撮影を始める」を押す。
  ・撮影モードのあいだに取り込み元フォルダへ届いた写真（IMG_数字.JPG）だけを、
    いま撮っている MEA-Probe の DIV フォルダへ移す。6 枚そろったら「次の MEA-Probe へ」を押して切り替える。

培地写真以外で EOS Utility を使うとき
  ・撮影モードでないときは、取り込み元フォルダの中身には一切触れない。
  ・撮影モードを始める前からあった写真にも触れない（始めたあとに届いた写真だけが対象）。
  ・最後の MEA-Probe を終えたとき、「撮影を終える」を押したとき、写真が 30 分届かなかったとき、
    culture-tools を閉じたときに、撮影モードは自動で終わる。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "stitch"))
import culture_stitch as cs                         # noqa: E402

RAW_RE = cs.RAW_PATTERN                              # IMG_数字.JPG
EXTRA_RE = re.compile(r"^\d{6}_\d+_extra\d+\.jpe?g$", re.IGNORECASE)   # 7 枚目以降（拡大写真など）：撮影日_番号_extra連番.JPG
IDLE_LIMIT = 30 * 60                                 # 写真がこの秒数届かなければ撮影モードを終える
SCAN_DEPTH = 2                                       # 取り込み元フォルダの何段下まで見るか（日付ごとのフォルダに保存する設定に対応）
POLL = 1.0


# ---------------------------------------------------------------- 設定（取り込み元フォルダ）

def load_inbox() -> Path | None:
    try:
        p = json.loads(cs.CONFIG.read_text(encoding="utf-8")).get("inbox")
    except (OSError, ValueError, AttributeError):
        return None
    return Path(p) if p else None


def save_inbox(p: Path) -> None:
    try:
        cfg = json.loads(cs.CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg["inbox"] = str(p)
    cs.CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


PICK_SCRIPT = r"""
import sys, tkinter as tk
from tkinter import filedialog
w = tk.Tk(); w.withdraw(); w.attributes('-topmost', True)
p = filedialog.askdirectory(parent=w, mustexist=True, title='EOS Utility の保存先（取り込み元フォルダ）を選んでください')
open(sys.argv[1], 'w', encoding='utf-8').write(p or '')
"""


def pick_folder() -> Path | None:
    """フォルダを選ぶ画面を出す。画面の配信を止めないよう、別のプロセスで開く。"""
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "picked.txt"
        subprocess.run([sys.executable, "-c", PICK_SCRIPT, str(out)], check=False)
        try:
            s = out.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return Path(s) if s else None


# ---------------------------------------------------------------- 撮影モード

def today_targets(root: Path, scan: list[dict], day: dt.date | None = None) -> list[dict]:
    """今日の DIV フォルダが既にある MEA-Probe（フォルダ作成で作ったもの）"""
    day = day or dt.date.today()
    out = []
    for s in scan:
        div = (day - dt.date.fromisoformat(s["seed"])).days
        if div >= 0 and div in s["divs"]:
            out.append({"key": s["key"], "probe": s["probe"], "div": div,
                        "path": str(root / s["key"] / f"DIV{div}"), "seed": s["seed"]})
    return out


def raw_files(folder: Path) -> list[Path]:
    """フォルダにある写真（IMG_数字.JPG）を、撮った順に"""
    try:
        return sorted((f for f in folder.iterdir() if f.is_file() and RAW_RE.match(f.name)), key=lambda f: f.stat().st_mtime)
    except OSError:
        return []


def extra_files(folder: Path) -> list[Path]:
    try:
        return sorted((f for f in folder.iterdir() if f.is_file() and EXTRA_RE.match(f.name)), key=lambda f: f.stat().st_mtime)
    except OSError:
        return []


def extra_name(folder: Path, probe: int, suffix: str) -> str:
    """追加の写真の名前（例：261109_5_extra1.JPG）。IMG_ で始まらないので合成の対象にならない。"""
    k = 1
    while any((folder / f"{dt.date.today():%y%m%d}_{probe}_extra{k}{ext}").exists() for ext in (suffix, ".JPG", ".jpg")):
        k += 1
    return f"{dt.date.today():%y%m%d}_{probe}_extra{k}{suffix}"


def raw_count(folder: Path) -> int:
    return len(raw_files(folder))


def walk(inbox: Path, depth: int = SCAN_DEPTH):
    """取り込み元フォルダの中の写真（IMG_数字.JPG）"""
    try:
        for p in inbox.iterdir():
            if p.is_file() and RAW_RE.match(p.name):
                yield p
            elif p.is_dir() and depth > 0:
                yield from walk(p, depth - 1)
    except OSError:
        return


def companions(p: Path) -> list[Path]:
    """同時に保存される RAW など、同じ名前で拡張子だけ違うファイル"""
    try:
        return [q for q in p.parent.iterdir() if q.is_file() and q.stem == p.stem and q != p]
    except OSError:
        return []


class Session:
    lock = threading.Lock()
    active = False
    inbox: Path | None = None
    queue: list[dict] = []
    idx = 0
    switches: list[float] = []     # 各 MEA-Probe の撮影を始めた時刻（届いた時刻で、どの MEA-Probe の写真かを決める）
    last = 0.0                     # 最後に写真が届いた時刻（何もしない時間が長ければ終える）
    baseline: set[str] = set()     # 始める前からあった写真と、戻した写真（触れない）
    seen: dict[str, float] = {}    # 写真に初めて気づいた時刻
    sizes: dict[str, int] = {}     # 書き込み途中のファイルを避けるため、前回見たときの大きさ
    warned: set[str] = set()
    history: list[dict] = []       # 移した写真（「1 枚戻す」で使う）
    notes: list[str] = []
    finished = False
    ended = ""
    cutoff = 0.0                   # 終えた時刻。これより前に届いた写真だけ、終えたあとも数秒は移す
    drain_until = 0.0
    gen = 0                        # 撮影モードを始め直したとき、前の見張りを止めるための番号

    @classmethod
    def start(cls, inbox: Path, queue: list[dict]) -> str:
        with cls.lock:
            if cls.active:
                return "既に撮影モード中です。別のタブで撮影中でないか確認してください"
            now = time.time()
            cls.active, cls.finished, cls.ended = True, False, ""
            cls.inbox, cls.queue, cls.idx, cls.switches = inbox, queue, 0, [now]
            cls.last = now
            cls.baseline = {str(p) for p in walk(inbox)}
            cls.seen, cls.sizes, cls.warned, cls.history, cls.notes = {}, {}, set(), [], []
            cls.gen += 1
            gen = cls.gen
        print(f"撮影モードを始めました: {inbox} → " + "、".join(q["key"] for q in queue))
        threading.Thread(target=cls.watch, args=(gen,), daemon=True).start()
        return ""

    @classmethod
    def _close(cls, reason: str, finished: bool = False) -> None:
        """撮影モードを終える（lock の中で呼ぶ）。終える直前に届いて書き込み中だった写真は、数秒のうちに移しきる。"""
        now = time.time()
        cls.active, cls.finished, cls.ended = False, finished, reason
        cls.cutoff, cls.drain_until = now, now + 5
        print(f"撮影モードを終えました: {reason}")

    @classmethod
    def stop(cls, reason: str) -> None:
        with cls.lock:
            if cls.active:
                cls._take_new()
                cls._close(reason)

    @classmethod
    def note(cls, msg: str) -> None:
        cls.notes = (cls.notes + [f"{time.strftime('%H:%M:%S')}  {msg}"])[-8:]
        print(msg)

    @classmethod
    def watch(cls, gen: int) -> None:
        while True:
            time.sleep(POLL)
            with cls.lock:
                if gen != cls.gen:
                    return
                if not cls.active:
                    if time.time() > cls.drain_until:
                        return
                elif time.time() - cls.last > IDLE_LIMIT:
                    cls._close("写真が 30 分届かなかったので、撮影モードを終えました")
                    continue
                cls._take_new()

    @classmethod
    def _take_new(cls) -> None:
        """撮影モード中に届いた写真を、届いたときに撮っていた MEA-Probe の DIV フォルダへ移す（lock の中で呼ぶ）"""
        now = time.time()
        for p in walk(cls.inbox):
            k = str(p)
            if k in cls.baseline:
                continue
            seen = cls.seen.setdefault(k, now)
            if not cls.active and seen > cls.cutoff:
                continue                                    # 撮影モードを終えたあとに届いた写真には触れない
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size == 0 or cls.sizes.get(k) != size:      # まだ書き込み中かもしれないので、次に見たときに移す
                cls.sizes[k] = size
                continue
            # 「次へ」を押す直前に届いた写真は、押す前の MEA-Probe のもの
            ti = max(i for i, t in enumerate(cls.switches) if t <= seen) if seen >= cls.switches[0] else 0
            target = Path(cls.queue[ti]["path"])
            if not target.is_dir():
                if k not in cls.warned:
                    cls.warned.add(k)
                    cls.note(f"{p.name}：移し先のフォルダ {target.parent.name}\\{target.name} が見つからないため、取り込み元に残しています")
                continue
            name = p.name
            if raw_count(target) >= cs.N_IMAGES:            # 6 枚そろったあとの写真は、合成に使わない追加の写真として名前を変える
                name = extra_name(target, cls.queue[ti]["probe"], p.suffix)
            dst = target / name
            if dst.exists():
                cls.baseline.add(k)
                cls.note(f"{p.name} は移し先に同じ名前のファイルがあるため、取り込み元に残しました")
                continue
            try:
                shutil.move(str(p), str(dst))
            except OSError:
                continue                                    # EOS Utility がまだ使っている。次に見たときに移す
            extra = []
            for c in companions(p):
                cname = Path(name).stem + c.suffix
                try:
                    shutil.move(str(c), str(target / cname))
                    extra.append([c.name, cname])
                except OSError:
                    pass
            if name != p.name:
                cls.note(f"{p.name} は 7 枚目以降なので、追加の写真 {name} として保存しました（合成には使いません）")
            cls.sizes.pop(k, None)
            cls.history.append({"src": k, "dst": str(dst), "extra": extra, "target": ti})
            cls.last = time.time()

    @classmethod
    def next(cls) -> None:
        with cls.lock:
            if not cls.active:
                return
            cls._take_new()
            if cls.idx + 1 >= len(cls.queue):
                cls._close("撮影が完了しました", finished=True)
                return
            cls.idx += 1
            cls.switches.append(time.time())

    @classmethod
    def undo(cls) -> str:
        """直前に移した 1 枚を取り込み元へ戻す（撮り直したいとき）"""
        with cls.lock:
            if not cls.history:
                return "戻せる写真がありません"
            h = cls.history[-1]
            if h["target"] != cls.idx:
                return "前の MEA-Probe の写真は戻せません"
            src, dst = Path(h["src"]), Path(h["dst"])
            try:
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                for orig, moved in h["extra"]:
                    shutil.move(str(dst.parent / moved), str(src.parent / orig))
            except OSError as e:
                return f"戻せませんでした（{e}）"
            cls.history.pop()
            cls.baseline.add(str(src))                     # 戻した写真は、もう一度取り込まない
            cls.note(f"{src.name} を取り込み元へ戻しました")
            return ""

    @classmethod
    def status(cls) -> dict:
        with cls.lock:
            q = []
            for i, t in enumerate(cls.queue):
                q.append({**t, "count": raw_count(Path(t["path"])), "label": f"MEA-Probe {t['probe']}"})
            # いまの MEA-Probe のフォルダにある写真すべて（中断のあとに再開したときは、前回の分も含む）
            cur = Path(cls.queue[cls.idx]["path"]) if cls.queue else None
            shots = [f.name for f in raw_files(cur)] if cur else []
            extras = [f.name for f in extra_files(cur)] if cur else []
            can_undo = bool(cls.history) and cls.history[-1]["target"] == cls.idx
            return {"active": cls.active, "finished": cls.finished, "ended": cls.ended, "idx": cls.idx,
                    "queue": q, "shots": shots, "extras": extras, "undo": can_undo, "notes": cls.notes, "need": cs.N_IMAGES,
                    "inbox": str(cls.inbox) if cls.inbox else ""}


_thumbs: dict[str, bytes] = {}


def thumb(name: str) -> bytes | None:
    """いまの MEA-Probe のフォルダにある写真の縮小画像"""
    if not Session.queue or not (RAW_RE.match(name) or EXTRA_RE.match(name)) or "/" in name or "\\" in name:
        return None
    p = Path(Session.queue[Session.idx]["path"]) / name
    if not p.is_file():
        return None
    if str(p) not in _thumbs:
        from PIL import Image, ImageOps
        try:
            im = ImageOps.exif_transpose(Image.open(p))
        except OSError:
            return None
        im.thumbnail((360, 360))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=80)
        _thumbs[str(p)] = buf.getvalue()
    return _thumbs[str(p)]


# ---------------------------------------------------------------- 画面

CSS = """
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;margin:0 0 16px}
.box h2{margin:0 0 8px}
.path{font-family:Consolas,monospace;font-size:13px;word-break:break-all}
.hint{color:var(--quiet);font-size:13px;line-height:1.7;margin:6px 0 0}
.btn{background:transparent;color:var(--ink);border:1px solid rgba(255,255,255,.15);border-radius:8px;padding:6px 14px;font:inherit;cursor:pointer}
.btn:hover{border-color:rgba(255,255,255,.3)} .btn:disabled{opacity:.4;cursor:default}
.pri{background:var(--accent);color:#111113;border:0;border-radius:8px;padding:10px 22px;font:inherit;font-weight:600;cursor:pointer}
.pri:disabled{background:#3f3f46;color:#a1a1aa;cursor:default}
.pick{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:8px;cursor:pointer;user-select:none}
.pick:hover{background:rgba(255,255,255,.04)}
.pick .ord{width:26px;height:26px;border-radius:50%;border:1px solid rgba(255,255,255,.2);display:flex;align-items:center;
  justify-content:center;font-weight:700;font-size:13px;flex:none}
.pick.on .ord{background:var(--accent);border-color:var(--accent);color:#111113}
.pick b{font-size:15px;min-width:120px;white-space:nowrap} .pick span.q{color:var(--quiet)}
#now{border-left:4px solid var(--accent)}
#now .head{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
#now .big{font-size:26px;font-weight:700} #now .cnt{font-size:22px;font-weight:700;color:var(--accent)}
#now .cnt.full{color:var(--good)} #now .cnt.over{color:var(--bad);font-size:16px}
#shots{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin:14px 0 4px}
#shots img{width:100%;aspect-ratio:3/2;object-fit:cover;border-radius:6px;border:1px solid var(--line);display:block}
#extras .xl{color:var(--quiet);font-size:12px;margin:12px 0 6px}
#extras .xg{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}
#extras figure{margin:0} #extras img{width:100%;aspect-ratio:3/2;object-fit:cover;border-radius:6px;border:1px solid rgba(245,158,11,.5);display:block}
#extras figcaption{font-size:11px;color:var(--quiet);margin-top:3px;word-break:break-all}
#shots .ph{aspect-ratio:3/2;border-radius:6px;border:1px dashed rgba(255,255,255,.15)}
.row{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap}
.glow{box-shadow:0 0 0 3px rgba(245,158,11,.35)}
ol.q{margin:4px 0 0;padding-left:22px;line-height:2} ol.q li.cur{color:var(--accent);font-weight:600} ol.q li.done{color:var(--quiet)}
#notes{color:var(--quiet);font-size:12px;margin-top:8px;white-space:pre-line}
"""

JS = r"""
const T = document.body.dataset.token, $ = id => document.getElementById(id);
const post = (p, b) => fetch(p, {method: 'POST', headers: {'X-Token': T, 'Content-Type': 'application/json'}, body: JSON.stringify(b || {})}).then(r => r.json());
const order = [];
document.querySelectorAll('.pick').forEach(el => el.onclick = () => {
  const k = el.dataset.key, i = order.indexOf(k);
  if (i >= 0) order.splice(i, 1); else order.push(k);
  document.querySelectorAll('.pick').forEach(e => {
    const j = order.indexOf(e.dataset.key);
    e.classList.toggle('on', j >= 0); e.querySelector('.ord').textContent = j >= 0 ? j + 1 : '';
  });
  if ($('start')) { $('start').disabled = !order.length || $('start').dataset.noinbox === '1';
    $('start').textContent = order.length ? `撮影を始める（${order.length} 個）` : '撮る MEA-Probe を順に選んでください'; }
});
if ($('inbox')) $('inbox').onclick = async () => {
  $('inbox').disabled = true; $('inbox').textContent = 'フォルダを選んでいます…';
  const r = await post('/capture/inbox'); if (r.error) alert(r.error); location.reload();
};
if ($('start')) $('start').onclick = async () => { const r = await post('/capture/start', {keys: order}); if (r.error) alert(r.error); location.reload(); };
let seen = '';
async function poll() {
  let s; try { s = await (await fetch('/capture/status?t=' + T)).json(); } catch (e) { return; }
  if (!s.active) { location.reload(); return; }
  const cur = s.queue[s.idx], n = cur.count, need = s.need;
  $('cur').textContent = `${cur.label}（DIV${cur.div}・${+cur.seed.slice(5, 7)}/${+cur.seed.slice(8, 10)} 播種）`;
  $('cnt').textContent = `${n} / ${need} 枚` + (s.extras.length ? `　＋ 追加 ${s.extras.length} 枚` : '');
  $('cnt').classList.toggle('full', n === need); $('cnt').classList.toggle('over', n > need);
  const ids = s.idx + ':' + s.shots.join(',') + '|' + s.extras.join(',');
  if (ids !== seen) {
    seen = ids;
    let h = s.shots.map(x => `<img src="/capture/thumb/${encodeURIComponent(x)}?t=${T}" title="${x}">`).join('');
    for (let i = s.shots.length; i < need; i++) h += '<div class="ph"></div>';
    $('shots').innerHTML = h;
    $('extras').innerHTML = s.extras.length ? '<div class="xl">追加の写真（合成には使いません）</div><div class="xg">'
      + s.extras.map(x => `<figure><img src="/capture/thumb/${encodeURIComponent(x)}?t=${T}"><figcaption>${x}</figcaption></figure>`).join('') + '</div>' : '';
  }
  $('next').textContent = s.idx + 1 < s.queue.length ? `次の MEA-Probe へ（${s.queue[s.idx + 1].label}）` : "撮影を完了する";
  $('next').className = (n === need ? 'pri glow' : 'btn');
  $('undo').disabled = !s.undo;
  // 最後の MEA-Probe を撮り終えたら「撮影を完了する」だけにする（中断は、撮り終える前にやめるときのもの）
  $('end').style.display = (s.idx + 1 >= s.queue.length && n === need) ? 'none' : '';
  $('q').innerHTML = s.queue.map((x, i) => `<li class="${i === s.idx ? 'cur' : i < s.idx ? 'done' : ''}">${x.label}（DIV${x.div}・${+x.seed.slice(5, 7)}/${+x.seed.slice(8, 10)} 播種）　${x.count} 枚</li>`).join('');
  $('notes').textContent = s.notes.join('\n');
  setTimeout(poll, 1000);
}
if ($('next')) {
  $('next').onclick = async () => {
    const s = await (await fetch('/capture/status?t=' + T)).json(), cur = s.queue[s.idx];
    if (cur.count !== s.need && !confirm(`${cur.label} の写真は ${cur.count} 枚です（6 枚ではありません）。このまま次へ進みますか？`)) return;
    await post('/capture/next'); poll();
  };
  $('undo').onclick = async () => { const r = await post('/capture/undo'); if (r.error) alert(r.error); seen = ''; };
  $('end').onclick = async () => { if (confirm('撮影モードを途中で中断しますか？ ここまでに移した写真はそのまま残ります。これ以降に届いた写真は、取り込み元フォルダに残ります。')) { await post('/capture/end'); location.reload(); } };
  poll();
}
"""


def stitched(t: dict) -> bool:
    done = re.compile(rf"^\d{{6}}_{t['probe']}\.jpe?g$", re.IGNORECASE)
    try:
        return any(done.match(f.name) for f in Path(t["path"]).iterdir())
    except OSError:
        return False


def shot_label(n: int, done: bool = False) -> str:
    """選ぶ一覧に、そのフォルダに既にある写真の枚数を出す（中断したあとの再開や、撮り終えたものの見分けに使う）"""
    if done:
        return '<span style="margin-left:auto;color:var(--quiet)">合成済み</span>'
    if n == 0:
        return ""
    color = "var(--good)" if n == cs.N_IMAGES else "var(--accent)"
    word = "撮影済み" if n == cs.N_IMAGES else "撮影途中"
    return f'<span style="margin-left:auto;color:{color}">{word}　{n} / {cs.N_IMAGES} 枚</span>'


def render_page(root: Path, scan: list[dict], token: str, brand: str, favicon: str) -> str:
    st = Session.status()
    inbox = load_inbox()
    if st["active"]:
        body = ('<h1>撮影中</h1>'
                '<section class="box" id="now"><div class="head"><span class="big" id="cur"></span><span class="cnt" id="cnt"></span></div>'
                '<div id="shots"></div><div id="extras"></div>'
                '<div class="row"><button class="btn" id="next"></button><button class="btn" id="undo">1 枚戻す（撮り直し）</button>'
                '<span style="flex:1"></span><button class="btn" id="end" title="残りの MEA-Probe を撮らずに、撮影モードを途中で終える">途中で中断する</button></div>'
                '<div id="notes"></div></section>'
                '<section class="box"><h2>撮る順番</h2><ol class="q" id="q"></ol>'
                f'<p class="hint">取り込み元：<span class="path">{st["inbox"]}</span><br>'
                '撮影モードを始めてから届いた写真だけを、いまの MEA-Probe のフォルダへ移しています。</p></section>')
        return page(body, token, brand, favicon)

    done = ""
    if st["ended"]:
        link = f'<a href="/stitch?t={token}" style="color:var(--accent)">「合成」へ進む</a>' if st["finished"] else ""
        done = f'<section class="box" style="border-left:4px solid var(--good)"><h2>{st["ended"]}</h2><p class="hint">{link}</p></section>'

    ok_inbox = inbox is not None and inbox.is_dir()
    inbox_html = (f'<span class="path">{inbox}</span>' if ok_inbox else
                  '<span style="color:var(--bad)">まだ選ばれていません</span>' if inbox is None else
                  f'<span style="color:var(--bad)">見つかりません：</span><span class="path">{inbox}</span>')
    targets = today_targets(root, scan)
    picks = "".join(
        f'<div class="pick" data-key="{t["key"]}"><span class="ord"></span><b>MEA-Probe {t["probe"]}</b>'
        f'<span class="q">DIV{t["div"]}　{dt.date.fromisoformat(t["seed"]):%m/%d} 播種</span>'
        f'{shot_label(raw_count(Path(t["path"])), stitched(t))}</div>' for t in targets)
    if not targets:
        picks = (f'<p class="hint">今日の DIV フォルダがまだありません。先に <a href="/folders?t={token}" '
                 'style="color:var(--accent)">「フォルダ作成」</a> で作ってください。</p>')
    body = (f'<h1>撮影</h1>{done}'
            '<section class="box"><h2>取り込み元フォルダ（EOS Utility の保存先）</h2>'
            f'<div class="row" style="margin-top:0">{inbox_html}<button class="btn" id="inbox">変更</button></div>'
            '<p class="hint">EOS Utility の保存先を、このフォルダに固定しておきます。撮影中に保存先を切り替える必要はありません。<br>'
            '撮影モードを始めてから届いた写真（IMG_数字.JPG）だけを移します。それ以外のとき、または始める前からあった写真には触れないので、'
            '培地写真以外の撮影にもそのまま使えます。<br>Google ドライブではなく、パソコン内のフォルダを選んでください。</p></section>'
            '<section class="box"><h2>今日撮る MEA-Probe（撮る順にクリック）</h2>'
            f'{picks}</section>')
    bar = (f'<div id="bar"><button class="pri" id="start" disabled data-noinbox="{0 if ok_inbox else 1}">'
           f'{"撮る MEA-Probe を順に選んでください" if ok_inbox else "先に取り込み元フォルダを選んでください"}</button></div>')
    return page(body + bar, token, brand, favicon)


def page(body: str, token: str, brand: str, favicon: str) -> str:
    return (f'<!doctype html><html lang="ja"><meta charset="utf-8"><title>撮影 | culture-tools</title>'
            f'<link rel="icon" type="image/svg+xml" href="{favicon}"><style>{cs.PAGE_CSS}{CSS}</style>'
            f'<body data-token="{token}"><header class="top">{brand}</header><main>{body}</main>'
            f'<script>{JS}</script></body></html>')


def start(root: Path, scan: list[dict], keys: list[str]) -> str:
    """撮影モードを始める。問題があれば、その理由を返す。"""
    inbox = load_inbox()
    if inbox is None or not inbox.is_dir():
        return "取り込み元フォルダを選んでください"
    if root.resolve() == inbox.resolve() or root.resolve() in inbox.resolve().parents:
        return "取り込み元フォルダには、写真のフォルダ（00_Culture_Media）の外のフォルダを選んでください"
    by_key = {t["key"]: t for t in today_targets(root, scan)}
    queue = [by_key[k] for k in keys if k in by_key]
    if not queue:
        return "撮る MEA-Probe を選んでください"
    return Session.start(inbox, queue)
