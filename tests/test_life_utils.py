"""Prompt 38 — life-utility tools (one unit per family)."""

from __future__ import annotations

import random
from unittest.mock import AsyncMock

import pytest

import tools.convert_tool as cv
import tools.datetime_tool as dtt
import tools.random_tool as rnd
import tools.rss_tool as rss
import tools.timer_tool as tmr
import tools.url_summary_tool as ut

# ---- A: datetime ------------------------------------------------------------


@pytest.mark.asyncio
async def test_datetime_speaks_spanish() -> None:
    res = await dtt.current_datetime_speak()
    assert res.success
    assert any(d in res.user_message for d in dtt._DAYS)
    assert "son las" in res.user_message or "es la" in res.user_message


# ---- C: random --------------------------------------------------------------


@pytest.mark.asyncio
async def test_coin_and_dice_are_seedable() -> None:
    random.seed(1)
    c = await rnd.coin_flip()
    assert c.data["result"] in ("cara", "cruz")
    d = await rnd.roll_dice(3, 6)
    assert len(d.data["rolls"]) == 3 and all(1 <= r <= 6 for r in d.data["rolls"])
    assert d.data["total"] == sum(d.data["rolls"])


@pytest.mark.asyncio
async def test_pick_uuid_password(monkeypatch) -> None:
    assert (await rnd.pick_random(["a", "b"])).data["pick"] in ("a", "b")
    assert not (await rnd.pick_random([])).success
    assert len((await rnd.generate_uuid()).data["uuid"]) == 36

    async def _ok_pbcopy(*a, **k):
        class P:
            returncode = 0
            async def communicate(self, data=None):
                return (b"", b"")
        return P()

    monkeypatch.setattr(rnd.asyncio, "create_subprocess_exec", _ok_pbcopy)
    res = await rnd.generate_password(16, "strong")
    assert res.data["length"] == 16 and res.data["copied"] is True
    assert "16 caracteres" in res.user_message  # the password itself is NOT spoken


# ---- G: convert -------------------------------------------------------------


@pytest.mark.asyncio
async def test_convert_units_and_currency(monkeypatch) -> None:
    km = await cv.convert(5, "km", "mi")
    assert abs(km.data["value"] - 3.11) < 0.05
    temp = await cv.convert(20, "C", "F")
    assert abs(temp.data["value"] - 68.0) < 0.01
    mass = await cv.convert(1000, "g", "kg")
    assert mass.data["value"] == 1.0
    monkeypatch.setattr(cv, "_fiat_rates", AsyncMock(return_value={"usd": 1.0, "mxn": 17.0}))
    cur = await cv.convert(100, "USD", "MXN")
    assert cur.data["value"] == 1700.0


# ---- B: timers --------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_list_timers() -> None:
    tmr._timers.clear()
    res = await tmr.start_timer(25, "pasta")
    assert res.success and "25 minutos" in res.user_message
    listed = await tmr.list_timers()
    assert listed.data["timers"] and listed.data["timers"][0]["label"] == "pasta"
    assert not (await tmr.start_timer(0)).success
    tmr._timers[res.data["id"]]["task"].cancel()  # don't leave a 25-min task running


# ---- E: rss -----------------------------------------------------------------


def test_rss_parses_rss_and_atom() -> None:
    rss_xml = '<rss><channel><item><title>Uno</title><link>http://a</link></item>' \
              '<item><title>Dos</title><link>http://b</link></item></channel></rss>'
    items = rss._parse(rss_xml)
    assert [i["title"] for i in items] == ["Uno", "Dos"] and items[0]["link"] == "http://a"
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Tres</title>' \
           '<link href="http://c"/></entry></feed>'
    a = rss._parse(atom)
    assert a[0]["title"] == "Tres" and a[0]["link"] == "http://c"


# ---- F: url summary ---------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_url(monkeypatch) -> None:
    import trafilatura

    class _Resp:
        text = "<html><body>contenido</body></html>"

    class _Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(ut.httpx, "AsyncClient", lambda *a, **k: _Client())
    monkeypatch.setattr(trafilatura, "extract", lambda html: "Un artículo largo sobre macOS y voz. " * 5)
    monkeypatch.setattr(ut, "_llm_summary", AsyncMock(return_value="Trata sobre asistentes de voz en Mac."))
    res = await ut.summarize_url("example.com")
    assert res.success and "voz" in res.user_message


# ---- D: birthdays -----------------------------------------------------------


@pytest.mark.asyncio
async def test_birthday_remember_and_queries(tmp_path, monkeypatch) -> None:
    import datetime as _dt

    import memory.birthdays as bd
    import tools.birthday_tool as bt

    monkeypatch.setattr(bd, "settings", type("S", (), {"EMMA_HOME": tmp_path}))
    res = await bt.birthday_remember("Ana", "8 de junio")
    assert res.success and res.data["month"] == 6 and res.data["day"] == 8
    assert not (await bt.birthday_remember("Bob", "no es fecha")).success

    t = _dt.date.today()
    bd.remember("HoyCumple", t.month, t.day)
    assert "HoyCumple" in (await bt.birthdays_today()).user_message
    assert "HoyCumple" in (await bt.birthdays_this_week()).user_message
