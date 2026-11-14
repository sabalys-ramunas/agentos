"""The dev node's fake tabs: outlines with refs, actions that change state, the captcha signal, pictures."""

from __future__ import annotations

import struct
import zlib

import pytest

from agentos.core.errors import RefNotFound
from agentos.core.models import ActionKind, Signal
from agentos.dev.image import Raster, encode_png
from agentos.dev.web import CHALLENGE_URL, HEIGHT, WIDTH, DevInteraction, frame_of_page, new_tab
from agentos.runner.fakes import BASE


def ref_for(text: str, name: str) -> str:
    """The ref on the outline line that names `name`."""
    line = next(line for line in text.splitlines() if f'"{name}"' in line and "[ref=" in line)
    return line.split("[ref=")[1].split("]")[0]


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


async def open_tab(url: str):
    interaction = DevInteraction()
    page = new_tab("biz-a/social")
    await interaction.navigate(page, url)
    return interaction, page


async def test_outline_lists_actionable_elements_with_refs():
    interaction, page = await open_tab(f"{BASE}/")
    snap = await interaction.snapshot(page)
    assert snap.url == f"{BASE}/" and snap.title == "Home"
    assert 'heading "Feed"' in snap.text
    assert ref_for(snap.text, "New post") == "e1"
    assert snap.ref_count == 1
    assert snap.signal is Signal.OK


async def test_act_changes_state_and_old_refs_go_stale():
    interaction, page = await open_tab(f"{BASE}/p/1")
    first = await interaction.snapshot(page)
    like = ref_for(first.text, "Like")
    assert "pressed=false" in first.text

    result = await interaction.act(page, like, ActionKind.CLICK)
    assert result.ok and result.changed is True and result.signal is Signal.OK

    # a click re-renders the tab: the refs of the snapshot before it no longer resolve
    with pytest.raises(RefNotFound):
        await interaction.act(page, like, ActionKind.CLICK)

    again = await interaction.snapshot(page)
    assert "pressed=true" in next(line for line in again.text.splitlines() if "[ref=" in line)


async def test_acting_without_a_snapshot_is_refused():
    interaction, page = await open_tab(f"{BASE}/")
    with pytest.raises(RefNotFound):
        await interaction.act(page, "e1", ActionKind.CLICK)


async def test_composer_dialog_takes_typing_and_shows_the_value():
    interaction, page = await open_tab(f"{BASE}/compose")
    snap = await interaction.snapshot(page)
    assert 'dialog "Create new post"' in snap.text
    caption = ref_for(snap.text, "Write a caption...")
    result = await interaction.act(page, caption, ActionKind.TYPE, "Fresh rye")
    assert result.changed is True
    again = await interaction.snapshot(page)
    assert 'value="Fresh rye"' in again.text


async def test_captcha_page_reports_the_captcha_signal_until_a_human_solves_it():
    interaction, page = await open_tab(CHALLENGE_URL)
    navigated = await interaction.navigate(page, CHALLENGE_URL)
    assert navigated.signal is Signal.CAPTCHA and navigated.status == 200
    snap = await interaction.snapshot(page)
    assert snap.signal is Signal.CAPTCHA and snap.signal_detail == "challenge page"
    assert "Verify you are human" in snap.text
    assert await interaction.signal(page) is Signal.CAPTCHA

    solved = await interaction.act(page, ref_for(snap.text, "I am not a robot"), ActionKind.CLICK)
    assert solved.url == f"{BASE}/" and solved.signal is Signal.OK
    assert await interaction.signal(page) is Signal.OK


async def test_every_lane_has_its_own_profile():
    interaction = DevInteraction()
    first, second = new_tab("biz-a/social"), new_tab("biz-b/social")
    for page in (first, second):
        await interaction.navigate(page, f"{BASE}/p/1")
    snap = await interaction.snapshot(first)
    await interaction.act(first, ref_for(snap.text, "Like"), ActionKind.CLICK)
    other = await interaction.snapshot(second)
    assert "pressed=false" in other.text and "pressed=true" not in other.text


async def test_screenshot_is_a_png_of_the_tab_and_the_frame_is_reused_until_it_changes():
    interaction, page = await open_tab(f"{BASE}/")
    assert png_size(await interaction.screenshot(page)) == (WIDTH, HEIGHT)
    version, frame = frame_of_page(page)
    assert frame_of_page(page)[1] is frame

    snap = await interaction.snapshot(page)
    await interaction.act(page, ref_for(snap.text, "New post"), ActionKind.CLICK)
    assert frame_of_page(page)[1] is not frame and frame_of_page(page)[0] != version


def test_png_encodes_every_pixel_row():
    raster = Raster(3, 2, (0, 0, 0))
    raster.rect(0, 0, 1, 1, (255, 0, 0))
    data = encode_png(raster)
    assert png_size(data) == (3, 2)
    # the IDAT payload is the zlib stream of: a filter byte, then 3 pixels, per row
    idat_len = struct.unpack(">I", data[33:37])[0]
    raw = zlib.decompress(data[41:41 + idat_len])
    assert raw == b"\x00" + bytes([255, 0, 0, 0, 0, 0, 0, 0, 0]) + b"\x00" + bytes(9)
