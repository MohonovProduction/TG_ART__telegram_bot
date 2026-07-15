import unittest
from pathlib import Path

from app.renderer import VideoInfo, _parse_fps, _tile_bounds
from app.media import append_title_suffix, detect_source_kind
from app.sticker_pack import make_sticker_set_name, validate_pack_emoji, validate_pack_title


class RendererTests(unittest.TestCase):
    def test_parse_fps(self):
        self.assertGreater(_parse_fps("30000/1001"), 29.9)
        self.assertEqual(_parse_fps("0/0"), 0)

    def test_alpha_detection(self):
        self.assertTrue(VideoInfo(100, 100, 3, 30, "yuva444p10le", "prores").has_alpha)
        self.assertFalse(VideoInfo(100, 100, 3, 30, "yuv420p", "h264").has_alpha)

    def test_tile_bounds_cover_every_pixel(self):
        bounds = [_tile_bounds(101, 50, 3, 1, column, 0) for column in range(3)]
        self.assertEqual(bounds, [(0, 0, 33, 50), (33, 0, 34, 50), (67, 0, 34, 50)])

    def test_sticker_set_name(self):
        self.assertEqual(
            make_sticker_set_name("Summer Art", "MyArtBot"),
            "summer_art_by_myartbot",
        )
        self.assertEqual(
            make_sticker_set_name("summer_art_by_myartbot", "MyArtBot"),
            "summer_art_by_myartbot",
        )

    def test_pack_values(self):
        self.assertEqual(validate_pack_title(" My pack "), "My pack")
        self.assertEqual(validate_pack_emoji(" 🎨 "), "🎨")
        with self.assertRaises(ValueError):
            make_sticker_set_name("123", "MyArtBot")

    def test_media_detection_and_title_suffix(self):
        self.assertEqual(detect_source_kind(Path("art.png")), "image")
        self.assertEqual(detect_source_kind(Path("art.mov")), "video")
        self.assertEqual(
            append_title_suffix("My art", "by @mohonovproduction"),
            "My art by @mohonovproduction",
        )
        self.assertEqual(
            append_title_suffix("My art by @mohonovproduction", "by @mohonovproduction"),
            "My art by @mohonovproduction",
        )


if __name__ == "__main__":
    unittest.main()
