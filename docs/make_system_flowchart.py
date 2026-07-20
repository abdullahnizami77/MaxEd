#!/usr/bin/env python3
"""
Deterministic generator for the "How BALANCECHECK works" flowchart SVG.

Stdlib only. No randomness, no clock, no network, no external references.
Byte-identical on every run.

Design (winner = strategy A's five-pass layout engine, hardened with the
best ideas from the two runners-up):

  1. MEASURE  Per-character Helvetica/Arial advance tables (1/1000 em), taken
              as the *exact* metric, then combined with two independent
              conservative bounds:
                 * exact width x FALLBACK (a font 25% wider than Arial), and
                 * the flat per-character floor 0.62*fs (0.68*fs bold).
              The layout width used everywhere is the max of the two, so a
              line fits under BOTH models at once. (Runner-up B/C used only
              one model each; A's audit showed a single model can hide a
              whole class of error.)
  2. LAYOUT   Nodes are pure data (see CONTENT). Every box measures its own
              wrapped text and derives its height from the real line count.
              Nothing about a height or a canvas size is hardcoded.
              The decision rhombus is sized in closed form from the true
              rhombus half-width at each text line's own vertical offset
              (grafted from strategy C; strategy A used a looser bbox rule).
  3. ROUTE    Axis-aligned polylines with radius-clamped rounded elbows.
              The decision fans out through a bus into three lanes; each
              branch label sits in an opaque chip (grafted from strategy B)
              placed in a *gap* in its drop line, so no edge is ever drawn
              underneath a label and the "no edge crosses a label" invariant
              needs no exceptions. The two feedback edges run in dedicated
              gutters outside every column.
  4. RENDER   Every primitive registers its bounding box on the Doc; the
              viewBox is computed from real content bounds plus the margin.
              No module-level mutable state: a Doc instance owns everything,
              so build() is re-entrant and testable.
  5. VERIFY   Two structurally different checks must both pass, and the file
              is written ONLY if they do (strategy A wrote first and reported
              failures afterwards, leaving broken artifacts on disk):
                a) model check - overlaps, edge/box and edge/label crossings,
                   text fit, bounds;
                b) parse check - the emitted SVG is re-parsed with
                   xml.etree, text extents are rebuilt from the serialized
                   x/y/font-size/text-anchor attributes alone and re-checked
                   against the serialized rect geometry. This is the check
                   that is *not* circular with the layout model.

Run:  python3 docs/make_system_flowchart.py  ->  system_flowchart.svg
Exit code 0 on success; non-zero and nothing written on any violation.
"""

import os
import sys
import xml.etree.ElementTree as ET

# ==========================================================================
# 1. MEASURE
# ==========================================================================

# Advance widths in 1/1000 em for Helvetica. Arial and Liberation Sans are
# metrically compatible, so the stack below resolves to these numbers on
# every platform GitHub is read on.
_REG = {
    ' ': 278, '!': 278, '"': 355, '#': 556, '$': 556, '%': 889, '&': 667,
    "'": 191, '(': 333, ')': 333, '*': 389, '+': 584, ',': 278, '-': 333,
    '.': 278, '/': 278, '0': 556, '1': 556, '2': 556, '3': 556, '4': 556,
    '5': 556, '6': 556, '7': 556, '8': 556, '9': 556, ':': 278, ';': 278,
    '<': 584, '=': 584, '>': 584, '?': 556, '@': 1015, 'A': 667, 'B': 667,
    'C': 722, 'D': 722, 'E': 667, 'F': 611, 'G': 778, 'H': 722, 'I': 278,
    'J': 500, 'K': 667, 'L': 556, 'M': 833, 'N': 722, 'O': 778, 'P': 667,
    'Q': 778, 'R': 722, 'S': 667, 'T': 611, 'U': 722, 'V': 667, 'W': 944,
    'X': 667, 'Y': 667, 'Z': 611, '[': 278, '\\': 278, ']': 278, '^': 469,
    '_': 556, '`': 333, 'a': 556, 'b': 556, 'c': 500, 'd': 556, 'e': 556,
    'f': 278, 'g': 556, 'h': 556, 'i': 222, 'j': 222, 'k': 500, 'l': 222,
    'm': 833, 'n': 556, 'o': 556, 'p': 556, 'q': 556, 'r': 333, 's': 500,
    't': 278, 'u': 556, 'v': 500, 'w': 722, 'x': 500, 'y': 500, 'z': 500,
    '{': 334, '|': 260, '}': 334, '~': 584,
}
_BOLD = {
    ' ': 278, '!': 333, '"': 474, '#': 556, '$': 556, '%': 889, '&': 722,
    "'": 238, '(': 333, ')': 333, '*': 389, '+': 584, ',': 278, '-': 333,
    '.': 278, '/': 278, '0': 556, '1': 556, '2': 556, '3': 556, '4': 556,
    '5': 556, '6': 556, '7': 556, '8': 556, '9': 556, ':': 333, ';': 333,
    '<': 584, '=': 584, '>': 584, '?': 611, '@': 975, 'A': 722, 'B': 722,
    'C': 722, 'D': 722, 'E': 667, 'F': 611, 'G': 778, 'H': 722, 'I': 278,
    'J': 556, 'K': 722, 'L': 611, 'M': 833, 'N': 722, 'O': 778, 'P': 667,
    'Q': 778, 'R': 722, 'S': 667, 'T': 611, 'U': 722, 'V': 667, 'W': 944,
    'X': 667, 'Y': 667, 'Z': 611, '[': 333, '\\': 278, ']': 333, '^': 584,
    '_': 556, '`': 333, 'a': 556, 'b': 611, 'c': 556, 'd': 611, 'e': 556,
    'f': 333, 'g': 611, 'h': 611, 'i': 278, 'j': 278, 'k': 556, 'l': 278,
    'm': 889, 'n': 611, 'o': 611, 'p': 611, 'q': 611, 'r': 389, 's': 556,
    't': 333, 'u': 611, 'v': 556, 'w': 778, 'x': 556, 'y': 556, 'z': 500,
    '{': 389, '|': 280, '}': 389, '~': 584,
}

FALLBACK = 1.25      # tolerate a substitute sans up to 25% wider than Arial
FLAT_REG = 0.62      # flat per-character floor, regular
FLAT_BOLD = 0.68     # flat per-character floor, bold
LH = 1.32            # line-height multiple
ASCENT = 0.80        # baseline offset inside a line slot, in em
DESCENT = 0.26       # descender depth below the baseline, in em


def exact_width(s, fs, bold=False):
    """Width of `s` in Helvetica/Arial at font-size `fs`. No safety factor."""
    tbl = _BOLD if bold else _REG
    return sum(tbl.get(ch, 700) for ch in s) / 1000.0 * fs


def flat_width(s, fs, bold=False):
    """Flat per-character bound; independent of the advance table."""
    return (FLAT_BOLD if bold else FLAT_REG) * fs * len(s)


def text_width(s, fs, bold=False):
    """Layout width: satisfies the exact metric with FALLBACK headroom AND
    the flat per-character bound at the same time."""
    return max(exact_width(s, fs, bold) * FALLBACK, flat_width(s, fs, bold))


def wrap(text, max_w, fs, bold=False):
    """Greedy word wrap. Never emits a line wider than max_w under
    text_width(); an over-long single token is hard split."""
    lines, cur = [], ''
    for w in text.split():
        trial = w if not cur else cur + ' ' + w
        if text_width(trial, fs, bold) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ''
        if text_width(w, fs, bold) <= max_w:
            cur = w
        else:
            piece = ''
            for ch in w:
                if text_width(piece + ch, fs, bold) <= max_w:
                    piece += ch
                else:
                    lines.append(piece)
                    piece = ch
            cur = piece
    if cur:
        lines.append(cur)
    return lines or ['']


def block_h(n, fs):
    return n * LH * fs


def baseline(top, i, fs):
    return top + LH * fs * i + LH * fs * ASCENT


# ==========================================================================
# CONTENT  (all copy lives here, separate from the layout code)
# ==========================================================================
TITLE = 'How BALANCECHECK works'
SUBTITLE = ('Draft the reply, check every claim, stop safely, '
            'and learn from human decisions.')
BANNER = ('Every draft, check, tool call, decision and score is recorded '
          'in one event log.')

NODES = {
    'n1': ('process', '1. Read the account records',
           'Invoices, payments, credit notes and how each payment was applied.'),
    'n2': ('process', '2. Calculate the trusted facts',
           'Code computes open amounts, invoice status and the final balance. '
           'The LLM does not do the maths.'),
    'n3': ('process', '3. Write the client reply',
           'A small language model turns those trusted facts into a clear '
           'email. The same prompt carries retrieved approved examples and, '
           'on a re-draft, the exact correction.'),
    'n4': ('process', '4. Break the reply into claims',
           'Code pulls out six kinds of claim: document references, amounts, '
           'totals, dates, statuses and softer statements. Anything it spots '
           'but cannot pin to a ledger figure is escalated, never dropped.'),
    'n5': ('process', '5. Check every claim',
           'Code checks factual claims against the ledger. An isolated LLM '
           "checks only soft claims such as 'as discussed'. Two more checks "
           'read the whole draft: the listed amounts must match the totals, '
           'and required content must be present.'),
    'rev': ('loop', 'Revision path',
            'The gate sends back the exact correction and the model writes a '
            'new reply. At most two revisions, and a repeated draft escalates '
            'instead of looping. An opt-in mode replaces the re-draft with a '
            'bounded tool-using agent.'),
    'hum': ('human', '7. Human review',
            'A person approves, edits or declines the reply. Nothing is ever '
            'sent automatically. An edited reply is re-checked by code before '
            'it is stored.'),
    'stop': ('terminal', 'Safe stop',
             'Abstain when the records cannot support any true reply. The '
             'clearest case is caught before drafting, so no model call is '
             'spent. Escalate when a claim cannot be verified, the revision '
             'budget runs out, or the draft repeats itself.'),
    'n8': ('memory', '8. Learn from the human decision',
           'Approved and edited replies become examples, keyed by the shape '
           'of the ledger case. Declines are logged but not learned from. '
           'Evaluation scenarios never enter memory.'),
    'n9': ('memory', '9. Improve future drafts',
           'The next similar account receives the most relevant approved '
           'examples in its drafting prompt.'),
}

DECISION = '6. What did the checks find? (fixed rules, no model)'
LAB_LEFT = 'Fixable error'
LAB_CTR = 'Correct and complete'
LAB_RIGHT = 'Ambiguous or unverifiable'
FB_LEFT = 'Re-draft with the exact correction'
FB_RIGHT = 'Used on a future similar account'

DESC = ('Vertical flowchart of the BALANCECHECK pipeline. Step 1 reads the '
        'account records: invoices, payments, credit notes and how each '
        'payment was applied. Step 2 computes the trusted facts in code. '
        'Step 3 drafts the client reply with a small language model. Step 4 '
        'breaks the reply into six kinds of claim. Step 5 checks every claim, '
        'in code for factual claims and with an isolated model for soft '
        'claims, plus two whole-draft checks. Step 6 applies a fixed rule '
        'table with no model involved, and has three outcomes. A fixable '
        'error takes the revision path, which loops back to step 3 so the '
        'model re-drafts with the exact correction and the checks run again. '
        'A correct and complete draft goes to step 7, human '
        'review, where a person approves, edits or declines it. An ambiguous '
        'or unverifiable case leads to a safe stop: abstain or escalate. '
        'Step 8 '
        'learns from the human decision and step 9 improves future drafts, '
        'feeding approved examples back into step 3. Every draft, check, tool '
        'call, decision and score is recorded in one event log.')

# ==========================================================================
# palette
# ==========================================================================
BG = '#ffffff'
INK = '#161b22'
MUTED = '#454e58'
EDGE = '#4c555f'
FB_EDGE = '#7a5aa8'
FONT = 'Helvetica Neue, Helvetica, Arial, sans-serif'

KIND = {
    #             fill       stroke     accent     legend label
    'process':   ('#f4f7fb', '#c3ccd6', '#2f6feb', 'Pipeline step'),
    'decision':  ('#f6f2fd', '#c9bce6', '#6b46c1', 'Decision'),
    'loop':      ('#fdf6ec', '#e0c48f', '#b8730a', 'Loops back'),
    'human':     ('#f1faf3', '#a9d5b5', '#1a7f37', 'Human decision'),
    'terminal':  ('#fdf3f2', '#e4bab4', '#b3261e', 'Stops, nothing is sent'),
    'memory':    ('#eff9fa', '#a5cfd6', '#0f6f7d', 'Learning loop'),
    'log':       ('#f2f3f5', '#c8ccd2', '#57606a', 'Event log'),
}

# ==========================================================================
# 2. LAYOUT constants
# ==========================================================================
MARGIN = 40
BR_W = 336                 # branch column width
BR_GAP = 46                # gap between branch columns
PITCH = BR_W + BR_GAP
CONTENT_W = 3 * BR_W + 2 * BR_GAP
MAIN_W = 700
GUT_OFF = 46               # feedback gutter, outside the content columns
GUT_LABEL = 22             # room for the rotated gutter label

PAD_X = 14
ACCENT_W = 5
ACCENT_PAD = 3
TEXT_L_OFF = ACCENT_PAD + ACCENT_W + 12
PAD_T = 12
PAD_B = 12
HEAD_GAP = 4
V_GAP = 34

FS_HEAD = 17.0
FS_BODY = 14.0
FS_LABEL = 13.5

CX = MARGIN + CONTENT_W / 2.0 + GUT_OFF + GUT_LABEL
CONTENT_L = CX - CONTENT_W / 2.0
CONTENT_R = CX + CONTENT_W / 2.0
MAIN_X = CX - MAIN_W / 2.0
GUT_L = CONTENT_L - GUT_OFF
GUT_R = CONTENT_R + GUT_OFF
LANE = (CX - PITCH, CX, CX + PITCH)


# ==========================================================================
# Doc: render buffer, bounds and collision registries (no globals)
# ==========================================================================
def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def f(v):
    return ('%.2f' % v).rstrip('0').rstrip('.')


class Doc(object):
    def __init__(self):
        self.out = []
        self.bounds = [1e9, 1e9, -1e9, -1e9]
        self.boxes = []    # (id, x0, y0, x1, y1) - never overlap each other
        self.labels = []   # (id, x0, y0, x1, y1) - never overlap anything
        self.edges = []    # (id, [(x, y), ...]) - never cross a box or label

    def emit(self, s):
        self.out.append(s)

    def bnd(self, x0, y0, x1, y1):
        b = self.bounds
        b[0] = min(b[0], x0)
        b[1] = min(b[1], y0)
        b[2] = max(b[2], x1)
        b[3] = max(b[3], y1)

    def text(self, x, y, s, fs, bold=False, anchor='start', fill=INK, extra=''):
        self.emit('<text x="%s" y="%s" font-size="%s"%s%s fill="%s"%s>%s</text>'
                  % (f(x), f(y), f(fs),
                     ' font-weight="700"' if bold else '',
                     '' if anchor == 'start' else ' text-anchor="%s"' % anchor,
                     fill, (' ' + extra) if extra else '', esc(s)))

    # -- free-standing text run (title, subtitle, legend): registered as a
    #    label so nothing may be placed on top of it
    def run(self, name, x, y, s, fs, bold=False, fill=INK):
        self.text(x, baseline(y, 0, fs), s, fs, bold, fill=fill)
        w = text_width(s, fs, bold)
        self.bnd(x, y, x + w, y + block_h(1, fs))
        self.labels.append((name, x, y, x + w, y + block_h(1, fs)))
        return w, block_h(1, fs)


# ==========================================================================
# shapes
# ==========================================================================
class Box(object):
    def __init__(self, nid, kind, heading, body, x, w,
                 hfs=FS_HEAD, bfs=FS_BODY):
        self.id = nid
        self.kind = kind
        self.x = x
        self.w = w
        self.hfs = hfs
        self.bfs = bfs
        self.inner = w - TEXT_L_OFF - PAD_X
        self.hlines = wrap(heading, self.inner, hfs, True)
        self.blines = wrap(body, self.inner, bfs, False) if body else []
        self.h = PAD_T + block_h(len(self.hlines), hfs)
        if self.blines:
            self.h += HEAD_GAP + block_h(len(self.blines), bfs)
        self.h += PAD_B
        self.y = 0.0

    cx = property(lambda self: self.x + self.w / 2.0)
    cy = property(lambda self: self.y + self.h / 2.0)
    right = property(lambda self: self.x + self.w)
    bottom = property(lambda self: self.y + self.h)

    def place(self, y):
        self.y = y
        return self

    def render(self, doc):
        fill, stroke, accent, _ = KIND[self.kind]
        dash = ' stroke-dasharray="7 4"' if self.kind == 'loop' else ''
        doc.emit('<g role="group" aria-label="%s">' % esc(self.hlines[0]))
        doc.emit('<rect x="%s" y="%s" width="%s" height="%s" rx="11" ry="11" '
                 'fill="%s" stroke="%s" stroke-width="1.6"%s/>'
                 % (f(self.x), f(self.y), f(self.w), f(self.h),
                    fill, stroke, dash))
        doc.emit('<rect x="%s" y="%s" width="%s" height="%s" rx="2.5" ry="2.5" '
                 'fill="%s"/>'
                 % (f(self.x + ACCENT_PAD), f(self.y + 9), f(ACCENT_W),
                    f(max(self.h - 18, 6)), accent))
        tx = self.x + TEXT_L_OFF
        top = self.y + PAD_T
        for i, ln in enumerate(self.hlines):
            doc.text(tx, baseline(top, i, self.hfs), ln, self.hfs, True, fill=INK)
        top += block_h(len(self.hlines), self.hfs) + HEAD_GAP
        for i, ln in enumerate(self.blines):
            doc.text(tx, baseline(top, i, self.bfs), ln, self.bfs, False,
                     fill=MUTED)
        doc.emit('</g>')
        doc.bnd(self.x, self.y, self.right, self.bottom)
        doc.boxes.append((self.id, self.x, self.y, self.right, self.bottom))


class Diamond(object):
    """Rhombus with half-axes a (horizontal) and b (vertical).

    Sizing is closed form, not a bbox approximation: for a line whose glyph
    box spans dy from the centre, the rhombus half-width available there is
    a * (1 - |dy| / b).  So a must satisfy

        a >= (line_half_width + pad) / (1 - dy_max / b)

    for every line; we take the max.  b is derived from the text height.
    """

    def __init__(self, nid, heading, cx, wrap_w, hfs=FS_HEAD, pad_x=16.0,
                 pad_y=30.0):
        self.id = nid
        self.kind = 'decision'
        self.hfs = hfs
        self.lines = wrap(heading, wrap_w, hfs, True)
        n = len(self.lines)
        self.th = block_h(n, hfs)
        self.b = self.th / 2.0 + pad_y
        a = 0.0
        top = -self.th / 2.0
        for i, ln in enumerate(self.lines):
            slot_top = top + LH * hfs * i
            base = slot_top + LH * hfs * ASCENT
            dy = max(abs(base - hfs * ASCENT), abs(base + hfs * DESCENT))
            room = 1.0 - dy / self.b
            assert room > 0.05, 'diamond text taller than its rhombus'
            a = max(a, (text_width(ln, hfs, True) / 2.0 + pad_x) / room)
        self.a = a
        self.cx = cx
        self.cy = 0.0

    w = property(lambda self: 2 * self.a)
    h = property(lambda self: 2 * self.b)
    x = property(lambda self: self.cx - self.a)
    right = property(lambda self: self.cx + self.a)
    y = property(lambda self: self.cy - self.b)
    bottom = property(lambda self: self.cy + self.b)

    def place(self, top):
        self.cy = top + self.b
        return self

    def render(self, doc):
        fill, _stroke, accent, _ = KIND[self.kind]
        doc.emit('<g role="group" aria-label="%s">' % esc(self.lines[0]))
        doc.emit('<path d="M %s %s L %s %s L %s %s L %s %s Z" fill="%s" '
                 'stroke="%s" stroke-width="1.8"/>'
                 % (f(self.cx), f(self.y), f(self.right), f(self.cy),
                    f(self.cx), f(self.bottom), f(self.x), f(self.cy),
                    fill, accent))
        top = self.cy - self.th / 2.0
        for i, ln in enumerate(self.lines):
            doc.text(self.cx, baseline(top, i, self.hfs), ln, self.hfs, True,
                     anchor='middle', fill=INK)
        doc.emit('</g>')
        doc.bnd(self.x, self.y, self.right, self.bottom)
        doc.boxes.append((self.id, self.x, self.y, self.right, self.bottom))

    def half_width_at(self, dy):
        return self.a * (1.0 - abs(dy) / self.b)


class Chip(object):
    """Opaque label plate (grafted from strategy B) placed in a deliberate
    gap in its edge, so no line is ever drawn beneath it."""

    PAD_X = 9.0
    PAD_Y = 5.0

    def __init__(self, nid, text, max_w, fs=FS_LABEL, bold=True):
        self.id = nid
        self.fs = fs
        self.bold = bold
        self.lines = wrap(text, max_w - 2 * self.PAD_X, fs, bold)
        self.tw = max(text_width(ln, fs, bold) for ln in self.lines)
        self.w = self.tw + 2 * self.PAD_X
        self.h = block_h(len(self.lines), fs) + 2 * self.PAD_Y
        self.cx = 0.0
        self.cy = 0.0

    x = property(lambda self: self.cx - self.w / 2.0)
    right = property(lambda self: self.cx + self.w / 2.0)
    y = property(lambda self: self.cy - self.h / 2.0)
    bottom = property(lambda self: self.cy + self.h / 2.0)

    def place(self, cx, cy):
        self.cx, self.cy = cx, cy
        return self

    def render(self, doc, colour=INK):
        doc.emit('<rect x="%s" y="%s" width="%s" height="%s" rx="6" ry="6" '
                 'fill="%s" stroke="#d8dde3" stroke-width="1"/>'
                 % (f(self.x), f(self.y), f(self.w), f(self.h), BG))
        top = self.y + self.PAD_Y
        for i, ln in enumerate(self.lines):
            doc.text(self.cx, baseline(top, i, self.fs), ln, self.fs,
                     self.bold, anchor='middle', fill=colour)
        doc.bnd(self.x, self.y, self.right, self.bottom)
        doc.labels.append((self.id, self.x, self.y, self.right, self.bottom))


# ==========================================================================
# 3. ROUTE
# ==========================================================================
def rounded_path(pts, r=13.0):
    if len(pts) < 3:
        return 'M %s %s L %s %s' % (f(pts[0][0]), f(pts[0][1]),
                                    f(pts[-1][0]), f(pts[-1][1]))
    d = ['M %s %s' % (f(pts[0][0]), f(pts[0][1]))]
    for i in range(1, len(pts) - 1):
        px, py = pts[i - 1]
        cx, cy = pts[i]
        nx, ny = pts[i + 1]
        d1 = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
        d2 = ((nx - cx) ** 2 + (ny - cy) ** 2) ** 0.5
        rr = min(r, d1 / 2.0, d2 / 2.0)
        if rr <= 0.5:
            d.append('L %s %s' % (f(cx), f(cy)))
            continue
        d.append('L %s %s' % (f(cx - (cx - px) / d1 * rr),
                              f(cy - (cy - py) / d1 * rr)))
        d.append('Q %s %s %s %s' % (f(cx), f(cy),
                                    f(cx + (nx - cx) / d2 * rr),
                                    f(cy + (ny - cy) / d2 * rr)))
    d.append('L %s %s' % (f(pts[-1][0]), f(pts[-1][1])))
    return ' '.join(d)


def edge(doc, name, pts, color=EDGE, marker='ah', width=1.9, dashed=False):
    doc.emit('<path d="%s" fill="none" stroke="%s" stroke-width="%s"%s '
             'stroke-linecap="round"%s/>'
             % (rounded_path(pts), color, f(width),
                ' stroke-dasharray="6 5"' if dashed else '',
                (' marker-end="url(#%s)"' % marker) if marker else ''))
    for (x, y) in pts:
        doc.bnd(x - 7, y - 7, x + 7, y + 7)
    doc.edges.append((name, list(pts)))


def vlabel(doc, name, text, cx, cy, fs=FS_LABEL, color=FB_EDGE):
    """Rotated (-90) label inside a feedback gutter."""
    w = text_width(text, fs, False)
    yy = cy + fs * 0.34
    doc.emit('<text x="%s" y="%s" font-size="%s" fill="%s" '
             'text-anchor="middle" transform="rotate(-90 %s %s)">%s</text>'
             % (f(cx), f(yy), f(fs), color, f(cx), f(yy), esc(text)))
    x0, x1 = cx - fs * 0.66, cx + fs * 0.66
    y0, y1 = cy - w / 2.0, cy + w / 2.0
    doc.bnd(x0, y0, x1, y1)
    doc.labels.append((name, x0, y0, x1, y1))


# ==========================================================================
# 2. LAYOUT / build
# ==========================================================================
def mk(nid, x, w, hfs=FS_HEAD, bfs=FS_BODY):
    kind, head, body = NODES[nid]
    return Box(nid, kind, head, body, x, w, hfs, bfs)


def build(doc):
    y = MARGIN

    # ---- title block -----------------------------------------------------
    _, hh = doc.run('title', CONTENT_L, y, TITLE, 30.0, True, INK)
    y += hh + 2
    _, hh = doc.run('subtitle', CONTENT_L, y, SUBTITLE, 16.0, False, MUTED)
    y += hh + 20

    # ---- banner (the event-log strip) ------------------------------------
    banner = Box('banner', 'log', BANNER, '', CONTENT_L, CONTENT_W, 15.5)
    banner.place(y)
    y = banner.bottom + 30

    # ---- main spine, part one -------------------------------------------
    n1 = mk('n1', MAIN_X, MAIN_W).place(y)
    n2 = mk('n2', MAIN_X, MAIN_W).place(n1.bottom + V_GAP)
    n3 = mk('n3', MAIN_X, MAIN_W).place(n2.bottom + V_GAP)
    n4 = mk('n4', MAIN_X, MAIN_W).place(n3.bottom + V_GAP)
    n5 = mk('n5', MAIN_X, MAIN_W).place(n4.bottom + V_GAP)

    # ---- decision --------------------------------------------------------
    dia = Diamond('dia', DECISION, CX, MAIN_W * 0.5).place(n5.bottom + V_GAP)

    # ---- branch fan ------------------------------------------------------
    chips = [Chip('lab_left', LAB_LEFT, BR_W),
             Chip('lab_ctr', LAB_CTR, BR_W),
             Chip('lab_right', LAB_RIGHT, BR_W)]
    bus_y = dia.bottom + 34
    chip_cy = bus_y + 22 + max(c.h for c in chips) / 2.0
    branch_top = chip_cy + max(c.h for c in chips) / 2.0 + 22

    rev = mk('rev', LANE[0] - BR_W / 2.0, BR_W).place(branch_top)
    hum = mk('hum', LANE[1] - BR_W / 2.0, BR_W).place(branch_top)
    stop = mk('stop', LANE[2] - BR_W / 2.0, BR_W).place(branch_top)
    branch_bottom = max(rev.bottom, hum.bottom, stop.bottom)

    # ---- main spine, part two -------------------------------------------
    n8 = mk('n8', MAIN_X, MAIN_W).place(branch_bottom + 42)
    n9 = mk('n9', MAIN_X, MAIN_W).place(n8.bottom + V_GAP)

    boxes = [banner, n1, n2, n3, n4, n5, dia, rev, hum, stop, n8, n9]

    # ================= render: boxes first, edges on top ==================
    for b in boxes:
        b.render(doc)

    def down(a, b_):
        edge(doc, '%s->%s' % (a.id, b_.id), [(CX, a.bottom), (CX, b_.y)])

    down(n1, n2)
    down(n2, n3)
    down(n3, n4)
    down(n4, n5)
    down(n5, dia)
    down(hum, n8)
    down(n8, n9)

    # decision fan: trunk to a bus, then one drop per lane, each drop split
    # around its chip so no line runs under a label
    edge(doc, 'dia->bus', [(CX, dia.bottom), (CX, bus_y)], marker=None)
    doc.emit('<path d="M %s %s L %s %s" fill="none" stroke="%s" '
             'stroke-width="1.9" stroke-linecap="round"/>'
             % (f(LANE[0]), f(bus_y), f(LANE[2]), f(bus_y), EDGE))
    doc.bnd(LANE[0], bus_y - 4, LANE[2], bus_y + 4)
    doc.edges.append(('bus', [(LANE[0], bus_y), (LANE[2], bus_y)]))

    for lane_x, chip, box in zip(LANE, chips, (rev, hum, stop)):
        chip.place(lane_x, chip_cy)
        edge(doc, 'bus->%s.a' % box.id,
             [(lane_x, bus_y), (lane_x, chip.y)], marker=None)
        edge(doc, 'bus->%s.b' % box.id,
             [(lane_x, chip.bottom), (lane_x, box.y)])
    for chip in chips:
        chip.render(doc)

    # ---- feedback edge 1: revision path back up to step 3 ---------------
    # The model re-drafts; extraction and checks then run again from scratch.
    # Nothing re-enters the extractor directly, so this must target n3.
    edge(doc, 'rev->n3',
         [(rev.x, rev.cy), (GUT_L, rev.cy), (GUT_L, n3.cy), (n3.x, n3.cy)],
         color=FB_EDGE, marker='ahfb', dashed=True)
    vlabel(doc, 'fb_left', FB_LEFT, GUT_L - GUT_LABEL * 0.55,
           (rev.cy + n3.cy) / 2.0)

    # ---- feedback edge 2: step 9 back into the drafting prompt ----------
    edge(doc, 'n9->n3',
         [(n9.right, n9.cy), (GUT_R, n9.cy), (GUT_R, n3.cy), (n3.right, n3.cy)],
         color=FB_EDGE, marker='ahfb', dashed=True)
    vlabel(doc, 'fb_right', FB_RIGHT, GUT_R + GUT_LABEL * 0.55,
           (n9.cy + n3.cy) / 2.0)

    # ---- legend ----------------------------------------------------------
    lx, ly = CONTENT_L, n9.bottom + 26
    fs, swx, row_h = 13.0, 13.0, 24.0
    for i, k in enumerate(['process', 'decision', 'loop', 'human',
                           'terminal', 'memory']):
        txt = KIND[k][3]
        w = swx + 7 + text_width(txt, fs, False)
        if lx + w > CONTENT_R and lx > CONTENT_L:
            lx, ly = CONTENT_L, ly + row_h
        doc.emit('<rect x="%s" y="%s" width="%s" height="%s" rx="3" ry="3" '
                 'fill="%s"/>' % (f(lx), f(ly), f(swx), f(swx), KIND[k][2]))
        doc.text(lx + swx + 7, ly + swx - 2.0, txt, fs, False, fill=MUTED)
        doc.bnd(lx, ly - 3, lx + w, ly + swx + 4)
        doc.labels.append(('legend_%s' % k, lx, ly - 3, lx + w, ly + swx + 4))
        lx += w + 26

    return boxes, chips, dia


# ==========================================================================
# 5a. VERIFY - model check
# ==========================================================================
def overlap(a, b, tol=0.5):
    return (a[1] < b[3] - tol and b[1] < a[3] - tol and
            a[2] < b[4] - tol and b[2] < a[4] - tol)


def seg_hits_rect(p, q, r, shrink=2.0):
    """Liang-Barsky: does segment p-q cross the interior of rect r
    (shrunk by `shrink` on each side)?"""
    x0, y0, x1, y1 = r[0] + shrink, r[1] + shrink, r[2] - shrink, r[3] - shrink
    if x1 <= x0 or y1 <= y0:
        return False
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, p[0] - x0), (dx, x1 - p[0]),
                   (-dy, p[1] - y0), (dy, y1 - p[1])):
        if pp == 0:
            if qq < 0:
                return False
        else:
            t = qq / float(pp)
            if pp < 0:
                if t > t1:
                    return False
                t0 = max(t0, t)
            else:
                if t < t0:
                    return False
                t1 = min(t1, t)
    return t1 - t0 > 1e-6


def pt_in_rect(pt, r, tol=2.5):
    return (r[0] - tol <= pt[0] <= r[2] + tol and
            r[1] - tol <= pt[1] <= r[3] + tol)


def check_model(doc, boxes, chips, dia, w, h, svg):
    bad = []
    for name, pts in doc.edges:
        ends = (pts[0], pts[-1])
        for br in doc.boxes:
            rect = (br[1], br[2], br[3], br[4])
            if any(pt_in_rect(e, rect) for e in ends):
                continue
            for i in range(len(pts) - 1):
                if seg_hits_rect(pts[i], pts[i + 1], rect):
                    bad.append('EDGE %s CROSSES BOX %s' % (name, br[0]))
                    break
        for lr in doc.labels:
            rect = (lr[1], lr[2], lr[3], lr[4])
            for i in range(len(pts) - 1):
                if seg_hits_rect(pts[i], pts[i + 1], rect, shrink=0.0):
                    bad.append('EDGE %s CROSSES LABEL %s' % (name, lr[0]))
                    break
    allr = doc.boxes + doc.labels
    for i in range(len(allr)):
        for j in range(i + 1, len(allr)):
            if overlap(allr[i], allr[j]):
                bad.append('OVERLAP %s <-> %s' % (allr[i][0], allr[j][0]))
    # text fit, under BOTH metrics, independently spelled out
    for b in boxes:
        if not isinstance(b, Box):
            continue
        for ln, fs, bold in ([(l, b.hfs, True) for l in b.hlines] +
                             [(l, b.bfs, False) for l in b.blines]):
            if exact_width(ln, fs, bold) * FALLBACK > b.inner + 0.01:
                bad.append('OVERFLOW(fallback x%.2f) %s: %r'
                           % (FALLBACK, b.id, ln))
            if flat_width(ln, fs, bold) > b.inner + 0.01:
                bad.append('OVERFLOW(flat) %s: %r' % (b.id, ln))
    for c in chips:
        for ln in c.lines:
            if max(exact_width(ln, c.fs, c.bold) * FALLBACK,
                   flat_width(ln, c.fs, c.bold)) > c.w - 2 * Chip.PAD_X + 0.01:
                bad.append('OVERFLOW chip %s: %r' % (c.id, ln))
    # diamond: true rhombus containment for every line, at its own dy
    top = dia.cy - dia.th / 2.0
    for i, ln in enumerate(dia.lines):
        base = baseline(top, i, dia.hfs)
        for edge_y in (base - dia.hfs * ASCENT, base + dia.hfs * DESCENT):
            half = text_width(ln, dia.hfs, True) / 2.0
            if half > dia.half_width_at(edge_y - dia.cy) - 0.01:
                bad.append('DIAMOND LINE OUTSIDE RHOMBUS: %r' % ln)
    if any(ord(ch) > 126 for ch in svg):
        bad.append('NON-ASCII in output')
    # Written as escapes on purpose: the repo forbids these characters in any
    # source file, so this guard must not contain them literally.
    if '\u2013' in svg or '\u2014' in svg:
        bad.append('EN/EM DASH in output')
    b = doc.bounds
    if b[0] < 0 or b[1] < 0 or b[2] > w or b[3] > h:
        bad.append('CONTENT OUTSIDE VIEWBOX %s vs %sx%s' % (b, w, h))
    # every spec string must actually be rendered
    rendered = ' '.join(seg for seg in svg.split('>') if seg)
    for s in ([TITLE, SUBTITLE, BANNER, DECISION, LAB_LEFT, LAB_CTR,
               LAB_RIGHT, FB_LEFT, FB_RIGHT] +
              [t for v in NODES.values() for t in v[1:]]):
        if esc(s) not in svg.replace('\n', ' '):
            # allow wrapped runs: check every word survives in order
            words = esc(s).split()
            pos, ok = 0, True
            for wd in words:
                pos = svg.find(wd, pos)
                if pos < 0:
                    ok = False
                    break
                pos += len(wd)
            if not ok:
                bad.append('SPEC STRING MISSING: %r' % s[:60])
    del rendered
    return bad


# ==========================================================================
# 5b. VERIFY - parse check (does not use the layout model at all)
# ==========================================================================
NS = '{http://www.w3.org/2000/svg}'


def check_parsed(svg):
    """Re-parse the serialized SVG and re-derive every text extent from the
    emitted attributes alone, then re-check containment and overlap."""
    bad = []
    root = ET.fromstring(svg)
    vb = [float(v) for v in root.get('viewBox').split()]
    rects, texts = [], []

    def walk(node, weight=None):
        for el in node:
            tag = el.tag
            if tag == NS + 'g':
                walk(el, weight)
            elif tag == NS + 'rect':
                rects.append((float(el.get('x')), float(el.get('y')),
                              float(el.get('x')) + float(el.get('width')),
                              float(el.get('y')) + float(el.get('height')),
                              el.get('fill'), el.get('stroke')))
            elif tag == NS + 'text':
                fs = float(el.get('font-size'))
                bold = el.get('font-weight') == '700'
                s = ''.join(el.itertext())
                w = exact_width(s, fs, bold) * FALLBACK
                anch = el.get('text-anchor', 'start')
                x, yy = float(el.get('x')), float(el.get('y'))
                if anch == 'middle':
                    x0 = x - w / 2.0
                elif anch == 'end':
                    x0 = x - w
                else:
                    x0 = x
                texts.append((s, x0, yy - fs * ASCENT, x0 + w,
                              yy + fs * DESCENT, el.get('transform')))
    walk(root)

    # background rect present, opaque, covering the whole viewBox
    bgs = [r for r in rects
           if r[0] <= vb[0] + 0.01 and r[1] <= vb[1] + 0.01
           and r[2] >= vb[0] + vb[2] - 0.01 and r[3] >= vb[1] + vb[3] - 0.01
           and r[4] and r[4] != 'none']
    if not bgs:
        bad.append('NO OPAQUE FULL-VIEWBOX BACKGROUND RECT')

    # every non-rotated text run inside the viewBox
    for (s, x0, y0, x1, y1, tr) in texts:
        if tr:
            continue
        if x0 < vb[0] or y0 < vb[1] or x1 > vb[0] + vb[2] or y1 > vb[1] + vb[3]:
            bad.append('TEXT OUTSIDE VIEWBOX: %r' % s[:40])

    # every text run that sits inside a stroked node rect must fit it
    nodes = [r for r in rects if r[5] and r[5] != 'none'
             and r[2] - r[0] < vb[2] - 20]
    for (s, x0, y0, x1, y1, tr) in texts:
        if tr:
            continue
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        for r in nodes:
            if r[0] <= cx <= r[2] and r[1] <= cy <= r[3]:
                if x0 < r[0] + 2 or x1 > r[2] - 2:
                    bad.append('PARSED TEXT OVERFLOWS ITS RECT: %r' % s[:40])
                break

    # stroked node rects must not overlap each other
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            a, b = nodes[i], nodes[j]
            if (a[0] < b[2] - 0.5 and b[0] < a[2] - 0.5 and
                    a[1] < b[3] - 0.5 and b[1] < a[3] - 0.5):
                bad.append('PARSED RECT OVERLAP %s <-> %s' % (a[:4], b[:4]))
    return bad


# ==========================================================================
# 4. RENDER shell + main
# ==========================================================================
def serialize(doc, W, H):
    head = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" '
            'viewBox="0 0 %s %s" preserveAspectRatio="xMidYMid meet" '
            'role="img" aria-labelledby="ttl dsc" font-family="%s">'
            % (f(W), f(H), f(W), f(H), FONT),
            '<title id="ttl">%s</title>' % esc(TITLE),
            '<desc id="dsc">%s</desc>' % esc(DESC),
            '<defs>']
    for mid, col in (('ah', EDGE), ('ahfb', FB_EDGE)):
        head.append('<marker id="%s" markerUnits="userSpaceOnUse" '
                    'markerWidth="12" markerHeight="9" refX="12" refY="4.5" '
                    'viewBox="0 0 12 9" orient="auto">'
                    '<path d="M 0 0 L 12 4.5 L 0 9 L 2.6 4.5 Z" fill="%s"/>'
                    '</marker>' % (mid, col))
    head.append('</defs>')
    head.append('<rect x="0" y="0" width="%s" height="%s" fill="%s"/>'
                % (f(W), f(H), BG))
    return '\n'.join(head + doc.out + ['</svg>']) + '\n'


def main():
    doc = Doc()
    boxes, chips, dia = build(doc)
    W = doc.bounds[2] + MARGIN
    H = doc.bounds[3] + MARGIN
    svg = serialize(doc, W, H)

    problems = check_model(doc, boxes, chips, dia, W, H, svg)
    problems += check_parsed(svg)

    print('canvas: %d x %d' % (int(W), int(H)))
    print('bounds: x %.1f..%.1f  y %.1f..%.1f'
          % (doc.bounds[0], doc.bounds[2], doc.bounds[1], doc.bounds[3]))
    print('boxes: %d  labels: %d  edges: %d'
          % (len(doc.boxes), len(doc.labels), len(doc.edges)))

    if problems:
        print('SELF-CHECK FAILED (%d) - nothing written:' % len(problems))
        for p in problems:
            print('  - ' + p)
        return 1

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'system_flowchart.svg')
    with open(path, 'w') as fh:
        fh.write(svg)
    print('SELF-CHECK OK (model + parse). wrote %s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
