"""Portable v1 envelope validation. Host mappings are deliberately out of band."""

import json
import re
from pathlib import Path

from defusedxml import ElementTree
from jsonschema import Draft202012Validator


def validator():
    return Draft202012Validator(json.loads(Path(__file__).with_name("release-manifest-v1.schema.json").read_text()))


def media_fields(xml):
    """Shared v1 profile grammar, independent of storage and request authority."""
    try:
        tree = ElementTree.fromstring(xml)
    except Exception as exc:
        raise ValueError("MEDIA_SCHEMA_UNSUPPORTED") from exc
    fields = set()
    for node in tree.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "Text" and node.attrib.get("valueType", "text") != "text":
            raise ValueError("MEDIA_SCHEMA_UNSUPPORTED")
        if tag in {"Image", "Audio", "AudioPlus", "Video", "Pdf", "HyperText", "TimeSeries"}:
            if tag not in {"Image", "Audio", "AudioPlus", "Video", "Pdf"}:
                raise ValueError("MEDIA_SCHEMA_UNSUPPORTED")
            value = node.attrib.get("value", "")
            if not re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", value):
                raise ValueError("MEDIA_SCHEMA_UNSUPPORTED")
            fields.add(value[1:])
        elif node.attrib.get("value", "").startswith("$") and tag != "Text":
            raise ValueError("MEDIA_SCHEMA_UNSUPPORTED")
    return sorted(fields)
