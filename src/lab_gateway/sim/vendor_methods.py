"""The method files as they sit inside each simulated vendor system.

In a real lab these live in OpenLab CDS, SCIEX OS and TopSpin. The gateway only ever reads
their fingerprint. All values are illustrative.
"""

HPLC_METHODS = {
    "PeptidePurity_214nm_v2.amx": (
        "column=C18 2.1x100 mm 1.7 um\n"
        "column_temp_c=60\n"
        "flow_ml_min=0.40\n"
        "mobile_phase_a=0.1% TFA in water\n"
        "mobile_phase_b=0.1% TFA in acetonitrile\n"
        "gradient_min_pctb=0:10,15:45,15.5:95,16.5:95,17:10\n"
        "detector=DAD 214 nm, bw 4 nm, 10 Hz\n"
        "injection_ul=2\n"
        "run_time_min=18\n"
        "pressure_limit_bar=1000\n"
    ),
}

LCMS_METHODS = {
    "PlasmaStab_LC_v2": (
        "column=C18 2.1x50 mm 1.7 um\n"
        "column_temp_c=50\n"
        "flow_ml_min=0.60\n"
        "gradient_min_pctb=0:5,2.0:60,2.2:95,2.6:95,2.7:5\n"
        "run_time_min=3.0\n"
        "injection_ul=5\n"
    ),
    "PlasmaStab_MRM_v2": (
        "source=ESI positive\n"
        "scan=MRM, analyte and internal standard per component table\n"
        "dwell_ms=20\n"
    ),
    # A draft someone is still working on. It exists on the instrument but is not approved.
    "PlasmaStab_LC_v3": (
        "column=C18 2.1x50 mm 1.7 um\n"
        "column_temp_c=55\n"
        "flow_ml_min=0.70\n"
        "gradient_min_pctb=0:5,1.6:60,1.8:95,2.2:95,2.3:5\n"
        "run_time_min=2.5\n"
        "injection_ul=5\n"
    ),
    "PlasmaStab_MRM_v3": (
        "source=ESI positive\n"
        "scan=scheduled MRM, analyte and internal standard per component table\n"
        "dwell_ms=10\n"
    ),
}

NMR_TEMPLATES = {
    "PEPTIDE_1H_ESGP": (
        "pulprog=zgesgp\n"
        "ns=64\n"
        "ds=4\n"
        "d1_s=2.0\n"
        "sw_ppm=12.0\n"
        "o1p_ppm=4.70\n"
        "td=32768\n"
        "steps=sample in, lock, atma, topshim, rga, zg, sample out\n"
        "processing=efp lb=0.3, apk, abs\n"
    ),
}
