import torch
from src.preprocessing import preprocess_frame


def test_preprocess_frame_output_shape():
    """224x224 RGB tensor with batch dim from RGBA bytes."""
    width, height = 640, 480
    rgba_bytes = bytes(width * height * 4)  # black RGBA image
    tensor = preprocess_frame(rgba_bytes, width, height)
    assert tensor.shape == (1, 3, 224, 224)
    assert tensor.dtype == torch.float32


def test_preprocess_frame_value_range():
    """Output should be normalized (not 0-255)."""
    width, height = 100, 100
    # All-white RGBA image (255 per channel)
    rgba_bytes = bytes([255] * (width * height * 4))
    tensor = preprocess_frame(rgba_bytes, width, height)
    # After ImageNet normalization, white pixels should NOT be 1.0 or 255.0
    assert tensor.max().item() < 10.0  # normalized values are roughly -2 to +3
    assert tensor.min().item() > -10.0


def test_preprocess_frame_strips_alpha():
    """Alpha channel should be discarded -- output has 3 channels."""
    width, height = 10, 10
    # RGBA: R=100, G=150, B=200, A=255 for every pixel
    pixel = bytes([100, 150, 200, 255])
    rgba_bytes = pixel * (width * height)
    tensor = preprocess_frame(rgba_bytes, width, height)
    assert tensor.shape[1] == 3  # RGB, not RGBA
