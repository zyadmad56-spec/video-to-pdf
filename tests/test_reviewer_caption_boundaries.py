"""Caption-loss regressions written independently after the local repair."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lecture_materials.timing import parse_captions


class CaptionBoundaryCase(unittest.TestCase):
    def test_whitespace_between_timing_and_first_payload_keeps_both_cues(self):
        captions = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n \nشرح AVL\n\n00:00:01.000 --> 00:00:02.000\nnext\n"
        records = parse_captions(captions)
        self.assertEqual([(r["start"], r["end"], r["raw_text"]) for r in records],
                         [(0, 1, "شرح AVL"), (1, 2, "next")])

    def test_blank_lines_inside_multiline_payload_do_not_drop_a_sentence(self):
        records = parse_captions("WEBVTT\n\n00:00.000 --> 00:02.000\nfirst\n \nsecond\n\nthird\n")
        self.assertEqual(records[0]["raw_text"], "first second third")

    def test_bom_crlf_tags_entities_and_cue_settings_preserve_language(self):
        records = parse_captions("\ufeffWEBVTT\r\n\r\ncue-a\r\n00:00.100 --> 00:02.200 align:start position:100%\r\n \r\nشرح <c>AVL</c> &amp; BST\r\n")
        self.assertEqual(records[0]["raw_text"], "شرح AVL & BST")
        self.assertEqual((records[0]["start"], records[0]["end"]), (.1, 2.2))

    def test_srt_adjacent_numeric_identifiers_do_not_become_speech(self):
        records = parse_captions("1\n00:00:00,000 --> 00:00:01,000\nfirst\n2\n00:00:01,000 --> 00:00:02,000\nsecond\n")
        self.assertEqual([r["text"] for r in records], ["first", "second"])

    def test_vtt_identifiers_between_cues_do_not_become_speech(self):
        records = parse_captions("WEBVTT\n\na\n00:00.000 --> 00:01.000\nfirst\n\nidentifier-b\n00:01.000 --> 00:02.000\nsecond\n")
        self.assertEqual([r["text"] for r in records], ["first", "second"])

    def test_numeric_vtt_payload_and_adjacent_whitespace_cues_survive(self):
        records = parse_captions("WEBVTT\n\n00:00.000 --> 00:01.000\n \n42\n00:01.000 --> 00:02.000\n \n43\n")
        self.assertEqual([r["text"] for r in records], ["42", "43"])

    def test_metadata_arrows_and_styles_are_not_cues_or_payload(self):
        source = ("WEBVTT\n\nNOTE comment\n00:00.000 --> 00:01.000\nnot speech\n\n"
                  "STYLE\n::cue { color:red }\n\nREGION\nid:region-1\n\n"
                  "00:01.000 --> 00:02.000\nactual speech\n\nNOTE tail --> comment\nnot speech\n")
        records = parse_captions(source)
        self.assertEqual([r["raw_text"] for r in records], ["actual speech"])

    def test_only_overlapping_rolling_prefix_is_removed(self):
        source = ("WEBVTT\n\n00:00.000 --> 00:03.000\nشرح AVL\n\n"
                  "00:02.000 --> 00:04.000\nشرح AVL trees\n\n"
                  "00:05.000 --> 00:06.000\nشرح AVL\n")
        records = parse_captions(source)
        self.assertEqual([r["text"] for r in records], ["شرح AVL", "trees", "شرح AVL"])
        self.assertEqual(records[1]["raw_text"], "شرح AVL trees")

    def test_empty_cue_resets_rolling_text_without_creating_speech(self):
        records = parse_captions("WEBVTT\n\n00:00.000 --> 00:03.000\nAVL\n\n00:01.000 --> 00:02.000\n \n\n00:02.000 --> 00:04.000\nAVL\n")
        self.assertEqual([r["id"] for r in records], ["audio-000001", "audio-000002"])
        self.assertEqual([r["text"] for r in records], ["AVL", "AVL"])

    def test_malformed_and_reversed_times_fail_instead_of_losing_cues(self):
        for timing in ("00:00.000 -->", "bad --> 00:01.000", "00:02.000 --> 00:01.000", "00:01.000 --> 00:01.000"):
            with self.subTest(timing=timing), self.assertRaises(ValueError):
                parse_captions("WEBVTT\n\n" + timing + "\ntext\n")

    def test_empty_and_metadata_only_sources_have_no_spoken_records(self):
        for source in ("", "WEBVTT\n\nNOTE words --> ignored\nnot speech\n"):
            with self.subTest(source=source):
                self.assertEqual(parse_captions(source), [])
        with self.assertRaises(ValueError):
            parse_captions(None)


if __name__ == "__main__":
    unittest.main()
