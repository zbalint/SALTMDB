import unittest

from saltmdb.utils.chunking import chunk_text


class TestChunkText(unittest.TestCase):
    def test_empty_string_returns_no_chunks(self):
        self.assertEqual(chunk_text("", 1200, 200), [])

    def test_whitespace_only_returns_no_chunks(self):
        self.assertEqual(chunk_text("   \n\t  ", 1200, 200), [])

    def test_text_shorter_than_chunk_size_is_one_chunk(self):
        text = "short text"
        chunks = chunk_text(text, 1200, 200)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], {"text": text, "char_start": 0, "char_end": len(text)})

    def test_text_at_stride_boundary_is_one_chunk(self):
        # stride = chunk_size - overlap = 1000. A text of exactly that length is the largest
        # input that still produces a single chunk: after chunk 0 covers [0, 1000), start
        # advances to 1000, and the loop condition `start < len(text)` (1000 < 1000) is False.
        text = "x" * 1000
        chunks = chunk_text(text, 1200, 200)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], {"text": text, "char_start": 0, "char_end": 1000})

    def test_text_exactly_chunk_size_produces_trailing_overlap_chunk(self):
        # One char past the stride boundary (1200 > stride=1000) is enough to trigger a second,
        # heavily-overlapping tail chunk -- not a bug, just the direct consequence of the loop
        # condition being `start < len(text)` rather than `end < len(text)`. This exact
        # algorithm (including this behavior) is what 3 prior benchmark rounds validated.
        text = "x" * 1200
        chunks = chunk_text(text, 1200, 200)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0], {"text": text, "char_start": 0, "char_end": 1200})
        self.assertEqual(chunks[1]["char_start"], 1000)
        self.assertEqual(chunks[1]["char_end"], 1200)

    def test_multi_chunk_stride_and_slice_reconstruction(self):
        text = "".join(f"{i:04d}-" for i in range(400))  # 2000 chars
        chunk_size, overlap = 1200, 200
        chunks = chunk_text(text, chunk_size, overlap)
        self.assertGreater(len(chunks), 1)

        stride = chunk_size - overlap
        for i in range(len(chunks) - 1):
            self.assertEqual(chunks[i + 1]["char_start"], chunks[i]["char_start"] + stride)

        for c in chunks:
            self.assertEqual(text[c["char_start"] : c["char_end"]], c["text"])

        # Last chunk must end exactly at len(text), never overshoot the raw (possibly
        # out-of-range) start + chunk_size bound.
        self.assertEqual(chunks[-1]["char_end"], len(text))

    def test_hand_verifiable_small_case(self):
        text = "abcdefghijklmnopqrstuvwxy"  # 25 chars
        chunks = chunk_text(text, chunk_size=10, overlap=3)
        expected_offsets = [(0, 10), (7, 17), (14, 24), (21, 25)]
        self.assertEqual([(c["char_start"], c["char_end"]) for c in chunks], expected_offsets)
        for c, (start, end) in zip(chunks, expected_offsets):
            self.assertEqual(c["text"], text[start:end])

    def test_chunk_size_less_than_or_equal_to_overlap_raises(self):
        with self.assertRaises(ValueError):
            chunk_text("some text long enough to matter", chunk_size=100, overlap=100)
        with self.assertRaises(ValueError):
            chunk_text("some text long enough to matter", chunk_size=100, overlap=150)


if __name__ == "__main__":
    unittest.main()
