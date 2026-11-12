#!/usr/bin/env python3
"""The Day the Desk Got a Mail Slot (v2) - a ~21 s pixel-art film about Claudius's mailbox.

Everything is drawn in code on a 320x180 canvas (Riso palette of the barcik.training sites),
24 fps, then upscaled x6 with nearest-neighbour to 1920x1080. Music and sound effects are
synthesised with numpy. Source story: publications.barcik.training/claudius-mailbox

v2 over v1: a jointed arm (shoulder-elbow-hand, two-bone IK) and clear poses for Robert,
a glass tube with real rounded elbows that the capsule follows, eased motion everywhere
(trapezoid walks, eased camera, anticipation + squash on hops and the stamp), a proper
walk cycle with bob and lagging head/mailbag, blinks and idle breathing, dust puffs,
a flight path that passes through the open window without vanishing, and a soft-limited mix.

Usage (training-ops venv has Pillow + numpy):
  python make_film.py film  <frames_dir> <out_dir>     # frames + wav + mp4 + gif + poster + sheet
  python make_film.py check <out.png> 1.0 2.5 7.6 ...   # contact sheet of single frames (for review)
  python make_film.py seq   <out.png> <t0> <n> [x0 y0 w h]  # n consecutive frames (motion review)
  python make_film.py times                             # print the beat timeline
"""
import math, os, random, subprocess, sys, wave
from PIL import Image, ImageDraw
import numpy as np

W, H, WW, FPS = 320, 180, 524, 24
FLOOR, BAND = 136, 158
DUR = 21.25

# ---- palette (riso.css) ---------------------------------------------------------------------
NAVY = (29, 42, 107); INK = (18, 26, 71); BLUE = (42, 75, 215); MUTED = (82, 91, 140)
PINK = (255, 95, 162); PINKD = (200, 30, 99); ORANGE = (255, 122, 61); ORANGED = (214, 84, 30)
YELLOW = (255, 225, 77); YELLOWD = (236, 184, 44); GREEN = (29, 154, 108); GREEND = (18, 112, 80)
PAPER = (243, 239, 228); PANEL = (251, 248, 240); CREAM = (255, 243, 214); WHITE = (255, 255, 255)
SKIN = (246, 201, 160); SKIND = (220, 160, 122)
DOT = (229, 223, 206); GROOVE = (243, 226, 186); GLASS = (214, 224, 246); GLASSH = (240, 245, 255)
GLASSD = (186, 198, 232)
SKY = (168, 205, 255); DUSK = (255, 160, 150); NIGHT = (22, 28, 78); FLOORC = (40, 54, 126)
SWEATER = (34, 58, 170)
SLEEVE = (58, 92, 228)


def clamp(v, a=0.0, b=1.0): return max(a, min(b, v))
def seg(t, a, b): return clamp((t - a) / (b - a))
def smooth(u): return u * u * (3 - 2 * u)
def smoother(u): return u * u * u * (u * (u * 6 - 15) + 10)
def ease_out(u, p=3): return 1 - (1 - u) ** p
def ease_in(u, p=2): return u ** p
def back_out(u, s=1.8): u -= 1; return 1 + u * u * ((s + 1) * u + s)
def lerp(a, b, u): return a + (b - a) * u
def mix(c1, c2, u): return tuple(int(round(lerp(a, b, u))) for a, b in zip(c1, c2))
def R(v): return int(math.floor(v + 0.5))


def trap(u, a=0.18):
    """Distance fraction for a trapezoid speed profile: accelerate, cruise, decelerate."""
    u = clamp(u); vmax = 1.0 / (1.0 - a)
    if u < a: return 0.5 * vmax * u * u / a
    if u > 1 - a: return 1 - 0.5 * vmax * (1 - u) ** 2 / a
    return vmax * (u - a / 2)


def trap_speed(u, a=0.18):
    u = clamp(u)
    if u < a: return u / a
    if u > 1 - a: return (1 - u) / a
    return 1.0


# ---- timeline (seconds) ---------------------------------------------------------------------
T_POP, T_FUN, T_SPLIT, T_TRAY = 0.6, 1.2, 1.9, 2.4
PIPE_DUR = 2.1
T_PIPE_END = T_SPLIT + PIPE_DUR
T_DESK = T_PIPE_END + 0.25
WAIT0, WAIT1 = 4.7, 6.9
T_SAY0, T_SAY1 = 6.9, 8.2
T_WAKE = 7.45
X_TRAY = 155                                      # where Claudius stands to fetch and read
WALK1 = (7.95, 222, X_TRAY, 1.2)                   # (start, x0, x1, duration)
T_W1E = WALK1[0] + WALK1[3]
T_READ0, T_READ1 = T_W1E + 0.3, T_W1E + 1.35
WALK2 = (T_READ1, X_TRAY, 209, 1.0)
T_W2E = WALK2[0] + WALK2[3]
T_TYPE0, T_TYPE1 = T_W2E + 0.15, T_W2E + 1.65
T_TAKE = T_TYPE1 + 0.25
WALK3 = (T_TAKE, 209, 326, 1.6)
T_W3E = WALK3[0] + WALK3[3]
T_PLACED = T_W3E + 0.35
T_REACH = T_PLACED + 0.5
T_LIFT = T_REACH + 0.3
T_SLAM = T_LIFT + 0.45
T_ENV = T_SLAM + 0.55
T_GRAB = T_ENV + 0.15
T_TOSS = T_GRAB + 0.3
T_FLY0 = T_TOSS + 0.2
T_FLY1 = T_FLY0 + 1.4
T_END = T_FLY1 - 0.1
CAM = [(0, 0), (2.5, 0), (4.0, 182), (7.6, 182), (9.0, 0), (13.5, 0), (14.9, 204), (DUR, 204)]
CAPS = [(0.15, 2.5, "A LETTER ARRIVES AT THE MAIL SLOT."),
        (2.5, 4.6, "ONE COPY GOES STRAIGHT TO ROBERT."),
        (4.6, 6.9, "THE OTHER WAITS AS A FILE. NOBODY RUSHES."),
        (6.9, T_W1E, "UNTIL ROBERT OPENS A SESSION."),
        (T_W1E, T_W2E, "CLAUDIUS READS THE LETTER..."),
        (T_W2E, T_TAKE, "...AND TYPES A DRAFT."),
        (T_TAKE, T_SLAM, "NOTHING LEAVES WITHOUT ROBERT'S YES."),
        (T_SLAM, T_END, "APPROVED. SENT. SIGNED AS WRITTEN BY AN AI."),
        (T_END + 0.3, DUR, "PUBLICATIONS.BARCIK.TRAINING/CLAUDIUS-MAILBOX")]
BLINKS_C = [1.3, 3.9, 8.75, 10.3, 12.5, 14.1, 15.9, 16.35, 18.4, 20.3]
BLINKS_R = [0.9, 3.2, 5.6, 9.4, 11.2, 13.0, 15.0, 17.6, 20.0]


def blinking(t, table): return any(b <= t < b + 0.09 for b in table)


# ---- fonts ----------------------------------------------------------------------------------
F5 = {k: v.split('/') for k, v in {
 'A': ".###./#...#/#...#/#####/#...#/#...#/#...#", 'B': "####./#...#/#...#/####./#...#/#...#/####.",
 'C': ".###./#...#/#..../#..../#..../#...#/.###.", 'D': "####./#...#/#...#/#...#/#...#/#...#/####.",
 'E': "#####/#..../#..../####./#..../#..../#####", 'F': "#####/#..../#..../####./#..../#..../#....",
 'G': ".###./#...#/#..../#.###/#...#/#...#/.###.", 'H': "#...#/#...#/#...#/#####/#...#/#...#/#...#",
 'I': ".###./..#../..#../..#../..#../..#../.###.", 'J': "..###/...#./...#./...#./...#./#..#./.##..",
 'K': "#...#/#..#./#.#../##.../#.#../#..#./#...#", 'L': "#..../#..../#..../#..../#..../#..../#####",
 'M': "#...#/##.##/#.#.#/#.#.#/#...#/#...#/#...#", 'N': "#...#/##..#/#.#.#/#..##/#...#/#...#/#...#",
 'O': ".###./#...#/#...#/#...#/#...#/#...#/.###.", 'P': "####./#...#/#...#/####./#..../#..../#....",
 'Q': ".###./#...#/#...#/#...#/#.#.#/#..#./.##.#", 'R': "####./#...#/#...#/####./#.#../#..#./#...#",
 'S': ".####/#..../#..../.###./....#/....#/####.", 'T': "#####/..#../..#../..#../..#../..#../..#..",
 'U': "#...#/#...#/#...#/#...#/#...#/#...#/.###.", 'V': "#...#/#...#/#...#/#...#/#...#/.#.#./..#..",
 'W': "#...#/#...#/#...#/#.#.#/#.#.#/##.##/#...#", 'X': "#...#/#...#/.#.#./..#../.#.#./#...#/#...#",
 'Y': "#...#/#...#/.#.#./..#../..#../..#../..#..", 'Z': "#####/....#/...#./..#../.#.../#..../#####",
 '.': "...../...../...../...../...../.##../.##..", ',': "...../...../...../...../..#../..#../.#...",
 '!': "..#../..#../..#../..#../..#../...../..#..", '?': ".###./#...#/....#/...#./..#../...../..#..",
 "'": "..#../..#../...../...../...../...../.....", ':': "...../.##../.##../...../.##../.##../.....",
 '-': "...../...../...../.###./...../...../.....", '/': "....#/....#/...#./..#../.#.../#..../#....",
 '@': ".###./#...#/#.###/#.#.#/#.###/#..../.###.", ' ': "...../...../...../...../...../...../.....",
}.items()}
F3 = {k: v.split('/') for k, v in {
 'A': ".#./#.#/###/#.#/#.#", 'I': "###/.#./.#./.#./###", 'N': "##./#.#/#.#/#.#/#.#",
 'O': "###/#.#/#.#/#.#/###", 'K': "#.#/#.#/##./#.#/#.#", 'Z': "###/..#/.#./#../###",
 'X': "#.#/#.#/.#./#.#/#.#", '2': "###/..#/###/#../###", ' ': ".../.../.../.../...",
}.items()}

INS = {0: [], 1: [1], 2: [2, 1], 3: [3, 2, 1], 4: [4, 2, 1, 1]}


class G:
    """Pixel canvas: inclusive rects, rounded rects, text."""
    def __init__(self, im):
        self.im = im; self.d = ImageDraw.Draw(im)

    def r(self, x0, y0, x1, y1, c):
        if x1 < x0: x0, x1 = x1, x0
        if y1 < y0: y0, y1 = y1, y0
        self.d.rectangle([x0, y0, x1, y1], fill=c)

    def p(self, x, y, c): self.d.point((x, y), fill=c)
    def l(self, x0, y0, x1, y1, c, w=1): self.d.line([x0, y0, x1, y1], fill=c, width=w)

    def rr(self, x, y, w, h, c, r=1):
        ins = INS[r]
        for i in range(h):
            k = min(i, h - 1 - i)
            n = ins[k] if k < len(ins) else 0
            if w - 2 * n > 0: self.r(x + n, y + i, x + w - 1 - n, y + i, c)

    def orr(self, x, y, w, h, c, r=1, out=NAVY):
        self.rr(x - 1, y - 1, w + 2, h + 2, out, min(4, r + 1) if r else 0)
        self.rr(x, y, w, h, c, r)

    def box(self, x0, y0, x1, y1, c, out=NAVY):
        self.r(x0, y0, x1, y1, out); self.r(x0 + 1, y0 + 1, x1 - 1, y1 - 1, c)

    def text(self, x, y, s, c, scale=1, font=F5):
        fw = len(next(iter(font.values()))[0])
        for ch in s:
            gl = font.get(ch)
            if gl:
                for ry, row in enumerate(gl):
                    for rx, v in enumerate(row):
                        if v == '#':
                            self.r(x + rx * scale, y + ry * scale, x + rx * scale + scale - 1, y + ry * scale + scale - 1, c)
            x += (fw + 1) * scale


def tw(s, scale=1, fw=5): return (len(s) * (fw + 1) - 1) * scale


class Pen:
    """Local sprite coordinates around (cx, cy), mirrored when d == -1."""
    def __init__(self, g, cx, cy, d=1): self.g, self.cx, self.cy, self.d = g, cx, cy, d
    def wx(self, x): return self.cx + self.d * x
    def X(self, x0, x1): return (self.cx + x0, self.cx + x1) if self.d > 0 else (self.cx - x1, self.cx - x0)
    def r(self, x0, y0, x1, y1, c):
        a, b = self.X(min(x0, x1), max(x0, x1)); self.g.r(a, self.cy + y0, b, self.cy + y1, c)
    def p(self, x, y, c): self.g.p(self.wx(x), self.cy + y, c)
    def l(self, x0, y0, x1, y1, c, w=1): self.g.l(self.wx(x0), self.cy + y0, self.wx(x1), self.cy + y1, c, w)
    def rr(self, x, y, w, h, c, r=1): self.g.rr(self.X(x, x + w - 1)[0], self.cy + y, w, h, c, r)
    def orr(self, x, y, w, h, c, r=1, out=NAVY): self.g.orr(self.X(x, x + w - 1)[0], self.cy + y, w, h, c, r, out)


def track(t, keys):
    """Piecewise motion. keys = [(time, (x, y)), (time, (x, y), ease, arc), ...]; ease/arc shape the
    segment that arrives at that key. Returns float (x, y)."""
    if t <= keys[0][0]: return keys[0][1]
    for k0, k1 in zip(keys, keys[1:]):
        if t < k1[0]:
            ez = k1[2] if len(k1) > 2 else smooth
            arc = k1[3] if len(k1) > 3 else 0
            u = ez(seg(t, k0[0], k1[0]))
            p0, p1 = k0[1], k1[1]
            return lerp(p0[0], p1[0], u), lerp(p0[1], p1[1], u) - arc * math.sin(math.pi * clamp(u))
    return keys[-1][1]


def Rp(p): return R(p[0]), R(p[1])


# ---- small props ----------------------------------------------------------------------------
def envelope(g, cx, cy, tag=False):
    x0, y0 = cx - 6, cy - 4
    g.box(x0, y0, x0 + 12, y0 + 8, PANEL)
    g.l(x0 + 1, y0 + 1, cx, cy, MUTED); g.l(x0 + 11, y0 + 1, cx, cy, MUTED)
    g.r(cx - 1, cy, cx, cy + 1, PINK)
    if tag:
        tx, ty = cx, cy + 2
        g.box(tx, ty, tx + 10, ty + 8, YELLOW)
        g.text(tx + 2, ty + 2, "AI", NAVY, font=F3)


def mini_env(g, cx, cy):
    g.box(cx - 3, cy - 2, cx + 2, cy + 2, PANEL)
    g.p(cx - 1, cy, PINK); g.p(cx, cy, PINK)


def paper(g, cx, cy, check=False):
    x0, y0 = cx - 4, cy - 5
    g.box(x0, y0, x0 + 8, y0 + 10, PANEL)
    for i, n in enumerate((5, 4, 5, 3)):
        g.r(x0 + 2, y0 + 2 + 2 * i, x0 + 1 + n, y0 + 2 + 2 * i, MUTED)
    if check:
        for dx, dy in ((2, 6), (3, 7), (4, 6), (5, 5), (6, 4), (3, 6), (4, 5), (5, 4), (6, 3)):
            g.p(x0 + dx, y0 + dy, GREEN)


def flat_paper(g, x0, y0, check=False):
    g.box(x0, y0, x0 + 13, y0 + 6, PANEL)
    g.r(x0 + 2, y0 + 2, x0 + 8, y0 + 2, MUTED); g.r(x0 + 2, y0 + 4, x0 + 6, y0 + 4, MUTED)
    if check:
        for dx, dy in ((7, 3), (8, 4), (9, 3), (10, 2), (11, 1), (8, 3), (9, 2), (10, 1)):
            g.p(x0 + dx, y0 + dy, GREEN)


def sparkle(g, x, y, c, n=1):
    g.p(x, y, WHITE)
    for k in range(1, n + 1):
        for dx, dy in ((k, 0), (-k, 0), (0, k), (0, -k)): g.p(x + dx, y + dy, c)


def puff(g, x, y, u, c=PANEL, spread=7):
    """Two little dust clouds kicked out sideways from (x, y); u runs 0..1 over the puff."""
    if not 0 <= u < 1: return
    e = ease_out(u, 2)
    for sgn in (-1, 1):
        px, py = x + sgn * R(2 + spread * e), y - R(2 * e)
        if u < 0.45: g.r(px - 1, py - 1, px, py, c)
        elif u < 0.8: g.p(px, py, c); g.p(px + sgn, py + 1, c)
        else: g.p(px, py, c)
        if u < 0.6: g.p(px - sgn * 2, py + 1, c)


def bubble(g, x, y, s, tail):
    w, h = tw(s) + 9, 13
    g.orr(x, y, w, h, PANEL, 2)
    tx, ty = tail
    for i in range(6):
        g.r(tx - 1 - i // 2 + i, y + h + i, tx + 5 - i, y + h + i, NAVY)
    for i in range(5):
        g.r(tx - i // 2 + i, y + h - 1 + i, tx + 4 - i, y + h - 1 + i, PANEL)
    g.text(x + 5, y + 3, s, NAVY)


def badge_mark(g, x, y, ch='!', col=YELLOW):
    g.orr(x - 3, y - 5, 7, 11, col, 1)
    g.text(x - 2, y - 3, ch, NAVY)


# ---- Claudius -------------------------------------------------------------------------------
def _walk_bob(ph):
    a = abs(math.sin(ph))
    return 1 if a > 0.85 else (-1 if a < 0.4 else 0)


def claudius(g, cx, fy, d=1, pose='idle', t=0.0, walk=None, energy=1.0, eyes='open', look=0, front=False,
             lift=0, smile=False, sq=0, breathe=True):
    """Draws Claudius with feet on fy. walk = gait phase (radians), energy = 0..1 stride strength,
    sq = +1 squash / -1 stretch (head offset). Returns the world position of his front hand."""
    shrink = min(4, lift // 2)
    for hw, yy in ((10 - shrink, fy + 1), (8 - shrink, fy + 2), (5 - shrink, fy + 3)):
        for x in range(cx - hw, cx + hw + 1):
            if (x + yy) % 2 == 0: g.p(x, yy, INK)
    fy -= lift
    p = Pen(g, cx, fy, 1 if front else d)
    b = hb = la = lb = fa = fb = swing = bagx = bagy = 0
    if walk is not None:
        s, c = math.sin(walk), math.cos(walk)
        la = R(3.6 * s); lb = -la
        fa = R(1.6 * max(0.0, c) * energy); fb = R(1.6 * max(0.0, -c) * energy)
        if energy > 0.35:
            b, hb, bagy = _walk_bob(walk), _walk_bob(walk - 0.6), _walk_bob(walk - 1.1)
        bagx = R(1.2 * math.sin(walk - 0.9) * energy)
        swing = R(2 * s)
    elif pose == 'sleep':
        hb = 1 + (1 if math.sin(t * 2.4) > 0 else 0)
    elif breathe and pose in ('idle', 'hold', 'read', 'wave'):
        hb = 1 if math.sin(t * 2.5 + cx * 0.1) > 0.55 else 0
    hb = max(b - 1, min(b + 1, hb)) if pose != 'sleep' else hb

    def leg(x0, dx, up, col, foot=1):
        p.r(x0 + dx, -9, x0 + dx + 2, -3 - up, col)
        if foot > 0: p.r(x0 + dx, -2 - up, x0 + dx + 3, -1 - up, col)
        else: p.r(x0 + dx - 1, -2 - up, x0 + dx + 2, -1 - up, col)

    def arm(sx, sy, hx, hy):
        p.l(sx, sy, hx, hy, NAVY, 2)
        p.rr(hx - 2, hy - 2, 5, 5, NAVY, 1); p.r(hx - 1, hy - 1, hx + 1, hy + 1, YELLOW)

    if front:
        leg(-5, 0, 0, NAVY, -1); leg(2, 0, 0, NAVY, 1)
    else:
        leg(1, la, fa, MUTED); leg(-4, lb, fb, NAVY)
    back = None
    if front:
        fh = (11, -22 - 2 * (int(t * 7) % 2)) if pose == 'wave' else (9, -8)
        back = (-10, -8)
    elif pose == 'hold': fh, back = (10, -12 + b), (8, -11 + b)
    elif pose == 'read': fh, back = (9, -18 + b), (7, -17 + b)
    elif pose == 'type':
        k = int(t * 11) % 2
        fh, back = (10, -12 + k), (7, -11 - k)
    elif pose == 'wave': fh = (10, -23 - 2 * (int(t * 7) % 2))
    elif pose == 'toss': fh = (9, -27)
    elif walk is not None: fh = (6 - swing, -8 + b)
    else: fh = (7, -8 + b)
    if front: arm(-7, -14, *back)
    # body
    p.orr(-7, -16 + b, 14, 10, ORANGE, 2)
    p.r(-5, -7 + b, 4, -7 + b, ORANGED); p.r(-6, -8 + b, 5, -8 + b, ORANGED)
    if front: p.r(-2, -13, 1, -11, CREAM); p.p(-1, -12, PINK)
    else: p.r(1, -13 + b, 4, -11 + b, CREAM); p.p(2, -12 + b, PINK); p.p(3, -12 + b, PINK)
    p.l(5, -15 + b, -6 + bagx, -10 + b + bagy, PINKD)
    # mailbag on the back hip (lags the bob, swings a little)
    p.orr(-12 + bagx, -12 + b + bagy, 7, 6, PINK, 1)
    p.r(-12 + bagx, -10 + b + bagy, -6 + bagx, -10 + b + bagy, PINKD); p.p(-9 + bagx, -9 + b + bagy, YELLOW)
    # head (lags the body by a fraction of a step)
    hy = -31 + hb + sq
    p.orr(-10, hy, 20, 15, YELLOW, 3)
    p.r(-7, hy + 14, 6, hy + 14, YELLOWD); p.r(-8, hy + 13, 7, hy + 13, YELLOWD)
    p.r(-6, hy + 1, -3, hy + 1, CREAM)
    vx, vw = (-7, 14) if front else (-4, 13)
    p.rr(vx, hy + 3, vw, 8, INK, 2)
    e1, e2 = (vx + 3, vx + 9) if front else (vx + 3 + look, vx + 8 + look)
    for ex in (e1, e2):
        if eyes == 'open': p.r(ex, hy + 5, ex + 1, hy + 8, CREAM)
        elif eyes == 'wide': p.r(ex, hy + 4, ex + 1, hy + 9, CREAM)
        elif eyes == 'happy':
            p.p(ex - 1, hy + 7, CREAM); p.r(ex, hy + 6, ex + 1, hy + 6, CREAM); p.p(ex + 2, hy + 7, CREAM)
        else: p.r(ex - 1, hy + 7, ex + 2, hy + 7, MUTED if eyes == 'sleep' else CREAM)
    if smile: p.r(e1 + 3, hy + 9, e2 - 2, hy + 9, PINK)
    # laurel wreath (top leaves trail the head by one more pixel on fast moves)
    for i, (lx, ly) in enumerate(((-13, hy + 5), (-13, hy + 1), (-11, hy - 2), (-8, hy - 4), (-4, hy - 5))):
        ly += (1 if (walk is not None and i >= 3 and hb < b) else 0)
        for x in (lx, -1 - lx - 2):
            p.r(x, ly, x + 2, ly + 1, GREEN)
            inner = x + 2 if x < 0 else x
            p.p(inner, ly + 1, GREEND); p.p(x + 1, ly - 1, GREEN)
    if back and not front: arm(-2, -13 + b, *back)
    arm(6 if front else 4, -14 + b, *fh)
    return (p.wx(fh[0]), fy + fh[1])


# ---- Robert ---------------------------------------------------------------------------------
SHOULDER = (384.0, 88.0)
ARM_L = 14.0


def elbow_for(hand, s=SHOULDER, l1=ARM_L, l2=ARM_L):
    """Two-bone IK. The elbow always bends to the same side (down/back), so it never flips."""
    dx, dy = hand[0] - s[0], hand[1] - s[1]
    d = max(1e-3, math.hypot(dx, dy))
    if d > l1 + l2 - 0.01:
        k = (l1 + l2 - 0.01) / d; dx, dy, d = dx * k, dy * k, l1 + l2 - 0.01
    ux, uy = dx / d, dy / d
    a = (l1 * l1 - l2 * l2 + d * d) / (2 * d); h = math.sqrt(max(0.0, l1 * l1 - a * a))
    return s[0] + a * ux + h * uy, s[1] + a * uy - h * ux


def robert_body(g, look='fwd', mouth=0, blink=False):
    g.orr(371, 74, 27, 40, MUTED, 3)                       # chair back
    g.r(394, 78, 396, 110, mix(MUTED, NAVY, 0.35))
    g.orr(373, 81, 22, 30, SWEATER, 3)                     # sweater (darker than the sleeve)
    g.r(376, 81, 391, 81, mix(SWEATER, BLUE, 0.6))
    g.r(388, 84, 389, 108, mix(SWEATER, NAVY, 0.35))       # back fold
    g.r(380, 78, 386, 81, SKIN); g.r(379, 81, 387, 82, PANEL)
    g.orr(377, 63, 13, 14, SKIN, 3)                        # head, facing left
    g.rr(376, 61, 15, 5, INK, 2); g.r(387, 64, 390, 73, INK); g.r(385, 65, 386, 67, INK)
    g.p(386, 71, SKIND); g.p(377, 71, SKIND); g.p(376, 71, SKIN)
    g.r(378, 67, 382, 67, NAVY); g.r(378, 71, 382, 71, NAVY); g.r(378, 67, 378, 71, NAVY); g.r(382, 67, 382, 71, NAVY)
    g.r(383, 68, 386, 68, NAVY)
    if blink:
        g.r(379, 68, 381, 70, SKIN); g.r(379, 70, 381, 70, INK)
    else:
        ey = 69 + (1 if look == 'down' else -1 if look == 'up' else 0)
        g.r(379, 68, 381, 70, PANEL); g.r(379, ey, 380, ey, INK) if look == 'fwd' else g.p(380, ey, INK)
    if mouth == 2: g.r(378, 73, 380, 75, INK)
    elif mouth == 1: g.r(378, 74, 380, 75, INK)
    else: g.r(378, 74, 380, 74, PINKD)


def robert_arm(g, hand, point=False):
    """Near arm: shoulder -> elbow -> hand, outlined as one shape so the joint reads cleanly."""
    e = elbow_for(hand)
    pts = []
    for (a, b) in ((SHOULDER, e), (e, hand)):
        n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1]) * 2))
        pts += [(R(lerp(a[0], b[0], k / n)), R(lerp(a[1], b[1], k / n))) for k in range(n + 1)]
    pts = list(dict.fromkeys(pts))
    for x, y in pts: g.r(x - 2, y - 2, x + 2, y + 2, NAVY)
    for x, y in pts: g.r(x - 1, y - 1, x + 1, y + 1, SLEEVE)
    ex, ey = R(e[0]), R(e[1])
    g.p(ex, ey, mix(BLUE, NAVY, 0.4))
    hx, hy = R(hand[0]), R(hand[1])
    g.rr(hx - 2, hy - 2, 5, 5, NAVY, 1); g.r(hx - 1, hy - 1, hx + 1, hy + 1, SKIN)
    g.r(hx + 1, hy - 1, hx + 1, hy + 1, BLUE)                     # cuff
    if point:
        g.r(hx - 5, hy - 2, hx - 2, hy, NAVY); g.r(hx - 4, hy - 1, hx - 2, hy - 1, SKIN)


def mug(g, hx, hy):
    g.box(hx - 6, hy - 4, hx - 1, hy + 3, PINK); g.r(hx - 5, hy - 3, hx - 2, hy - 3, PINKD)


def stamp(g, x, y, squash=0):
    g.r(x - 2, y - 10 + squash, x + 2, y - 8 + squash, NAVY); g.r(x - 1, y - 9 + squash, x + 1, y - 9 + squash, ORANGE)
    g.r(x - 1, y - 7 + squash, x + 1, y - 5, NAVY); g.p(x, y - 6, ORANGE)
    w = 5 + squash
    g.r(x - w, y - 4 + squash, x + w, y - 1, NAVY); g.r(x - w + 1, y - 3 + squash, x + w - 1, y - 2, ORANGE)
    g.r(x - w + 1, y, x + w - 1, y, GREEN)


# ---- room -----------------------------------------------------------------------------------
def background(g, nraw):
    g.r(0, 0, WW, 135, PAPER)
    for y in range(22, 100, 6):
        for x in range((y // 6 % 2) * 3, WW, 6): g.p(x, y, DOT)
    g.r(0, 0, WW, 4, MUTED); g.r(0, 5, WW, 5, NAVY)
    g.r(0, 102, WW, 135, CREAM); g.r(0, 101, WW, 101, NAVY); g.r(0, 102, WW, 103, PANEL)
    for x in range(4, WW, 10): g.r(x, 105, x, 131, GROOVE)
    g.r(0, 132, WW, 135, MUTED); g.r(0, 132, WW, 132, NAVY)
    g.r(0, FLOOR, WW, H, FLOORC); g.r(0, FLOOR, WW, FLOOR, MUTED)
    for i, y in enumerate((143, 150, 157)):
        g.r(0, y, WW, y, NAVY)
        for x in range(12 + 23 * i, WW, 58): g.r(x, y - 6, x, y - 1, NAVY)
    # daylight falling through the two windows onto the floor (fades with the night)
    day = 1 - nraw
    if day > 0.15:
        lit = mix(FLOORC, SKY, 0.28)
        for (xa, xb) in ((240, 290), (470, 512)):
            for y in range(FLOOR + 2, BAND):
                sh = R((y - FLOOR) * 0.9)
                for x in range(xa + sh, xb + sh):
                    if day > 0.6: on = (x + y) % 2 == 0
                    else: on = x % 2 == 0 and y % 2 == 0
                    if on: g.p(x, y, lit)


def sky_tile(w, h, wp, t, seed=0):
    nraw = smooth(seg(wp, 0.08, 0.38)) - smooth(seg(wp, 0.62, 0.92))
    dusk = 4 * nraw * (1 - nraw)
    col = mix(mix(SKY, NIGHT, nraw), DUSK, dusk * 0.75)
    im = Image.new('RGB', (w, h), col); g = G(im)
    if nraw > 0.55:
        rnd = random.Random(7 + seed)
        for i in range(12):
            x, y = rnd.randrange(2, w - 2), rnd.randrange(2, h - 12)
            if (int(t * 9) + i) % 6: g.p(x, y, CREAM)
    sx, sy = w - 15, 11 + R(70 * nraw)
    g.d.ellipse([sx - 5, sy - 5, sx + 5, sy + 5], fill=mix(YELLOW, ORANGE, dusk))
    mx, my = 13, 12 + R(70 * (1 - nraw))
    g.d.ellipse([mx - 5, my - 5, mx + 5, my + 5], fill=CREAM)
    g.d.ellipse([mx - 2, my - 7, mx + 7, my + 2], fill=col)
    cc = mix(WHITE, col, 0.75 * nraw + 0.3 * dusk)
    for i, (bx, by) in enumerate(((6, 9), (30, 22))):
        x = int((bx + seed * 9 + t * (2.2 + i)) % (w + 24)) - 14
        g.rr(x, by, 14, 4, cc, 2); g.rr(x + 3, by - 2, 7, 3, cc, 1)
    hc, hc2 = mix(GREEN, INK, 0.72 * nraw), mix(GREEND, INK, 0.72 * nraw)
    for x in range(w):
        g.r(x, h - 7 - int(3 * math.sin(x / 8.0 + 1 + seed)), x, h, hc)
        g.r(x, h - 3 - int(2 * math.sin(x / 5.0 + 4 + seed)), x, h, hc2)
    return im, nraw


def night_level(wp):
    return smooth(seg(wp, 0.08, 0.38)) - smooth(seg(wp, 0.62, 0.92))


def windows(g, t, wp):
    tile, nraw = sky_tile(49, 53, wp, t)
    g.r(230, 28, 284, 86, NAVY); g.im.paste(tile, (233, 31))
    g.r(256, 31, 258, 83, NAVY); g.r(233, 56, 281, 57, NAVY)
    g.r(227, 86, 287, 89, NAVY); g.r(228, 87, 286, 87, PANEL)
    tile2, _ = sky_tile(41, 49, wp, t, seed=3)
    g.r(461, 26, 507, 82, NAVY); g.im.paste(tile2, (464, 29))
    g.r(458, 82, 510, 85, NAVY); g.r(459, 83, 509, 83, PANEL)
    for x0 in (449, 508):                                    # open shutters
        g.box(x0, 26, x0 + 11, 82, PINK)
        for y in range(30, 80, 4): g.r(x0 + 2, y, x0 + 9, y, PINKD)
    return nraw


def decor(g, t, wp):
    # clock
    g.d.ellipse([119, 35, 137, 53], fill=PANEL, outline=NAVY)
    for a in range(0, 360, 90):
        g.p(128 + R(7 * math.sin(math.radians(a))), 44 - R(7 * math.cos(math.radians(a))), MUTED)
    am = t * 0.35 + 8 * math.pi * smooth(wp); ah = 5.2 + 2 * math.pi * smooth(wp)
    g.l(128, 44, 128 + R(4 * math.sin(ah)), 44 - R(4 * math.cos(ah)), NAVY, 2)
    g.l(128, 44, 128 + R(6.5 * math.sin(am)), 44 - R(6.5 * math.cos(am)), PINKD)
    g.p(128, 44, INK)
    # poster with a Roman column
    g.box(163, 44, 189, 74, YELLOW); g.r(163, 44, 189, 44, NAVY)
    g.d.ellipse([169, 48, 183, 62], fill=PINK)
    g.r(170, 52, 182, 53, PANEL); g.r(169, 51, 183, 51, NAVY); g.r(172, 54, 180, 67, PANEL)
    for x in (174, 176, 178): g.r(x, 55, x, 66, MUTED)
    g.r(170, 68, 182, 69, PANEL); g.r(169, 70, 183, 70, NAVY)
    g.p(176, 42, NAVY); g.p(176, 43, NAVY)
    # bookshelf
    g.r(408, 60, 446, 62, NAVY); g.r(409, 60, 445, 60, ORANGE)
    g.r(411, 63, 412, 66, NAVY); g.r(442, 63, 443, 66, NAVY)
    x = 410
    for i, (c, w, h) in enumerate(((PINK, 4, 12), (BLUE, 3, 10), (YELLOW, 4, 13), (GREEN, 3, 9), (NAVY, 3, 11),
                                   (ORANGE, 4, 12), (PANEL, 3, 8), (PINKD, 3, 11))):
        g.box(x, 59 - h, x + w, 59, c); x += w
    g.orr(439, 54, 5, 5, GREEN, 2); g.r(440, 51, 441, 53, GREEND)
    # plant: leaves sway on a slow sine, each a little out of phase
    o = -14                                                  # v2: moved left, out of Claudius's way
    for i, (x1, y1) in enumerate(((290, 108), (297, 102), (303, 110), (287, 116), (305, 118))):
        sw = R(1.2 * math.sin(t * 1.4 + i * 1.3)) if i < 3 else 0
        g.l(296 + o, 124, x1 + o + sw, y1, GREEND)
        g.orr(x1 + o + sw - 3, y1 - 3, 7, 6, GREEN, 2, GREEND); g.p(x1 + o + sw, y1 - 1, GREEND); g.p(x1 + o + sw, y1, GREEND)
    g.d.polygon([(289 + o, 123), (303 + o, 123), (301 + o, 135), (291 + o, 135)], fill=PINK, outline=NAVY)
    g.r(288 + o, 122, 304 + o, 124, NAVY); g.r(289 + o, 123, 303 + o, 123, PINKD)


def door(g, flap):
    g.r(9, 56, 48, 135, NAVY); g.r(11, 58, 46, 135, BLUE)
    for (x0, y0, x1, y1) in ((15, 62, 42, 86), (15, 108, 42, 131)):
        g.r(x0, y0, x1, y1, NAVY); g.r(x0 + 1, y0 + 1, x1 - 1, y1 - 1, BLUE)
        g.r(x0 + 2, y0 + 2, x1 - 2, y0 + 2, (84, 112, 232))
    g.r(42, 97, 44, 100, NAVY); g.p(43, 98, YELLOW); g.p(43, 99, YELLOW)
    g.box(17, 92, 40, 102, YELLOW); g.r(20, 96, 37, 98, INK)
    if flap == 2: g.r(19, 94, 38, 99, INK); g.r(19, 92, 38, 93, YELLOWD)
    elif flap == 1: g.r(20, 95, 37, 98, INK); g.r(20, 94, 37, 94, YELLOWD)
    # envelope sign above the door
    g.box(20, 40, 38, 52, PANEL); g.l(21, 41, 29, 47, NAVY); g.l(37, 41, 29, 47, NAVY)
    g.r(28, 53, 30, 55, NAVY)
    # doormat
    g.r(8, 138, 50, 141, PINKD)
    for x in range(9, 50, 2): g.p(x, 139, PINK); g.p(x + 1, 140, PINK)


# ---- the glass tube: a centreline with rounded elbows, rendered once as a distance field -----
PIPE_PTS = [(63.0, 119.5), (55.5, 119.5), (55.5, 12.5), (355.5, 12.5), (355.5, 69.0)]
PIPE_RAD = [5.0, 9.0, 9.0]
_PIPE = {}


def _pipe_samples(step=0.25):
    pts = [np.array(p, float) for p in PIPE_PTS]
    prims, cur = [], pts[0]
    for i in range(1, len(pts) - 1):
        a, p, b = pts[i - 1], pts[i], pts[i + 1]; r = PIPE_RAD[i - 1]
        din = (p - a) / np.linalg.norm(p - a); dout = (b - p) / np.linalg.norm(b - p)
        s, e = p - din * r, p + dout * r
        prims.append(('line', cur, s))
        c = s + dout * r
        a0 = math.atan2(s[1] - c[1], s[0] - c[0]); a1 = math.atan2(e[1] - c[1], e[0] - c[0])
        da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
        prims.append(('arc', c, r, a0, da)); cur = e
    prims.append(('line', cur, pts[-1]))
    out = []
    for pr in prims:
        if pr[0] == 'line':
            _, A, B = pr; n = max(1, int(math.ceil(np.linalg.norm(B - A) / step)))
            out += [A + (B - A) * k / n for k in range(n)]
        else:
            _, c, r, a0, da = pr; n = max(1, int(math.ceil(abs(da) * r / step)))
            out += [c + r * np.array([math.cos(a0 + da * k / n), math.sin(a0 + da * k / n)]) for k in range(n)]
    out.append(pts[-1])
    S = np.array(out)
    cum = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(S, axis=0), axis=1))])
    return S, cum


def pipe_geom():
    if not _PIPE:
        S, cum = _pipe_samples()
        T = np.gradient(S, axis=0); T /= np.linalg.norm(T, axis=1, keepdims=True)
        N = np.stack([T[:, 1], -T[:, 0]], axis=1)          # left-hand normal in screen coordinates
        x0, y0 = int(S[:, 0].min()) - 6, int(S[:, 1].min()) - 6
        x1, y1 = int(S[:, 0].max()) + 6, int(S[:, 1].max()) + 6
        ys, xs = np.mgrid[y0:y1 + 1, x0:x1 + 1]
        P = np.stack([xs.ravel() + 0.5, ys.ravel() + 0.5], axis=1)
        best = np.full(len(P), 1e9); idx = np.zeros(len(P), int)
        for k in range(0, len(S), 40):
            d = ((P[:, None, :] - S[None, k:k + 40, :]) ** 2).sum(-1)
            j = d.argmin(1); dj = d[np.arange(len(P)), j]
            m = dj < best; best[m] = dj[m]; idx[m] = j[m] + k
        dist = np.sqrt(best)
        side = ((P - S[idx]) * N[idx]).sum(1)
        rgba = np.zeros((len(P), 4), np.uint8)

        def paint(mask, col): rgba[mask, :3] = col; rgba[mask, 3] = 255
        paint(dist < 4.0, NAVY)
        paint(dist < 3.0, GLASS)
        paint((dist < 3.0) & (side < -1.5), GLASSD)
        paint((dist < 3.0) & (side > 0.5) & (side <= 1.5), GLASSH)
        img = Image.fromarray(rgba.reshape(ys.shape + (4,)), 'RGBA')
        _PIPE.update(S=S, cum=cum, img=img, org=(x0, y0))
    return _PIPE


def pipe(g):
    P = pipe_geom(); g.im.paste(P['img'], P['org'], P['img'])
    g.r(59, 114, 61, 125, NAVY); g.r(60, 115, 60, 124, MUTED)          # flange at the copier
    for x in range(100, 340, 62): g.r(x, 8, x + 2, 17, NAVY); g.r(x + 1, 6, x + 1, 8, NAVY)
    for y in (40, 84): g.r(51, y, 60, y + 2, NAVY)
    g.r(351, 40, 360, 42, NAVY)
    g.r(350, 67, 361, 70, NAVY); g.r(351, 68, 360, 68, MUTED)          # mouth over Robert's desk


def pipe_pos(dist):
    P = pipe_geom(); S, cum = P['S'], P['cum']
    dist = clamp(dist, 0.0, cum[-1])
    i = int(np.clip(np.searchsorted(cum, dist), 1, len(cum) - 1))
    u = (dist - cum[i - 1]) / max(1e-9, cum[i] - cum[i - 1])
    q = S[i - 1] + (S[i] - S[i - 1]) * u
    return float(q[0]), float(q[1])


def machine(g, t, active):
    dx = (int(t * 30) % 3 - 1) if active else 0
    g.r(64 + dx, 132, 66 + dx, 135, NAVY); g.r(82 + dx, 132, 84 + dx, 135, NAVY)
    g.orr(62 + dx, 112, 25, 21, PINK, 2)
    g.r(64 + dx, 131, 84 + dx, 131, PINKD); g.r(63 + dx, 130, 85 + dx, 130, PINKD)
    g.rr(65 + dx, 115, 19, 9, INK, 1)
    g.text(68 + dx, 117, "X2", YELLOW if (active and int(t * 12) % 2) else CREAM, font=F3)
    g.p(79 + dx, 118, GREEN if not active else YELLOW); g.p(79 + dx, 121, PINK)
    for y in (126, 128): g.r(75 + dx, y, 83 + dx, y, PINKD)
    g.p(66 + dx, 127, GREEN if int(t * 2) % 2 or active else GREEND); g.p(69 + dx, 127, YELLOW)
    g.d.polygon([(59 + dx, 100), (89 + dx, 100), (81 + dx, 111), (67 + dx, 111)], fill=YELLOW, outline=NAVY)
    g.r(58 + dx, 99, 90 + dx, 100, NAVY); g.r(62 + dx, 102, 70 + dx, 102, CREAM)


def tray_table(g, letter_y):
    g.r(100, 125, 101, 135, NAVY); g.r(126, 125, 127, 135, NAVY); g.box(97, 121, 130, 125, ORANGE)
    if letter_y is not None: envelope(g, 113, letter_y)
    g.orr(102, 112, 24, 9, PANEL, 1); g.text(109, 113, "IN", NAVY)


def claudius_desk(g, lamp, paper_h, letter):
    if lamp > 0:
        for y in range(95, 110):
            xa, xb = 158 - (y - 95) // 2, 170 + int((y - 95) * 1.9)
            for x in range(xa, xb + 1):
                if (x + y) % 2 == 0 and (lamp > 0.5 or (x // 2 + y // 2) % 2 == 0): g.p(x, y, YELLOW)
    g.r(151, 114, 153, 135, NAVY); g.r(196, 114, 198, 135, NAVY); g.r(151, 126, 198, 126, NAVY)
    g.box(147, 109, 202, 114, ORANGE); g.r(148, 113, 201, 113, ORANGED)
    g.box(156, 115, 172, 123, CREAM); g.r(163, 119, 165, 119, NAVY)
    g.r(152, 107, 160, 108, NAVY); g.l(156, 107, 156, 96, NAVY); g.l(156, 96, 161, 91, NAVY)
    g.d.polygon([(159, 88), (166, 87), (171, 94), (158, 95)], fill=PINK, outline=NAVY)
    if lamp > 0: g.r(160, 95, 169, 95, YELLOW)
    if letter: envelope(g, 167, 104)
    if paper_h > 0:
        g.box(182, 99 - paper_h, 193, 100, PANEL)
        for i, y in enumerate(range(99 - paper_h + 2, 99, 2)):
            g.r(184, y, 189 + (i % 3) - 1, y, MUTED)
    g.r(177, 99, 198, 101, INK); g.p(176, 100, MUTED); g.p(199, 100, MUTED)
    g.orr(179, 103, 18, 5, BLUE, 1)
    for x in range(181, 196, 2): g.p(x, 105, CREAM); g.p(x + 1, 107, CREAM)


def robert_desk(g, pile):
    g.box(345, 111, 413, 135, GREEND); g.r(349, 115, 409, 131, NAVY); g.r(350, 116, 408, 130, GREEND)
    g.r(376, 122, 382, 123, YELLOW)
    g.box(341, 107, 417, 112, GREEN); g.r(342, 111, 416, 111, GREEND)
    for i in range(pile):
        envelope(g, 409 - (i % 2), 103 - 2 * i)


# ---- motion helpers -------------------------------------------------------------------------
def cam_at(t):
    for (t0, c0), (t1, c1) in zip(CAM, CAM[1:]):
        if t0 <= t <= t1: return R(lerp(c0, c1, smoother(seg(t, t0, t1))))
    return CAM[-1][1]


def walk_stride(D):
    k = max(1, round(D / (4.4 * math.pi)))
    return k, D / (k * math.pi)          # the gait phase ends on k*pi: feet together


def walk_state(wk, t):
    t0, x0, x1, dur = wk
    D = abs(x1 - x0); u = seg(t, t0, t0 + dur)
    dist = D * trap(u)
    _, stride = walk_stride(D)
    return R(x0 + math.copysign(dist, x1 - x0)), dist / stride, trap_speed(u)


def footfalls(wk):
    t0, x0, x1, dur = wk; D = abs(x1 - x0); k, stride = walk_stride(D)
    out = []
    for j in range(k):
        target = stride * (math.pi / 2 + j * math.pi); lo, hi = 0.0, 1.0
        for _ in range(40):
            mid = (lo + hi) / 2
            if D * trap(mid) < target: lo = mid
            else: hi = mid
        out.append(t0 + lo * dur)
    return out


def tween(a, b, u, arc=0):
    u = smooth(clamp(u))
    return R(lerp(a[0], b[0], u)), R(lerp(a[1], b[1], u) - arc * math.sin(math.pi * u))


# flight of the signed reply: quadratic Bezier to the open window, then it shrinks into the sky
FLY = ((338.0, 103.0), (356.0, -16.0), (486.0, 50.0))
FLY_A = 0.68


def fly_state(u):
    if u < FLY_A:
        v = u / FLY_A
        (ax, ay), (bx, by), (cx, cy) = FLY
        x = (1 - v) ** 2 * ax + 2 * (1 - v) * v * bx + v * v * cx
        y = (1 - v) ** 2 * ay + 2 * (1 - v) * v * by + v * v * cy
        return x, y + 1.2 * math.sin(v * 18), 'big'
    w = (u - FLY_A) / (1 - FLY_A)
    x, y = 486 + 10 * ease_out(w, 2), 50 - 10 * ease_out(w, 2)
    return x, y, ('mini' if w < 0.55 else 'dot' if w < 0.9 else 'gone')


# ---- Robert's near hand ---------------------------------------------------------------------
REST = (371.0, 103.0)        # holding the mug handle on the desk
MOUTH = (378.0, 80.0)
POINT = (362.0, 81.0)
STAMP_AT = (396.0, 101.0)    # stamp on the desk at (396, 106), beside his elbow
OVER = (360.0, 80.0)
HIGH = (360.0, 77.0)
DOWN = (359.0, 96.0)
PAPER_X0, PAPER_Y0 = 350, 100


def robert_hand(t, wp):
    """Returns (hand_xy, mode) where mode in {'mug', 'free', 'point', 'stamp'}."""
    if T_SAY0 - 0.05 <= t < T_SAY1 + 0.35:
        wag = math.sin((t - T_SAY0) * 13) * (1 - seg(t, T_SAY1 - 0.2, T_SAY1))
        keys = [(T_SAY0 - 0.05, REST), (T_SAY0 + 0.18, POINT, back_out, 0),
                (T_SAY1, POINT), (T_SAY1 + 0.35, REST, smooth, 0)]
        x, y = track(t, keys)
        if T_SAY0 + 0.18 <= t < T_SAY1: y += 1.2 * wag
        return (x, y), ('point' if t < T_SAY1 + 0.1 else 'free')
    if T_REACH <= t < T_SLAM + 0.95:
        keys = [(T_REACH, REST), (T_REACH + 0.22, STAMP_AT, smooth), (T_LIFT, STAMP_AT),
                (T_SLAM - 0.2, OVER, smooth, 5), (T_SLAM - 0.08, HIGH, ease_out),
                (T_SLAM, DOWN, lambda u: u ** 3), (T_SLAM + 0.22, DOWN), (T_SLAM + 0.34, (360.0, 91.0), ease_out),
                (T_SLAM + 0.62, STAMP_AT, smooth, 4), (T_SLAM + 0.7, STAMP_AT), (T_SLAM + 0.95, REST, smooth)]
        holding = T_REACH + 0.22 <= t < T_SLAM + 0.7
        return track(t, keys), ('stamp' if holding else 'free')
    sip = 0.0
    if wp > 0:
        sip = max(math.sin(math.pi * seg(wp, 0.12, 0.36)), math.sin(math.pi * seg(wp, 0.60, 0.84)))
    u = smooth(clamp(sip * 1.25))
    return (lerp(REST[0], MOUTH[0], u), lerp(REST[1], MOUTH[1], u) - 3 * math.sin(math.pi * u)), 'mug'


# ---- the film -------------------------------------------------------------------------------
def render(t):
    world = Image.new('RGB', (WW, H), PAPER); g = G(world)
    wp = seg(t, WAIT0, WAIT1) if WAIT0 < t < WAIT1 else 0.0
    background(g, night_level(wp))
    nraw = windows(g, t, wp)
    decor(g, t, wp)
    flap = 2 if T_POP - 0.08 < t < T_POP + 0.25 else (1 if T_POP - 0.16 < t < T_POP + 0.35 else 0)
    door(g, flap)
    pipe(g)
    # capsule in the tube, with a short bright streak behind it
    if T_SPLIT < t < T_PIPE_END:
        L = pipe_geom()['cum'][-1]
        u = seg(t, T_SPLIT, T_PIPE_END); d = L * trap(u, 0.12)
        sp = trap_speed(u, 0.12)
        for off in (8, 14):
            if d - off > 2 and sp > 0.6:
                qx, qy = pipe_pos(d - off); g.p(R(qx), R(qy), WHITE)
        mini_env(g, *Rp(pipe_pos(d)))
    # arriving letter: out of the slot, arc into the funnel, drop
    if T_POP <= t < T_FUN + 0.2:
        keys = [(T_POP, (26.0, 97.0)), (T_POP + 0.1, (34.0, 97.0), ease_out),
                (T_FUN - 0.05, (74.0, 90.0), smooth, 22), (T_FUN + 0.2, (74.0, 104.0), ease_in)]
        x, y = track(t, keys)
        envelope(g, R(x), R(y))
    machine(g, t, T_FUN + 0.1 < t < T_SPLIT)
    if T_SPLIT - 0.05 < t < T_SPLIT + 0.25:
        k = R(3 + 14 * ease_out(seg(t, T_SPLIT - 0.05, T_SPLIT + 0.25)))
        for a in range(-150, -20, 26):
            ca, sa = math.cos(math.radians(a)), math.sin(math.radians(a))
            g.l(74 + R(ca * k), 98 + R(sa * k), 74 + R(ca * (k + 3)), 98 + R(sa * (k + 3)), YELLOW)
    # the waiting copy: lands in the tray with a small bounce
    ty = None
    if T_TRAY <= t < T_W1E + 0.05:
        ty = 109 - R(2 * math.sin(math.pi * seg(t, T_TRAY, T_TRAY + 0.16)))
    tray_table(g, ty)
    if T_SPLIT <= t < T_TRAY:
        keys = [(T_SPLIT, (80.0, 100.0)), (T_TRAY, (113.0, 109.0), lambda u: u * u * (3 - 2 * u), 22)]
        envelope(g, *Rp(track(t, keys)))
    puff(g, 113, 113, seg(t, T_TRAY, T_TRAY + 0.3), PANEL, 9)

    lamp = 0.0
    if t >= T_TYPE0 - 0.12: lamp = 1.0 if not (T_TYPE0 - 0.08 <= t < T_TYPE0 - 0.04) else 0.4
    ph = R(1 + 11 * seg(t, T_TYPE0, T_TYPE1)) if T_TYPE0 <= t < T_TYPE1 + 0.1 else 0
    claudius_desk(g, lamp, ph, t >= T_W2E + 0.12)

    # --- Robert
    look = 'fwd'
    if T_DESK - 0.1 <= t < T_DESK + 1.0 or T_PLACED - 0.1 <= t < T_ENV: look = 'down'
    if T_FLY0 + 0.15 <= t < T_FLY1: look = 'up'
    mouth = 0
    if T_SAY0 <= t < T_SAY1: mouth = (2, 1, 2, 0, 1, 2)[int((t - T_SAY0) * 10) % 6]
    robert_body(g, look, mouth, blinking(t, BLINKS_R) and mouth == 0)
    pile = 2 if t < T_DESK + 1.0 else 3
    robert_desk(g, pile)
    if T_PIPE_END <= t < T_DESK:
        envelope(g, 355, R(lerp(72, 102, seg(t, T_PIPE_END, T_DESK) ** 2)))
    elif T_DESK <= t < T_DESK + 1.0:
        u = seg(t, T_DESK, T_DESK + 0.12)
        hop = R(2 * math.sin(math.pi * u)) if u < 1 else 0
        envelope(g, *tween((355, 102 - hop), (409, 99), seg(t, T_DESK + 0.55, T_DESK + 1.0), arc=8))
    puff(g, 355, 106, seg(t, T_DESK, T_DESK + 0.3), PANEL, 8)
    # draft / approved paper / outgoing envelope on the desk
    if T_PLACED <= t < T_ENV: flat_paper(g, PAPER_X0, PAPER_Y0, check=t >= T_SLAM)
    elif T_ENV <= t < T_GRAB: envelope(g, 357, 102, tag=True)
    hand, mode = robert_hand(t, wp)
    if mode != 'mug': mug(g, *Rp(REST))
    if mode == 'mug' and hand[1] > REST[1] - 1:
        for k in range(3):
            if (int(t * 5) + k) % 3: g.p(367 + (1 if (int(t * 4) + k) % 2 else 0), 95 - 2 * k, MUTED)
    if mode == 'stamp':
        hx, hy = Rp(hand)
        robert_arm(g, hand)
        sq = 1 if T_SLAM <= t < T_SLAM + 0.09 else 0
        stamp(g, hx, hy + 5 + sq, squash=sq)
        g.rr(hx - 2, hy - 2, 5, 5, NAVY, 1); g.r(hx - 1, hy - 1, hx + 1, hy + 1, SKIN)
    else:
        robert_arm(g, hand, point=(mode == 'point'))
        if mode == 'mug': mug(g, *Rp(hand))
        stamp(g, R(STAMP_AT[0]), 106)            # the stamp waits on the desk beside his elbow
    if T_DESK + 0.05 <= t < T_DESK + 0.75:
        u = seg(t, T_DESK + 0.05, T_DESK + 0.3)
        badge_mark(g, 384, 50 - R(4 * back_out(u)) + 4)
    if T_PLACED + 0.1 <= t < T_REACH:
        n = 1 + int(seg(t, T_PLACED + 0.1, T_REACH) * 3)
        g.orr(374, 46, 21, 9, PANEL, 2); g.r(380, 56, 383, 57, NAVY); g.p(381, 59, NAVY)
        for i in range(min(n, 3)): g.r(378 + 5 * i, 49, 380 + 5 * i, 51, NAVY)
    if T_SAY0 <= t < T_SAY1:
        yb = 34 + R(5 * (1 - back_out(seg(t, T_SAY0, T_SAY0 + 0.2))))
        bubble(g, 290, yb, "CHECK YOUR MAIL!", (372, yb))

    # --- Claudius
    blink = blinking(t, BLINKS_C)
    eo = 'blink' if blink else 'open'
    held = None
    kw = dict(t=t)
    if t < T_WAKE:
        cx = 222; claudius(g, cx, FLOOR, -1, 'sleep', eyes='sleep', **kw)
        for i in range(3):
            ph_ = (t * 0.55 + i / 3.0) % 1.0
            zx, zy = cx + 9 + R(ph_ * 13 + 2 * math.sin(ph_ * 9)), FLOOR - 36 - R(ph_ * 20)
            if ph_ < 0.45: g.text(zx, zy, "Z", MUTED, font=F3)
            elif ph_ < 0.92: g.text(zx, zy - 2, "Z", NAVY)
    elif t < WALK1[0]:
        a0, a1, a2 = T_WAKE + 0.07, T_WAKE + 0.33, T_WAKE + 0.42
        if t < a0: lift, sq = 0, 1
        elif t < a1:
            u = seg(t, a0, a1); lift, sq = R(7 * math.sin(math.pi * u)), (-1 if u < 0.5 else 0)
        elif t < a2: lift, sq = 0, 1
        else: lift, sq = 0, 0
        d = 1 if t < T_WAKE + 0.2 else -1
        claudius(g, 222, FLOOR, d, 'idle', eyes='wide', lift=lift, sq=sq, breathe=False, **kw)
        puff(g, 222, FLOOR, seg(t, a1, a1 + 0.3), PANEL, 9)
        if t < T_WAKE + 0.45:
            u = seg(t, T_WAKE, T_WAKE + 0.18)
            badge_mark(g, 222, FLOOR - 46 - R(6 * back_out(u)) - lift)
    elif t < T_W1E:
        cx, ph, e = walk_state(WALK1, t)
        claudius(g, cx, FLOOR, -1, 'walk', walk=ph, energy=e, eyes=eo, **kw)
    elif t < T_READ0:
        sq = 1 if t < T_W1E + 0.1 else 0
        hand = claudius(g, X_TRAY, FLOOR, -1, 'hold', sq=sq, breathe=False, **kw)
        held = ('env', tween((113, 109), (hand[0] - 3, hand[1] - 6), seg(t, T_W1E + 0.03, T_READ0 - 0.04), arc=6))
    elif t < T_READ1:
        lk = (-1, 0, 1, 0)[int((t - T_READ0) * 6) % 4]
        hand = claudius(g, X_TRAY, FLOOR, -1, 'read', look=lk, eyes=eo, **kw)
        fl = 1 if t < T_READ0 + 0.08 else 0
        held = ('paper', (hand[0] - 5, hand[1] - 2 - fl))
    elif t < T_W2E:
        cx, ph, e = walk_state(WALK2, t)
        hand = claudius(g, cx, FLOOR, 1, 'hold', walk=ph, energy=e, eyes=eo, **kw)
        held = ('env', (hand[0] + 3, hand[1] - 6))
    elif t < T_TYPE0:
        hand = claudius(g, 209, FLOOR, -1, 'hold', sq=1 if t < T_W2E + 0.08 else 0, **kw)
        if t < T_W2E + 0.12: held = ('env', (hand[0] - 3, hand[1] - 6))
    elif t < T_TYPE1:
        claudius(g, 209, FLOOR, -1, 'type', look=(0, 1)[int(t * 3) % 2], **kw)
    elif t < T_TAKE:
        hand = claudius(g, 209, FLOOR, -1, 'hold', eyes='happy', **kw)
        held = ('paper', tween((188, 93), (hand[0] - 3, hand[1] - 7), seg(t, T_TYPE1, T_TAKE - 0.05), arc=4))
    elif t < T_W3E:
        cx, ph, e = walk_state(WALK3, t)
        hand = claudius(g, cx, FLOOR, 1, 'hold', walk=ph, energy=e, eyes=eo, **kw)
        held = ('paper', (hand[0] + 3, hand[1] - 7))
    elif t < T_PLACED:
        hand = claudius(g, 326, FLOOR, 1, 'hold', sq=1 if t < T_W3E + 0.08 else 0, **kw)
        u = seg(t, T_W3E + 0.03, T_PLACED)
        if u < 0.85:
            held = ('paper', tween((hand[0] + 3, hand[1] - 7), (PAPER_X0 + 7, PAPER_Y0 + 2), u / 0.85, arc=10))
        else:
            flat_paper(g, PAPER_X0, PAPER_Y0)
    elif t < T_GRAB:
        hop, sq = 0, 0
        if T_SLAM + 0.03 <= t < T_SLAM + 0.3:
            u = seg(t, T_SLAM + 0.03, T_SLAM + 0.3)
            hop, sq = R(5 * math.sin(math.pi * u)), (-1 if u < 0.5 else 0)
        elif T_SLAM + 0.3 <= t < T_SLAM + 0.38:
            sq = 1
        claudius(g, 326, FLOOR, 1, 'idle', eyes='happy' if t >= T_SLAM + 0.05 else eo,
                 look=1, lift=hop, sq=sq, smile=t >= T_SLAM + 0.05, **kw)
    elif t < T_TOSS:
        hand = claudius(g, 326, FLOOR, 1, 'hold', eyes='happy', smile=True, **kw)
        held = ('tag', tween((357, 102), (hand[0] + 3, hand[1] - 6), seg(t, T_GRAB, T_TOSS - 0.12), arc=8))
    elif t < T_FLY0:
        if t < T_TOSS + 0.07:
            hand = claudius(g, 326, FLOOR, 1, 'hold', eyes='happy', smile=True, sq=1, breathe=False, **kw)
            held = ('tag', (hand[0] + 3, hand[1] - 6))
        else:
            hand = claudius(g, 326, FLOOR, 1, 'toss', eyes='happy', smile=True, sq=-1, **kw)
            held = ('tag', (hand[0] + 3, hand[1] - 6))
    elif t < T_END:
        claudius(g, 326, FLOOR, 1, 'wave', eyes=eo, look=1, smile=True, **kw)
    else:
        u = seg(t, T_END, T_END + 0.28)
        claudius(g, 326, FLOOR, front=True, pose='wave', eyes='blink' if blink else 'happy', smile=True,
                 lift=R(5 * math.sin(math.pi * u)), sq=(-1 if u < 0.5 else 0), **kw)
        puff(g, 326, FLOOR, seg(t, T_END + 0.28, T_END + 0.55), PANEL, 9)
    if held:
        kind, (hx, hy) = held
        if kind == 'env': envelope(g, hx, hy)
        elif kind == 'tag': envelope(g, hx, hy, tag=True)
        else: paper(g, hx, hy)
    for wk in (WALK1, WALK2, WALK3):          # small scuff where each walk stops
        x1 = wk[2]
        puff(g, x1 + (4 if wk[2] > wk[1] else -4), FLOOR, seg(t, wk[0] + wk[3] - 0.05, wk[0] + wk[3] + 0.22), MUTED, 5)

    # --- effects
    if T_SLAM <= t < T_SLAM + 0.35:
        k = R(4 + 22 * ease_out(seg(t, T_SLAM, T_SLAM + 0.35)))
        for a in range(0, 360, 45):
            sparkle(g, 357 + R(math.cos(math.radians(a)) * k), 100 + R(math.sin(math.radians(a)) * k * 0.6), YELLOW, 1)
    if T_SLAM + 0.05 <= t < T_ENV + 0.2:
        u = seg(t, T_SLAM + 0.05, T_SLAM + 0.45)
        by = 82 - R(24 * ease_out(u))
        rr = max(2, R(8 * min(1.15, back_out(seg(t, T_SLAM + 0.05, T_SLAM + 0.25)))))
        g.d.ellipse([339 - rr, by - rr, 339 + rr, by + rr], fill=GREEN, outline=NAVY)
        if rr >= 6:
            for dx, dy in ((-4, 0), (-3, 1), (-2, 2), (-1, 3), (0, 2), (1, 1), (2, 0), (3, -1), (4, -2), (5, -3)):
                g.r(339 + dx, by + dy - 1, 339 + dx, by + dy, WHITE)
    if T_ENV - 0.05 <= t < T_ENV + 0.2:
        k = R(3 + 10 * ease_out(seg(t, T_ENV - 0.05, T_ENV + 0.2)))
        for a in range(20, 380, 60):
            sparkle(g, 357 + R(math.cos(math.radians(a)) * k), 102 + R(math.sin(math.radians(a)) * k * 0.7), PINK, 1)
    if T_FLY0 <= t < T_FLY1:
        u = seg(t, T_FLY0, T_FLY1)
        x, y, size = fly_state(u)
        fx, fy = R(x), R(y)
        flap_i = int(t * 12) % 3
        if size == 'big':
            for k in range(1, 4):
                uu = u - 0.035 * k
                if uu > 0 and (int(t * 24) + k) % 2:
                    tx, ty, _ = fly_state(uu)
                    sparkle(g, R(tx) - 8, R(ty) + 4, YELLOW, 0)
            for sgn in (-1, 1):
                wx0 = fx + sgn * 7
                dy = (-6, -3, 1)[flap_i]
                for i in range(6):
                    yy = fy - 1 + R(dy * i / 5.0)
                    g.r(wx0 + sgn * i, yy - 1, wx0 + sgn * i, yy + (2 if i < 4 else 1), NAVY)
                for i in range(5):
                    yy = fy - 1 + R(dy * i / 5.0)
                    g.r(wx0 + sgn * i, yy, wx0 + sgn * i, yy + (1 if i < 3 else 0), WHITE)
            envelope(g, fx, fy, tag=True)
        elif size == 'mini':
            mini_env(g, fx, fy)
            g.r(fx - 6, fy - 3 + flap_i, fx - 4, fy - 3 + flap_i, WHITE); g.r(fx + 3, fy - 3 + flap_i, fx + 5, fy - 3 + flap_i, WHITE)
        elif size == 'dot':
            g.r(fx, fy, fx + 1, fy + 1, PANEL)
            if int(t * 12) % 2: g.p(fx + 2, fy - 1, YELLOW)

    # --- night falls while the file waits
    if nraw > 0.01:
        top = world.crop((0, 0, WW, BAND))
        world.paste(Image.blend(top, Image.new('RGB', top.size, NIGHT), 0.5 * nraw), (0, 0))
        if lamp == 0 and nraw > 0.3:
            pass

    # --- camera, shake, caption band
    cam = cam_at(t)
    frame = Image.new('RGB', (W, H), INK)
    dy = 0
    if T_SLAM <= t < T_SLAM + 0.3: dy = (2, -2, 1, -1, 1, 0, 0, 0)[int((t - T_SLAM) * FPS)]
    frame.paste(world.crop((cam, 0, cam + W, H)), (0, dy))
    f = G(frame)
    f.r(0, BAND, W, H, INK); f.r(0, BAND, W, BAND, PINK); f.r(0, BAND + 1, W, BAND + 1, YELLOW)
    for t0, t1, s in CAPS:
        if t0 <= t < t1:
            n = int((t - t0) * 48) + 1
            x = (W - tw(s)) // 2
            f.text(x, BAND + 9, s[:n], CREAM)
            if n < len(s): f.r(x + n * 6, BAND + 9, x + n * 6 + 3, BAND + 15, YELLOW)
    if t >= T_END:
        u = seg(t, T_END, T_END + 0.5)
        y = R(lerp(-50, 12, back_out(u, 1.4)))
        f.r(47, y + 3, 279, y + 47, PINK); f.r(44, y, 276, y + 44, INK)
        f.r(44, y, 276, y + 1, YELLOW); f.r(44, y + 43, 276, y + 44, YELLOW)
        for i, s in enumerate(("THE DAY THE DESK", "GOT A MAIL SLOT")):
            x = (W - tw(s, 2)) // 2
            f.text(x + 1, y + 7 + i * 17 + 1, s, PINKD, 2); f.text(x, y + 7 + i * 17, s, YELLOW if i else CREAM, 2)
    return frame


# ---- sound ----------------------------------------------------------------------------------
SR = 44100


def make_audio(path):
    rng = np.random.default_rng(11)
    buf = np.zeros(int(SR * (DUR + 0.5)))

    def put(t0, sig):
        i = int(t0 * SR); j = min(len(buf), i + len(sig))
        if i < len(buf): buf[i:j] += sig[:j - i]

    def tone(f0, dur, vol, f1=None, kind='sq', duty=0.5, decay=6.0):
        n = int(dur * SR); tt = np.arange(n) / SR
        f = np.linspace(f0, f1 if f1 else f0, n)
        ph = np.cumsum(f) / SR
        if kind == 'sq': s = np.where((ph % 1.0) < duty, 1.0, -1.0)
        elif kind == 'tri': s = 2 * np.abs(2 * (ph % 1.0) - 1) - 1
        else: s = np.sin(2 * np.pi * ph)
        env = np.exp(-decay * tt / max(dur, 1e-6)) * np.minimum(1, tt / 0.004) * np.minimum(1, (dur - tt) / 0.006)
        return s * env * vol

    def noise(dur, vol, decay=8.0, smooth_n=1):
        n = int(dur * SR); s = rng.uniform(-1, 1, n)
        if smooth_n > 1: s = np.convolve(s, np.ones(smooth_n) / smooth_n, 'same') * math.sqrt(smooth_n) * 0.6
        tt = np.arange(n) / SR
        return s * np.exp(-decay * tt / dur) * np.minimum(1, tt / 0.002) * vol

    def hz(m): return 440.0 * 2 ** ((m - 69) / 12.0)

    # music: 120 bpm, C - Am - F - G, two-second bars; quieter while the file waits overnight
    chords = [[60, 64, 67], [57, 60, 64], [53, 57, 60], [55, 59, 62]] * 2 + [[60, 64, 67]] * 3
    leads = {0: [76, 79, 76, 72], 1: [76, 81, 76, 72], 2: [77, 81, 77, 72], 3: [74, 79, 71, 74]}
    for bar, ch in enumerate(chords):
        t0 = bar * 2.0
        if t0 >= DUR: break
        quiet = 0.45 if 4.0 <= t0 < 8.0 else 1.0
        last = bar == len(chords) - 1
        for b in ((0,) if last else (0, 2)):
            put(t0 + b * 0.5, tone(hz(ch[0] - 12), 1.2 if last else 0.46, 0.15 * quiet, kind='tri', decay=2.5))
        for i in range(4 if last else 8):
            put(t0 + i * 0.25, tone(hz(ch[(0, 1, 2, 1)[i % 4]] + 12), 0.2, 0.032 * quiet * (1 - i / 6 if last else 1), duty=0.25, decay=4))
        if not (4.0 <= t0 < 8.0) and not last:
            notes = leads[bar % 4] if bar < 8 else ([72, 76, 79, 84] if bar == 8 else [84, 79, 84, 0])
            for i, m in enumerate(notes):
                if m: put(t0 + i * 0.5, tone(hz(m), 0.42, 0.045, duty=0.5, decay=3.5))
    # effects, each tied to a timeline constant
    put(T_POP - 0.1, noise(0.06, 0.06, smooth_n=8))                       # slot flap
    put(T_POP, tone(420, 0.09, 0.14, 950))
    put(T_FUN, tone(700, 0.16, 0.11, 260))
    for i in range(14):
        put(T_FUN + 0.12 + i * 0.04, tone(hz(rng.choice([72, 74, 76, 79, 81, 84])), 0.035, 0.06))
    put(T_SPLIT, tone(1320, 0.12, 0.12)); put(T_SPLIT + 0.1, tone(1760, 0.2, 0.12))
    n = int(PIPE_DUR * SR); x = np.linspace(0, 1, n)
    env = np.array([trap_speed(v, 0.12) for v in x[::200]]); env = np.interp(np.arange(n), np.arange(0, n, 200), env)
    wh = np.convolve(rng.uniform(-1, 1, n), np.ones(24) / 24, 'same') * 0.5
    put(T_SPLIT, wh * (0.3 + 0.7 * env) * np.minimum(1, (1 - x) * 30) * (0.7 + 0.3 * np.sin(x * 60)))
    put(T_TRAY, tone(320, 0.1, 0.14, 150, kind='sin')); put(T_DESK, tone(300, 0.1, 0.14, 140, kind='sin'))
    put(T_DESK + 0.08, tone(1568, 0.25, 0.09))
    for i, m in enumerate((67, 72, 69, 72, 76)):
        put(T_SAY0 + 0.05 + i * 0.11, tone(hz(m), 0.08, 0.09, duty=0.3))
    put(T_WAKE, tone(660, 0.08, 0.12)); put(T_WAKE + 0.09, tone(990, 0.16, 0.12))
    put(T_WAKE + 0.33, tone(150, 0.06, 0.12, 90, kind='sin'))            # landing
    for wk in (WALK1, WALK2, WALK3):
        for tt in footfalls(wk):
            put(tt, noise(0.03, 0.06, smooth_n=6) + tone(170, 0.03, 0.05, kind='sin'))
    put(T_W1E + 0.1, tone(880, 0.07, 0.09))
    for i in range(4):
        put(T_READ0 + 0.1 + i * 0.2, tone(hz(84 + (i % 2) * 3), 0.04, 0.045, duty=0.25))
    put(T_TYPE0 - 0.12, noise(0.02, 0.08, smooth_n=3))                   # lamp click
    tt = T_TYPE0
    while tt < T_TYPE1:
        put(tt, noise(0.018, float(rng.uniform(0.07, 0.13)), smooth_n=2)); tt += float(rng.choice([0.07, 0.09, 0.09, 0.14]))
    put(T_TYPE1, tone(2093, 0.3, 0.08, kind='sin', decay=5))
    put(T_PLACED - 0.05, noise(0.08, 0.05, smooth_n=12))                 # paper on the desk
    put(T_LIFT - 0.05, tone(500, 0.05, 0.06, 380))                        # stamp picked up
    put(T_SLAM - 0.2, tone(300, 0.18, 0.04, 700, kind='sin', decay=1))    # wind-up
    put(T_SLAM, noise(0.12, 0.32, smooth_n=10) + tone(110, 0.12, 0.3, 50, kind='sin', decay=3))
    put(T_SLAM, tone(60, 0.3, 0.26, kind='sin', decay=5))
    for i, m in enumerate((72, 76, 79, 84)):
        put(T_SLAM + 0.12 + i * 0.085, tone(hz(m), 0.14 if i < 3 else 0.4, 0.1, duty=0.5, decay=3))
    put(T_ENV - 0.05, noise(0.18, 0.1, smooth_n=14)); put(T_ENV, tone(1200, 0.1, 0.06, 1900))
    put(T_TOSS + 0.07, tone(480, 0.2, 0.11, 1300))
    n = int((T_FLY1 - T_FLY0) * SR); tt_ = np.arange(n) / SR
    put(T_FLY0, np.sin(2 * np.pi * (1100 + 500 * tt_) * tt_) * (np.sin(2 * np.pi * 12 * tt_) > 0) * 0.03 * np.linspace(1, 0.1, n))
    for i, m in enumerate((60, 64, 67, 72, 76)):
        put(T_END + 0.05 + i * 0.07, tone(hz(m), 1.2, 0.06, duty=0.5, decay=4))
    put(T_END + 0.28, tone(150, 0.06, 0.1, 90, kind='sin'))
    k = int(DUR * SR); nf = int(0.6 * SR)
    fade = np.ones(len(buf)); fade[k - nf:k] = np.linspace(1, 0, nf); fade[k:] = 0
    fade[:int(0.01 * SR)] = np.linspace(0, 1, int(0.01 * SR))
    raw = buf * fade
    pre_peak = float(np.abs(raw).max())
    out = np.tanh(raw * 3.6)                               # soft-knee limiter, never hard-clips
    out *= 0.89 / max(1e-9, float(np.abs(out).max()))      # peak at about -1 dBFS
    stats = dict(pre_peak=round(pre_peak, 3), rms_dbfs=round(20 * math.log10(float(np.sqrt((out[:k] ** 2).mean()))), 1),
                 hot_share=round(float((np.abs(raw) > 0.5).mean()), 5))
    with wave.open(path, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((out[:k] * 32767).astype('<i2').tobytes())
    return stats


# ---- outputs --------------------------------------------------------------------------------
def character_sheet(path):
    cells = [("idle", dict(pose='idle')), ("walk", dict(pose='walk', walk=math.pi / 2)),
             ("walk", dict(pose='walk', walk=0.0)), ("asleep", dict(pose='sleep', eyes='sleep')),
             ("reads", dict(pose='read')), ("types", dict(pose='type')),
             ("carries", dict(pose='hold', eyes='happy')), ("hello", dict(pose='wave', front=True, eyes='happy', smile=True))]
    cw, chh = 44, 62
    im = Image.new('RGB', (cw * len(cells), chh), PAPER); g = G(im)
    for y in range(3, chh, 6):
        for x in range((y // 6 % 2) * 3, im.width, 6): g.p(x, y, DOT)
    g.r(0, 50, im.width, chh, FLOORC); g.r(0, 50, im.width, 50, MUTED)
    for i, (name, kw) in enumerate(cells):
        hand = claudius(g, i * cw + 22, 50, 1, t=0.3, breathe=False, **kw)
        if name == 'reads': paper(g, hand[0] + 5, hand[1] - 2)
        if name == 'carries': envelope(g, hand[0] + 3, hand[1] - 6, tag=True)
        if name == 'asleep': g.text(i * cw + 33, 8, "Z", NAVY); g.text(i * cw + 30, 16, "Z", MUTED, font=F3)
    im.resize((im.width * 8, im.height * 8), Image.NEAREST).save(path)


def print_times():
    for k in ('T_POP', 'T_FUN', 'T_SPLIT', 'T_TRAY', 'T_PIPE_END', 'T_DESK', 'WAIT0', 'WAIT1', 'T_SAY0', 'T_WAKE',
              'T_W1E', 'T_READ0', 'T_READ1', 'T_W2E', 'T_TYPE0', 'T_TYPE1', 'T_TAKE', 'T_W3E', 'T_PLACED',
              'T_REACH', 'T_LIFT', 'T_SLAM', 'T_ENV', 'T_GRAB', 'T_TOSS', 'T_FLY0', 'T_FLY1', 'T_END', 'DUR'):
        print('%-11s %6.2f' % (k, globals()[k]))
    for wk in (WALK1, WALK2, WALK3): print('steps', [round(x, 2) for x in footfalls(wk)])


def main():
    mode = sys.argv[1]
    if mode == 'times': print_times(); return
    if mode == 'check':
        out, times = sys.argv[2], [float(x) for x in sys.argv[3:]]
        cols = 2 if len(times) > 1 else 1
        rows = (len(times) + cols - 1) // cols
        sheet = Image.new('RGB', (cols * W * 3, rows * H * 3), INK)
        for i, t in enumerate(times):
            sheet.paste(render(t).resize((W * 3, H * 3), Image.NEAREST), ((i % cols) * W * 3, (i // cols) * H * 3))
        sheet.save(out); return
    if mode == 'seq':
        out, t0, n = sys.argv[2], float(sys.argv[3]), int(sys.argv[4])
        box = [int(v) for v in sys.argv[5:9]] if len(sys.argv) >= 9 else [0, 0, W, H]
        x0, y0, bw, bh = box; sc = max(1, min(6, 1800 // (bw * 4)))
        cols = 4; rows = (n + cols - 1) // cols
        sheet = Image.new('RGB', (cols * bw * sc + cols - 1, rows * bh * sc + rows - 1), PINK)
        start = int(round(t0 * FPS))
        for i in range(n):
            fr = render((start + i) / FPS).crop((x0, y0, x0 + bw, y0 + bh)).resize((bw * sc, bh * sc), Image.NEAREST)
            sheet.paste(fr, ((i % cols) * (bw * sc + 1), (i // cols) * (bh * sc + 1)))
        sheet.save(out); return
    frames, out = sys.argv[2], sys.argv[3]
    os.makedirs(frames, exist_ok=True); os.makedirs(out, exist_ok=True)
    n = int(round(DUR * FPS))
    for i in range(n):
        render(i / FPS).save(os.path.join(frames, 'f_%04d.png' % i))
    wav = os.path.join(frames, 'audio.wav'); stats = make_audio(wav)
    pat = os.path.join(frames, 'f_%04d.png')
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-framerate', str(FPS), '-i', pat, '-i', wav,
                    '-vf', 'scale=1920:1080:flags=neighbor', '-c:v', 'libx264', '-crf', '16', '-preset', 'slow',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart', '-shortest',
                    os.path.join(out, 'claudius-mail-slot.mp4')], check=True)
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-framerate', str(FPS), '-i', pat, '-filter_complex',
                    'fps=12,scale=640:360:flags=neighbor,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=none',
                    os.path.join(out, 'claudius-mail-slot.gif')], check=True)
    render(DUR - 0.05).resize((1920, 1080), Image.NEAREST).save(os.path.join(out, 'poster.png'))
    character_sheet(os.path.join(out, 'claudius-sheet.png'))
    print('frames:', n, 'duration:', DUR, 'audio:', stats)


if __name__ == '__main__':
    main()
