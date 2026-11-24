"""lab_gateway: the translation layer between a planning agent and the vendor software
that runs LC-MS, HPLC and NMR instruments.

The agent sees a few typed tools (request a measurement, follow it, read the result).
Behind them, one adapter per vendor speaks that vendor's supported interface, a ledger
records every call that changes an instrument, and a QC gate decides whether a finished
run is scientifically usable. Everything in this repository runs against simulators.
"""

__version__ = "0.1.0"
