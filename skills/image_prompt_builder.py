import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from models.schemas import Slot, VisualSpec


class ImagePromptBuilderSkill:
    """Build evidence-labelled graphics whose text follows the accepted piece."""

    ACCENT_COLORS = {
        "web3-security": "#00FFA3",
        "pentesting": "#FF3366",
        "agentic-security": "#00E5FF",
        "agentic-payments": "#FFB800",
    }
    EVIDENCE_LABELS = {
        "kb": "KB FINDING",
        "live": "WEB RESEARCH",
        "fixture": "DEMO FIXTURE",
    }

    @staticmethod
    def _compact(text: str, limit: int) -> str:
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        shortened = text[:limit - 3].rsplit(" ", 1)[0]
        return (shortened or text[:limit - 3]) + "..."

    def build_spec(self, slot: Slot) -> VisualSpec:
        service = slot.service or "web3-security"
        accent = self.ACCENT_COLORS.get(service, "#00FFA3")
        # The takeaway comes from content that actually passed editorial review.
        accepted_text = (slot.draft_tweets or [slot.draft_content or ""])[0]
        takeaway = re.sub(r"^\s*(?:\d+[/.:)]|\[\d+\])\s*", "", accepted_text)
        takeaway = re.sub(r"https?://[^\s)\]>]+", "", takeaway, flags=re.IGNORECASE)
        takeaway = re.sub(r"\[KB(?: Finding)?:[^\]]*\]", "", takeaway, flags=re.IGNORECASE)
        takeaway = " ".join(takeaway.split())
        if not takeaway:
            raise ValueError("A visual requires nonempty accepted content to use as its takeaway")
        title = self._compact(slot.resolved_topic or slot.focus or takeaway, 160)
        subtitle = f"SanadSquad Research // {service.upper()}"
        label = self.EVIDENCE_LABELS.get(slot.source_provenance, "RESEARCH NOTE")
        prompt = (
            f"Dark technical research card titled '{title}' with evidence label '{label}'. "
            f"Dark slate background (#0B0F19), {accent} accents, simple architectural nodes. "
            f"Use only this approved takeaway: {takeaway}. Aspect ratio 16:9."
        )
        return VisualSpec(
            title=title, subtitle=subtitle, theme="dark-cyber", accent_color=accent,
            key_takeaway=takeaway, image_prompt=prompt, evidence_label=label,
        )

    def validate_spec_alignment(self, slot: Slot, spec: VisualSpec) -> None:
        if slot.status != "accepted":
            raise ValueError("Visuals may only be generated for accepted content")
        expected = self.build_spec(slot)
        for field in ("title", "key_takeaway", "subtitle", "accent_color", "evidence_label"):
            if getattr(spec, field) != getattr(expected, field):
                raise ValueError(f"Visual {field} does not match the accepted content and evidence")

    @staticmethod
    @lru_cache(maxsize=128)
    def _font(size: int, bold: bool = False):
        for name in (("arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("arial.ttf", "DejaVuSans.ttf")):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default(size=size)

    @staticmethod
    def _text_width(draw, text, font):
        left, _, right, _ = draw.textbbox((0, 0), text, font=font, anchor="lt")
        return right - left

    @classmethod
    def _wrap_pixels(cls, draw, text: str, font, width: int):
        lines = []
        line = ""
        for word in text.split():
            candidate = f"{line} {word}" if line else word
            if cls._text_width(draw, candidate, font) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            # Split long identifiers by measured pixels using a bounded binary search.
            while cls._text_width(draw, word, font) > width:
                low, high = 1, len(word)
                while low < high:
                    middle = (low + high + 1) // 2
                    if cls._text_width(draw, word[:middle], font) <= width:
                        low = middle
                    else:
                        high = middle - 1
                lines.append(word[:low])
                word = word[low:]
            line = word
        if line:
            lines.append(line)
        return lines

    def _fit_text(self, draw, text: str, width: int, height: int, size: int, bold=False):
        def layout(font_size):
            font = self._font(font_size, bold)
            lines = self._wrap_pixels(draw, text, font, width)
            bounds = font.getbbox("Ag")
            line_height = bounds[3] - bounds[1] + 7
            return font, lines, line_height

        best = layout(12)
        if len(best[1]) * best[2] <= height:
            low, high = 13, max(12, size)
            while low <= high:
                middle = (low + high) // 2
                candidate = layout(middle)
                if len(candidate[1]) * candidate[2] <= height:
                    best = candidate
                    low = middle + 1
                else:
                    high = middle - 1
            return best
        font, lines, line_height = best
        # A card is a preview: an ellipsis visibly marks exceptionally long excerpts.
        count = max(1, height // line_height)
        lines = lines[:count]
        last = lines[-1] if lines else ""
        while last and self._text_width(draw, last + "...", font) > width:
            last = last[:-1]
        if lines:
            lines[-1] = last.rstrip() + "..."
        return font, lines, line_height

    def _draw_text_box(self, draw, text, box, color, size, bold=False):
        left, top, right, bottom = box
        font, lines, line_height = self._fit_text(draw, text, right - left, bottom - top, size, bold)
        for index, line in enumerate(lines):
            bearing_left, bearing_top, _, _ = draw.textbbox((0, 0), line, font=font, anchor="lt")
            draw.text((left - bearing_left, top + index * line_height - bearing_top),
                      line, fill=color, font=font, anchor="lt")

    def render_card_image(self, spec: VisualSpec, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        width, height = 1200, 675
        image = Image.new("RGB", (width, height), color="#0A0E17")
        draw = ImageDraw.Draw(image)
        for x in range(0, width, 50):
            draw.line([(x, 0), (x, height)], fill="#151D2A", width=1)
        for y in range(0, height, 50):
            draw.line([(0, y), (width, y)], fill="#151D2A", width=1)
        draw.rectangle([(20, 20), (width - 20, height - 20)], outline="#1E293B", width=2)
        draw.rectangle([(20, 20), (width - 20, 28)], fill=spec.accent_color)

        self._draw_text_box(draw, "SANADSQUAD", (60, 55, 250, 90), "#FFFFFF", 24, True)
        self._draw_text_box(draw, spec.subtitle, (280, 58, 1140, 94), spec.accent_color, 20, True)
        draw.rectangle([(60, 115), (310, 155)], outline=spec.accent_color, fill="#0F172A", width=1)
        self._draw_text_box(draw, spec.evidence_label, (75, 125, 295, 151), spec.accent_color, 20)
        self._draw_text_box(draw, spec.title, (60, 180, 1140, 335), "#F8FAFC", 42, True)

        draw.rectangle([(60, 360), (1140, 560)], fill="#0F172A", outline="#334155", width=2)
        draw.rectangle([(60, 360), (66, 560)], fill=spec.accent_color)
        self._draw_text_box(draw, "KEY TAKEAWAY", (90, 380, 1110, 410), "#94A3B8", 20)
        self._draw_text_box(draw, spec.key_takeaway, (90, 422, 1110, 545), "#E2E8F0", 26)
        self._draw_text_box(draw, "SanadSquad Security Intelligence", (60, 615, 620, 645), "#64748B", 20)
        self._draw_text_box(draw, "sanadsquad.com // @sanadsquad", (700, 615, 1140, 645), "#64748B", 20)
        image.save(output_path, "PNG")
        return output_path


image_prompt_skill = ImagePromptBuilderSkill()
