import unittest

from subtitle_timing import calculate_scene_timings


class SceneTimingTests(unittest.TestCase):
    def test_assigns_cumulative_second_offsets(self):
        layers = [
            {"id": "a", "duration": 1.25},
            {"id": "b", "duration_seconds": 0.75},
            {"id": "c", "durationSeconds": 2},
        ]

        result = calculate_scene_timings(layers)

        self.assertEqual([item["start"] for item in result], [0, 1.25, 2])
        self.assertEqual([item["duration"] for item in result], [1.25, 0.75, 2])
        self.assertNotIn("start_frame", result[0])

    def test_assigns_frame_offsets_when_fps_is_available(self):
        layers = [
            {"id": "intro", "duration": 1},
            {"id": "caption", "durationInFrames": 15},
            {"id": "outro", "duration_in_frames": 30},
        ]

        result = calculate_scene_timings(layers, fps=30)

        self.assertEqual([item["startFrame"] for item in result], [0, 30, 45])
        self.assertEqual([item["durationInFrames"] for item in result], [30, 15, 30])
        self.assertEqual([item["start"] for item in result], [0, 1, 1.5])

    def test_rejects_invalid_duration(self):
        with self.assertRaises(ValueError):
            calculate_scene_timings([{"id": "bad", "duration": 0}])

        with self.assertRaises(ValueError):
            calculate_scene_timings([{"id": "frames", "durationInFrames": 30}])


if __name__ == "__main__":
    unittest.main()
