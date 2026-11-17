"""python -m noyito_alpaca [--config config.toml] [--list]"""

import argparse
import logging
from pathlib import Path

from .config import load_config
from .hidrelay import enumerate_relays
from .server import serve


def main() -> None:
    ap = argparse.ArgumentParser(description="NOYITO USB relay Alpaca server")
    ap.add_argument("-c", "--config", default="config.toml", type=Path)
    ap.add_argument("--list", action="store_true", help="list attached relay boards (read-only) and exit")
    args = ap.parse_args()

    if args.list:
        boards = enumerate_relays()
        for b in boards:
            print(f'{b.label}\n    config:  serial = "{b.serial}"   usb_path = "{b.usb_path}"')
        if not boards:
            print("No NOYITO/DCTTech HID relay boards found (16c0:05df).")
        return

    cfg = load_config(args.config)
    logging.basicConfig(level=cfg.server.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    serve(cfg)


if __name__ == "__main__":
    main()
