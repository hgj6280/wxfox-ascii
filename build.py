# -*- coding: utf-8 -*-
"""Build the self-contained 小狐狐 / 酒狐 terminal MV bundle.

Usage: python3 build.py <repo-with-payloads> <output-dir>
Reuses the lyric/spectrum data from an existing checkout (payload only, no code)
and writes a fully standalone distributed copy of the project.
"""
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HERO_W, HERO_H = 92, 30
HERO_ASPECT = HERO_W / HERO_H


# ---------------------------------------------------------------- hero plate
def _ell(x, y, cx, cy, rx, ry):
    return ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0


def _tri(x, y, cx, apex_y, half_w, base_y, shrink=0.0, dy=0.0):
    ay = apex_y + shrink * (base_y - apex_y) + dy
    hw = half_w * (1 - shrink)
    by = base_y + dy
    if y < ay or y > by:
        return False
    t = (y - ay) / max(1e-6, by - ay)
    return abs(x - cx) <= hw * t


def build_hero():
    """Canonical fox-head plate: solid silhouette + feature masks."""
    W, H = HERO_W, HERO_H
    cx, cy, r = 45.5, 16.6, 41.0
    body = [[_ell(x, y, cx, cy, r, 11.7) for x in range(W)] for y in range(H)]
    face = [[_ell(x, y, cx, cy + 0.7, r, 9.4) for x in range(W)] for y in range(H)]
    ear = [[False] * W for _ in range(H)]
    for sign in (-1, 1):
        ex = cx + sign * 27.0
        for y in range(H):
            for x in range(W):
                if _tri(x, y, ex, 0.2, 12.0, 12.6):
                    body[y][x] = True
                if _tri(x, y, ex, 0.2, 8.2, 10.6, shrink=0.26, dy=2.6):
                    ear[y][x] = True
    ink = [row[:] for row in body]
    for y in range(H):
        for x in range(W):
            if not body[y][x]:
                continue
            for ex, ey in ((cx - 19.0, cy - 1.8), (cx + 19.0, cy - 1.8)):
                if ((x - ex) / 3.1) ** 2 + ((y - ey) / 2.0) ** 2 <= 1.0:
                    ink[y][x] = False
            if abs(x - cx) <= 1.7 and abs(y - (cy + 5.6)) <= 1.6:
                ink[y][x] = False

    def art(rows):
        out = []
        for y in range(H):
            line = ''.join('\u2588' if rows[y][x] else ' ' for x in range(W)).rstrip()
            out.append(line)
        while out and not out[0].strip():
            out.pop(0)
        while out and not out[-1].strip():
            out.pop()
        return out

    return {'body': art(body), 'face': art(face), 'ear': art(ear), 'ink': art(ink)}


# ------------------------------------------------------------- static assets
def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b''):
            digest.update(chunk)
    return digest.hexdigest()


CALIBRATION = {
    'format': 'WXMV-1',
    'note': '几何参数为可选校准项；缺失或不支持时自动回退到默认值。',
    'palette': {'paper': [255, 247, 222], 'ink': [192, 78, 36], 'accent': [74, 44, 18], 'mode': 'dark_ink_on_light'},
    'frame': {'top': 3, 'bottom': 1, 'left': 2, 'right': 2},
    'cell': {'sub': 8, 'fg_luma': 0.62, 'bg_weight': 0.0},
    'render': {'ink_threshold': 0.28, 'paper_fx': 0.10, 'hero_scale': 1.0},
    'type': {'label': 2, 'label_value': 1},
    'runtime': {'thread_scale': 1.0, 'asset_bytes': 3},
}

if __name__ == '__main__':
    source = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path('/tmp/world-ascii')
    dest = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else ROOT / 'dist'
    dest.mkdir(parents=True, exist_ok=True)

    hero = build_hero()
    payload = {'format': 'WXMV-1', 'hero': hero}

    # lyric/spectrum data is payload, not code; carry it over byte-for-byte.
    song = next((source / n for n in ('song.mp3', 'media/song.mp3') if (source / n).is_file()), None)
    lyrics_src = source / 'lyrics.json'
    spectrum_src = source / 'spectrum.json'

    payload['lyrics'] = json.loads(lyrics_src.read_text(encoding='utf-8'))
    if spectrum_src.is_file():
        data = json.loads(spectrum_src.read_text(encoding='utf-8'))
        payload['spectrum'] = {'fps': data.get('fps', 30), 'frames': data['frames']}
    payload['calibration'] = CALIBRATION
    payload['provenance'] = {
        'hero': 'procedural (WXMV-1 canonical fox plate)',
        'lyrics': lyrics_src.name,
        'spectrum': spectrum_src.name if spectrum_src.is_file() else None,
    }

    out = dest / 'payload.json'
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'payload  {out}  {out.stat().st_size/1e6:.2f} MB')

    # copy the player sources verbatim from the working tree
    for name in ('wxfox.py', 'world-execute-me-ascii.command', 'run.sh', 'config.json'):
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    print('sources  copied')
