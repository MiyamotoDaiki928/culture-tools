"""MEA-Probe 培地写真の合成ツール

使い方:
    python culture_stitch.py stitch [フォルダ ...]   合成（フォルダ省略時は 00_Culture_Media 全体を走査）
    python culture_stitch.py cleanup                 直前の合成で処理したフォルダの元画像をごみ箱へ移動

合成のあとは確認ページを自分のパソコン内だけで配信し、ページのボタンから元画像を削除できる。

合成の本体は image-stitcher (https://github.com/kkito0726/image-stitcher) と同じく
OpenCV の cv2.Stitcher をスキャンモード・既定設定で使う。
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

TOOL_DIR = Path(__file__).resolve().parent
CONFIG = TOOL_DIR.parent / "app" / "config.json"   # 写真のフォルダの場所（初回起動時に選んだもの）
REVIEW_DIR = TOOL_DIR / "review"
MANIFEST = REVIEW_DIR / "last_run.json"

N_IMAGES = 6            # 1 フォルダあたりの撮影枚数
GRID = 8                # 電極は 8×8
MARGIN_PITCH = 1.0      # 最外周の電極中心から外側に残す余白（電極間距離の何倍か）
JPEG_QUALITY = 95       # image-stitcher のダウンロード既定値と同じ
MIN_ELECTRODES = 56     # 格子上で検出できた電極がこれ未満なら要確認
PAGE_TIMEOUT = 20      # 確認ページからの合図がこの秒数途絶えたら終了する
RAW_PATTERN = re.compile(r"^IMG_\d+\.jpe?g$", re.IGNORECASE)


def load_root() -> Path | None:
    """初回起動時に選んだ写真のフォルダ（00_Culture_Media）を読む。まだ選んでいなければ None"""
    try:
        p = json.loads(CONFIG.read_text(encoding="utf-8")).get("root")
    except (OSError, ValueError, AttributeError):
        return None
    return Path(p) if p else None


def save_root(root: Path) -> None:
    CONFIG.write_text(json.dumps({"root": str(root)}, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- フォルダ情報

@dataclass
class FolderInfo:
    path: Path
    raw_files: list[Path]
    probe: int | None
    seed_date: dt.date | None
    div: int | None


def parse_folder(folder: Path, raw_files: list[Path]) -> FolderInfo:
    """フォルダ名から Probe 番号・播種日・DIV（Days In Vitro: 播種後経過日数）を読み取る。

    想定する構成:
      261028播種(6)/DIV7              … 現行
      260617播種(5, 6)/day7/5         … 旧形式（Probe ごとのサブフォルダ）
    """
    probe = seed = div = None
    parts = folder.parts
    for i, name in enumerate(parts):
        m = re.match(r"^(\d{6})播種\(([\d,\s]+)\)$", name)
        if not m:
            continue
        seed = dt.datetime.strptime(m.group(1), "%y%m%d").date()
        probes = [int(x) for x in re.findall(r"\d+", m.group(2))]
        rest = parts[i + 1:]
        if rest:
            d = re.match(r"^(?:DIV|day)(\d+)$", rest[0], re.IGNORECASE)
            if d:
                div = int(d.group(1))
        if len(probes) == 1:
            probe = probes[0]
        else:
            for name2 in rest[1:]:
                if name2.isdigit() and int(name2) in probes:
                    probe = int(name2)
        break
    return FolderInfo(folder, raw_files, probe, seed, div)


def exif_datetime(path: Path) -> dt.datetime | None:
    try:
        exif = Image.open(path).getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)   # 撮影日時 / 更新日時
        return dt.datetime.strptime(raw, "%Y:%m:%d %H:%M:%S") if raw else None
    except Exception:
        return None


def find_targets(paths: list[Path]) -> list[FolderInfo]:
    folders: dict[Path, list[Path]] = {}
    for base in paths:
        candidates = [base] + [p for p in base.rglob("*") if p.is_dir()]
        for d in candidates:
            raws = sorted(p for p in d.iterdir() if p.is_file() and RAW_PATTERN.match(p.name))
            if raws:
                folders[d] = raws
    return [parse_folder(d, r) for d, r in sorted(folders.items())]


# ---------------------------------------------------------------- 画像処理

def stitch(images: list[np.ndarray]) -> np.ndarray:
    stitcher = cv2.Stitcher.create(cv2.Stitcher_SCANS)
    status, pano = stitcher.stitch(images)
    if status != cv2.Stitcher_OK:
        reasons = {
            cv2.Stitcher_ERR_NEED_MORE_IMGS: "重なりが見つからない画像がある",
            cv2.Stitcher_ERR_HOMOGRAPHY_EST_FAIL: "画像同士の位置関係を推定できない",
            cv2.Stitcher_ERR_CAMERA_PARAMS_ADJUST_FAIL: "位置関係の調整に失敗",
        }
        raise RuntimeError(f"合成に失敗しました（{reasons.get(status, status)}）")
    return pano


def detect_electrodes(img: np.ndarray, ref_width: int) -> np.ndarray:
    """明るい背景の中の暗い正方形（電極）をテンプレートマッチングで探す。"""
    f = 2576 / ref_width        # 1 枚の幅 2576 px のとき電極は約 34 px。縮尺をそろえて検出する
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, None, fx=f, fy=f, interpolation=cv2.INTER_AREA).astype(np.float32)
    s = 34
    tmpl = np.ones((2 * s, 2 * s), np.float32)
    tmpl[s // 2:s // 2 + s, s // 2:s // 2 + s] = 0
    score = cv2.matchTemplate(g, tmpl, cv2.TM_CCOEFF_NORMED)
    peak = cv2.dilate(score, np.ones((101, 101)))
    pk = np.argwhere((score == peak) & (score > 0.55))
    return (pk[:, ::-1] + s) / f


@dataclass
class Lattice:
    origin: np.ndarray      # 電極 (0, 0) の中心
    a: np.ndarray           # 列方向に 1 電極進むベクトル
    b: np.ndarray           # 行方向に 1 電極進むベクトル
    found: int              # 格子上で見つかった電極数
    rms: float              # 当てはめ誤差（電極間距離に対する比）


def fit_lattice(pts: np.ndarray) -> Lattice:
    if len(pts) < 10:
        raise RuntimeError(f"電極がほとんど検出できません（{len(pts)} 個）")
    d = np.linalg.norm(pts[:, None] - pts[None], axis=2)
    np.fill_diagonal(d, np.inf)
    vec = pts[d.argmin(1)] - pts
    ang = np.arctan2(vec[:, 1], vec[:, 0]) % (np.pi / 2)
    ang = np.where(ang > np.pi / 4, ang - np.pi / 2, ang)
    theta, pitch = np.median(ang), np.median(d.min(1))
    c, s = np.cos(-theta), np.sin(-theta)
    q = pts @ np.array([[c, -s], [s, c]]).T / pitch
    phase = np.angle(np.exp(2j * np.pi * q).mean(0)) / (2 * np.pi)
    idx = np.round(q - phase).astype(int)
    on_grid = np.abs(q - phase - idx).max(1) < 0.2
    idx, pts = idx[on_grid], pts[on_grid]

    best = (-1, 0, 0, None)
    for i0 in range(idx[:, 0].min() - GRID + 1, idx[:, 0].max() + 1):
        for j0 in range(idx[:, 1].min() - GRID + 1, idx[:, 1].max() + 1):
            m = ((idx[:, 0] >= i0) & (idx[:, 0] < i0 + GRID)
                 & (idx[:, 1] >= j0) & (idx[:, 1] < j0 + GRID))
            if m.sum() > best[0]:
                best = (int(m.sum()), i0, j0, m)
    found, i0, j0, m = best
    ij = idx[m] - [i0, j0]
    A = np.c_[np.ones(len(ij)), ij]
    coef, *_ = np.linalg.lstsq(A, pts[m], rcond=None)
    resid = pts[m] - A @ coef
    rms = float(np.sqrt((resid ** 2).sum(1).mean()) / pitch)
    return Lattice(coef[0], coef[1], coef[2], found, rms)


def check_lattice(lat: Lattice) -> list[str]:
    warns = []
    la, lb = np.linalg.norm(lat.a), np.linalg.norm(lat.b)
    if lat.found < MIN_ELECTRODES:
        warns.append(f"格子上で検出できた電極が {lat.found}/64 個です")
    if abs(la - lb) / max(la, lb) > 0.03:
        warns.append(f"縦横の電極間距離が一致しません（{la:.0f} px と {lb:.0f} px）。合成がずれている可能性があります")
    cos = abs(np.dot(lat.a, lat.b)) / (la * lb)
    if cos > 0.03:
        warns.append("電極格子が直交していません。合成がずれている可能性があります")
    if lat.rms > 0.05:
        warns.append(f"電極位置が等間隔格子から外れています（誤差 {lat.rms:.1%}）。合成がずれている可能性があります")
    return warns


def crop_square(pano: np.ndarray, lat: Lattice) -> np.ndarray:
    pitch = (np.linalg.norm(lat.a) + np.linalg.norm(lat.b)) / 2
    angle = np.degrees(np.arctan2(lat.a[1], lat.a[0]))
    center = lat.origin + (GRID - 1) / 2 * (lat.a + lat.b)
    side = int(round(((GRID - 1) + 2 * MARGIN_PITCH) * pitch))
    M = cv2.getRotationMatrix2D((float(center[0]), float(center[1])), angle, 1.0)
    M[:, 2] += [side / 2 - center[0], side / 2 - center[1]]
    return cv2.warpAffine(pano, M, (side, side), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))


def imread(path: Path) -> np.ndarray:
    # 日本語パス対策（cv2.imread は Windows の非 ASCII パスを読めない）
    img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"画像を読み込めません: {path.name}")
    return img


def imwrite(path: Path, img: np.ndarray, quality: int = JPEG_QUALITY) -> None:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG の書き出しに失敗しました")
    buf.tofile(str(path))


# ---------------------------------------------------------------- 合成コマンド

@dataclass
class Result:
    folder: str
    status: str                      # ok / warn / skip / error
    output: str | None = None
    preview: str | None = None
    messages: list[str] = field(default_factory=list)
    raw_files: list[str] = field(default_factory=list)


def process(info: FolderInfo, force: bool) -> Result:
    res = Result(str(info.path), "ok", raw_files=[str(p) for p in info.raw_files])
    if len(info.raw_files) != N_IMAGES:
        res.status = "skip"
        res.messages.append(f"IMG ファイルが {len(info.raw_files)} 枚です（{N_IMAGES} 枚のフォルダのみ処理します）")
        return res
    if info.probe is None:
        res.status = "skip"
        res.messages.append("フォルダ名から MEA-Probe 番号を読み取れません")
        return res

    # 撮影日: EXIF を優先し、フォルダ名（播種日 + DIV）と照合する
    shot = exif_datetime(info.raw_files[0])
    expected = info.seed_date + dt.timedelta(days=info.div) if info.seed_date and info.div is not None else None
    if shot:
        date = shot.date()
        if expected and date != expected:
            res.messages.append(f"撮影日 {date:%y%m%d} がフォルダ名からの計算値 {expected:%y%m%d} と異なります（撮影日を採用）")
    elif expected:
        date = expected
        res.messages.append("EXIF に撮影日時がないため、フォルダ名から日付を計算しました")
    else:
        res.status = "skip"
        res.messages.append("撮影日を決められません（EXIF なし・フォルダ名から計算不可）")
        return res

    out = info.path / f"{date:%y%m%d}_{info.probe}.jpg"
    res.output = str(out)
    if out.exists() and not force:
        res.status = "skip"
        res.messages.append(f"{out.name} が既にあります（上書きしません）")
        return res

    try:
        images = [imread(p) for p in info.raw_files]
        pano = stitch(images)
        lat = fit_lattice(detect_electrodes(pano, images[0].shape[1]))
        warns = check_lattice(lat)
        cropped = crop_square(pano, lat)
    except Exception as e:  # noqa: BLE001
        res.status = "error"
        res.messages.append(str(e))
        return res

    imwrite(out, cropped)
    if warns:
        res.status = "warn"
        res.messages.extend(warns)

    # 確認用の縮小画像（検出した電極格子を重ねたもの）
    REVIEW_DIR.mkdir(exist_ok=True)
    prev = cv2.resize(cropped, (900, 900), interpolation=cv2.INTER_AREA)
    k = 900 / cropped.shape[0]
    pitch = (np.linalg.norm(lat.a) + np.linalg.norm(lat.b)) / 2
    for i in range(GRID):
        for j in range(GRID):
            x = (MARGIN_PITCH + i) * pitch * k
            y = (MARGIN_PITCH + j) * pitch * k
            cv2.circle(prev, (int(x), int(y)), 14, (60, 220, 60), 2)
    name = re.sub(r"[^\w]+", "_", str(info.path.relative_to(info.path.anchor))) + ".jpg"
    imwrite(REVIEW_DIR / name, prev, 85)
    res.preview = name
    res.messages.insert(0, f"電極 {lat.found}/64 個を検出、電極間距離 {pitch:.0f} px、傾き補正 {np.degrees(np.arctan2(lat.a[1], lat.a[0])):+.2f}°")
    return res


LABELS = {"ok": "正常", "warn": "要確認", "error": "失敗", "skip": "対象外", "deleted": "削除済み"}
ORDER = ["error", "warn", "ok", "skip", "deleted"]

PAGE_CSS = """
:root{--bg:#18181b;--panel:#111113;--card:#1f1f23;--line:rgba(255,255,255,.08);--ink:#fafafa;--quiet:#a1a1aa;
  --accent:#f59e0b;--good:#4ade80;--warn:#fbbf24;--bad:#f87171}
body{font-family:"Segoe UI","Yu Gothic UI","Meiryo",sans-serif;margin:0;background:var(--bg);color:var(--ink);font-size:14px}
main{max-width:1000px;margin:0 auto;padding:24px 24px 120px}
h1{font-size:18px;font-weight:600;margin:8px 0}
main>p{color:var(--quiet);font-size:13px;line-height:1.7}
section{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--quiet);border-radius:10px;
  margin:0 0 16px;padding:12px 16px}
section.ok{border-left-color:var(--good)} section.warn{border-left-color:var(--warn)} section.error{border-left-color:var(--bad)}
section.skip,section.deleted{opacity:.55}
h2{font-size:14px;font-weight:600;margin:0 0 4px;word-break:break-all} h2 span.st{display:inline-block;min-width:5em;font-weight:700}
section.ok .st{color:var(--good)} section.warn .st{color:var(--warn)} section.error .st{color:var(--bad)}
section.skip .st,section.deleted .st{color:var(--quiet)}
.out{margin:0;font-weight:600} ul{margin:4px 0 8px;font-size:13px;color:var(--quiet);padding-left:20px}
img{display:block;width:450px;max-width:100%;border:1px solid var(--line);border-radius:6px}
label.pick{display:block;margin:4px 0 8px;font-size:14px;cursor:pointer}
label.pick input{transform:scale(1.25);margin-right:8px;accent-color:var(--accent)}
.done{color:var(--good);font-weight:600} .fail{color:var(--bad)}
#bar{position:fixed;left:0;right:0;bottom:0;background:var(--panel);border-top:1px solid var(--line);color:var(--ink);
  padding:12px 24px;display:flex;gap:12px;align-items:center}
#bar button{font:inherit;font-size:14px;padding:8px 16px;border-radius:8px;cursor:pointer}
#del,#run{background:var(--accent);color:#111113;border:0;font-weight:600}
#del:disabled,#run:disabled{background:#3f3f46;color:var(--quiet);cursor:default}
#quit,#bar .sub{background:transparent;color:var(--ink);border:1px solid var(--line)} #msg{flex:1;color:var(--quiet)}
header.top{position:sticky;top:0;z-index:10;height:52px;display:flex;align-items:center;gap:10px;padding:0 24px;
  background:var(--panel);border-bottom:1px solid var(--line);font-family:"Segoe UI","Yu Gothic UI",sans-serif}
header.top svg{width:30px;height:30px;flex:none}
header.top .name{font-weight:600;font-size:15px;letter-spacing:-.01em;color:var(--ink);white-space:nowrap}
header.top .name b{color:var(--accent);font-weight:600}
"""

PAGE_JS = """
const token = document.body.dataset.token;
const boxes = [...document.querySelectorAll('input.pick')];
const del = document.getElementById('del'), msg = document.getElementById('msg');
function refresh(){
  const n = boxes.filter(b => b.checked && !b.disabled).length;
  del.disabled = n === 0;
  del.textContent = n ? `チェックした ${n} フォルダの元画像をごみ箱へ移動` : '元画像を削除するフォルダにチェックを入れてください';
}
boxes.forEach(b => b.addEventListener('change', refresh)); refresh();
async function post(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-Token':token}, body:JSON.stringify(body||{})});
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}
del.addEventListener('click', async () => {
  const ids = boxes.filter(b => b.checked && !b.disabled).map(b => Number(b.value));
  if(!confirm(`${ids.length} フォルダの元画像（各6枚）をごみ箱へ移動します。よろしいですか？`)) return;
  del.disabled = true; msg.textContent = '移動中...';
  try {
    const res = await post('/delete', {ids});
    for(const r of res.results){
      const sec = document.getElementById('r' + r.id), box = sec.querySelector('input.pick');
      box.checked = false; box.disabled = true;
      const p = document.createElement('p');
      p.className = r.errors.length ? 'fail' : 'done';
      p.textContent = r.errors.length ? `${r.moved} 枚を移動、${r.errors.length} 枚は移動できませんでした: ${r.errors.join(' / ')}` : `元画像 ${r.moved} 枚をごみ箱へ移動しました`;
      sec.insertBefore(p, box.parentElement.nextSibling);
    }
    msg.textContent = '完了しました。';
  } catch(e) { msg.textContent = 'エラー: ' + e.message; }
  refresh();
});
const ping = () => post('/ping').catch(() => {});
ping(); const beat = setInterval(ping, 5000);
document.getElementById('quit').addEventListener('click', async () => {
  clearInterval(beat);
  try { await post('/quit'); } catch(e) {}
  document.getElementById('bar').innerHTML = '<span>終了しました。このタブは閉じて構いません。</span>';
  window.close();
});
"""


LOGO_SVG = ("<svg viewBox=\"0 0 32 32\" fill=\"none\" aria-hidden=\"true\">"
          # 円形の培養器と、その中央に並ぶ 3×3 の電極。右上の 1 つだけオレンジで強調する
          "<circle cx=\"16\" cy=\"16\" r=\"14\" fill=\"#f59e0b\" fill-opacity=\".14\" stroke=\"#f59e0b\" stroke-width=\"2\"/>"
          "<circle cx=\"10\" cy=\"10\" r=\"1.8\" fill=\"#fafafa\" fill-opacity=\".9\"/>"
          "<circle cx=\"16\" cy=\"10\" r=\"1.8\" fill=\"#fafafa\" fill-opacity=\".9\"/>"
          "<circle cx=\"10\" cy=\"16\" r=\"1.8\" fill=\"#fafafa\" fill-opacity=\".9\"/>"
          "<circle cx=\"16\" cy=\"16\" r=\"1.8\" fill=\"#fafafa\" fill-opacity=\".9\"/>"
          "<circle cx=\"22\" cy=\"16\" r=\"1.8\" fill=\"#fafafa\" fill-opacity=\".9\"/>"
          "<circle cx=\"10\" cy=\"22\" r=\"1.8\" fill=\"#fafafa\" fill-opacity=\".9\"/>"
          "<circle cx=\"16\" cy=\"22\" r=\"1.8\" fill=\"#fafafa\" fill-opacity=\".9\"/>"
          "<circle cx=\"22\" cy=\"22\" r=\"1.8\" fill=\"#fafafa\" fill-opacity=\".9\"/>"
          "<circle cx=\"22\" cy=\"10\" r=\"2.4\" fill=\"#fbbf24\"/>"
          "</svg>")
HEADER = f'<header class="top">{LOGO_SVG}<span class="name">culture-<b>stitcher</b></span></header>'

# ブラウザのタブに表示するアイコン。タブの背景が白でも電極が見えるよう、円の内側を黒で塗る
FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32' fill='none'>"
    "<circle cx='16' cy='16' r='14' fill='#111113'/>"
    "<circle cx='16' cy='16' r='14' fill='#f59e0b' fill-opacity='.14' stroke='#f59e0b' stroke-width='2.4'/>"
    + "".join(f"<circle cx='{x}' cy='{y}' r='2' fill='#fafafa'/>"
              for y in (10, 16, 22) for x in (10, 16, 22) if (x, y) != (22, 10))
    + "<circle cx='22' cy='10' r='2.6' fill='#fbbf24'/></svg>"
)
FAVICON = "data:image/svg+xml," + FAVICON_SVG.replace("#", "%23")


def deletable(r: Result) -> bool:
    return (r.status in ("ok", "warn") and bool(r.output) and Path(r.output).exists()
            and any(Path(f).exists() for f in r.raw_files))


def render_page(results: list[Result], token: str | None = None, brand: str | None = None,
                extra_bar: str = "", extra_js: str = "", folder_label=None) -> str:
    """確認ページ。token ありはアプリの実行中に配信する削除ボタン付き、なしは保存用。"""
    live = token is not None
    header = f'<header class="top">{brand}</header>' if brand else HEADER
    rows = []
    for i, r in enumerate(results):
        if r.preview and r.output:
            src = f"/preview/{i}?t={token}" if live else r.preview
            href = f"/full/{i}?t={token}" if live else Path(r.output).as_uri()
            img = f'<a href="{html.escape(href)}" target="_blank"><img src="{html.escape(src)}"></a>'
        else:
            img = ""
        msgs = "".join(f"<li>{html.escape(m)}</li>" for m in r.messages)
        out = html.escape(Path(r.output).name) if r.output and r.status in ("ok", "warn") else ""
        pick = ""
        if live and deletable(r):
            checked = " checked" if r.status == "ok" else ""
            pick = (f'<label class="pick"><input type="checkbox" class="pick" value="{i}"{checked}>'
                    f'問題なし（元画像を削除してよい）</label>')
        rows.append(f'<section id="r{i}" class="{r.status}"><h2><span class="st">{LABELS[r.status]}</span>'
                    f'{html.escape(folder_label(r.folder) if folder_label else r.folder)}</h2><p class="out">{out}</p><ul>{msgs}</ul>{pick}{img}</section>')
    if live:
        note = ("緑の円は検出した電極の位置です。画像をクリックすると原寸の合成画像を開きます。"
                "確認できたフォルダにチェックを入れ、下のボタンで元画像をごみ箱へ移動します。"
                "「正常」には最初からチェックが入っています。「要確認」は原寸で確かめてからチェックしてください。")
        bar = (f'<div id="bar"><button id="del"></button><span id="msg"></span>{extra_bar}'
               '<button id="quit">終了</button></div>')
        script = f"<script>{PAGE_JS}{extra_js}</script>"
    else:
        note = ("緑の円は検出した電極の位置です。画像をクリックすると原寸の合成画像を開きます。"
                "このページは記録用です。元画像の削除は、culture-tools の「合成」画面から行えます。")
        bar = script = ""
    return (f'<!doctype html><html lang="ja"><meta charset="utf-8"><title>合成結果の確認 | culture-stitcher</title>'
            f'<link rel="icon" type="image/svg+xml" href="{FAVICON}">'
            f'<style>{PAGE_CSS}</style><body data-token="{token or ""}">'
            f'{header}<main>'
            f'<h1>合成結果の確認（{dt.datetime.now():%Y-%m-%d %H:%M}）</h1><p>{note}</p>'
            f'{"".join(rows)}</main>{bar}{script}</body></html>')


def save_manifest(results: list[Result]) -> None:
    REVIEW_DIR.mkdir(exist_ok=True)
    MANIFEST.write_text(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=1), encoding="utf-8")


def trash_raw_files(r: Result) -> tuple[int, list[str]]:
    from send2trash import send2trash
    moved, errors = 0, []
    for f in r.raw_files:
        p = Path(f)
        if not p.exists():
            continue
        try:
            send2trash(str(p))
            moved += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{p.name}（{e}）")
    return moved, errors


def serve_review(results: list[Result], open_browser: bool) -> None:
    """確認ページを自分のパソコン内だけで配信し、ページのボタンから削除を受け付ける。

    「終了」が押されるか、ページが閉じられて合図（/ping）が途絶えると終了する。
    """
    import secrets
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    token = secrets.token_urlsafe(16)   # 他のページから勝手に削除を指示されないための合言葉
    lock = threading.Lock()
    last_ping = [time.monotonic() + 60]  # 最初の 1 分はブラウザが開くのを待つ

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):      # アクセスごとの表示は出さない
            pass

        def _authorized(self) -> bool:
            q = parse_qs(urlparse(self.path).query).get("t", [None])[0]
            return secrets.compare_digest(self.headers.get("X-Token") or q or "", token)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path) -> None:
            if path.exists():
                self._send(200, path.read_bytes(), "image/jpeg")
            else:
                self._send(404, b"not found", "text/plain")

        def do_GET(self):
            if not self._authorized():
                return self._send(403, b"forbidden", "text/plain")
            parts = urlparse(self.path).path.strip("/").split("/")
            if parts == [""]:
                return self._send(200, render_page(results, token).encode("utf-8"), "text/html; charset=utf-8")
            if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) < len(results):
                r = results[int(parts[1])]
                if parts[0] == "preview" and r.preview:
                    return self._file(REVIEW_DIR / r.preview)
                if parts[0] == "full" and r.output:
                    return self._file(Path(r.output))
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._authorized():
                return self._send(403, b"forbidden", "text/plain")
            path = urlparse(self.path).path
            if path == "/ping":
                last_ping[0] = time.monotonic()
                return self._send(200, b"{}", "application/json")
            if path == "/quit":
                self._send(200, b"{}", "application/json")
                threading.Thread(target=server.shutdown, daemon=True).start()
                return
            if path != "/delete":
                return self._send(404, b"not found", "text/plain")
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            out = []
            with lock:
                for i in body.get("ids", []):
                    if not (isinstance(i, int) and 0 <= i < len(results)) or not deletable(results[i]):
                        continue
                    r = results[i]
                    moved, errors = trash_raw_files(r)
                    print(f"  元画像 {moved} 枚をごみ箱へ移動: {r.folder}")
                    for e in errors:
                        print(f"    移動できませんでした: {e}")
                    if not errors:
                        r.status = "deleted"
                    out.append({"id": i, "moved": moved, "errors": errors})
                save_manifest(results)
            self._send(200, json.dumps({"results": out}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)   # このパソコンからしか接続できない
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    print("\n確認ページを開きました。元画像の削除はページ下のボタンから行えます。")
    print("確認が済んだらページの「終了」を押すか、ページを閉じてください。この画面も自動で閉じます。")
    print(f"ページが開かない場合はこのアドレスを開いてください: {url}")
    if open_browser:
        webbrowser.open(url)

        def watchdog():
            while True:
                time.sleep(2)
                if time.monotonic() - last_ping[0] > PAGE_TIMEOUT:
                    print("確認ページが閉じられたので終了します。")
                    server.shutdown()
                    return
        threading.Thread(target=watchdog, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()


def cmd_stitch(args) -> None:
    roots = [Path(p) for p in args.folders] or [load_root()]
    if roots[0] is None:
        print("写真のフォルダが設定されていません。culture-tools を一度起動して選ぶか、フォルダを指定してください。")
        sys.exit(1)
    targets = find_targets(roots)
    if not targets:
        print("合成前の写真が入ったフォルダはありませんでした。")
        sys.exit(3)
    results = []
    for info in targets:
        if len(info.raw_files) == N_IMAGES:
            print(f"処理中: {info.path}")
        r = process(info, args.force)
        print(f"  [{r.status}] " + " / ".join(r.messages))
        results.append(r)
    results.sort(key=lambda r: ORDER.index(r.status))
    done = sum(r.status in ("ok", "warn") for r in results)
    if done == 0:
        # 前回の記録（元画像を削除.bat の対象）は残したまま終える
        if all(r.status == "skip" and r.output and Path(r.output).exists() for r in results):
            print("\n合成前の写真が入ったフォルダはありませんでした。")
            sys.exit(3)             # 10 秒後に自動で閉じる
        else:
            print("\n合成前の写真が入ったフォルダはありましたが、上に表示した理由で合成できませんでした。")
            sys.exit(4)             # どのフォルダかを確かめられるよう、画面は閉じずに残す
    save_manifest(results)
    page = REVIEW_DIR / "review.html"
    page.write_text(render_page(results), encoding="utf-8")
    print(f"\n{done} フォルダを合成しました。記録用の確認ページ: {page}")
    if args.no_serve:
        if not args.no_open:
            webbrowser.open(page.as_uri())
        return
    serve_review(results, not args.no_open)


# ---------------------------------------------------------------- 削除コマンド

def cmd_cleanup(args) -> None:
    if not MANIFEST.exists():
        print("合成の記録がありません。先に合成を実行してください。")
        return
    results = [Result(**r) for r in json.loads(MANIFEST.read_text(encoding="utf-8"))]
    targets = [r for r in results if deletable(r)]
    if not targets:
        print("削除対象の元画像はありません。")
        return
    print("次のフォルダの元画像をごみ箱へ移動します:")
    for r in targets:
        mark = "（要確認だったフォルダ）" if r.status == "warn" else ""
        print(f"  {r.folder}  → 合成画像 {Path(r.output).name}{mark}")
    if input("よろしいですか？ [y/N]: ").strip().lower() != "y":
        print("中止しました。")
        return
    total = 0
    for r in targets:
        moved, errors = trash_raw_files(r)
        total += moved
        for e in errors:
            print(f"  移動できませんでした: {e}")
        if not errors:
            r.status = "deleted"
    save_manifest(results)
    print(f"{total} 枚をごみ箱へ移動しました。")


def main() -> None:
    ap = argparse.ArgumentParser(description="MEA-Probe 培地写真の合成ツール")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stitch", help="合成する")
    s.add_argument("folders", nargs="*", help="対象フォルダ（省略時は 00_Culture_Media 全体）")
    s.add_argument("--force", action="store_true", help="既存の合成画像を上書きする")
    s.add_argument("--no-open", action="store_true", help="確認ページを自動で開かない")
    s.add_argument("--no-serve", action="store_true", help="削除ボタン付きのページを配信せず、記録用ページだけ作る")
    s.set_defaults(func=cmd_stitch)
    c = sub.add_parser("cleanup", help="直前の合成で処理した元画像をごみ箱へ移動する")
    c.set_defaults(func=cmd_cleanup)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
