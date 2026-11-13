"""crop_photos.py's crop math (the cropping itself needs macOS)."""

import crop_photos as cp


def test_face_is_framed_the_same_way():
    info = {"w": 1000, "h": 1500, "face": [400, 300, 200, 200]}
    left, top, side = cp.crop_box(info, {})
    assert side == round(200 / cp.FACE_SHARE)
    assert abs((left + side / 2) - 500) <= 1                              # face centered across
    assert abs((300 + 100 - top) / side - cp.FACE_CENTER) < 0.01          # and at the same height


def test_crop_stays_inside_the_image():
    info = {"w": 300, "h": 300, "face": [0, 0, 200, 200]}    # face in the corner, crop bigger than the image
    left, top, side = cp.crop_box(info, {})
    assert side == 300 and left == 0 and top == 0


def test_adjustments():
    info = {"w": 2000, "h": 2000, "face": [900, 900, 200, 200]}
    _, top, side = cp.crop_box(info, {})
    _, top2, side2 = cp.crop_box(info, {"zoom": 1.2})
    assert side2 > side and top2 < top
    _, top3, _ = cp.crop_box(info, {"up": 0.1})
    assert top3 > top            # the crop moves down, so the face sits higher


def test_no_face_takes_the_upper_middle():
    assert cp.crop_box({"w": 400, "h": 600}, {}) == (0, 0, 400)


def test_names_match_members():
    ids = cp.member_ids()
    assert ids[cp.slug("Lauren Boebert")] == "B000825"
    assert ids["b000825"] == "B000825"
