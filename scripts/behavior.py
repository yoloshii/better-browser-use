"""Human-like behavioral simulation for browser automation.

Detection systems analyze behavior patterns beyond just fingerprints.
This module provides realistic human-like interactions:
- Bezier curve mouse movements (not linear)
- Variable typing speeds with typo patterns
- Reading time based on content length
- Natural pauses and scrolling behavior

Extracted from ultimate-scraper behavior/human.py — 100% self-contained,
stdlib only, no external dependencies.
"""

import random
import asyncio
import math
from dataclasses import dataclass
from typing import List, Tuple, Any


@dataclass
class Point:
    """2D point for mouse movement."""
    x: float
    y: float


class BezierCurve:
    """Generate Bezier curve points for natural mouse movement.

    Linear mouse movements are a strong bot indicator.
    Bezier curves simulate the natural arc of human hand movement.
    """

    @staticmethod
    def generate_points(
        start: Tuple[int, int],
        end: Tuple[int, int],
        steps: int = 50,
        curvature: float = 0.3,
    ) -> List[Tuple[int, int]]:
        """Generate points along a cubic Bezier curve between start and end."""
        curvature = curvature * (0.8 + random.random() * 0.4)

        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = math.sqrt(dx * dx + dy * dy)

        offset = distance * curvature * (1 if random.random() > 0.5 else -1)

        if distance > 0:
            perp_x = -dy / distance
            perp_y = dx / distance
        else:
            perp_x, perp_y = 0, 1

        cp1 = (
            start[0] + dx * 0.33 + perp_x * offset * random.uniform(0.5, 1.5),
            start[1] + dy * 0.33 + perp_y * offset * random.uniform(0.5, 1.5),
        )
        cp2 = (
            start[0] + dx * 0.67 + perp_x * offset * random.uniform(-0.5, 0.5),
            start[1] + dy * 0.67 + perp_y * offset * random.uniform(-0.5, 0.5),
        )

        points = []
        for i in range(steps + 1):
            t = i / steps
            x = (
                (1 - t) ** 3 * start[0]
                + 3 * (1 - t) ** 2 * t * cp1[0]
                + 3 * (1 - t) * t ** 2 * cp2[0]
                + t ** 3 * end[0]
            )
            y = (
                (1 - t) ** 3 * start[1]
                + 3 * (1 - t) ** 2 * t * cp1[1]
                + 3 * (1 - t) * t ** 2 * cp2[1]
                + t ** 3 * end[1]
            )
            points.append((int(x), int(y)))

        return points

    @staticmethod
    def generate_movement_delays(
        steps: int,
        base_delay_ms: float = 5,
        variance: float = 0.5,
    ) -> List[float]:
        """Generate delays between mouse movement steps.

        Humans slow down at the start and end of movements (ease-in-out).
        """
        delays = []
        for i in range(steps):
            t = i / steps if steps > 0 else 0
            ease = t * t * (3 - 2 * t)
            speed_factor = 0.5 + abs(0.5 - ease)
            delay = base_delay_ms * speed_factor * (1 + (random.random() - 0.5) * variance)
            delays.append(max(1, delay))

        return delays


class HumanTyping:
    """Simulate human-like typing patterns.

    Humans don't type at constant speeds. This class models:
    - Variable inter-key delays based on character pairs
    - Slower typing for special characters
    - Occasional pauses (as if thinking)
    """

    BASE_DELAY = 80  # ms

    CHAR_MULTIPLIERS = {
        "space": 1.2,
        "uppercase": 1.3,
        "punctuation": 1.5,
        "number": 1.1,
    }

    FAST_DIGRAPHS = {
        "th", "he", "in", "er", "an", "re", "on", "at", "en", "nd",
        "ti", "es", "or", "te", "of", "ed", "is", "it", "al", "ar",
        "st", "to", "nt", "ng", "se", "ha", "as", "ou", "io", "le",
        "ve", "co", "me", "de", "hi", "ri", "ro", "ic", "ne", "ea",
    }

    # Typo simulation: probability and adjacent key map (QWERTY layout)
    TYPO_PROBABILITY = 0.03  # 3% per character

    ADJACENT_KEYS = {
        "a": "sqwz", "b": "vghn", "c": "xdfv", "d": "sfec", "e": "wrd",
        "f": "dgrc", "g": "fhtv", "h": "gjyn", "i": "uok", "j": "hkun",
        "k": "jlim", "l": "kop", "m": "njk", "n": "bhmj", "o": "iplk",
        "p": "ol", "q": "wa", "r": "eft", "s": "adwz", "t": "rgy",
        "u": "yij", "v": "cfgb", "w": "qase", "x": "zsdc", "y": "thu",
        "z": "xas",
    }

    @classmethod
    def get_inter_key_delay(
        cls,
        char: str,
        prev_char: str = "",
        intensity: float = 1.0,
    ) -> float:
        """Calculate delay before typing a character (milliseconds)."""
        delay = cls.BASE_DELAY

        if char == " ":
            delay *= cls.CHAR_MULTIPLIERS["space"]
        elif char.isupper():
            delay *= cls.CHAR_MULTIPLIERS["uppercase"]
        elif char in ".,!?;:":
            delay *= cls.CHAR_MULTIPLIERS["punctuation"]
        elif char.isdigit():
            delay *= cls.CHAR_MULTIPLIERS["number"]

        digraph = (prev_char + char).lower()
        if digraph in cls.FAST_DIGRAPHS:
            delay *= 0.7

        variance = delay * 0.3
        delay = max(20, delay + random.gauss(0, variance))

        if random.random() < 0.01:
            delay += random.uniform(200, 500)

        delay *= intensity
        return delay

    @classmethod
    async def type_text(
        cls,
        page: Any,
        text: str,
        intensity: float = 1.0,
    ) -> None:
        """Type text character-by-character with human-like timing.

        Uses page.keyboard.type() for each character individually,
        applying realistic inter-key delays. At intensity >= 0.8,
        injects occasional typos (wrong adjacent key → backspace → correct).
        """
        prev_char = ""
        for char in text:
            delay = cls.get_inter_key_delay(char, prev_char, intensity)
            await asyncio.sleep(delay / 1000)

            # Typo injection: wrong key → notice pause → backspace → correct key
            lower = char.lower()
            if (intensity >= 0.8
                    and lower in cls.ADJACENT_KEYS
                    and random.random() < cls.TYPO_PROBABILITY):
                wrong = random.choice(cls.ADJACENT_KEYS[lower])
                if char.isupper():
                    wrong = wrong.upper()
                await page.keyboard.type(wrong)
                await asyncio.sleep(random.uniform(0.15, 0.4) * intensity)
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.05, 0.15) * intensity)

            await page.keyboard.type(char)
            prev_char = char


class ReadingBehavior:
    """Simulate human reading patterns."""

    WPM = 250
    CHARS_PER_WORD = 5

    @classmethod
    def calculate_read_time(
        cls,
        content_length: int,
        has_images: bool = False,
        intensity: float = 1.0,
    ) -> float:
        """Calculate realistic reading time for content (seconds)."""
        words = content_length / cls.CHARS_PER_WORD
        minutes = words / cls.WPM
        seconds = minutes * 60

        if has_images:
            estimated_images = max(1, content_length // 3000)
            seconds += estimated_images * random.uniform(2, 4)

        variance = seconds * 0.2
        seconds = max(0.5, seconds + random.gauss(0, variance))
        seconds *= intensity

        return min(seconds, 30)

    @classmethod
    def calculate_scroll_pause(
        cls,
        scroll_distance: int,
        intensity: float = 1.0,
    ) -> float:
        """Calculate pause time after scrolling (seconds)."""
        base_pause = (scroll_distance / 500) * 0.5
        pause = max(0.2, base_pause + random.uniform(-0.2, 0.3))
        return pause * intensity


class HumanBehavior:
    """Orchestrator for human-like browser behavior.

    Combines all behavioral simulation components for
    realistic automation that evades detection.
    """

    def __init__(self, intensity: float = 1.0):
        self.intensity = max(0.5, min(2.0, intensity))
        self.bezier = BezierCurve()
        self.typing = HumanTyping()
        self.reading = ReadingBehavior()

    async def move_to_element(
        self,
        page: Any,
        locator: Any,
        click: bool = False,
    ) -> None:
        """Move mouse to element with natural Bezier curve.

        Args:
            page: Playwright Page
            locator: Playwright Locator (from ref resolution)
            click: Whether to click after moving
        """
        try:
            current_pos = await page.evaluate("""(() => {
                const t = window.__bbu_mouse;
                return t ? {x: t.x, y: t.y} : null;
            })()""")
            if current_pos:
                start = (current_pos["x"], current_pos["y"])
            else:
                # First movement — inject tracker and use viewport center
                await page.evaluate("""(() => {
                    if (!window.__bbu_mouse) {
                        window.__bbu_mouse = {x: 0, y: 0};
                        document.addEventListener('mousemove', e => {
                            window.__bbu_mouse.x = e.clientX;
                            window.__bbu_mouse.y = e.clientY;
                        }, {passive: true});
                    }
                })()""")
                vp = page.viewport_size
                start = (vp["width"] // 2 if vp else 500, vp["height"] // 2 if vp else 300)
        except Exception:
            start = (500, 300)

        try:
            box = await locator.bounding_box()
            if not box:
                if click:
                    await locator.click()
                return

            end = (
                int(box["x"] + box["width"] / 2 + random.uniform(-5, 5)),
                int(box["y"] + box["height"] / 2 + random.uniform(-5, 5)),
            )
        except Exception:
            if click:
                await locator.click()
            return

        steps = max(20, int(math.sqrt(
            (end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2
        ) / 10))
        points = self.bezier.generate_points(start, end, steps)
        delays = self.bezier.generate_movement_delays(
            steps, base_delay_ms=5 * self.intensity
        )

        for point, delay in zip(points, delays):
            await page.mouse.move(point[0], point[1])
            await asyncio.sleep(delay / 1000)

        if click:
            await asyncio.sleep(random.uniform(0.05, 0.15) * self.intensity)
            await page.mouse.click(end[0], end[1])

    async def human_type(
        self,
        page: Any,
        locator: Any,
        text: str,
        clear_first: bool = False,
    ) -> None:
        """Type text with human-like timing into a locator.

        Args:
            page: Playwright Page
            locator: Playwright Locator
            text: Text to type
            clear_first: Whether to clear existing content first
        """
        await self.move_to_element(page, locator, click=True)
        await asyncio.sleep(random.uniform(0.1, 0.3) * self.intensity)

        if clear_first:
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.05)
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.1)

        await self.typing.type_text(page, text, self.intensity)

    async def smooth_scroll(
        self,
        page: Any,
        direction: str = "down",
        amount: int = 300,
    ) -> None:
        """Scroll with eased acceleration/deceleration.

        Args:
            page: Playwright Page
            direction: "up" or "down"
            amount: Total pixels to scroll
        """
        sign = 1 if direction == "down" else -1
        # Break into small increments with easing
        increments = max(5, amount // 60)
        per_step = amount / increments

        for i in range(increments):
            t = i / increments
            # Ease-in-out
            ease = t * t * (3 - 2 * t)
            speed = 0.3 + ease * 0.7  # Start slower, speed up
            delta = per_step * speed * sign

            await page.mouse.wheel(0, int(delta))
            await asyncio.sleep(random.uniform(0.01, 0.04) * self.intensity)

        # Reading pause after scroll
        pause = self.reading.calculate_scroll_pause(amount, self.intensity)
        await asyncio.sleep(pause)

    async def reading_pause(
        self,
        content_length: int,
        has_images: bool = False,
        max_pause: float = 10.0,
    ) -> None:
        """Pause to simulate reading content."""
        pause = self.reading.calculate_read_time(
            content_length, has_images, self.intensity
        )
        pause = min(pause, max_pause)
        await asyncio.sleep(pause)

    async def random_micro_movement(self, page: Any) -> None:
        """Perform small random mouse movement (humans rarely keep mouse still)."""
        try:
            current = await page.evaluate("""(() => {
                const t = window.__bbu_mouse;
                return t ? {x: t.x, y: t.y} : null;
            })()""")
            cx = current["x"] if current else 500
            cy = current["y"] if current else 300
            x = cx + random.randint(-30, 30)
            y = cy + random.randint(-30, 30)
            await page.mouse.move(max(0, x), max(0, y))
        except Exception:
            pass

    async def natural_wait(
        self,
        min_seconds: float = 0.5,
        max_seconds: float = 2.0,
    ) -> None:
        """Wait for a natural random duration."""
        wait = random.uniform(min_seconds, max_seconds) * self.intensity
        await asyncio.sleep(wait)
