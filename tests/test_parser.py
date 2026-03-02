"""Tests for rbxbundle.parser — uses stdlib unittest only."""

from __future__ import annotations

import struct
import textwrap
import unittest
import xml.etree.ElementTree as ET

from rbxbundle.parser import (
    BinReader,
    decode_attributes_serialize,
    get_name,
    get_properties_node,
    get_source,
    get_value,
    iter_top_level_items,
    parse_attributes,
)
from rbxbundle.utils import local_tag


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_item(class_name: str, name: str, source: str = "", value: str | None = None) -> ET.Element:
    item = ET.Element("Item", attrib={"class": class_name})
    props = ET.SubElement(item, "Properties")
    n = ET.SubElement(props, "string", attrib={"name": "Name"})
    n.text = name
    if source:
        src = ET.SubElement(props, "ProtectedString", attrib={"name": "Source"})
        src.text = source
    if value is not None:
        v = ET.SubElement(props, "string", attrib={"name": "Value"})
        v.text = value
    return item


MINIMAL_RBXMX = textwrap.dedent("""\
    <roblox>
        <Item class="DataModel">
            <Item class="Workspace">
                <Properties>
                    <string name="Name">Workspace</string>
                </Properties>
            </Item>
            <Item class="ReplicatedStorage">
                <Properties>
                    <string name="Name">ReplicatedStorage</string>
                </Properties>
                <Item class="ModuleScript">
                    <Properties>
                        <string name="Name">MyModule</string>
                        <ProtectedString name="Source">return {}</ProtectedString>
                    </Properties>
                </Item>
            </Item>
        </Item>
    </roblox>
""")


# ---------------------------------------------------------------------------
# get_properties_node
# ---------------------------------------------------------------------------

class TestGetPropertiesNode(unittest.TestCase):
    def test_returns_properties_element(self):
        item = make_item("Script", "Foo")
        props = get_properties_node(item)
        self.assertIsNotNone(props)
        self.assertEqual(local_tag(props.tag), "Properties")

    def test_returns_none_when_missing(self):
        item = ET.Element("Item", attrib={"class": "Script"})
        self.assertIsNone(get_properties_node(item))


# ---------------------------------------------------------------------------
# get_name
# ---------------------------------------------------------------------------

class TestGetName(unittest.TestCase):
    def test_reads_name(self):
        item = make_item("Script", "MyScript")
        props = get_properties_node(item)
        self.assertEqual(get_name(props), "MyScript")

    def test_returns_none_for_no_props(self):
        self.assertIsNone(get_name(None))

    def test_empty_name_returns_empty_string(self):
        item = make_item("Script", "")
        props = get_properties_node(item)
        self.assertEqual(get_name(props), "")


# ---------------------------------------------------------------------------
# get_source
# ---------------------------------------------------------------------------

class TestGetSource(unittest.TestCase):
    def test_reads_protected_string(self):
        item = make_item("Script", "S", source="print('hi')")
        props = get_properties_node(item)
        self.assertEqual(get_source(props), "print('hi')")

    def test_returns_none_when_no_source(self):
        item = make_item("Folder", "F")
        props = get_properties_node(item)
        self.assertIsNone(get_source(props))

    def test_returns_none_for_no_props(self):
        self.assertIsNone(get_source(None))

    def test_reads_plain_string_source(self):
        item = ET.Element("Item", attrib={"class": "Script"})
        props = ET.SubElement(item, "Properties")
        src = ET.SubElement(props, "string", attrib={"name": "Source"})
        src.text = "return 1"
        self.assertEqual(get_source(props), "return 1")


# ---------------------------------------------------------------------------
# get_value
# ---------------------------------------------------------------------------

class TestGetValue(unittest.TestCase):
    def test_reads_value(self):
        item = make_item("StringValue", "V", value="hello")
        props = get_properties_node(item)
        self.assertEqual(get_value(props), "hello")

    def test_returns_none_when_absent(self):
        item = make_item("Folder", "F")
        props = get_properties_node(item)
        self.assertIsNone(get_value(props))


# ---------------------------------------------------------------------------
# iter_top_level_items
# ---------------------------------------------------------------------------

class TestIterTopLevelItems(unittest.TestCase):
    def test_unwraps_datamodel(self):
        root = ET.fromstring(MINIMAL_RBXMX)
        items = iter_top_level_items(root)
        classes = [it.attrib.get("class") for it in items]
        self.assertIn("Workspace", classes)
        self.assertIn("ReplicatedStorage", classes)
        self.assertNotIn("DataModel", classes)

    def test_no_datamodel_returns_direct_items(self):
        xml = "<roblox><Item class='Script'/></roblox>"
        root = ET.fromstring(xml)
        items = iter_top_level_items(root)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].attrib["class"], "Script")

    def test_empty_model_returns_empty_list(self):
        root = ET.fromstring("<roblox/>")
        self.assertEqual(iter_top_level_items(root), [])


# ---------------------------------------------------------------------------
# parse_attributes
# ---------------------------------------------------------------------------

class TestParseAttributes(unittest.TestCase):
    def test_no_attributes_returns_empty(self):
        item = make_item("Script", "S")
        props = get_properties_node(item)
        self.assertEqual(parse_attributes(props), [])

    def test_returns_empty_for_none(self):
        self.assertEqual(parse_attributes(None), [])

    def test_reads_xml_attributes_node(self):
        item = ET.Element("Item", attrib={"class": "Script"})
        props = ET.SubElement(item, "Properties")
        attrs_node = ET.SubElement(props, "Attributes")
        attr = ET.SubElement(attrs_node, "Attribute", attrib={"name": "Speed", "type": "number"})
        attr.text = "100"
        result = parse_attributes(props)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], ("Speed", "number", "100"))

    def test_broken_attributes_serialize_falls_back_to_xml_with_warning(self):
        item = ET.Element("Item", attrib={"class": "Script"})
        props = ET.SubElement(item, "Properties")

        broken = ET.SubElement(props, "BinaryString", attrib={"name": "AttributesSerialize"})
        broken.text = "AAAA"  # decodes, but binary parser fails predictably

        attrs_node = ET.SubElement(props, "Attributes")
        attr = ET.SubElement(attrs_node, "Attribute", attrib={"name": "Speed", "type": "number"})
        attr.text = "120"

        with self.assertLogs("rbxbundle", level="WARNING") as logs:
            result = parse_attributes(
                props,
                source_file="model.rbxmx",
                section="Properties",
                owner_path="Workspace/Runner",
            )

        self.assertEqual(result, [("Speed", "number", "120")])
        self.assertTrue(any("attributes_serialize_parse_failed" in msg for msg in logs.output))
        self.assertTrue(any("file=model.rbxmx" in msg for msg in logs.output))
        self.assertTrue(any("owner_path=Workspace/Runner" in msg for msg in logs.output))



# ---------------------------------------------------------------------------
# BinReader
# ---------------------------------------------------------------------------

class TestBinReader(unittest.TestCase):
    def test_read_u8(self):
        r = BinReader(bytes([0x42]))
        self.assertEqual(r.read_u8(), 0x42)

    def test_read_u32_little_endian(self):
        data = struct.pack("<I", 305419896)
        r = BinReader(data)
        self.assertEqual(r.read_u32(), 305419896)

    def test_read_string(self):
        text = b"hello"
        data = struct.pack("<I", len(text)) + text
        r = BinReader(data)
        self.assertEqual(r.read_string(), "hello")

    def test_eof_raises(self):
        r = BinReader(b"")
        with self.assertRaises(ValueError):
            r.read_u8()

    def test_remaining(self):
        r = BinReader(b"\x01\x02\x03")
        self.assertEqual(r.remaining(), 3)
        r.read_u8()
        self.assertEqual(r.remaining(), 2)


if __name__ == "__main__":
    unittest.main()
