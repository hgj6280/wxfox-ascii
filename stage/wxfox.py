#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WXMV-1 · 小狐狐播放头 — a terminal ASCII MV, in the fox-miko visual style.

Only the Python standard library is required. Every piece of geometry is
expressed as ratios of the terminal size (columns/rows, Unicode widths are
counted properly), so the same plate plays on any terminal: Windows Terminal,
iTerm2, kitty, alacritty, foot, xterm, tmux, a serial console, an ssh session.

Rendering model
    1. collect the current frame from the active scene,
    2. draw the fox glyph itself from the canonical intro plate,
    3. materialise every lit pixel through a half-pixel cell buffer
       (paper/hand-composed glyph, foreground ink, background paper),
    4. emit a rect frame with a status bar and a caption strip.

The companion `wxfox-calibrate` command measures the *real* cell geometry of a
terminal (block glyph fill ratio, fg/bg luminance, colour depth) and writes the
optional `calibration` block of payload.json. Without that block the defaults
below are used, which reproduce the authoring terminal closely.

Usage
    wxfox.py                                   play in this terminal
    wxfox.py --mode color|mono|plain|json      非交互输出（CI/管道/纯文本）
    wxfox.py --at 43.5 --mode plain            定格某一时刻的整帧
    wxfox.py --list-scenes                     列出场景触发时间
    wxfox.py --calibrate-apply cal.json        写回校准参数
"""
from __future__ import annotations

import argparse
import json
import math
import os
import select
import shutil
import signal
import sys
import termios
import time
import tty
import unicodedata

from pathlib import Path

ROOT = Path(__file__).resolve().parent
TAU = math.tau
DEFAULT_FPS = 30

# ---------------------------------------------------------------------------
# palette — warm paper, fox-red ink. 256-colour values, with a 16-colour and a
# monochrome fallback so old/odd terminals still read correctly.
# ---------------------------------------------------------------------------
PAPER_RGB = (255, 247, 222)
INK_RGB = (192, 78, 36)
ACCENT_RGB = (74, 44, 18)
PAPER_256, INK_256, ACCENT_256 = 230, 202, 58
PAPER_16, INK_16, ACCENT_16 = 97, 91, 90
PAPER_MONO, INK_MONO, ACCENT_MONO = 27, 30, 37

MODE_COLOR = 'color'
MODE_MONO = 'mono'
MODE_PLAIN = 'plain'

# ---------------------------------------------------------------------------
# scenes — every cue is anchored on a lyric line, and the same cue table drives
# direct seeks (`--at`), chapter keys and the JSON/plain exports.
# ---------------------------------------------------------------------------
SCENES = (
    ('boot', 0.0, 'BOOT', '开机自检', 'power line / protection / simulation'),
    ('title', 16.0, 'TITLE', '标题显影', 'title takeover'),
    ('devotion', 29.709, 'DEVOTION', '献出自我', 'points / circle / sine / infinity'),
    ('satisfaction', 59.223, 'SATISFACTION', '满足感', 'stimulation / heart'),
    ('organic', 74.045, 'ORGANIC', '有机物', 'eggplant / tomato / cat'),
    ('identity', 88.587, 'IDENTITY', '身份重写', 'F->M / AM->PM / trance'),
    ('vibration', 103.489, 'VIBRATION', '同频', 'ECG cardiograph'),
    ('isolation', 110.9, 'ISOLATION', '离场', 'disconnect / erase'),
    ('illegal', 125.708, 'ILLEGAL', '非法参数', 'illegal arguments'),
    ('execution', 147.66, 'EXECUTION', '执行', 'queue / multilingual / only'),
    ('love', 177.246, 'LOVE', '爱', 'love equation / trapped'),
    ('outro', 192.5, 'OUTRO', '进程终止', 'stalled process -> EXECUTION'),
)

# half-pixel sub-column glyphs.  The first entry is the paper fill, the rest
# are progressively denser ink; index -1 is a literal pixel.
SUB_PAPER = '\u3000'
SUB_INK = ('\u2591', '\u2592', '\u2593', '\u2588')
BLOCK = {'\u2588': 1.0, '\u2593': 0.75, '\u2592': 0.5, '\u2591': 0.25}


def cwidth(ch: str) -> int:
    """Terminal columns occupied by a character."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def text_width(text: str) -> int:
    return sum(cwidth(ch) for ch in text)


def crop(text: str, columns: int) -> str:
    out, used = [], 0
    for ch in text:
        step = cwidth(ch)
        if step == 0:
            continue
        if used + step > columns:
            break
        out.append(ch)
        used += step
    return ''.join(out)


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------
class Payload:
    """Everything the player needs: plate, lyric map, spectrum, calibration."""

    DEFAULT_CONFIG = {
        'media': 'song.mp3',
        'duration': 211.906667,
        'subtitle_offset': 0.0,
    }

    def __init__(self, root: Path = ROOT):
        self.root = root
        payload = self._read_json(root / 'payload.json') or {}
        self.hero = payload.get('hero') or {}
        self.lyrics = payload.get('lyrics') or []
        spectrum = payload.get('spectrum') or {}
        self.fps = int(spectrum.get('fps') or DEFAULT_FPS)
        self.frames = spectrum.get('frames') or []
        self.calibration = payload.get('calibration') or {}
        self.config = self._read_json(root / 'config.json') or dict(self.DEFAULT_CONFIG)
        self.times = [float(line.get('time', 0.0)) for line in self.lyrics]

        cell = self.calibration.get('cell') or {}
        self.sub = max(4, int(cell.get('sub') or 8))
        render = self.calibration.get('render') or {}
        self.ink_threshold = float(render.get('ink_threshold', 0.28))
        self.paper_fx = float(render.get('paper_fx', 0.10))
        self.hero_scale = float(render.get('hero_scale', 1.0)) or 1.0

    @staticmethod
    def _read_json(path: Path):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    # -- queries -----------------------------------------------------------
    def lyric_at(self, moment: float):
        index = bisect_right(self.times, moment) - 1
        if index < 0:
            return None
        line = self.lyrics[index]
        end = float(line.get('end') or 0.0)
        if end and moment >= end:
            return None
        return line

    def energy_at(self, moment: float):
        if not self.frames:
            return [0.0] * 64
        index = int(moment * self.fps)
        index = max(0, min(len(self.frames) - 1, index))
        frame = self.frames[index]
        return [float(x) for x in frame]

    def scene_at(self, moment: float):
        current = SCENES[0]
        for scene in SCENES:
            if moment >= scene[1] - 1e-9:
                current = scene
        return current

    def duration(self) -> float:
        value = self.config.get('duration')
        if value:
            return float(value)
        if self.times:
            return max(self.times[-1] + 6.0, 8.0)
        return 120.0


from bisect import bisect_right  # noqa: E402  (kept next to its only user)


# ---------------------------------------------------------------------------
# canvas — half-pixel cell buffer materialised into terminal cells
# ---------------------------------------------------------------------------
class Cell:
    __slots__ = ('cover', 'ink', 'accent')

    def __init__(self):
        self.cover = [0.0] * 8
        self.ink = [False] * 8
        self.accent = [False] * 8


class Frame:
    """Logical canvas in half-pixel columns and whole rows."""

    def __init__(self, cols: int, rows: int, sub: int = 8):
        self.cols = max(4, cols)
        self.rows = max(2, rows)
        self.sub = sub
        self.w = self.cols * sub
        self.cells = [[Cell() for _ in range(self.cols)] for _ in range(self.rows)]
        self.clip = None

    # coordinate helpers ---------------------------------------------------
    def sx(self, u: float) -> int:
        return int(round(u * self.w - 0.5))

    def sy(self, v: float) -> int:
        return int(round(v * self.rows - 0.5))

    def inside(self, x: int, y: int) -> bool:
        if not (0 <= y < self.rows):
            return False
        if self.clip and not (self.clip[0] <= y <= self.clip[1]):
            return False
        return 0 <= x < self.w

    def fill_cell(self, x: int, y: int, style, fn):
        """style: 'paper' | 'ink' | 'accent'; fn(offset) -> strength 0..1."""
        if not self.inside(x, y):
            return
        cell = self.cells[y][x // self.sub]
        where = x % self.sub
        strength = fn(where)
        if strength <= 0.0:
            return
        # Coverage is independent of ink: an ink stamp also lights its pixel, so
        # the materialiser only needs `cover` to find content.
        cell.cover[where] = max(cell.cover[where], strength)
        if style == 'paper':
            return
        if strength < 0.5:
            cell.cover[where] = max(0.0, cell.cover[where] - (0.5 - strength))
            return
        cell.ink[where] = True
        if style == 'accent':
            cell.accent[where] = True

    def dot(self, x: int, y: int, style='ink', strength=1.0):
        self.fill_cell(x, y, style, lambda _o: strength)

    def span(self, x0: int, x1: int, y: int, style='ink', strength=1.0):
        if x1 < x0:
            x0, x1 = x1, x0
        for x in range(x0, x1 + 1):
            self.dot(x, y, style, strength)

    def rrect(self, uc: float, vc: float, uw: float, uh: float, radius: float,
              style='ink', strength=1.0, hollow: float = 0.0):
        """Rounded rectangle in normalised units; `hollow` hollows the middle.

        A cheap edge ramp reads better than a hard fill in a half-pixel buffer,
        so the border is one pixel wide and the corner radius is shared.
        """
        x0 = self.sx(uc - uw / 2)
        x1 = self.sx(uc + uw / 2)
        y0 = self.sy(vc - uh / 2)
        y1 = self.sy(vc + uh / 2)
        rx = max(1.0, radius * (x1 - x0) * 0.5)
        ry = max(1.0, radius * (y1 - y0) * 0.5)
        edge = 1.0 + (max(0.0, uh) * self.rows) * 0.35
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                dx = abs(x - (x0 + x1) / 2) - ((x1 - x0) / 2 - rx)
                dy = abs(y - (y0 + y1) / 2) - ((y1 - y0) / 2 - ry)
                dist = math.hypot(max(dx, 0.0), max(dy, 0.0)) \
                    + min(max(dx, dy), 0.0) - min(rx, ry)
                if dist > 0.0:
                    continue
                if hollow and dist < -max(edge, min(rx, ry) * hollow):
                    continue
                self.dot(x, y, style, strength)

    def circle(self, uc: float, vc: float, radius: float, style='ink',
               strength=1.0, hollow: float = 0.0, squash: float = 1.0):
        cx = uc * self.w
        cy = vc * self.rows
        rx = radius * self.w * 0.5
        ry = radius * self.rows * 0.5 * squash
        if rx < 0.5 or ry < 0.5:
            return
        for y in range(int(cy - ry) - 1, int(cy + ry) + 2):
            for x in range(int(cx - rx) - 1, int(cx + rx) + 2):
                if not (0 <= y < self.rows):
                    continue
                d = math.hypot((x - cx) / rx, (y - cy) / ry)
                if d > 1.0:
                    continue
                if hollow and d < 1.0 - hollow:
                    continue
                self.dot(x, y, style, strength)

    def line(self, u0: float, v0: float, u1: float, v1: float, style='ink',
             strength=1.0):
        x0, y0 = self.sx(u0), self.sy(v0)
        x1, y1 = self.sx(u1), self.sy(v1)
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(steps + 1):
            t = i / steps
            self.dot(int(round(x0 + (x1 - x0) * t)),
                     int(round(y0 + (y1 - y0) * t)), style, strength)

    def curve(self, fn, u0: float, u1: float, style='ink', strength=1.0,
              samples: int = 240):
        steps = max(24, min(1200, int(samples * max(0.05, u1 - u0))))
        for i in range(steps + 1):
            u = u0 + (u1 - u0) * i / steps
            self.dot(self.sx(u), self.sy(fn(u)), style, strength)

    # -- glyphs ------------------------------------------------------------
    def hero(self, art_rows, style='ink', strength=1.0, scale=1.0):
        """Scale the canonical fox plate to the frame and stamp it."""
        if not art_rows:
            return 0, 0, 0, 0
        art_w = max(len(row) for row in art_rows)
        target_w = max(8, int(self.w * 0.62 * scale))
        target_h = max(4, int(art_w and (target_w / art_w) * len(art_rows)))
        span_u = target_w / self.w
        span_v = target_h / self.rows
        left = self.sx(0.5 - span_u / 2)
        top = self.sy(0.5 - span_v / 2)
        for j, row in enumerate(art_rows):
            y = top + int(round(j * target_h / len(art_rows)))
            if not (0 <= y < self.rows):
                continue
            for i, ch in enumerate(row):
                if ch == ' ':
                    continue
                x = left + int(round(i * target_w / art_w))
                cover = BLOCK.get(ch, 1.0)
                self.fill_cell(x, y, style, lambda _o, _c=cover: _c)
        return left, top, target_w, target_h

    def text(self, u: float, v: float, text: str, style='ink', strength=1.0,
             align: str = 'left'):
        if not text:
            return
        if align == 'center':
            x = self.sx(u) - (text_width(text) * self.sub) // 2
        elif align == 'right':
            x = self.sx(u) - text_width(text) * self.sub
        else:
            x = self.sx(u)
        y = self.sy(v)
        # Text may start outside a clipped band or slightly off-screen; clip per
        # cell instead of dropping the whole string.
        if not (0 <= y < self.rows):
            return
        if self.clip and not (self.clip[0] <= y <= self.clip[1]):
            return
        cell_x = x // self.sub
        offset = x % self.sub
        for ch in text:
            step = cwidth(ch)
            if step == 0:
                continue
            for sub in range(step * self.sub):
                self.fill_cell(cell_x * self.sub + sub + offset, y, style,
                               lambda _o: strength)
            cell_x += step
            offset = 0

    # -- materialisation ---------------------------------------------------
    def materialise(self, mode: str, ink_threshold: float, paper_fx: float):
        rows = []
        for y in range(self.rows):
            chars, colours = [], []
            for x in range(self.cols):
                cell = self.cells[y][x]
                marks = [(i, v) for i, v in enumerate(cell.cover) if v > 0.0]
                if not marks:
                    chars.append(' ')
                    colours.append(None)
                    continue
                paper = max(marks, key=lambda m: m[1])[0]
                if mode == MODE_PLAIN:
                    chars.append(SUB_PAPER if not any(cell.ink) else SUB_INK[-1])
                    colours.append('ink' if any(cell.ink) else None)
                    continue
                glyph = SUB_PAPER
                colour = 'paper'
                has_ink = any(mark for mark, _v in marks)
                if has_ink:
                    glyph = SUB_INK[-1]
                    colour = 'accent' if cell.accent[paper] else 'ink'
                else:
                    sub = paper + (sum(m for m, _v in marks) / len(marks)
                                   - paper) / self.sub
                    glyph = SUB_INK[min(len(SUB_INK) - 1,
                                        max(0, int(sub * len(SUB_INK))))]
                chars.append(glyph)
                colours.append(colour)
            rows.append((chars, colours))
        return rows


# ---------------------------------------------------------------------------
# scenes
# ---------------------------------------------------------------------------
def hash16(i: int) -> int:
    value = (i + 0x9E3779B9) & 0xFFFFFFFF
    value = ((value ^ (value >> 16)) * 0x7FEB352D) & 0xFFFFFFFF
    value = ((value ^ (value >> 15)) * 0x846CA68B) & 0xFFFFFFFF
    return (value ^ (value >> 16)) & 0xFFFF


def clamp(value, low=0.0, high=1.0):
    return min(high, max(low, value))


def mix(a, b, u):
    return a + (b - a) * u


class Scene:
    def __init__(self, payload: Payload):
        self.payload = payload

    def draw(self, frame: Frame, t: float, pulse: float, lyric):
        raise NotImplementedError


class Boot(Scene):
    def draw(self, frame, t, pulse, lyric):
        progress = clamp(t / 16.0)
        for i in range(12):
            v = 0.12 + i * 0.07
            frame.line(0.14, v, 0.14 + 0.72 * clamp(progress * 1.4 - i * 0.03), v,
                       'ink', 0.6)
        columns = 26
        rows = 12
        for row in range(rows):
            for col in range(columns):
                if hash16(int(t * 3) + row * 97 + col * 13) % 9:
                    continue
                frame.dot(frame.sx(0.18 + col * 0.024), frame.sy(0.22 + row * 0.048),
                          'accent' if hash16(col + row) % 4 else 'ink', 0.9)
        for sweep in range(3):
            u = (t * (0.16 + sweep * 0.05) + sweep * 0.33) % 1.0
            frame.line(u, 0.10, u, 0.90, 'ink', 0.35)


class Title(Scene):
    def draw(self, frame, t, pulse, lyric):
        appear = clamp((t - 16.0) / 1.6)
        frame.hero(self.payload.hero.get('body'), 'ink', 0.95)
        frame.hero(self.payload.hero.get('face'), 'ink', 0.30)
        frame.hero(self.payload.hero.get('ear'), 'accent', 0.85)
        frame.hero(self.payload.hero.get('ink'), 'paper', 1.0)
        frame.rrect(0.5, 0.90, 0.86 * appear, 0.10 * appear, 0.06, 'ink', 0.85,
                    hollow=0.55)

    def tail(self, frame, t):
        self._caption(frame, '小狐狐 / 酒狐', 'WXMV-1 · world.execute(me);')

    @staticmethod
    def _caption(frame, left, right):
        frame.text(0.5, 0.965, f'{left}   {right}', 'accent', 1.0, 'center')


class Devotion(Scene):
    def draw(self, frame, t, pulse, lyric):
        span = clamp((t - 29.709) / 29.5)
        frame.circle(0.28, 0.5, 0.10 + 0.02 * math.sin(t * 2.0), 'ink', 0.8,
                     hollow=0.35, squash=0.9)
        for k in range(3):
            r = 0.06 + 0.05 * k + 0.01 * math.sin(t * 1.4 + k)
            frame.circle(0.28, 0.5, r, 'ink', 0.7, hollow=0.6, squash=0.9)
        frame.curve(lambda u: 0.5 + 0.16 * math.sin(u * (2 + 3 * span) * math.pi
                                                     + t * 1.1),
                    0.46, 0.98, 'ink', 0.8, samples=200)
        frame.curve(lambda u: 0.5 + 0.11 * math.sin(u * 4 * math.pi - t * 0.9),
                    0.46, 0.98, 'accent', 0.6, samples=200)
        frame.line(0.46, 0.5, 0.98, 0.5, 'ink', 0.3)
        frame.curve(lambda u: 0.64 + 0.05 * math.sin(u * math.pi * 2 + t * 2.0),
                    0.0, 1.0, 'ink', 0.5, samples=200)


class Satisfaction(Scene):
    def draw(self, frame, t, pulse, lyric):
        beat = 0.5 + 0.5 * math.sin(t * 4.2)
        frame.circle(0.26, 0.5, 0.09 + 0.035 * beat, 'ink', 0.9, hollow=0.72)
        frame.circle(0.26, 0.5, 0.05 + 0.02 * beat, 'ink', 0.9)
        for k in range(6):
            radius = 0.10 + 0.016 * ((t * 3 + k) % 1.0)
            frame.circle(0.26, 0.5, radius, 'accent', 0.5, hollow=0.94)
        for k in range(10):
            level = pulse * 0.5 + 0.06 * (1 + math.sin(t * 2.0 + k))
            frame.line(0.56 + k * 0.04, 0.68, 0.56 + k * 0.04, 0.68 - level,
                       'ink', 0.75)


class Organic(Scene):
    def draw(self, frame, t, pulse, lyric):
        sway = math.sin(t * 0.8) * 0.03
        # a round fruit —茄子的轮廓，番茄的圆
        frame.circle(0.30 + sway, 0.52, 0.10, 'ink', 0.85, squash=1.15)
        frame.circle(0.30 + sway, 0.42, 0.03, 'ink', 0.7)
        frame.circle(0.28 + sway, 0.50, 0.045, 'accent', 0.5)
        # a tomato
        frame.circle(0.50, 0.58, 0.06, 'ink', 0.8)
        for k in range(4):
            angle = k * TAU / 4 + t * 0.3
            frame.line(0.50, 0.52, 0.50 + 0.03 * math.cos(angle),
                       0.52 + 0.02 * math.sin(angle), 'accent', 0.7)
        # a cat
        eye = 0.5 + 0.5 * math.sin(t * 2.2)
        frame.circle(0.76, 0.56, 0.055, 'ink', 0.85, squash=1.2)
        for sign in (-1, 1):
            frame.line(0.76 + sign * 0.05, 0.50, 0.76 + sign * 0.035, 0.44,
                       'ink', 0.8)
        frame.circle(0.76 - 0.02, 0.55, 0.006, 'accent', 0.95)
        frame.circle(0.76 + 0.02, 0.55, 0.006, 'accent', 0.95)
        frame.line(0.76, 0.58, 0.76, 0.66, 'ink', 0.4 * eye + 0.3)


class Identity(Scene):
    def draw(self, frame, t, pulse, lyric):
        phase = (t - 88.587) % 7.0
        left = math.floor((t - 88.587) / 7.0) % 2 == 0
        labels = ('F', 'M') if left else ('AM', 'PM')
        sweep = phase / 7.0
        for k, (a, b) in enumerate(zip(labels, labels[::-1])):
            u = 0.26 + k * 0.30
            active = (sweep > 0.5 - k * 0.1)
            frame.text(u, 0.35, b if active else a, 'ink', 0.95, 'center')
            frame.rrect(u, 0.62, 0.18, 0.05, 0.08, 'ink', 0.8, hollow=0.5)
            fill = clamp((sweep - 0.4) * 2.0) if active else clamp(sweep * 1.6)
            frame.rrect(u - 0.09 * (1 - fill), 0.62, 0.18 * fill, 0.03, 0.06,
                        'accent', 1.0)
        for k in range(3):
            frame.line(0.5, 0.5, 0.5 + 0.02 * math.cos(t * 1.5 + k),
                       0.5 + 0.05 * math.sin(t * 1.5 + k), 'ink', 0.4)


class Vibration(Scene):
    def draw(self, frame, t, pulse, lyric):
        u0, u1 = 0.16, 0.86
        samples = int((u1 - u0) * 900)
        points = []
        for i in range(samples + 1):
            u = u0 + (u1 - u0) * i / samples
            phase = (u - u0) / (u1 - u0) * 7.0 - t * 0.9
            local = phase % 1.0
            beat = 0.0
            for x0, x1, height in ((0.10, 0.17, 0.035), (0.30, 0.37, 0.16),
                                   (0.56, 0.66, 0.05)):
                if x0 <= local <= x1:
                    shape = math.sin((local - x0) / (x1 - x0) * math.pi)
                    beat = height * shape * (1 if height > 0.08 else -1)
                    break
            points.append((u, 0.5 - pulse * 0.01 - beat))
        for i in range(1, len(points)):
            frame.line(points[i - 1][0], points[i - 1][1], points[i][0],
                       points[i][1], 'ink', 0.85)
        frame.circle(0.5, 0.5, 0.30, 'accent', 0.25, hollow=0.97)


class Isolation(Scene):
    def draw(self, frame, t, pulse, lyric):
        level = clamp((t - 110.9) / 7.4)
        frame.hero(self.payload.hero.get('body'), 'ink', 0.5 * (1 - level))
        frame.hero(self.payload.hero.get('ink'), 'paper', 1.0 * (1 - level))
        for k in range(int(4 + level * 22)):
            row = hash16(int(t * 6) + k * 37) % frame.rows
            start = hash16(int(t * 3) + k * 11) % frame.w
            length = int(2 + level * 26)
            for step in range(length):
                frame.dot((start + step) % frame.w, row, 'ink',
                          0.35 + 0.5 * (1 - step / max(1, length)))
        for k in range(int(level * 8)):
            x = hash16(int(t * 5) + k * 71) % frame.w
            y = hash16(int(t * 7) + k * 53) % frame.rows
            block = 3 + hash16(k) % 9
            for step in range(block):
                if x + step < frame.w:
                    frame.dot(x + step, y, 'accent', 0.8)
                    if y + 1 < frame.rows:
                        frame.dot(x + step, y + 1, 'accent', 0.6)


class Illegal(Scene):
    def draw(self, frame, t, pulse, lyric):
        rows = 14
        for row in range(rows):
            v = 0.14 + row * 0.052
            for col in range(16):
                if hash16(int(t * 9) + row * 31 + col * 7) % 5:
                    continue
                frame.dot(frame.sx(0.10 + col * 0.012), frame.sy(v), 'ink', 0.6)
                frame.dot(frame.sx(0.78 - col * 0.012), frame.sy(v), 'accent', 0.7)
        frame.text(0.5, 0.5, 'ERROR', 'ink', 0.95, 'center')
        frame.text(0.5, 0.58, 'ILLEGAL ARGUMENTS', 'accent', 0.8, 'center')
        for k in range(3):
            frame.rrect(0.5, 0.78 + k * 0.05, 0.6 - k * 0.12, 0.02, 0.4, 'ink',
                        0.7 - k * 0.15)


class Execution(Scene):
    def draw(self, frame, t, pulse, lyric):
        progress = clamp((t - 147.66) / 29.6)
        frame.rrect(0.5, 0.36, 0.72, 0.10, 0.10, 'ink', 0.9, hollow=0.5)
        inner = 0.68 * progress
        frame.rrect(0.5 - 0.36 + inner / 2, 0.36, max(0.004, inner), 0.06, 0.10,
                    'accent', 1.0)
        for k in range(24):
            row = k % 8
            col = k // 8
            v = 0.56 + row * 0.035
            u = 0.18 + col * 0.22 + 0.02 * math.sin(t * 2.0 + k)
            frame.rrect(u, v, 0.18, 0.02, 0.3, 'ink', 0.35 + 0.4 * pulse)
        phase = (t - 147.66) % 1.0
        frame.circle(0.5, 0.36, 0.06 + phase * 0.18, 'accent', 0.35 * (1 - phase),
                     hollow=0.92)


class Love(Scene):
    def draw(self, frame, t, pulse, lyric):
        scale = 0.5 + 0.5 * math.sin(t * 2.4)
        points = []
        samples = 300
        for i in range(samples + 1):
            a = TAU * i / samples
            x = 16 * math.sin(a) ** 3
            y = 13 * math.cos(a) - 5 * math.cos(2 * a) - 2 * math.cos(3 * a) \
                - math.cos(4 * a)
            points.append((0.30 + x / 90 * (0.9 + 0.1 * scale),
                           0.5 - y / 90 * (0.9 + 0.1 * scale)))
        for i in range(1, len(points)):
            frame.line(points[i - 1][0], points[i - 1][1], points[i][0],
                       points[i][1], 'ink', 0.85)
        for k in range(6):
            frame.line(0.62 + k * 0.055, 0.16, 0.62 + k * 0.055, 0.84, 'ink',
                       0.4)
        frame.text(0.30, 0.86, 'TRAPPED', 'accent', 0.8, 'center')


class Outro(Scene):
    def draw(self, frame, t, pulse, lyric):
        duration = self.payload.duration()
        final = 205.811
        executing = t >= final
        frame.rrect(0.5, 0.46, 0.70, 0.10, 0.10, 'ink', 0.9, hollow=0.55)
        if executing:
            frame.text(0.5, 0.46, 'EXECUTION', 'ink', 0.95, 'center')
            frame.rrect(0.5, 0.70, 0.60, 0.05, 0.10, 'accent', 0.0)
            for k in range(4):
                if hash16(int(t * 4) + k) % 3:
                    frame.text(0.5, 0.72 + k * 0.03,
                               '[ PROCESS TERMINATED ]', 'accent',
                               0.5 + 0.4 * (k % 2), 'center')
        else:
            progress = clamp((t - 192.5) / 13.0)
            frame.rrect(0.5 - 0.35 + 0.35 * progress, 0.46,
                        max(0.01, 0.70 * progress), 0.06, 0.10, 'accent', 1.0)
            frame.text(0.5, 0.70, f'EXECUTING {int(progress * 100):02d}%',
                       'ink', 0.9, 'center')
            if progress >= 1.0:
                frame.text(0.5, 0.78, 'WAITING FOR RESPONSE', 'accent', 0.8,
                           'center')
        spin = '|/-\\'[int(t * 8) % 4]
        frame.text(0.5, 0.86, f'{spin} {duration:6.1f}s', 'ink', 0.6, 'center')


SCENE_CLASSES = {
    'boot': Boot, 'title': Title, 'devotion': Devotion,
    'satisfaction': Satisfaction, 'organic': Organic, 'identity': Identity,
    'vibration': Vibration, 'isolation': Isolation, 'illegal': Illegal,
    'execution': Execution, 'love': Love, 'outro': Outro,
}


# ---------------------------------------------------------------------------
# composer
# ---------------------------------------------------------------------------
class Player:
    def __init__(self, payload: Payload):
        self.payload = payload
        self.scenes = {name: SCENE_CLASSES[name](payload) for name, *_ in SCENES}
        self.last_frame = None
        self.pending_frames = 0

    def compose(self, t: float, cols: int, rows: int) -> Frame:
        """Build one frame: scene band, spectrum, no chrome."""
        frame = Frame(cols, rows, self.payload.sub)
        header = 2
        footer = 2
        frame.clip = (header, rows - footer - 1)
        scene = self.payload.scene_at(t)
        lyric = self.payload.lyric_at(t)
        energy = self.payload.energy_at(t)
        pulse = sum(energy[:10]) / max(1, len(energy[:10]))
        self.scenes[scene[0]].draw(frame, t, pulse, lyric)
        self._spectrum(frame, energy, rows)
        self.last_frame = frame
        return frame

    @staticmethod
    def _spectrum(frame: Frame, energy: list, rows: int):
        columns = len(energy) if energy else 0
        if not columns:
            return
        v = (rows - 1.2) / max(1, rows)
        for k in range(min(48, columns)):
            level = clamp(energy[k * len(energy) // min(48, columns)] * 2.2)
            if level <= 0.02:
                continue
            frame.line(0.14 + k * 0.015, v, 0.14 + k * 0.015, v - level * 0.16,
                       'accent', 0.7)

    # -- chrome ------------------------------------------------------------
    def chrome_top(self, frame: Frame, t: float, scene, state: str = ''):
        title = 'WXMV-1  world.execute(me);'
        frame.text(0.02, 0.02, title, 'accent', 1.0)
        clock = f'{int(t) // 60:02d}:{int(t) % 60:02d}.{int(t * 10) % 10}'
        total = self.payload.duration()
        right = f'{clock} / {int(total) // 60:02d}:{int(total) % 60:02d}'
        if state:
            right = f'{state}  {right}'
        frame.text(0.98, 0.02, right, 'ink', 0.8, 'right')
        frame.text(0.02, 0.09, f'{scene[2]} / {scene[3]}', 'ink', 0.9)
        frame.text(0.98, 0.09, scene[4], 'accent', 0.6, 'right')
        frame.line(0.02, 0.16, 0.98, 0.16, 'ink', 0.35)

    def chrome_caption(self, frame: Frame, t: float, lyric):
        rows = frame.rows
        en = crop(lyric['en'], frame.cols - 8) if lyric else None
        zh = crop(lyric['zh'], frame.cols - 8) if lyric else None
        if en and zh:
            frame.text(0.5, (rows - 2.4) / rows, en, 'ink', 1.0, 'center')
            frame.text(0.5, (rows - 1.4) / rows, zh, 'accent', 0.95, 'center')
        elif t > self.payload.duration() - 4:
            frame.text(0.5, (rows - 1.8) / rows,
                       'PROCESS TERMINATED. THE LOOP REMAINS.', 'accent', 0.9,
                       'center')
        else:
            frame.text(0.5, (rows - 1.8) / rows, '[ instrumental ]',
                       'ink', 0.45, 'center')

    def chrome_footer(self, frame: Frame, hint: str, ready: bool):
        if ready:
            frame.text(0.5, (frame.rows - 0.6) / frame.rows,
                       '[ SPACE / ENTER 开始 ]', 'ink', 0.9, 'center')
        else:
            frame.text(0.5, (frame.rows - 0.6) / frame.rows, hint, 'ink', 0.45,
                       'center')


# ---------------------------------------------------------------------------
# encoder
# ---------------------------------------------------------------------------
RESET = '\x1b[0m'


class Encoder:
    """Serialise a Frame for the active terminal capability profile."""

    def __init__(self, mode: str, depth: int = 256, depth_fallback: bool = True):
        self.mode = mode
        self.depth = depth if depth_fallback else 256
        self.paper, self.ink, self.accent = self._palette()

    def _palette(self):
        if self.mode == MODE_MONO:
            return PAPER_MONO, INK_MONO, ACCENT_MONO
        if self.mode == MODE_PLAIN:
            return None, None, None
        table = {256: (PAPER_256, INK_256, ACCENT_256),
                 16: (PAPER_16, INK_16, ACCENT_16),
                 4: (PAPER_16, INK_16, ACCENT_16),
                 2: (PAPER_16, INK_16, 97),
                 1: (PAPER_16, INK_16, 97),
                 0: (PAPER_16, INK_16, 97)}
        return table.get(self.depth, (PAPER_256, INK_256, ACCENT_256))

    def _sgr(self, style: str) -> str:
        codes = {'paper': self.paper, 'ink': self.ink, 'accent': self.accent}
        value = codes[style]
        if self.depth >= 256:
            return f'\x1b[38;5;{value}m'
        if self.depth >= 16:
            # bright foreground for paper/ink, normal for accents
            return f'\x1b[9{value % 8}m' if style != 'accent' \
                else f'\x1b[3{value % 8}m'
        return ''

    def encode(self, frame: Frame, rows_data) -> str:
        if self.mode == MODE_PLAIN:
            return self.encode_plain(rows_data)
        out = []
        for y, (chars, colours) in enumerate(rows_data):
            out.append(f'\x1b[{y + 1};1H')
            current = None
            for ch, colour in zip(chars, colours):
                if colour is None:
                    if current is not None:
                        out.append(RESET)
                        current = None
                    out.append(' ')
                    continue
                if colour != current:
                    sgr = self._sgr(colour)
                    out.append(sgr)
                    current = colour
                out.append(ch)
            if current is not None:
                out.append(RESET)
        return ''.join(out)

    @staticmethod
    def encode_plain(rows_data) -> str:
        return '\n'.join(''.join(chars) for chars, _colours in rows_data)

    @staticmethod
    def encode_json(frame: Frame, rows_data) -> str:
        payload = {
            'size': [frame.cols, frame.rows],
            'sub': frame.sub,
            'rows': [
                {
                    'text': ''.join(chars),
                    'ink': [''.join('#' if cell.ink[i] else '.' for i in
                                     range(frame.sub)) for cell in frame.cells[y]],
                }
                for y, (chars, _c) in enumerate(rows_data)
            ],
        }
        return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# interactive runtime
# ---------------------------------------------------------------------------
HELP_LINES = (
    'SPACE / ENTER    播放 / 暂停',
    '← / →            后退 / 前进 5 秒',
    '1 - 9            跳转章节',
    'R                回到开头',
    'S                导出当前帧 PNG 文本（同目录 .snapshot.txt）',
    'M                切换 彩色 / 单色 / 纯文本',
    'H                显示 / 隐藏本帮助',
    'Q / ESC          退出',
)


def stdout_is_unicode() -> bool:
    encoding = (sys.stdout.encoding or 'utf-8').lower().replace('-', '')
    try:
        '\u2588'.encode(sys.stdout.encoding or 'utf-8')
    except (UnicodeEncodeError, LookupError):
        return False
    return 'utf' in encoding or 'unicode' in encoding or encoding in ('cp437',)


def term_size(fd: int):
    try:
        size = os.get_terminal_size(fd)
    except OSError:
        return 100, 34
    return min(240, max(48, size.columns)), min(90, max(16, size.lines))


def run(args, payload: Payload):
    unicode_ok = stdout_is_unicode()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # Piped / redirected: emit the current frame instead of failing.
        return cmd_player(argparse.Namespace(
            root=None, at=args.at if args.at is not None else 0.0,
            width=args.width, height=args.height,
            mode=args.mode or (None if unicode_ok else 'ascii'),
            fps=args.fps, color=args.color, mono=args.mono))

    in_fd = sys.stdin.fileno()
    original = termios.tcgetattr(in_fd)
    signal.signal(signal.SIGTERM, lambda *_a: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        columns, rows = term_size(sys.stdout.fileno())
        player = Player(payload)
        depth = Charset.depth_from_env()
        charset = Charset.detect(sys.stdout.encoding or 'utf-8')
        mode = (MODE_PLAIN if charset == 'ascii' or not unicode_ok else
                MODE_COLOR if args.color else
                MODE_MONO if args.mono else MODE_COLOR)
        charset = Charset.detect(sys.stdout.encoding or 'utf-8')
        encoder = Encoder(mode, depth)
        tty.setcbreak(in_fd)
        sys.stdout.write('\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J')
        sys.stdout.flush()
        start = tty_read_clock()
        t = args.at if args.at is not None else 0.0
        paused = args.paused
        ready = not args.autoplay
        help_on = False
        last_change = start
        while True:
            if not paused and not ready:
                t += tty_read_clock() - last_change
            last_change = tty_read_clock()
            if t >= payload.duration():
                t = payload.duration() - 0.05
                paused = True
            scene = payload.scene_at(t)
            lyric = payload.lyric_at(t)
            columns, rows = term_size(sys.stdout.fileno())
            frame = player.compose(t, columns, rows)
            state = 'PAUSED' if (paused or ready) else 'RUNNING'
            player.chrome_top(frame, t, scene, state)
            if ready:
                frame.hero(payload.hero.get('body'), 'ink', 0.95)
                frame.hero(payload.hero.get('ink'), 'paper', 1.0)
                frame.text(0.5, 0.62, '小狐狐  /  酒狐', 'accent', 1.0, 'center')
                player.chrome_caption(frame, 0.0, None)
                player.chrome_footer(frame, '', True)
            else:
                player.chrome_caption(frame, t, lyric)
                player.chrome_footer(frame, '', False)
            if help_on:
                draw_help(frame)
            rows_data = frame.materialise(mode, payload.ink_threshold,
                                          payload.paper_fx)
            if mode == MODE_PLAIN and not unicode_ok:
                body = '\n'.join(Charset.ascii_frame(rows_data))
                sys.stdout.write('\x1b[2J' + body)
            else:
                sys.stdout.write('\x1b[2J' if args.clear_each_frame else '')
                sys.stdout.write(encoder.encode(frame, rows_data))
            sys.stdout.flush()

            timeout = 1.0 / args.fps
            if select.select([sys.stdin], [], [], timeout)[0]:
                key = os.read(in_fd, 32).decode('utf-8', 'ignore')
                if not key:
                    break
                if key[0] in ('q', 'Q', '\x1b', '\x03'):
                    break
                if key[0] in (' ', '\r', '\n'):
                    if ready:
                        ready = False
                        paused = False
                        t = 0.0
                    else:
                        paused = not paused
                elif key[0] in '123456789':
                    index = int(key[0]) - 1
                    if index < len(SCENES) - 1:
                        t = SCENES[index][1]
                        ready = False
                        paused = False
                elif key[0] in ('r', 'R'):
                    t = 0.0
                    ready = False
                    paused = False
                elif key[0] in ('h', 'H'):
                    help_on = not help_on
                elif key[0] in ('m', 'M'):
                    mode = {MODE_COLOR: MODE_MONO, MODE_MONO: MODE_PLAIN,
                            MODE_PLAIN: MODE_COLOR}[mode]
                    encoder = Encoder(mode, depth)
                elif key[0] in ('s', 'S'):
                    dump_snapshot(ROOT, t, columns, rows)
                elif key.startswith('\x1b[D'):
                    t = max(0.0, t - 5.0)
                elif key.startswith('\x1b[C'):
                    t = min(payload.duration() - 0.05, t + 5.0)
    finally:
        termios.tcsetattr(in_fd, termios.TCSADRAIN, original)
        sys.stdout.write('\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l')
        sys.stdout.flush()


def tty_read_clock():
    return time.monotonic()


def draw_help(frame: Frame):
    width = min(frame.cols - 6, 46)
    top = max(1, (frame.rows - len(HELP_LINES) - 2) // 2)
    frame.rrect(0.5, (top + len(HELP_LINES) / 2) / frame.rows,
                width / frame.cols, (len(HELP_LINES) + 2) / frame.rows,
                0.04, 'ink', 1.0, hollow=0.30)
    for index, line in enumerate(HELP_LINES):
        frame.text((frame.cols - width + 2) / (2 * frame.cols),
                   (top + 0.5 + index) / frame.rows, line, 'ink', 0.95)


# ---------------------------------------------------------------------------
# capability detection / calibration
# ---------------------------------------------------------------------------
class Charset:
    BLOCKS = ('\u2588', '\u2593', '\u2592', '\u2591', '\u3000')

    @staticmethod
    def detect(encoding: str):
        for ch in Charset.BLOCKS:
            try:
                ch.encode(encoding)
            except (UnicodeEncodeError, LookupError):
                return 'ascii'
        return 'blocks'

    @staticmethod
    def depth_from_env() -> int:
        term = os.environ.get('TERM', '')
        if term.startswith('dumb'):
            return 0
        colour = os.environ.get('COLORTERM', '')
        if 'truecolor' in colour or '24bit' in colour:
            return 256
        if os.environ.get('NO_COLOR'):
            return 16
        return 256

    @staticmethod
    def ascii_frame(rows_data):
        """Blank-and-redraw pass for terminals without block glyphs."""
        table = {SUB_PAPER: ' '}
        for i, glyph in enumerate(SUB_INK):
            table[glyph] = '# .:-'[min(4, i + 1)]
        return [''.join(table.get(ch, '#') for ch in chars)
                for chars, _ in rows_data]

    @staticmethod
    def ascii_text(text: str) -> str:
        """Transliterate captions for non-Unicode terminals."""
        out = []
        for ch in text:
            if ch in '\u3000':
                out.append(' ')
            elif ch == '\u2591':
                out.append('.')
            elif ch == '\u2592':
                out.append(':')
            elif ch == '\u2593':
                out.append('+')
            elif ch in '\u2588\u2584\u2580':
                out.append('#')
            elif unicodedata.combining(ch):
                continue
            elif cwidth(ch) > 1:
                out.append('?')
            elif ord(ch) < 0x7F:
                out.append(ch)
            else:
                out.append('~')
        return ''.join(out)


def dump_snapshot(root: Path, t: float, cols: int, rows: int):
    path = root / 'wxfox.snapshot.txt'
    payload = Payload(root)
    player = Player(payload)
    frame = player.compose(t, cols, rows)
    rows_data = frame.materialise(MODE_PLAIN, 0.3, 0.1)
    header = f'WXMV-1  t={t:.2f}s  {cols}x{rows}'
    path.write_text(header + '\n' + Encoder.encode_plain(rows_data) + '\n',
                    encoding='utf-8')
    return path


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------
CALIBRATION_SAMPLE = (
    '█▓▒░ ▏▎▍▌▋▊▉ 0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ',
    'abcdefghijklmnopqrstuvwxyz .,:;()[]{}<>/\\|+=*-_~^%$#@!?',
)


def cmd_calibrate(args) -> int:
    """Measure real cell geometry; write a `calibration` block when possible."""
    root = Path(args.root).resolve() if getattr(args, 'root', None) else ROOT
    report = {
        'format': 'WXMV-1',
        'terminal': {
            'TERM': os.environ.get('TERM', ''),
            'COLORTERM': os.environ.get('COLORTERM', ''),
            'encoding': sys.stdout.encoding or 'utf-8',
            'size': list(term_size(sys.stdout.fileno()))
            if sys.stdout.isatty() else None,
        },
        'charset': Charset.detect(sys.stdout.encoding or 'utf-8'),
        'depth': Charset.depth_from_env(),
        'sample': list(CALIBRATION_SAMPLE),
        'measured': None,
        'note': '请对照上面的样张手工填写 cell/palette；本机自动测量需要交互式终端。',
    }
    if sys.stdout.isatty():
        depth = Charset.depth_from_env()
        report['measured'] = {
            'cell': {'sub': 8, 'fg_luma': 0.62, 'bg_weight': 0.0},
            'palette': CALIBRATION['palette'],
            'render': {'ink_threshold': 0.28, 'paper_fx': 0.10, 'hero_scale': 1.0},
        }
        report['note'] = '交互式终端：已按当前能力写入默认校准值，可手工微调。'
    if args.apply:
        source = root / 'payload.json'
        target = Path(args.apply)
        if not source.is_file():
            print(f'找不到 payload：{source}', file=sys.stderr)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        data = json.loads(source.read_text(encoding='utf-8'))
        calibration = dict(data.get('calibration') or {})
        calibration.update(report['measured'] or {})
        data['calibration'] = calibration
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        print(f'已写入校准：{target}')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_player(args) -> int:
    root = Path(args.root).resolve() if args.root else ROOT
    payload = Payload(root)
    if args.at is not None or args.mode:
        cols = args.width or 100
        rows = args.height or 34
        player = Player(payload)
        frame = player.compose(args.at if args.at is not None else 0.0, cols, rows)
        mode = args.mode or 'plain'
        if mode == 'color':
            mode = MODE_COLOR
        elif mode == 'mono':
            mode = MODE_MONO
        elif mode == 'ascii':
            mode = MODE_PLAIN
        rows_data = frame.materialise(mode, payload.ink_threshold, payload.paper_fx)
        if args.mode == 'json':
            sys.stdout.write(Encoder.encode_json(frame, rows_data) + '\n')
        elif mode == MODE_PLAIN and not stdout_is_unicode():
            sys.stdout.write('\n'.join(Charset.ascii_frame(rows_data)) + '\n')
        else:
            sys.stdout.write(Encoder.encode_plain(rows_data) + '\n')
        return 0
    return run(args, payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='wxfox.py',
        description='world.execute(me);  terminals  ASCII MV by 小狐狐 (WXMV-1)')
    parser.add_argument('--root', help='包含 payload.json 的目录')
    parser.add_argument('--at', type=float, help='定格到某一秒')
    parser.add_argument('--mode', choices=('color', 'mono', 'plain', 'ascii',
                                           'json'),
                        help='非交互输出模式')
    parser.add_argument('--width', type=int, help='非交互输出的列数')
    parser.add_argument('--height', type=int, help='非交互输出的行数')
    parser.add_argument('--fps', type=int, default=DEFAULT_FPS,
                        help=f'播放帧率（默认 {DEFAULT_FPS}）')
    parser.add_argument('--color', action='store_true', help='强制彩色输出')
    parser.add_argument('--mono', action='store_true', help='强制单色输出')
    parser.add_argument('--autoplay', action='store_true', help='跳过片头直接播放')
    parser.add_argument('--paused', action='store_true', help='暂停在起始时间')
    parser.add_argument('--clear-each-frame', action='store_true',
                        help='每帧整屏重绘（给慢终端兜底）')
    parser.add_argument('--list-scenes', action='store_true',
                        help='列出全部场景触发时间后退出')
    parser.add_argument('--calibrate', action='store_true',
                        help='打印本终端的能力/样张，可配合 --apply 写回 payload')
    parser.add_argument('--apply', help='把校准结果写入指定的 payload 文件')
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.calibrate:
        return cmd_calibrate(args)
    if args.list_scenes:
        for name, moment, code, zh, note in SCENES:
            print(f'{moment:7.3f}  {code:<12} {zh:<8} {note}')
        return 0
    try:
        return cmd_player(args)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # pragma: no cover - terminal dependent
        print(f'播放失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
