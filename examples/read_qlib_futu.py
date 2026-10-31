"""Read a converted Futu dataset from Qlib's local data provider."""

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-uri", default="output/qlib_data/futu_day")
    parser.add_argument("--symbol", default="AAPL", help="Qlib instrument code from the export sidecar.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()

    import qlib
    from qlib.config import REG_US
    from qlib.data import D

    provider_uri = str(Path(args.provider_uri).expanduser().resolve())
    qlib.init(provider_uri=provider_uri, region=REG_US)
    features = D.features(
        [args.symbol],
        ["$open", "$high", "$low", "$close", "$volume", "$factor"],
        start_time=args.start,
        end_time=args.end,
        freq="day",
    )
    print(features.to_string())


if __name__ == "__main__":
    main()
