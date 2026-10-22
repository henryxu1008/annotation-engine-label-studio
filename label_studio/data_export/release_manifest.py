"""Portable v1 envelope validation. Host mappings are deliberately out of band."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator


def validator():
    return Draft202012Validator(json.loads(Path(__file__).with_name("release-manifest-v1.schema.json").read_text()))
