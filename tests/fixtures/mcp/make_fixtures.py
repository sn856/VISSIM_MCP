"""Generate the synthetic .att fixtures (not VISSIM data). Run once to create both files; the tests only read the generated files."""
from pathlib import Path

HERE = Path(__file__).resolve().parent

TRAVEL_TIMES = (
    "$VISION\n"
    "* File: C:\\runs\\sample\\network.inpx\n"
    "* Comment: Synthetic sample (not VISSIM data), mimics the travel time result format (M\u00fcller)\n"
    "* Date: 2026-10-12 10:00:00\n"
    "* PTV Vissim: 2026.00-00\n"
    "* \n"
    "* Table: Vehicle Travel Time Results\n"
    "* \n"
    "$VEHICLETRAVELTIMEMEASUREMENTEVALUATION:SIMRUN;TIMEINT;VEHICLETRAVELTIMEMEASUREMENT;"
    "TRAVTM(ALL);VEHS(ALL);DISTTRAV(ALL)\n"
    "1;0-900;1;123.45;80;1500.00\n"
    "1;0-900;2;98.10;75;1200.00\n"
    "1;900-1800;1;130.20;82;1500.00\n"
    "1;900-1800;2;;0;1200.00\n"
    "2;0-900;1;121.00;79;1500.00\n"
    "AVG;0-900;1;122.23;79.5;1500.00\n"
)

DATA_COLLECTION = (
    "$VISION\n"
    "* File: C:\\runs\\sample\\network.inpx\n"
    "* Comment: Synthetic sample (not VISSIM data), mimics the data collection result format\n"
    "* Table: Data Collection Results\n"
    "$TABLE:DATACOLLECTIONMEASUREMENTEVALUATION\n"
    "$DATACOLLECTIONMEASUREMENTEVALUATION:SIMRUN;TIMEINT;DATACOLLECTIONMEASUREMENT;VEHS(ALL);SPEED(ALL);LENGTH(ALL)\n"
    "1;0-900;1;120;98.5;4.50\n"
    "1;0-900;2;64;101.2;4.80\n"
    "1;900-1800;1;118;97.1;4.52\n"
)


def main() -> None:
    # The travel time sample uses a UTF-8 BOM (mimics VISSIM writing a BOM)
    (HERE / "travel_times_sample.att").write_bytes(b"\xef\xbb\xbf" + TRAVEL_TIMES.encode("utf-8"))
    (HERE / "data_collection_sample.att").write_text(DATA_COLLECTION, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
