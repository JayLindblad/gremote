"""Find out, on any Android TV, which Settings pages it has and how to open each one.

Nothing here is specific to one TV model or vendor. For every installed Settings app the TV's
own manifest is read (only that file is fetched over ADB, not the whole APK) to list every page
another app may open, including pages that have no intent filter and can only be opened by
name (e.g. Sony's Accessibility page on Android TV 9). Each page is then checked with the TV's
package manager, so a button is only offered when the TV will really open that page.

Shortcuts (Accessibility, Bluetooth, About, ...) try the standard Android action first, but
only keep its answer when it lands on a page whose name matches the shortcut; TVs route some
actions to placeholders or unrelated pages. Otherwise the TV's own page with a matching name
is used.
"""

import re
import struct
import zlib

# -- binary XML (AndroidManifest.xml inside an APK) ---------------------------------------
_RES_STRING_POOL, _RES_XML_START, _RES_XML_END, _RES_XML_RESOURCE_MAP = 0x0001, 0x0102, 0x0103, 0x0180
_TYPE_STRING, _TYPE_BOOLEAN = 0x03, 0x12
_ATTR_IDS = {0x01010003: "name", 0x01010010: "exported", 0x0101000E: "enabled",
             0x01010006: "permission", 0x01010202: "targetActivity"}


def _string_pool(data: bytes, at: int) -> list[str]:
    _, header, _, count, _, flags, strings_start, _ = struct.unpack_from("<HHIIIIII", data, at)
    utf8 = bool(flags & 0x100)
    offsets = struct.unpack_from(f"<{count}I", data, at + header)
    base = at + strings_start
    out = []
    for off in offsets:
        p = base + off
        if utf8:
            n = data[p]
            p += 2 if n & 0x80 else 1            # length in characters (skipped)
            n = data[p]
            if n & 0x80:
                n = ((n & 0x7F) << 8) | data[p + 1]
                p += 1
            p += 1
            out.append(data[p:p + n].decode("utf-8", "replace"))
        else:
            n = struct.unpack_from("<H", data, p)[0]
            p += 2
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0]
                p += 2
            out.append(data[p:p + n * 2].decode("utf-16-le", "replace"))
    return out


def parse_manifest(data: bytes) -> tuple[str, list[dict]]:
    """(package, activities) from a binary AndroidManifest.xml.

    Each activity: {name, exported, enabled, permission, target, filters: [{actions, data}]}.
    """
    strings: list[str] = []
    res_ids: list[int] = []
    package = ""
    activities: list[dict] = []
    stack: list[str] = []
    cur: dict | None = None
    flt: dict | None = None

    def attrs(at: int) -> dict:
        start, size, count = struct.unpack_from("<HHH", data, at + 16 + 8)
        out = {}
        for i in range(count):
            a = at + 16 + start + i * size
            _, name_i, raw_i, _, _, dtype, value = struct.unpack_from("<IIIHBBI", data, a)
            key = _ATTR_IDS.get(res_ids[name_i]) if name_i < len(res_ids) else None
            if key is None and name_i < len(strings):
                key = strings[name_i]
            if dtype == _TYPE_BOOLEAN:
                val = value != 0
            elif raw_i != 0xFFFFFFFF and raw_i < len(strings):
                val = strings[raw_i]
            elif dtype == _TYPE_STRING and value < len(strings):
                val = strings[value]
            else:
                val = value
            out[key] = val
        return out

    pos = struct.unpack_from("<HHI", data, 0)[1]          # skip the XML file header
    while pos + 8 <= len(data):
        ctype, _, csize = struct.unpack_from("<HHI", data, pos)
        if csize < 8:
            break
        if ctype == _RES_STRING_POOL:
            strings = _string_pool(data, pos)
        elif ctype == _RES_XML_RESOURCE_MAP:
            res_ids = list(struct.unpack_from(f"<{(csize - 8) // 4}I", data, pos + 8))
        elif ctype == _RES_XML_START:
            tag = strings[struct.unpack_from("<I", data, pos + 20)[0]]
            a = attrs(pos)
            if tag == "manifest":
                package = str(a.get("package", ""))
            elif tag in ("activity", "activity-alias") and "application" in stack:
                cur = {"name": str(a.get("name", "")), "exported": a.get("exported"),
                       "enabled": a.get("enabled", True) is not False,
                       "permission": a.get("permission"), "target": a.get("targetActivity"),
                       "filters": []}
                activities.append(cur)
            elif tag == "intent-filter" and cur is not None:
                flt = {"actions": [], "data": False}
                cur["filters"].append(flt)
            elif tag == "action" and flt is not None:
                flt["actions"].append(str(a.get("name", "")))
            elif tag == "data" and flt is not None:
                flt["data"] = True
            stack.append(tag)
        elif ctype == _RES_XML_END:
            tag = stack.pop() if stack else ""
            if tag in ("activity", "activity-alias"):
                cur = None
            elif tag == "intent-filter":
                flt = None
        pos += csize

    for act in activities:
        for key in ("name", "target"):
            v = act[key]
            if isinstance(v, str) and v:
                act[key] = package + v if v.startswith(".") else (v if "." in v else f"{package}.{v}")
        if act["exported"] is None:                    # Android's default before API 31
            act["exported"] = bool(act["filters"])
    return package, activities


# -- reading one file out of an APK on the TV ---------------------------------------------
_SAFE_PATH = re.compile(r"^/[\w./-]+\.apk$")
_BLOCK = 4096


async def _read(session, path: str, offset: int, length: int) -> bytes:
    first, last = offset // _BLOCK, (offset + length + _BLOCK - 1) // _BLOCK
    raw = await session.exec_bytes(
        f"dd if={path} bs={_BLOCK} skip={first} count={last - first} 2>/dev/null", timeout=30.0)
    return raw[offset - first * _BLOCK:offset - first * _BLOCK + length]


async def read_apk_manifest(session, path: str) -> bytes:
    """AndroidManifest.xml from an APK on the TV, fetching only the bytes needed."""
    if not _SAFE_PATH.match(path):
        raise ValueError(f"unexpected APK path {path!r}")
    size = int((await session.shell(f"stat -c %s {path} 2>/dev/null || wc -c < {path}")).split()[0])
    tail_len = min(size, 65557)
    tail = await _read(session, path, size - tail_len, tail_len)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise ValueError("not a zip file")
    cd_size, cd_offset = struct.unpack_from("<II", tail, eocd + 12)
    cd = await _read(session, path, cd_offset, cd_size)
    p = 0
    while p + 46 <= len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        method, = struct.unpack_from("<H", cd, p + 10)
        comp_size, _, name_len, extra_len, comment_len = struct.unpack_from("<IIHHH", cd, p + 20)
        local, = struct.unpack_from("<I", cd, p + 42)
        name = cd[p + 46:p + 46 + name_len]
        if name == b"AndroidManifest.xml":
            head = await _read(session, path, local, 30)
            n, e = struct.unpack_from("<HH", head, 26)
            blob = await _read(session, path, local + 30 + n + e, comp_size)
            return zlib.decompress(blob, -15) if method == 8 else blob
        p += 46 + name_len + extra_len + comment_len
    raise ValueError("no AndroidManifest.xml in the APK")


# -- deciding what is a page --------------------------------------------------------------
# Placeholders that "open" without showing anything (Android TV sends Settings pages a model
# lacks to frameworkpackagestubs' SettingsStub), and the app chooser.
USELESS = re.compile(r"frameworkpackagestubs|Stubs?\b|Stubs\$|EmptyStub|FallbackHome|"
                     r"Trampoline|ResolverActivity|ChooserActivity", re.I)
# Activities that are steps of a flow, dialogs, or that do something on their own.
_NOT_A_PAGE_CLASS = re.compile(
    r"Dialog|Picker|Pairing|InitialSetup|SetupWizard|Wizard|Receiver|Trampoline|Stub|Fallback|"
    r"Permission|Clear|Reset|Confirm|Suggestion|Workflow|Request|Factory|^Format|Migrate|"
    r"Unmount|Eula|Terms|Connection|Accessory|Search|VoiceAction|Provider|^Main$",
    re.I)
_NOT_A_PAGE_ACTION = re.compile(
    r"BOOT_COMPLETED|PAIRING|SETUP_STEP|SETUP_WIFI|CANVAS_SETUP|REQUEST_ENABLE|"
    r"REQUEST_DISCOVERABLE|GLOBAL_BUTTON|MOUNT|SUGGESTION|_STORAGE$|MOVE_APP|devicepicker|"
    r"\.SLEEP$|GET_CONNECTED|CONNECT_INPUT|SEARCH|DEVICE_NAME_FLOW|AUTO_SCAN|AUTO_PROGRAM|LCN_CONFLICT|CAM_SETUP|EXTERNAL_|\.VIEW$|"
    r"SET_AUTOFILL|APPLICATION_DETAILS|IGNORE_BATTERY|RESET|CLEAR|FACTORY", re.I)
_ENTRY_ACTIONS = {"android.intent.action.MAIN"}


def simple_class(component: str) -> str:
    """'pkg/com.x.about.AboutActivity' -> 'About'; 'pkg/.Settings$OverlaySettingsActivity' -> 'OverlaySettings'."""
    cls = component.rsplit("/", 1)[-1].rsplit(".", 1)[-1].split("$")[-1]
    return re.sub(r"Activity$", "", cls) or cls


def full(component: str) -> str:
    """'pkg/.Cls' -> 'pkg/pkg.Cls', so the same activity always compares equal."""
    pkg, _, cls = component.partition("/")
    return f"{pkg}/{pkg}{cls}" if cls.startswith(".") else component


def pages_in(package: str, activities: list[dict]) -> list[dict]:
    """Activities of one app that another app may open and that look like pages.

    Each: {component, actions: [page actions without data], has_filters}.
    """
    out = []
    for act in activities:
        if not act["exported"] or not act["enabled"] or act["permission"]:
            continue
        name = act["name"]
        cls = name.rsplit(".", 1)[-1].split("$")[-1]
        if _NOT_A_PAGE_CLASS.search(re.sub(r"Activity$", "", cls)):
            continue
        actions, flow = [], False
        for f in act["filters"]:
            for a in f["actions"]:
                if _NOT_A_PAGE_ACTION.search(a):
                    flow = True
                elif not f["data"] and a not in _ENTRY_ACTIONS and a not in actions:
                    actions.append(a)
        if flow:
            continue                       # handles a pairing/setup/scan step: not a page
        if act["filters"] and all(f["data"] for f in act["filters"]):
            continue                       # only opens for a given app/file (needs data)
        out.append({"component": f"{package}/{name}", "actions": actions,
                    "has_filters": bool(act["filters"])})
    return out


# -- shortcuts --------------------------------------------------------------------------
# key -> (actions to try, page-name patterns in order of preference). Vendor actions are just
# more candidates; a TV that lacks them simply does not resolve them.
SHORTCUTS: dict[str, tuple[list[str], list[str]]] = {
    "settings": (["android.settings.SETTINGS"], [r"^(Main)?Settings$", r"^TvSettings"]),
    "network": (["android.settings.WIFI_SETTINGS", "android.settings.WIRELESS_SETTINGS"],
                [r"^Network$", r"^(Network|Wifi|Connectivity)"]),
    "display": (["android.settings.DISPLAY_SETTINGS"], [r"^Display"]),
    "picture": (["com.sony.dtv.base.picturesettings"], [r"^Picture(Settings?|Quality)?$"]),
    "sound": (["android.settings.SOUND_SETTINGS"], [r"^Sound$", r"^Sound"]),
    "sony_sound": (["com.sony.dtv.base.soundsettings"], []),
    "speakers": (["com.sony.dtv.settings.intent.action.SPEAKERS_SETTINGS"], [r"^Speakers?"]),
    "inputs": ([], [r"^ExternalInputs$", r"^Inputs$"]),
    "remote_start": ([], [r"^RemoteStart$"]),
    "power": ([], [r"^(Eco|Power|Energy)"]),
    "apps": (["android.settings.APPLICATION_SETTINGS",
              "android.settings.MANAGE_APPLICATIONS_SETTINGS"], [r"^Apps$", r"^Applications?$"]),
    "accessibility": (["android.settings.ACCESSIBILITY_SETTINGS"], [r"^Accessibility$"]),
    "bluetooth": (["android.settings.BLUETOOTH_SETTINGS"], [r"^Bluetooth$", r"^Bluetooth"]),
    "datetime": (["android.settings.DATE_SETTINGS"], [r"^DateTime", r"^Date"]),
    "language": (["android.settings.LOCALE_SETTINGS"], [r"^Language", r"^Locale"]),
    "keyboard": (["android.settings.INPUT_METHOD_SETTINGS"], [r"^Keyboard", r"^InputMethod$"]),
    "storage": (["android.settings.INTERNAL_STORAGE_SETTINGS"], [r"^Storage"]),
    "privacy": (["android.settings.PRIVACY_SETTINGS"], [r"^Privacy"]),
    "location": (["android.settings.LOCATION_SOURCE_SETTINGS"], [r"^Location"]),
    "about": (["android.settings.DEVICE_INFO_SETTINGS"], [r"^About$", r"^DeviceInfo"]),
    "developer": (["android.settings.APPLICATION_DEVELOPMENT_SETTINGS"],
                  [r"^Development", r"^Developer"]),
}


# -- labels -------------------------------------------------------------------------------
# Clearer names where an action's or class's own name is cryptic (cosmetic only).
LABELS = {
    "com.sony.dtv.intent.action.BROADCAST_DIAG": "Signal diagnostics",
    "com.sony.dtv.tvsettings.intent.action.FAV_SETTING": "Favourite channels",
    "com.sony.dtv.intent.action.setting.showhide": "Show/hide channels",
    "com.sony.dtv.tvsettings.intent.action.PREFERENCE": "Channel preferences",
    "com.sony.dtv.tvsettings.intent.action.PARENTAL_CONTROL_SETTINGS": "Parental controls",
    "com.sony.dtv.settings.intent.action.NETWORK_STATUS": "Network status",
    "com.sony.dtv.smarthomesettings.ACTION_LAUNCH_VOICE_PORTAL": "Voice portal",
    "com.sony.dtv.tvsettings.intent.action.CHANNEL_SETUP": "Channel setup",
    "com.sony.dtv.base.audiosystemsettings": "Audio system",
    "com.sony.dtv.base.notification": "Notifications",
    "com.sony.dtv.touchpad.settings.START": "Touchpad remote setup",
    "com.sony.dtv.panelsettings.action.REFRESH_SETTING": "Panel refresh",
    "android.settings.action.MANAGE_WRITE_SETTINGS": "Modify system settings",
    "android.settings.action.MANAGE_OVERLAY_PERMISSION": "Display over other apps",
    "android.settings.MANAGE_UNKNOWN_APP_SOURCES": "Unknown sources",
    "android.settings.INPUT_METHOD_SUBTYPE_SETTINGS": "Keyboard languages",
    "Daydream": "Screen saver",
    "TtsEngineSettings": "Text-to-speech",
    "InputsCustomLabel": "Input labels",
    "DisplaySound": "Display & sound",
    "PanelSettings": "Panel settings",
    "ClosedCaption": "Closed captions",
    "AddWifiNetwork": "Add Wi-Fi network",
}
_WORDS = {"hdmi": "HDMI", "tv": "TV", "wifi": "Wi-Fi", "cec": "CEC", "pip": "PiP", "3d": "3D"}
_GENERIC = {"start", "manual", "main", "launch", "open", "show", "refresh", "advanced",
            "settings", "home", "preference", "preferences"}


def _humanize(word: str) -> str:
    word = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", word)        # camelCase -> camel Case
    word = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", word)     # TPRCSetup -> TPRC Setup
    word = re.sub(r"(?i)\bthree ?d\b", "3D", word.replace("_", " ")).strip()
    parts = word.split()
    if not parts:
        return word
    text = " ".join(_WORDS.get(p.lower()) or (p if p.isupper() and len(p) > 1 else p.lower())
                    for p in parts)
    return text[:1].upper() + text[1:]


def _package_word(component: str) -> str:
    pkg = component.split("/", 1)[0].rsplit(".", 1)[-1]
    return _humanize(re.sub(r"(?i)settings?$", "", pkg) or pkg)


def label_for(component: str, action: str | None = None) -> str:
    if action and action in LABELS:
        return LABELS[action]
    if action:
        word = action.rsplit(".", 1)[-1]
        word = re.sub(r"(?i)_?settings?$|^action_?", "", word) or word
        label = _humanize(word.lower() if word.isupper() else word)
        if label.lower() not in _GENERIC:
            return label
    cls = simple_class(component)
    if cls in LABELS:
        return LABELS[cls]
    label = _humanize(re.sub(r"Settings?$", "", cls) or cls)
    if label.lower() in _GENERIC:                    # 'Main', 'Advanced' -> say whose
        word = _package_word(component)
        label = word if label.lower() in {"main", "settings", "home"} else f"{word} ({label.lower()})"
    return label


# -- the scan -------------------------------------------------------------------------------
def _flag(target: str) -> str:
    return "-n" if "/" in target else "-a"


async def resolve(session, target: str) -> str | None:
    """The activity the TV would open for this action/component, or None."""
    # Quoted: component names can contain '$' (inner classes), which the shell would expand.
    out = await session.shell(f"cmd package resolve-activity --brief {_flag(target)} '{target}'")
    if "no activity found" in out.lower():
        return None
    last = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return last if "/" in last and " " not in last else None


async def usable(session, target: str) -> str | None:
    resolved = await resolve(session, target)
    return None if not resolved or USELESS.search(resolved) else resolved


async def settings_apps(session) -> list[tuple[str, str]]:
    """(package, apk path) of every installed app with 'settings' in its name."""
    out = await session.shell("pm list packages -f")
    apps = []
    for line in out.splitlines():
        m = re.match(r"package:(/\S+\.apk)=(\S+)$", line.strip())
        if m and re.search(r"settings", m.group(2), re.I):
            apps.append((m.group(2), m.group(1)))
    return sorted(apps)


async def scan(session, log=None) -> dict:
    """{"pages": [keys], "targets": {key: target}, "extra": [{label, target}], "problems": [...]}."""
    problems: list[str] = []
    pool: list[dict] = []
    for package, path in await settings_apps(session):
        try:
            _, activities = parse_manifest(await read_apk_manifest(session, path))
        except Exception as exc:          # one unreadable app must not stop the scan
            problems.append(f"{package}: {type(exc).__name__}: {exc}")
            continue
        pool.extend(pages_in(package, activities))

    # Which pool pages the TV really has (installed, enabled, resolvable).
    live = []
    for page in pool:
        if await usable(session, page["component"]):
            live.append(page)

    targets: dict[str, str] = {}
    covered: set[str] = set()
    for key, (actions, patterns) in SHORTCUTS.items():
        weak = None
        for action in actions:
            resolved = await usable(session, action)
            if not resolved:
                continue
            if not patterns or any(re.search(p, simple_class(resolved)) for p in patterns):
                targets[key] = action
                covered.add(full(resolved))
                break
            weak = weak or (action, resolved)
        if key in targets:
            continue
        for pattern in patterns:                     # the TV's own page with a matching name
            hits = [p for p in live if re.search(pattern, simple_class(p["component"]))
                    and full(p["component"]) not in covered]
            if hits:
                best = min(hits, key=lambda p: len(simple_class(p["component"])))
                targets[key] = best["component"]
                covered.add(full(best["component"]))
                break
        if key not in targets and weak:              # only an unrelated-looking match exists
            targets[key] = weak[0]
            covered.add(full(weak[1]))

    extra: list[dict] = []
    seen_labels: set[str] = set()
    for page in live:
        comp = page["component"]
        if full(comp) in covered:
            continue
        # One entry per page: open it by an action that reaches it (named in LABELS if possible,
        # else the shortest name), or by its component.
        entries = []
        for action in page["actions"]:
            resolved = await usable(session, action)
            if resolved and full(resolved) == full(comp):
                entries.append((action not in LABELS, len(label_for(comp, action)), action))
        target = min(entries)[2] if entries else comp
        label = label_for(comp, target if entries else None)
        if label.lower() in seen_labels:
            label = f"{label} ({_package_word(comp).lower()})"
        if label.lower() not in seen_labels:
            seen_labels.add(label.lower())
            extra.append({"label": label, "target": target})
    extra.sort(key=lambda e: e["label"].lower())
    return {"pages": [k for k in SHORTCUTS if k in targets], "targets": targets,
            "extra": extra, "problems": problems}
