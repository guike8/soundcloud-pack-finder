"""Offline fixtures exercise the real graph, classification, queue, and report code."""

import json
from importlib.resources import files


def fixture():
    return json.loads(files("packfinder").joinpath("fixtures/demo.json").read_text(encoding="utf-8"))


class DemoSoundCloud:
    def __init__(self):
        self.data = fixture()

    def resolve_profile(self,url):
        return self.get_profile("demo-0")

    def get_profile(self,user_id):
        return dict(self.data["profiles"][user_id])

    def iter_followers(self,user_id,limit):
        for key in self.data["followers"].get(user_id,[])[:limit]:
            yield self.get_profile(key)


class DemoTelegram:
    async def scan_channel(self,channel,limit=100):
        return fixture()["messages"].get(channel,[])[:limit]
