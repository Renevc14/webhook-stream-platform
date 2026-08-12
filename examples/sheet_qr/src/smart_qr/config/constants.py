"""SmartQR configuration constants."""

PRODUCT_SLUG = "smart-qr"
CHANGE_AGENT_ID = "smart-qr-v1"
STREAM_NAME = "stream:smart-qr"
CONSUMER_GROUP = "smart-qr-workers"
DEDUP_PREFIX = "dedup:smart-qr"
DEFAULT_QR_ECC = "H"
DEFAULT_QR_BOX = 4
DEFAULT_QR_BORDER = 2
DEFAULT_QR_FILL = "#000000"
DEFAULT_QR_BACK = "#FFFFFF"
DEFAULT_QR_LOGO_SCALE = 0.22
MIN_QR_LOGO_SCALE = 0.15
MAX_QR_LOGO_SCALE = 0.30
MAX_PAYLOAD_CHARS = 2000

# UI size presets → qr_box_size
QR_BOX_PRESETS = {"S": 3, "M": 4, "L": 6}
