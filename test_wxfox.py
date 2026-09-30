# -*- coding: utf-8 -*-
"""Headless checks: geometry, encoders and payload integrity."""
import json
import subprocess
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import wxfox  # noqa: E402

FAILS = []


def check(name, condition, detail=''):
    if condition:
        print(f'  ok   {name}')
    else:
        print(f'  FAIL {name} {detail}')
        FAILS.append(name)


def widths(text):
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
               for c in text)


def main():
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE
    payload = wxfox.Payload(root)
    print('payload')
    check('hero plate', payload.hero.get('body'), 'missing body art')
    check('lyric map', len(payload.lyrics) > 100, len(payload.lyrics))
    check('spectrum', len(payload.frames) > 1000, len(payload.frames))

    print('geometry')
    for cols, rows in ((48, 16), (80, 24), (96, 32), (132, 43), (200, 60)):
        frame = wxfox.Frame(cols, rows, payload.sub)
        frame.rrect(0.5, 0.5, 0.5, 0.4, 0.1, 'ink')
        frame.circle(0.5, 0.5, 0.2, 'ink', hollow=0.2)
        frame.line(0.1, 0.1, 0.9, 0.9, 'ink')
        data = frame.materialise(wxfox.MODE_PLAIN, 0.28, 0.10)
        check(f'{cols}x{rows} rows', len(data) == rows, len(data))
        check(f'{cols}x{rows} cols', all(len(c) == cols for c, _ in data))

    print('scenes')
    player = wxfox.Player(payload)
    # late scenes are deliberately sparse: count lit pixels over a window
    for name, moment, *_ in wxfox.SCENES:
        peak = 0
        for step in range(12):
            data = player.compose(moment + 0.5 + step, 90, 30).materialise(
                wxfox.MODE_PLAIN, 0.28, 0.10)
            peak = max(peak, sum(1 for chars, _ in data for ch in chars
                                 if ch.strip()))
        check(f'{name:12s} peak-lift={peak:4d}', peak > 25, 'scene too empty')

    print('chroma / encoders')
    frame = player.compose(43.0, 90, 30)
    colored = wxfox.Encoder(wxfox.MODE_COLOR, 256).encode(
        frame, frame.materialise(wxfox.MODE_COLOR, 0.28, 0.10))
    check('256 colour codes', '38;5;' in colored)
    mono = wxfox.Encoder(wxfox.MODE_MONO, 256).encode(
        frame, frame.materialise(wxfox.MODE_MONO, 0.28, 0.10))
    check('mono escapes', '\x1b[' in mono)
    plain = wxfox.Encoder.encode_plain(
        frame.materialise(wxfox.MODE_PLAIN, 0.28, 0.10))
    check('plain has no escapes', '\x1b' not in plain)
    check('plain keeps block ramp',
          any(g in plain for g in wxfox.SUB_INK), 'no block glyphs')
    ascii_rows = wxfox.Charset.ascii_frame(frame.materialise(
        wxfox.MODE_PLAIN, 0.28, 0.10))
    check('ascii frame is 7-bit', all(ord(c) < 128 for r in ascii_rows for c in r))
    check('ascii transliteration',
          all(ord(c) < 128 for c in wxfox.Charset.ascii_text('献出自我 ABC ▓▒░✓')))

    print('captions')
    for name, moment, *_ in wxfox.SCENES:
        for line in payload.lyrics:
            en, zh = line['en'], line['zh']
            assert isinstance(en, str) and isinstance(zh, str)
    check('caption width math', widths('献出自我') == 8)
    check('crop respects width', widths(wxfox.crop('献出自我abc', 5)) <= 5)

    print('cli')
    out = subprocess.run([sys.executable, str(HERE / 'wxfox.py'), '--root',
                          str(root), '--at', '42.0', '--mode', 'plain',
                          '--width', '72', '--height', '24'],
                         capture_output=True, text=True)
    check('cli snapshot exit', out.returncode == 0, out.stderr[:200])
    lines = out.stdout.splitlines()
    check('cli snapshot size', len(lines) == 24, len(lines))
    check('cli snapshot width', all(len(l) <= 72 for l in lines))
    out_json = subprocess.run([sys.executable, str(HERE / 'wxfox.py'), '--root',
                               str(root), '--at', '42.0', '--mode', 'json',
                               '--width', '64', '--height', '20'],
                              capture_output=True, text=True)
    check('cli json exit', out_json.returncode == 0, out_json.stderr[:200])
    doc = json.loads(out_json.stdout)
    check('cli json shape', doc['size'] == [64, 20] and len(doc['rows']) == 20)
    out_scenes = subprocess.run([sys.executable, str(HERE / 'wxfox.py'),
                                 '--list-scenes'], capture_output=True, text=True)
    check('scene list', out_scenes.returncode == 0
          and 'EXECUTION' in out_scenes.stdout)

    print()
    if FAILS:
        print(f'{len(FAILS)} FAILED: ' + ', '.join(FAILS))
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
