"""
This module contains a parser for PepXML files.
"""
import logging
import itertools
from functools import partial

import numpy as np
import pandas as pd
from lxml import etree

from .. import utils
from ..dataset import LinearPsmDataset

LOGGER = logging.getLogger(__name__)


# Functions -------------------------------------------------------------------
def read_pepxml(
    pepxml_files,
    decoy_prefix="decoy_",
    exclude_features=None,
    open_modification_bin_size=None,
    to_df=False,
):
    """Read PepXML files.

    Read peptide-spectrum matches (PSMs) from one or more pepxml files,
    aggregating them into a single
    :py:class:`~mokapot.dataset.LinearPsmDataset`.

    Specifically, mokapot will extract the search engine scores as a set of
    features (found under the :code:`search_scores` tag). Additionally, mokapot
    will add the peptide lengths, mass error, the number of enzymatic termini
    and the number of missed cleavages as features.

    Parameters
    ----------
    pepxml_files : str or tuple of str
        One or more PepXML files to read.
    decoy_prefix : str, optional
        The prefix used to indicate a decoy protein in the description lines of
        the FASTA file.
    exclude_features : str or tuple of str, optional
        One or more features to exclude from the dataset. This is useful in the
        case that a search engine score may be biased again decoy PSMs/CSMs.
    open_modification_bin_size : float, optional
        If specified, modification masses are binned according to the value.
        The binned mass difference is appended to the end of the peptide and
        will be used when grouping peptides for peptide-level confidence
        estimation. Use this option for open modification search results. We
        recommend 0.01 as a good starting point.
    to_df : bool, optional
        Return a :py:class:`pandas.DataFrame` instead of a
        :py:class:`~mokapot.dataset.LinearPsmDataset`.

    Returns
    -------
    LinearPsmDataset or pandas.DataFrame
        A :py:class:`~mokapot.dataset.LinearPsmDataset` or
        :py:class:`pandas.DataFrame` containing the parsed PSMs.
    """
    proton = 1.00727646677
    pepxml_files = utils.tuplize(pepxml_files)
    psms = pd.concat([_parse_pepxml(f, decoy_prefix) for f in pepxml_files])

    # Check that these PSMs are not from Percolator or PeptideProphet:
    illegal_cols = {
        "Percolator q-Value",
        "Percolator PEP",
        "Percolator SVMScore",
    }

    if illegal_cols.intersection(set(psms.columns)):
        raise ValueError(
            "The PepXML files appear to have generated by Percolator or "
            "PeptideProphet; hence, they should not be analyzed with mokapot."
        )

    # For open modification searches:
    psms["mass_diff"] = psms["exp_mass"] - psms["calc_mass"]
    if open_modification_bin_size is not None:
        bins = np.arange(
            psms["mass_diff"].min(),
            psms["mass_diff"].max() + open_modification_bin_size,
            step=open_modification_bin_size,
        )
        bin_idx = np.digitize(psms["mass_diff"], bins) - 1
        mods = (bins[bin_idx] + (open_modification_bin_size / 2.0)).round(4)
        psms["peptide"] = psms["peptide"] + "[" + mods.astype(str) + "]"

    # Calculate massdiff features
    exp_mz = psms["exp_mass"] / psms["charge"] + proton
    calc_mz = psms["calc_mass"] / psms["charge"] + proton
    psms["abs_mz_diff"] = (exp_mz - calc_mz).abs()

    # Log number of candidates:
    if "num_matched_peptides" in psms.columns:
        psms["num_matched_peptides"] = np.log10(psms["num_matched_peptides"])

    # Create charge columns:
    psms = pd.concat(
        [psms, pd.get_dummies(psms["charge"], prefix="charge")], axis=1
    )

    # psms = psms.drop("charge", axis=1)
    # -log10 p-values
    nonfeat_cols = [
        "ms_data_file",
        "scan",
        "ret_time",
        "label",
        "exp_mass",
        "calc_mass",
        "peptide",
        "proteins",
        "charge",
    ]

    if exclude_features is not None:
        exclude_features = list(utils.tuplize(exclude_features))
    else:
        exclude_features = []

    nonfeat_cols += exclude_features
    feat_cols = [c for c in psms.columns if c not in nonfeat_cols]
    psms = psms.apply(_log_features, features=feat_cols)

    if to_df:
        return psms

    dset = LinearPsmDataset(
        psms=psms,
        target_column="label",
        spectrum_columns=("ms_data_file", "scan", "ret_time"),
        peptide_column="peptide",
        protein_column="proteins",
        feature_columns=feat_cols,
        filename_column="ms_data_file",
        scan_column="scan",
        calcmass_column="calc_mass",
        expmass_column="exp_mass",
        rt_column="ret_time",
        charge_column="charge",
        copy_data=False,
    )

    return dset


def _parse_pepxml(pepxml_file, decoy_prefix):
    """Parse the PSMs of a PepXML into a DataFrame

    Parameters
    ----------
    pepxml_file : str
        The PepXML file to parse.
    decoy_prefix : str
        The prefix used to indicate a decoy protein in the description lines of
        the FASTA file.

    Returns
    -------
    pandas.DataFrame
        A :py:class:`pandas.DataFrame` containing the information about each
        PSM.
    """
    LOGGER.info("Reading %s...", pepxml_file)
    parser = etree.iterparse(str(pepxml_file), tag="{*}msms_run_summary")
    parse_fun = partial(_parse_msms_run, decoy_prefix=decoy_prefix)
    spectra = map(parse_fun, parser)
    try:
        psms = itertools.chain.from_iterable(spectra)
        df = pd.DataFrame.from_records(itertools.chain.from_iterable(psms))
        df["ms_data_file"] = df["ms_data_file"].astype("category")
    except etree.XMLSyntaxError:
        raise ValueError(
            f"{pepxml_file} is not a PepXML file or is malformed."
        )
    return df


def _parse_msms_run(msms_run, decoy_prefix):
    """Parse a single MS/MS run.

    Each of these corresponds to a raw MS data file.

    Parameters
    ----------
    msms_run: tuple of anything, lxml.etree.Element
        The second element of the tuple should be the XML element for a single
        msms_run. The first is not used, but is necessary for compatibility
        with using :code:`map()`.
    decoy_prefix : str
        The prefix used to indicate a decoy protein in the description lines of
        the FASTA file.

    Yields
    ------
    dict
        A dictionary describing all of the PSMs in a run.

    """
    msms_run = msms_run[1]
    ms_data_file = msms_run.get("base_name")
    run_ext = msms_run.get("raw_data")
    if not ms_data_file.endswith(run_ext):
        ms_data_file += run_ext

    run_info = {"ms_data_file": ms_data_file}
    for spectrum in msms_run.iter("{*}spectrum_query"):
        yield _parse_spectrum(spectrum, run_info, decoy_prefix)


def _parse_spectrum(spectrum, run_info, decoy_prefix):
    """Parse the PSMs for a single mass spectrum

    Parameters
    ----------
    spectrum : lxml.etree.Element
        The XML element for a single
    run_info : dict
        The parsed run data.
    decoy_prefix : str
        The prefix used to indicate a decoy protein in the description lines of
        the FASTA file.

    Yields
    ------
    dict
        A dictionary describing all of the PSMs for a spectrum.
    """
    spec_info = run_info.copy()
    spec_info["scan"] = int(spectrum.get("end_scan"))
    spec_info["charge"] = int(spectrum.get("assumed_charge"))
    spec_info["ret_time"] = float(spectrum.get("retention_time_sec"))
    spec_info["exp_mass"] = float(spectrum.get("precursor_neutral_mass"))
    for psms in spectrum.iter("{*}search_result"):
        for psm in psms.iter("{*}search_hit"):
            yield _parse_psm(psm, spec_info, decoy_prefix=decoy_prefix)


def _parse_psm(psm_info, spec_info, decoy_prefix):
    """Parse a single PSM

    Parameters
    ----------
    psm_info : lxml.etree.Element
        The XML element containing information about the PSM.
    spec_info : dict
        The parsed spectrum data.
    decoy_prefix : str
        The prefix used to indicate a decoy protein in the description lines of
        the FASTA file.

    Returns
    -------
    dict
        A dictionary containing parsed data about the PSM.
    """
    psm = spec_info.copy()
    psm["calc_mass"] = float(psm_info.get("calc_neutral_pep_mass"))
    psm["peptide"] = psm_info.get("peptide")
    psm["proteins"] = [psm_info.get("protein").split(" ")[0]]
    psm["label"] = not psm["proteins"][0].startswith(decoy_prefix)

    # Begin features:
    try:
        psm["missed_cleavages"] = int(psm_info.get("num_missed_cleavages"))
    except TypeError:
        pass

    try:
        psm["ntt"] = int(psm_info.get("num_tol_term"))
    except TypeError:
        pass

    try:
        psm["num_matched_peptides"] = int(psm_info.get("num_matched_peptides"))
    except TypeError:
        pass

    queries = [
        "{*}modification_info",
        "{*}search_score",
        "{*}alternative_protein",
    ]
    for element in psm_info.iter(*queries):
        if "modification_info" in element.tag:
            offset = 0
            mod_pep = psm["peptide"]
            for mod in element.iter("{*}mod_aminoacid_mass"):
                idx = offset + int(mod.get("position"))
                mass = mod.get("mass")
                mod_pep = mod_pep[:idx] + "[" + mass + "]" + mod_pep[idx:]
                offset += 2 + len(mass)

            psm["peptide"] = mod_pep

        elif "alternative_protein" in element.tag:
            psm["proteins"].append(element.get("protein").split(" ")[0])
            if not psm["label"]:
                psm["label"] = not psm["proteins"][-1].startswith(decoy_prefix)

        else:
            psm[element.get("name")] = element.get("value")

    psm["proteins"] = "\t".join(psm["proteins"])
    return psm


def _log_features(col, features):
    """Log-transform columns that are p-values or E-values.

    This function tries to detect feature columns that are p-values using a
    simple heuristic. If the column is a p-value, then it returns the -log (base
    10) of the column.

    Parameters:
    -----------
    col : pandas.Series
        A column of the dataset.
    features: list of str
        The features of the dataset. Only feature columns will be considered
        for transformation.

    Returns
    -------
    pandas.Series
        The log-transformed values of the column if the feature was determined
        to be a p-value.
    """
    if col.name not in features:
        return col

    col = col.astype(str).str.lower()

    # Detect columns written in scientific notation and log them:
    # This is specifically needed to preserve precision.
    if col.str.contains("e").any() and (col.astype(float) >= 0).all():
        split = col.str.split("e", expand=True)
        root = split.loc[:, 0]
        root = root.astype(float)
        power = split.loc[:, 1]
        power[pd.isna(power)] = "0"
        power = power.astype(int)

        zero_idx = root == 0
        root[zero_idx] = 1
        power[zero_idx] = power[~zero_idx].min()
        diff = power.max() - power.min()
        if abs(diff) >= 4:
            LOGGER.info("  - log-transformed the '%s' feature.", col.name)
            return np.log10(root) + power
        else:
            return col.astype(float)

    col = col.astype(float)

    # A simple heuristic to find p-value / E-value features:
    # Non-negative:
    if col.min() >= 0:
        # Make sure this isn't a binary column:
        if not np.array_equal(col.values, col.values.astype(bool)):
            # Only log if values span >4 orders of magnitude,
            # excluding values that are exactly zero:
            zero_idx = col == 0
            col_min = col[~zero_idx].min()
            if col.max() / col_min >= 10000:
                col[~zero_idx] = np.log10(col[~zero_idx])
                col[zero_idx] = col[~zero_idx].min() - 1
                LOGGER.info("  - log-transformed the '%s' feature.", col.name)
                return np.log10(col)

    return col
