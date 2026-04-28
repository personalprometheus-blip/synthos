"""Unit tests for synthos_build/agents/news/activist_registry.py
Runs on Mac py3.9.  No DB, no network."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "agents"))

from news.activist_registry import ActivistRegistry, _normalize_cik  # noqa: E402


def _write_config(payload: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    return path


class TestCikNormalization(unittest.TestCase):

    def test_pads_short(self):
        self.assertEqual(_normalize_cik("1336528"), "0001336528")

    def test_strips_leading_zeros_then_pads(self):
        self.assertEqual(_normalize_cik("0001336528"), "0001336528")

    def test_handles_int(self):
        self.assertEqual(_normalize_cik(1336528), "0001336528")

    def test_empty_returns_empty(self):
        self.assertEqual(_normalize_cik(""), "0000000000")

    def test_non_numeric_returns_empty(self):
        self.assertEqual(_normalize_cik("ABC123"), "")


class TestRegistryLoading(unittest.TestCase):

    def test_loads_entries(self):
        path = _write_config({
            "version": 1,
            "activists": [
                {"cik": "1336528", "name": "Pershing Square",
                 "principals": ["Bill Ackman"], "tier": 1, "notes": "verified"},
                {"cik": "0000921669", "name": "Icahn Enterprises",
                 "tier": 1},
            ],
        })
        try:
            reg = ActivistRegistry(config_path=path)
            reg.load()
            self.assertEqual(len(reg), 2)
            ack = reg.lookup("1336528")
            self.assertIsNotNone(ack)
            self.assertEqual(ack["name"], "Pershing Square")
            self.assertEqual(ack["tier"], 1)
            self.assertEqual(ack["principals"], ["Bill Ackman"])
            # CIK in normalized form on output
            self.assertEqual(ack["cik"], "0001336528")
        finally:
            os.unlink(path)

    def test_missing_file_empty_registry(self):
        reg = ActivistRegistry(config_path="/nonexistent/path/activists.json")
        self.assertFalse(reg.is_known("0001336528"))
        self.assertEqual(len(reg), 0)

    def test_malformed_json_empty_registry(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write("not json{")
        try:
            reg = ActivistRegistry(config_path=path)
            self.assertEqual(len(reg), 0)
        finally:
            os.unlink(path)

    def test_empty_activists_list(self):
        path = _write_config({"version": 1, "activists": []})
        try:
            reg = ActivistRegistry(config_path=path)
            reg.load()
            self.assertEqual(len(reg), 0)
        finally:
            os.unlink(path)

    def test_idempotent_load(self):
        path = _write_config({
            "activists": [{"cik": "1336528", "name": "X", "tier": 1}]
        })
        try:
            reg = ActivistRegistry(config_path=path)
            reg.load()
            reg.load()  # second call no-op
            self.assertEqual(len(reg), 1)
        finally:
            os.unlink(path)

    def test_skips_invalid_cik_entries(self):
        path = _write_config({
            "activists": [
                {"cik": "1336528", "name": "Valid", "tier": 1},
                {"cik": "",        "name": "Empty CIK", "tier": 1},
                {"cik": "ABC",     "name": "Bad CIK",   "tier": 1},
                {"name":           "No CIK", "tier": 1},
            ]
        })
        try:
            reg = ActivistRegistry(config_path=path)
            reg.load()
            self.assertEqual(len(reg), 1)
            self.assertTrue(reg.is_known("1336528"))
        finally:
            os.unlink(path)


class TestLookupAcrossCikFormats(unittest.TestCase):

    def setUp(self):
        path = _write_config({
            "activists": [
                {"cik": "1336528", "name": "Test", "tier": 1},
            ]
        })
        self.path = path
        self.reg = ActivistRegistry(config_path=path)

    def tearDown(self):
        os.unlink(self.path)

    def test_lookup_with_padded_cik(self):
        self.assertTrue(self.reg.is_known("0001336528"))

    def test_lookup_with_unpadded_cik(self):
        self.assertTrue(self.reg.is_known("1336528"))

    def test_lookup_with_int_cik(self):
        self.assertTrue(self.reg.is_known(1336528))

    def test_unknown_cik_returns_false(self):
        self.assertFalse(self.reg.is_known("9999999999"))

    def test_unknown_cik_returns_none(self):
        self.assertIsNone(self.reg.lookup("9999999999"))


class TestEnvOverridePath(unittest.TestCase):

    def test_env_overrides_default(self):
        path = _write_config({
            "activists": [{"cik": "5555555", "name": "Env Test", "tier": 2}]
        })
        try:
            os.environ["ACTIVISTS_CONFIG"] = path
            reg = ActivistRegistry()  # no explicit path
            reg.load()
            self.assertTrue(reg.is_known("5555555"))
        finally:
            os.environ.pop("ACTIVISTS_CONFIG", None)
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
