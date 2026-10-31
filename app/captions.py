"""Split narration scripts into sentences and short caption phrases, and write ASS subtitles.

The script text is never rewritten: captions show the words exactly as typed.
"""
import re

ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "prof", "mt", "no",
                 "approx", "e.g", "i.e", "a.m", "p.m", "u.s", "lt", "sgt", "capt", "gen"}
SOFT_BREAK = re.compile(r"[,;:—–…]+[\"'”’)\]]*$|-{2,}$")
HAS_WORD = re.compile(r"\w")


def split_sentences(text, lines=False):
    """Return a list of sentences. Blank lines and sentence punctuation both end a sentence;
    with lines=True (poems) every line break does too."""
    sentences = []
    blocks = re.split(r"\n\s*\n|\r\n\s*\r\n", text.strip())
    if lines:
        blocks = [line for block in blocks for line in block.splitlines()]
    for paragraph in blocks:
        paragraph = " ".join(paragraph.split())
        if not paragraph:
            continue
        # Cut after . ! ? or an ellipsis (plus any closing quotes) followed by whitespace.
        parts, start = [], 0
        for match in re.finditer(r"[.!?…]+[\"'”’)\]]*\s+", paragraph):
            parts.append(paragraph[start:match.end()].strip())
            start = match.end()
        parts.append(paragraph[start:].strip())
        current = ""
        for part in filter(None, parts):
            current = f"{current} {part}".strip() if current else part
            last_word = current.split()[-1].rstrip(".").lower().strip("\"'(“")
            # "Mr. Smith" or an initial like "J." should not end a sentence.
            if last_word in ABBREVIATIONS or (len(last_word) == 1 and last_word.isalpha()):
                continue
            sentences.append(current)
            current = ""
        if current:
            sentences.append(current)
    return sentences


WEAK_ENDINGS = {"a", "an", "the", "of", "to", "in", "on", "at", "for", "with", "and", "or", "but",
                "your", "my", "his", "her", "their", "our", "its", "from", "by", "into", "as", "is",
                "was", "that", "this", "very", "so", "be"}


def _bare(word):
    return re.sub(r"[^\w']", "", word).lower()


def _split_clause(indexes, words, target):
    """Split one clause into balanced chunks, nudging cuts so a chunk doesn't end on 'the', 'of'..."""
    n = len(indexes)
    if n <= target + 1:
        return [indexes]
    k = -(-n // target)
    cuts, prev = [], 0
    for j in range(1, k):
        cut = round(j * n / k)
        for candidate in (cut, cut + 1, cut - 1):
            if prev + 2 <= candidate <= n - 2 and _bare(words[indexes[candidate - 1]]) not in WEAK_ENDINGS:
                cut = candidate
                break
        cuts.append(cut)
        prev = cut
    bounds = [0, *cuts, n]
    return [indexes[a:b] for a, b in zip(bounds, bounds[1:]) if b > a]


def split_phrases(words, target):
    """Group a sentence's words into caption phrases of about `target` words.

    Returns lists of word indexes. Clauses (split at , ; : dashes) are kept together when short,
    tiny clauses join the next one, and long clauses are split into even chunks."""
    target = max(1, int(target))
    clauses, current = [], []
    for i, word in enumerate(words):
        current.append(i)
        if SOFT_BREAK.search(word):
            clauses.append(current)
            current = []
    if current:
        clauses.append(current)
    merged = []
    for clause in clauses:
        # A punctuation-only "word" (a lone dash) belongs with what came before.
        if merged and not any(HAS_WORD.search(words[i]) for i in clause):
            merged[-1].extend(clause)
        elif merged and len(merged[-1]) <= 2 and len(merged[-1]) + len(clause) <= 2 * target:
            merged[-1].extend(clause)   # "Naturally," + "you did what..." read better together
        else:
            merged.append(clause)
    phrases = []
    for clause in merged:
        phrases += _split_clause(clause, words, target)
    return phrases


def build_captions(words, word_times, target, offset=0.0):
    """Turn per-word timings into caption events: [{'start','end','text'}] (seconds)."""
    events = []
    for group in split_phrases(words, target):
        start = word_times[group[0]][0]
        end = max(word_times[i][1] for i in group)
        events.append({"start": offset + start, "end": offset + end,
                       "text": " ".join(words[i] for i in group)})
    return events


def _ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_color(hex_color, alpha=0):
    value = (hex_color or "#FFFFFF").lstrip("#")
    if len(value) != 6:
        value = "FFFFFF"
    r, g, b = value[0:2], value[2:4], value[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ass_escape(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def caption_margins(style, play_w=1080, play_h=1920):
    """Caption box from the X/Y/width settings (same rule as the browser preview).

    position_x: box center, % from the left. max_width: box width, % of the frame.
    position: bottom of the text, % from the bottom. Near an edge the box narrows (text wraps
    more) so the caption really is centered at X; it never leaves the frame."""
    x = min(max(float(style.get("position_x", 50)), 0.0), 100.0)
    half_pct = min(float(style.get("max_width", 84)) / 2, x, 100 - x)
    half_pct = max(half_pct, 12.0)                     # keep at least ~24% width for a word or two
    center_pct = min(max(x, half_pct), 100 - half_pct)
    half, center = play_w * half_pct / 100, play_w * center_pct / 100
    margin_v = int(play_h * float(style.get("position", 26)) / 100)
    return int(round(center - half)), int(round(play_w - center - half)), margin_v


# Watermark anchor -> ASS n alignment (which point of the text sits at X/Y).
WATERMARK_ANCHORS = {"tl": 7, "tc": 8, "tr": 9, "ml": 4, "c": 5, "mr": 6, "bl": 1, "bc": 2, "br": 3}


def watermark_line(wm, play_w, play_h, duration):
    """One Dialogue line showing the watermark for the whole video. X/Y are % of the frame
    (from the left / from the top) of the anchor point, so "br" at 96/96 hugs the bottom-right."""
    text = (wm.get("text") or "").strip()
    if not wm.get("enabled") or not text:
        return None
    an = WATERMARK_ANCHORS.get(wm.get("anchor"), 3)
    x = play_w * min(max(float(wm.get("position_x", 96)), 0), 100) / 100
    y = play_h * min(max(float(wm.get("position_y", 96)), 0), 100) / 100
    alpha = int(round(255 * (1 - min(max(float(wm.get("opacity", 70)), 0), 100) / 100)))
    tags = (rf"{{\an{an}\pos({x:.0f},{y:.0f})\q2\alpha&H{alpha:02X}&"
            rf"\4a&H{min(255, alpha + 120):02X}&}}")
    return f"Dialogue: 1,{_ass_time(0)},{_ass_time(duration + 1)},Watermark,,0,0,0,,{tags}{_ass_escape(text)}"


def write_ass(path, events, style, width=1080, height=1920, watermark=None, duration=0.0):
    """Write subtitles in a 1080x1920 reference frame, so sizes are 1080p pixels at any output or
    preview resolution."""
    play_w, play_h = 1080, 1920
    margin_l, margin_r, margin_v = caption_margins(style, play_w, play_h)
    bold = -1 if style.get("bold", True) else 0
    shadow = float(style.get("shadow", 2))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_w}",
        f"PlayResY: {play_h}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,{font},{size},{primary},{primary},{outline},{back},{bold},{italic},0,0,100,100,0,0,1,"
        "{ow},{shadow},2,{ml},{mr},{mv},1".format(
            font=style.get("font", "Arial Black").replace(",", " "),
            size=int(style.get("size", 84)),
            primary=_ass_color(style.get("color")),
            outline=_ass_color(style.get("outline_color", "#000000")),
            back=_ass_color("#000000", 0x60),
            bold=bold, italic=-1 if style.get("italic") else 0, ow=float(style.get("outline", 6)), shadow=0,      # the shadow is its own soft layer (below)
            ml=margin_l, mr=margin_r, mv=margin_v),
        "Style: Watermark,{font},{size},{color},{color},{outline},&H00000000,{bold},{italic},0,0,100,100,0,0,1,"
        "{ow},{sh},3,0,0,0,1".format(
            font=(watermark or {}).get("font", "Arial").replace(",", " "),
            size=int((watermark or {}).get("size", 40)),
            color=_ass_color((watermark or {}).get("color", "#FFFFFF")),
            outline=_ass_color((watermark or {}).get("outline_color", "#000000")),
            bold=-1 if (watermark or {}).get("bold", True) else 0, italic=-1 if (watermark or {}).get("italic") else 0,
            ow=float((watermark or {}).get("outline", 2)), sh=float((watermark or {}).get("shadow", 1))),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    # A soft drop shadow, like the preview: drawn on its own layer under the text (invisible text,
    # blurred shadow straight below), so the letters and outline stay sharp. libass's plain shadow is
    # a hard-edged copy offset down-right, which looks like doubled letters.
    soft = (rf"{{\1a&HFF&\3a&HFF&\shad0\xshad0\yshad{shadow:g}\blur{shadow * 0.7:.1f}\4a&H50&}}"
            if shadow > 0 else "")
    for ev in events:
        text = _ass_escape(ev["text"].upper() if style.get("uppercase") else ev["text"])
        when = f"{_ass_time(ev['start'])},{_ass_time(ev['end'])}"
        if soft:
            lines.append(f"Dialogue: 0,{when},Default,,0,0,0,,{soft}{text}")
        lines.append(f"Dialogue: 1,{when},Default,,0,0,0,,{text}")
    wm_line = watermark_line(watermark or {}, play_w, play_h, duration)
    if wm_line:
        lines.append(wm_line)
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
