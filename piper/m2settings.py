"""The sandbox's Maven settings, generated from the host's ~/.m2/settings.xml (policy maven_settings).

A sandbox builds with apt's Maven and the JDKs of the agent image, but private artifacts live in
registries that need credentials: a Nexus (username/password servers) or a GitLab package
registry (a `Deploy-Token` http header, 401 without it). Those sit in the
developer's host ~/.m2/settings.xml, next to things a sandbox must not get and paths that do not
exist there. piperd therefore writes, for every start, <box>/piper/maven-settings.xml and
maven-toolchains.xml and includes them onto ~/.m2/settings.xml / ~/.m2/toolchains.xml:

  - kept: the profiles that can be active in the sandbox - activeProfiles, activeByDefault, an
    activation by jdk/os/property/file - and every profile that defines repositories or plugin
    repositories (a `-P` one still needs its servers); their repositories, plugin repositories
    and properties; the servers those repositories (and the mirrors) reference; mirrors;
    pluginGroups
  - dropped: a profile with properties only that nothing activates (release/gpg passphrases
    usually live there), unreferenced servers (ossrh, a website deploy ...), proxies (hole's
    network is the sandbox's proxy)
  - rewritten: localRepository -> the mounted ~/.m2/repository; JAVA_<v>_HOME properties -> the
    agent image's JDKs (from `dependencies`: openjdk-<v>-jdk*, temurin-<v>-jdk*; the nearest
    version >= the requested one, else the highest; none listed: /usr/lib/jvm/default-java)
  - toolchains.xml: one jdk toolchain per JDK of the image (the host's describes host JDKs)

A personal include onto ~/.m2/settings.xml (or toolchains.xml) in ~/.hole/settings.json is the
developer's explicit override and wins: nothing is generated for that target. The per-session
file is rw in the sandbox (hole includes always are) - a session can only spoil its own copy.
The credentials reach the sandbox as with a plain hole's include of such a file.
"""
from __future__ import annotations

import os
import platform
import re
import xml.etree.ElementTree as ET

SETTINGS_TARGET = "~/.m2/settings.xml"
TOOLCHAINS_TARGET = "~/.m2/toolchains.xml"
SECURITY_TARGET = "~/.m2/settings-security.xml"
REPOSITORY_TARGET = "~/.m2/repository"
LOCAL_REPO = "${env.HOME}/.m2/repository"
DEFAULT_JDK = "/usr/lib/jvm/default-java"

_JDK_DEP_RX = re.compile(r"^(openjdk|temurin)-(\d+)-jdk")
_JAVA_HOME_RX = re.compile(r"^JAVA_((?:\d+_)*\d+)_HOME$")
_HOME_RX = re.compile(r"^(~|\$HOME|\$\{HOME\})(?=/|$)")


def host_arch() -> str:
    """Debian's architecture name of this machine (the agent image runs natively)."""
    m = platform.machine().lower()
    return {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(m, m)


def sandbox_jdks(dependencies, arch: str | None = None) -> dict[int, str]:
    """{major: jdk home} of the JDK packages in the personal settings' `dependencies`."""
    arch = arch or host_arch()
    out: dict[int, str] = {}
    for dep in dependencies or []:
        m = _JDK_DEP_RX.match(str(dep))
        if not m:
            continue
        v = int(m.group(2))
        out[v] = ("/usr/lib/jvm/java-%d-openjdk-%s" if m.group(1) == "openjdk" else "/usr/lib/jvm/temurin-%d-jdk-%s") % (v, arch)
    return dict(sorted(out.items()))


def pick_jdk(version: int, jdks: dict[int, str]) -> str:
    if not jdks:
        return DEFAULT_JDK
    higher = [v for v in jdks if v >= version]
    return jdks[min(higher)] if higher else jdks[max(jdks)]


def includes_target(personal: dict, target: str, extra_docs=()) -> bool:
    """Does the personal global (base or any profile; `extra_docs`: the project's settings too)
    already include something onto `target` - or onto a directory above it (~/.m2 -> ~/.m2)?"""
    want = _HOME_RX.sub("~", target.rstrip("/"))
    docs = [personal] + list((personal.get("profiles") or {}).values()) + list(extra_docs)
    for doc in docs:
        inc = ((doc or {}).get("files") or {}).get("include") or {}
        if not isinstance(inc, dict):
            continue
        for spec in inc.values():
            t = spec if isinstance(spec, str) else (spec or {}).get("path", "")
            t = _HOME_RX.sub("~", str(t).rstrip("/"))
            if t == want or (t and want.startswith(t + "/")):
                return True
    return False


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]


def _text(el: ET.Element | None, path: str) -> str:
    return ((el.findtext(path) if el is not None else None) or "").strip()


def _java_version(name: str) -> int | None:
    m = _JAVA_HOME_RX.match(name)
    if not m:
        return None
    parts = m.group(1).split("_")       # JAVA_1_8_HOME -> 8, JAVA_17_HOME -> 17
    return int(parts[1]) if parts[0] == "1" and len(parts) > 1 else int(parts[0])


def generate_settings(text: str, jdks: dict[int, str], changes: list[str] | None = None) -> str:
    """The sandbox settings.xml from the host's (text). Raises ET.ParseError on invalid XML."""
    changes = changes if changes is not None else []
    root = ET.fromstring(text)
    _strip_ns(root)
    active = {(e.text or "").strip() for e in root.findall("./activeProfiles/activeProfile")}
    kept: set[str] = set()
    profiles = root.find("profiles")
    for p in list(profiles) if profiles is not None else []:
        pid = _text(p, "id")
        act = p.find("activation")
        triggers = [c for c in (list(act) if act is not None else []) if c.tag != "activeByDefault"]
        if (pid in active or _text(p, "activation/activeByDefault").lower() == "true" or triggers
                or p.find("repositories") is not None or p.find("pluginRepositories") is not None):
            kept.add(pid)
            props = p.find("properties")
            for prop in list(props) if props is not None else []:
                v = _java_version(prop.tag)
                if v is not None:
                    prop.text = pick_jdk(v, jdks)
        else:
            profiles.remove(p)
            changes.append("maven: profile %s dropped (nothing activates it, no repositories)" % (pid or "?"))
    ap = root.find("activeProfiles")
    for e in list(ap) if ap is not None else []:
        if (e.text or "").strip() not in kept:
            ap.remove(e)
    refs = {_text(r, "id") for r in root.iter("repository")} | {_text(r, "id") for r in root.iter("pluginRepository")} \
        | {_text(m, "id") for m in root.findall("./mirrors/mirror")}
    servers = root.find("servers")
    for srv in list(servers) if servers is not None else []:
        sid = _text(srv, "id")
        if sid not in refs:
            servers.remove(srv)
            changes.append("maven: server %s dropped (no kept repository uses it)" % (sid or "?"))
    for px in root.findall("proxies"):
        root.remove(px)
        changes.append("maven: proxies dropped")
    lr = root.find("localRepository")
    if lr is None:
        lr = ET.Element("localRepository")
        root.insert(0, lr)
    lr.text = LOCAL_REPO
    if hasattr(ET, "indent"):
        ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def generate_toolchains(jdks: dict[int, str]) -> str:
    root = ET.Element("toolchains")
    for v, home in jdks.items():
        tc = ET.SubElement(root, "toolchain")
        ET.SubElement(tc, "type").text = "jdk"
        prov = ET.SubElement(tc, "provides")
        ET.SubElement(prov, "version").text = str(v)
        ET.SubElement(prov, "vendor").text = "openjdk"
        ET.SubElement(ET.SubElement(tc, "configuration"), "jdkHome").text = home
    if hasattr(ET, "indent"):
        ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def plan(personal: dict, real_home: str, box: str, enabled: bool = True, extra_docs=()) -> list[tuple[str, str, str]]:
    """What a session gets: [(host path, container target, kind)] with kind settings | toolchains |
    security (files under <box>/piper) | repository (the host's ~/.m2/repository, when no include
    brings a maven cache: Docker would otherwise create ~/.m2 root-owned around the two file mounts
    and Maven could not create its local repository). Empty when the policy is off or the host has
    no ~/.m2/settings.xml; a target the personal global (or `extra_docs`, the project's settings)
    already includes is left alone."""
    if not enabled:
        return []
    m2 = os.path.join(real_home, ".m2")
    if not os.path.isfile(os.path.join(m2, "settings.xml")):
        return []
    out = []
    if not includes_target(personal, SETTINGS_TARGET, extra_docs):
        out.append((os.path.join(box, "piper", "maven-settings.xml"), SETTINGS_TARGET, "settings"))
        if os.path.isfile(os.path.join(m2, "settings-security.xml")) and not includes_target(personal, SECURITY_TARGET, extra_docs):
            out.append((os.path.join(box, "piper", "maven-settings-security.xml"), SECURITY_TARGET, "security"))
    if not includes_target(personal, TOOLCHAINS_TARGET, extra_docs):
        out.append((os.path.join(box, "piper", "maven-toolchains.xml"), TOOLCHAINS_TARGET, "toolchains"))
    if out and not includes_target(personal, REPOSITORY_TARGET, extra_docs):
        out.append((os.path.join(m2, "repository"), REPOSITORY_TARGET, "repository"))
    return out
