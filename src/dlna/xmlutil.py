"""Small ElementTree helpers.

UPnP documents mix several namespaces and servers are not always consistent
about them, so everything is parsed with namespaces stripped and looked up by
local name.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET


def parse(data: bytes | str) -> ET.Element:
    root = ET.fromstring(data)
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        for key in [k for k in el.attrib if "}" in k]:
            el.attrib[key.split("}", 1)[1]] = el.attrib.pop(key)
    return root


def text(el: ET.Element, path: str, default: str = "") -> str:
    value = el.findtext(path)
    return value.strip() if value else default
