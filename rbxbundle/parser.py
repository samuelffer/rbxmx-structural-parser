from __future__ import annotations

import base64
import logging
import struct
from typing import List, Optional, Tuple
import xml.etree.ElementTree as ET

from .utils import local_tag

LOG = logging.getLogger("rbxbundle")

def get_properties_node(item: ET.Element) -> Optional[ET.Element]:
    for child in item:
        if local_tag(child.tag) == "Properties":
            return child
    return None

def get_name(props: Optional[ET.Element]) -> Optional[str]:
    if props is None:
        return None
    for p in props:
        if local_tag(p.tag) == "string" and p.attrib.get("name") == "Name":
            return p.text or ""
    return None

def get_source(props: Optional[ET.Element]) -> Optional[str]:
    if props is None:
        return None
    for p in props:
        if p.attrib.get("name") == "Source" and local_tag(p.tag) in {"ProtectedString", "string", "SharedString"}:
            return p.text or ""
    return None

def get_value(props: Optional[ET.Element]) -> Optional[str]:
    if props is None:
        return None
    for p in props:
        if p.attrib.get("name") == "Value":
            return (p.text or "").strip()
    return None

def iter_top_level_items(roblox_root: ET.Element) -> List[ET.Element]:
    direct_items = [c for c in list(roblox_root) if local_tag(c.tag) == "Item"]
    for it in direct_items:
        if it.attrib.get("class") == "DataModel":
            return [c for c in list(it) if local_tag(c.tag) == "Item"]
    return direct_items

class BinReader:
    def __init__(self, data: bytes):
        self.data = data
        self.i = 0

    def remaining(self) -> int:
        return len(self.data) - self.i

    def read_u8(self) -> int:
        if self.remaining() < 1:
            raise ValueError("Unexpected EOF (u8)")
        v = self.data[self.i]
        self.i += 1
        return v

    def read_u32(self) -> int:
        if self.remaining() < 4:
            raise ValueError("Unexpected EOF (u32)")
        v = struct.unpack_from("<I", self.data, self.i)[0]
        self.i += 4
        return v

    def read_i32(self) -> int:
        if self.remaining() < 4:
            raise ValueError("Unexpected EOF (i32)")
        v = struct.unpack_from("<i", self.data, self.i)[0]
        self.i += 4
        return v

    def read_f32(self) -> float:
        if self.remaining() < 4:
            raise ValueError("Unexpected EOF (f32)")
        v = struct.unpack_from("<f", self.data, self.i)[0]
        self.i += 4
        return v

    def read_f64(self) -> float:
        if self.remaining() < 8:
            raise ValueError("Unexpected EOF (f64)")
        v = struct.unpack_from("<d", self.data, self.i)[0]
        self.i += 8
        return v

    def read_bytes(self, n: int) -> bytes:
        if self.remaining() < n:
            raise ValueError("Unexpected EOF (bytes)")
        v = self.data[self.i : self.i + n]
        self.i += n
        return v

    def read_string(self) -> str:
        n = self.read_u32()
        raw = self.read_bytes(n)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")

def decode_attributes_serialize(blob: bytes) -> List[Tuple[str, str, str]]:
    r = BinReader(blob)
    out: List[Tuple[str, str, str]] = []
    size = r.read_u32()
    for _ in range(size):
        key = r.read_string()
        vtype = r.read_u8()
        if vtype == 0x02:
            out.append((key, "string", r.read_string()))
        elif vtype == 0x03:
            out.append((key, "boolean", "true" if r.read_u8() != 0 else "false"))
        elif vtype == 0x05:
            out.append((key, "number", repr(r.read_f32())))
        elif vtype == 0x06:
            out.append((key, "number", repr(r.read_f64())))
        elif vtype == 0x09:
            scale = r.read_f32(); offset = r.read_i32()
            out.append((key, "UDim", f"{{Scale={scale}, Offset={offset}}}"))
        elif vtype == 0x0A:
            xs = r.read_f32(); xo = r.read_i32()
            ys = r.read_f32(); yo = r.read_i32()
            out.append((key, "UDim2", f"{{X={{Scale={xs}, Offset={xo}}}, Y={{Scale={ys}, Offset={yo}}}}}"))
        elif vtype == 0x0E:
            out.append((key, "BrickColor", str(r.read_u32())))
        elif vtype == 0x0F:
            rr = r.read_f32(); gg = r.read_f32(); bb = r.read_f32()
            out.append((key, "Color3", f"{{R={rr}, G={gg}, B={bb}}}"))
        elif vtype == 0x10:
            xx = r.read_f32(); yy = r.read_f32()
            out.append((key, "Vector2", f"{{X={xx}, Y={yy}}}"))
        elif vtype == 0x11:
            xx = r.read_f32(); yy = r.read_f32(); zz = r.read_f32()
            out.append((key, "Vector3", f"{{X={xx}, Y={yy}, Z={zz}}}"))
        else:
            out.append((key, f"unknown(0x{vtype:02X})", "Unsupported attribute type; decoding stopped"))
            break
    return out

def parse_attributes(
    props: Optional[ET.Element],
    *,
    source_file: str = "<unknown>",
    section: str = "Properties",
    owner_path: str = "<unknown>",
) -> List[Tuple[str, str, str]]:
    if props is None:
        return []
    for p in props:
        if p.attrib.get("name") == "AttributesSerialize" and local_tag(p.tag) == "BinaryString":
            b64 = (p.text or "").strip()
            if not b64:
                return []
            try:
                blob = base64.b64decode(b64, validate=False)
            except (ValueError, TypeError) as exc:
                LOG.warning(
                    "attributes_serialize_decode_failed file=%s section=%s data_type=AttributesSerialize owner_path=%s error=%s",
                    source_file,
                    section,
                    owner_path,
                    exc,
                )
                blob = b""
            if blob:
                try:
                    return decode_attributes_serialize(blob)
                except ValueError as exc:
                    LOG.warning(
                        "attributes_serialize_parse_failed file=%s section=%s data_type=AttributesSerialize owner_path=%s error=%s",
                        source_file,
                        section,
                        owner_path,
                        exc,
                    )
    out: List[Tuple[str, str, str]] = []
    for node in props:
        if local_tag(node.tag) != "Attributes":
            continue
        for attr in list(node):
            if local_tag(attr.tag) != "Attribute":
                continue
            aname = (attr.attrib.get("name") or "").strip()
            atype = (attr.attrib.get("type") or "").strip() or "unknown"
            aval = attr.attrib.get("value")
            if aval is None:
                aval = (attr.text or "").strip()
            else:
                aval = str(aval).strip()
            if aname:
                out.append((aname, atype, aval))
    return out
