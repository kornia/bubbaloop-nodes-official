"""Convert raw RGBA bytes from the camera node to a normalized torch tensor."""

import numpy as np
import torch
from PIL import Image

# ImageNet normalization -- standard for ViT models including I-JEPA
_MEAN = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
_STD = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)

_RESIZE = (224, 224)


def preprocess_frame(rgba_bytes: bytes, width: int, height: int) -> torch.Tensor:
    """Convert RGBA bytes to a (1, 3, 224, 224) normalized float32 tensor.

    Args:
        rgba_bytes: Raw pixel data in RGBA format (4 bytes per pixel).
        width: Image width in pixels.
        height: Image height in pixels.

    Returns:
        Tensor of shape (1, 3, 224, 224) ready for model input.
    """
    image = Image.frombytes("RGBA", (width, height), rgba_bytes)
    image = image.convert("RGB").resize(_RESIZE, Image.BILINEAR)

    # HWC uint8 -> CHW float32 in [0, 1]
    arr = np.array(image, dtype=np.float32) / 255.0
    tensor = torch.from_numpy(arr).permute(2, 0, 1)  # (H, W, C) -> (C, H, W)

    # ImageNet normalization
    tensor = (tensor - _MEAN) / _STD

    return tensor.unsqueeze(0)  # add batch dimension -> (1, 3, 224, 224)
