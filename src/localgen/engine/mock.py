import asyncio
import hashlib
import random
import time
from io import BytesIO
from uuid import uuid4

from PIL import Image, ImageDraw

from localgen.engine import EngineStatus, Observation


class MockEngine:
    def __init__(self, delay=2.2):
        self.delay = delay
        self.status = EngineStatus()
        self.active = None

    async def check_previous(self):
        return True

    async def start(self):
        self.status = EngineStatus("ready", "MOCK_MODE", "演示模式")

    async def tick(self, verify=False):
        pass

    async def shutdown(self):
        self.active = None
        self.status = EngineStatus()
        return True

    async def recycle(self):
        self.active = None
        return True

    def missing(self, workflow):
        return []

    async def submit(self, graph, job_id):
        prompt_id = str(uuid4())
        self.active = (prompt_id, time.monotonic(), graph)
        return prompt_id

    async def observe(self, prompt_id, workflow, job_id):
        _, started, graph = self.active
        elapsed = time.monotonic() - started
        if elapsed < self.delay:
            return Observation(progress=(int(20 * elapsed / self.delay), 20))
        content = await asyncio.to_thread(self.draw, graph)
        self.active = None
        return Observation(image=content, progress=(20, 20))

    async def cancel(self, prompt_id):
        if self.active and self.active[0] == prompt_id:
            self.active = None
        return True

    def cleanup(self, job_id):
        pass

    @staticmethod
    def draw(graph):
        size = graph["4"]["inputs"]
        width, height = size["width"], size["height"]
        prompt = graph["2"]["inputs"]["text"]
        seed = graph["5"]["inputs"]["seed"]
        rng = random.Random(hashlib.sha256(f"{seed}:{prompt}".encode()).digest())
        image = Image.new("RGB", (width, height), (12, 15, 27))
        draw = ImageDraw.Draw(image, "RGBA")
        colors = [(124, 110, 250), (255, 125, 135), (76, 222, 198), (245, 178, 92)]
        for i in range(75):
            x, y, radius = rng.randrange(width), rng.randrange(height), rng.randrange(8, width // 3)
            color = (*colors[i % len(colors)], rng.randrange(25, 145))
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=color, width=2)
        draw.rectangle((14, height - 39, 116, height - 14), fill=(10, 13, 23, 240))
        draw.text((24, height - 32), "LOCALGEN DEMO", fill=(225, 226, 250))
        data = BytesIO()
        image.save(data, "PNG")
        return data.getvalue()
