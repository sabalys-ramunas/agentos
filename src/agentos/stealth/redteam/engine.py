"""The red team's engine probe (site/deep.html): what the machine and the browser's settings show
a vendor, judged one check at a time, and BotD's own verdicts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agentos.stealth.redteam.detectors import CAUGHT, CLEAN, SKIPPED, SOFTWARE_GL, SUSPICIOUS, Verdict


def judge_engine(deep: Mapping[str, Any]) -> list[Verdict]:
    """Verdicts on one lane's engine probe. Each check found by running the probe in stock Firefox,
    bare Camoufox and a production lane on the node and comparing (docs/design/redteam.md)."""
    out: list[Verdict] = []
    perms = deep.get("permissions") or {}
    v = Verdict("engine.geo_permission", "engine", SKIPPED, "no permission states",
                fix="permissions.default.geo = 0 and no pinned position: the site sees `prompt` and gets no position "
                    "until someone answers, as for a real user")
    if perms.get("geolocation"):
        v.evidence["geolocation"] = perms["geolocation"]
        if perms["geolocation"] == "granted":
            v.level, v.detail = SUSPICIOUS, ("geolocation is already granted on a site never visited: the browser allows "
                                             "location for every site (Camoufox sets permissions.default.geo = 1)")
        else:
            v.level, v.detail = CLEAN, f"geolocation permission is {perms['geolocation']}"
    out.append(v)

    gl = deep.get("webglTruth") or {}
    v = Verdict("engine.webgl_limits", "engine", SKIPPED, "no WebGL",
                fix="pin WebGL limits the renderer really has, or make Camoufox enforce the claimed ones (patch)")
    if isinstance(gl, dict) and gl.get("claimedMaxTexture"):
        v.evidence.update({k: gl.get(k) for k in ("renderer", "claimedMaxTexture", "allocatesClaimed", "allocatesDouble",
                                                  "listedButUnavailable")})
        problems = []
        if gl.get("allocatesDouble"):
            problems.append(f"a texture twice the claimed MAX_TEXTURE_SIZE ({gl['claimedMaxTexture']}) allocates")
        if gl.get("allocatesClaimed") is False:
            problems.append(f"a texture of the claimed MAX_TEXTURE_SIZE ({gl['claimedMaxTexture']}) does not allocate")
        if gl.get("listedButUnavailable"):
            problems.append(f"{len(gl['listedButUnavailable'])} listed extensions cannot be enabled")
        if problems:
            v.level, v.detail = CAUGHT, "the claimed GPU limits are false: " + "; ".join(problems)
        else:
            v.level, v.detail = CLEAN, "the claimed WebGL limits hold"
    out.append(v)

    audio = deep.get("audio") or {}
    v = Verdict("engine.audio", "engine", SKIPPED, "no audio context",
                fix="give the node a null audio sink (PipeWire or PulseAudio module-null-sink) so the browser has a "
                    "stereo output device")
    if isinstance(audio, dict) and "channels" in audio:
        v.evidence.update(audio)
        if not audio.get("channels"):
            v.level, v.detail = SUSPICIOUS, "the audio output has 0 channels: no sound device, a server"
        else:
            v.level, v.detail = CLEAN, f"{audio['channels']} audio channels"
    out.append(v)

    storage = deep.get("storage") or {}
    v = Verdict("engine.quota", "engine", SKIPPED, "no storage estimate",
                fix="pin dom.quotaManager.temporaryStorage.fixedLimit to 50 GiB or more so pages read 10 GiB, as about "
                    "92% of Firefox users do (Mozilla bug 2037260)")
    if isinstance(storage, dict) and storage.get("quotaGB") is not None:
        q = float(storage["quotaGB"])
        v.evidence["quota_gb"] = q
        if q < 10.5:
            v.level, v.detail = SUSPICIOUS, (f"the storage quota is {q:g} GB, a disk under 107 GB; most Firefox users "
                                             "read 10 GiB, and every lane on this node reads the same value")
        else:
            v.level, v.detail = CLEAN, f"storage quota {q:g} GB"
    out.append(v)

    media = deep.get("mediaCapabilities") or {}
    renderer = str(gl.get("renderer") or "") if isinstance(gl, dict) else ""
    v = Verdict("engine.hw_decode", "engine", SKIPPED, "no media capabilities",
                fix="a GPU node, or Camoufox's media:spoof_codecs and a patched decodingInfo")
    if isinstance(media, dict) and media.get("h264_1080"):
        v.evidence.update(media=media, renderer=renderer)
        hardware = renderer and not any(w in renderer.lower() for w in SOFTWARE_GL)
        if hardware and media["h264_1080"][2:] == "0":
            v.level, v.detail = SUSPICIOUS, (f"claims {renderer!r} yet cannot decode 1080p H.264 power-efficiently: "
                                             "no hardware decoder behind the GPU name")
        else:
            v.level, v.detail = CLEAN, "decoding capabilities fit the GPU"
    out.append(v)
    return out


def judge_botd(judges: Mapping[str, Any]) -> list[Verdict]:
    """Fingerprint.com's open-source BotD, run on our site: an independent judge we did not write."""
    result = judges.get("botd") or {}
    v = Verdict("judge.botd", "engine", SKIPPED, "BotD did not run", {"botd": result})
    if isinstance(result, dict) and "bot" in result:
        if result["bot"]:
            v.level, v.detail = CAUGHT, f"BotD says bot ({result.get('botKind', '?')})"
        else:
            v.level, v.detail = CLEAN, "BotD says not a bot"
    return [v]
