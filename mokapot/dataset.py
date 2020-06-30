"""
This module contains the classes and methods needed to import, validate and
normalize a collection of PSMs in PIN (Percolator INput) format.
"""
import logging
from abc import ABC, abstractmethod

import numpy as np
import pandas as pd

from . import qvalues
from . import utils
from .confidence import LinearPsmConfidence, CrossLinkedPsmConfidence

LOGGER = logging.getLogger(__name__)


# Classes ---------------------------------------------------------------------
class PsmDataset(ABC):
    """
    Store a collection of PSMs and their features.

    :meta private:
    """
    @property
    @abstractmethod
    def targets(self):
        """An array indicating whether each PSM is a target."""
        return

    @abstractmethod
    def assign_confidence(self, scores, desc):
        """
        Return how to assign confidence.

        Parameters
        ----------
        scores : numpy.ndarray
            An array of scores.
        desc : bool
            Are higher scores better?
        """
        return

    @abstractmethod
    def _update_labels(self, scores, fdr_threshold, desc):
        """
        Return the label for each PSM, given it's score.

        This method is used during model training to define positive
        examples. These are traditionally the target PSMs that fall
        within a specified FDR threshold.

        Parameters
        ----------
        scores : numpy.ndarray
            The score used to rank the PSMs.
        fdr_threshold : float
            The false discovery rate threshold to use.
        desc : bool
            Are higher scores better?

        Returns
        -------
        numpy.ndarray
            The label of each PSM, where 1 indicates a positive example,
            -1 indicates a negative example, and 0 removes the PSM from
            training. Typically, 0 is reserved for targets, below a
            specified FDR threshold.
        """
        return

    def __init__(self,
                 psms,
                 spectrum_columns,
                 feature_columns,
                 other_columns):
        """Initialize an object"""
        self._data = psms

        # Set columns
        self._spectrum_columns = utils.tuplize(spectrum_columns)
        self._feature_columns = feature_columns

        if other_columns is not None:
            other_columns = utils.tuplize(other_columns)
        else:
            other_columns = ()

        # Check that all of the columns exist:
        used_columns = sum([other_columns,
                            self._spectrum_columns],
                           tuple())

        missing_columns = [c not in self.data.columns for c in used_columns]
        if not missing_columns:
            raise ValueError("The following specified columns were not found: "
                             f"{missing_columns}")

        # Get the feature columns
        if feature_columns is None:
            feature_columns = tuple(c for c in self.data.columns
                                    if c not in used_columns)
        else:
            feature_columns = tuple(feature_columns)

    @property
    def data(self):
        """The full collection of PSMs."""
        return self._data

    @property
    def _metadata_columns(self):
        """A list of the metadata columns"""
        return tuple(c for c in self.data.columns
                     if c not in self._feature_columns)

    @property
    def metadata(self):
        """A :py:class:`pandas.DataFrame` of the metadata."""
        return self.data.loc[:, self._metadata_columns]

    @property
    def features(self):
        """A pandas DataFrame of the features."""
        return self.data.loc[:, self._feature_columns]

    @property
    def spectra(self):
        """
        A pandas DataFrame of the columns that uniquely identify a
        mass spectrum.
        """
        return self.data.loc[:, self._spectrum_columns]

    @property
    def columns(self):
        """The columns of the dataset."""
        return self.data.columns.tolist()

    def _find_best_feature(self, fdr_threshold):
        """
        Find the best feature to separate targets from decoys at the
        specified false-discovery rate threshold.

        Parameters
        ----------
        fdr_threshold : float
            The false-discovery rate threshold used to define the
            best feature.

        Returns
        -------
        A tuple of an str, int, and numpy.ndarray
        best_feature : str
            The name of the best feature.
        num_passing : int
            The number of accepted PSMs using the best feature.
        labels : numpy.ndarray
            The new labels defining positive and negative examples when
            the best feature is used.
        """
        best_feat = None
        best_positives = 0
        new_labels = None
        for desc in (True, False):
            labs = self.features.apply(self._update_labels,
                                       fdr_threshold=fdr_threshold,
                                       desc=desc)

            num_passing = (labs == 1).sum()
            feat_idx = num_passing.idxmax()
            num_passing = num_passing[feat_idx]

            if num_passing > best_positives:
                best_positives = num_passing
                best_feat = feat_idx
                new_labels = labs.loc[:, feat_idx].values

        if best_feat is None:
            raise RuntimeError("No PSMs found below the fdr_threshold.")

        return best_feat, best_positives, new_labels

    def _calibrate_scores(self, scores, fdr_threshold, desc=True):
        """
        Calibrate scores as described in Granholm et al. [1]_

        .. [1] Granholm V, Noble WS, Käll L. A cross-validation scheme
           for machine learning algorithms in shotgun proteomics. BMC
           Bioinformatics. 2012;13 Suppl 16(Suppl 16):S3.
           doi:10.1186/1471-2105-13-S16-S3

        Parameters
        ----------
        scores : numpy.ndarray
            The scores for each PSM.
        fdr_threshold: float
            The FDR threshold to use for calibration
        desc: bool
            Are higher scores better?

        Returns
        -------
        numpy.ndarray
            An array of calibrated scores.
        """
        labels = self._update_labels(scores, fdr_threshold, desc)
        target_score = np.min(scores[labels == 1])
        decoy_score = np.median(scores[labels == -1])

        return (scores - target_score) / (target_score - decoy_score)

    def _split(self, folds):
        """
        Get the indices for random, even splits of the dataset.

        Each tuple of integers contains the indices for a random subset of
        PSMs. PSMs are grouped by spectrum, such that all PSMs from the same
        spectrum only appear in one split. The typical use for this method
        is to split the PSMs into cross-validation folds.

        Parameters
        ----------
        folds: int
            The number of splits to generate.

        Returns
        -------
        A tuple of tuples of ints
            Each of the returned tuples contains the indices  of PSMs in a
            split.
        """
        cols = list(self._spectrum_columns)
        scans = list(self.data.groupby(cols, sort=False).indices.values())
        np.random.shuffle(scans)
        scans = list(scans)

        # Split the data evenly
        num = len(scans) // folds
        splits = [scans[i:i+num] for i in range(0, len(scans), num)]

        if len(splits[-1]) < num:
            splits[-2] += splits[-1]
            splits = splits[:-1]

        return tuple(utils.flatten(s) for s in splits)


class LinearPsmDataset(PsmDataset):
    """Store and analyze a collection of PSMs

    This class stores a collection of PSMs from typical, data-dependent
    acquisition proteomics experiments and defines the necessary fields
    for mokapot analysis.

    Parameters
    ----------
    psms : pandas.DataFrame
        A collection of PSMs.
    target_column : str
        The column specifying whether each PSM is a target (`True`) or a
        decoy (`False`). This column will be coerced to boolean, so the
        specifying targets as `1` and decoys as `-1` will not work
        correctly.
    spectrum_columns : str or tuple of str
        The column(s) that collectively identify unique mass spectra.
        Multiple columns can be useful to avoid combining scans from
        multiple mass spectrometry runs.
    peptide_columns : str or tuple of str
        The column(s) that collectively define a peptide. Multiple
        columns may be useful if sequence and modifications are provided
        as separate columns.
    protein_column : str
        The column that specifies which protein(s) the detected peptide
        might have originated from. This column should contain a
        delimited list of protein identifiers that match the FASTA file
        used for database searching.
    feature_columns : str or tuple of str, optional
        The column(s) specifying the feature(s) for mokapot analysis. If
        `None`, these are assumed to be all columns not specified in the
        previous parameters.

    Attributes
    ----------
    data : pandas.DataFrame
    metadata : pandas.DataFrame
    features : pandas.DataFrame
    spectra : pandas.DataFrame
    targets : numpy.ndarray
    columns : list of str

    """
    def __init__(self,
                 psms,
                 target_column,
                 spectrum_columns,
                 peptide_columns,
                 protein_column,
                 feature_columns=None):
        """Initialize a PsmDataset object."""
        self._target_column = target_column
        self._peptide_columns = utils.tuplize(peptide_columns)
        self._protein_column = utils.tuplize(protein_column)

        # Some error checking:
        if len(self._protein_column) > 1:
            raise ValueError("Only one column can be used for "
                             "'protein_column'.")

        # Finish initialization
        other_columns = sum([utils.tuplize(self._target_column),
                             self._peptide_columns,
                             self._protein_column],
                            tuple())

        super().__init__(psms=psms,
                         spectrum_columns=spectrum_columns,
                         feature_columns=feature_columns,
                         other_columns=other_columns)

        self._data[target_column] = self._data[target_column].astype(bool)

    @property
    def targets(self):
        """An array indicating whether each PSM is a target sequence."""
        return self.data[self._target_column].values

    def _update_labels(self, scores, fdr_threshold=0.01, desc=True):
        """
        Return the label for each PSM, given it's score.

        This method is used during model training to define positive
        examples, which are traditionally the target PSMs that fall
        within a specified FDR threshold.

        Parameters
        ----------
        scores : numpy.ndarray
            The score used to rank the PSMs.
        fdr_threshold : float
            The false discovery rate threshold to use.
        desc : bool
            Are higher scores better?

        Returns
        -------
        np.ndarray
            The label of each PSM, where 1 indicates a positive example,
            -1 indicates a negative example, and 0 removes the PSM from
            training. Typically, 0 is reserved for targets, below a
            specified FDR threshold.
        """
        qvals = qvalues.tdc(scores, target=self.targets, desc=desc)
        unlabeled = np.logical_and(qvals > fdr_threshold, self.targets)
        new_labels = np.ones(len(qvals))
        new_labels[~self.targets] = -1
        new_labels[unlabeled] = 0
        return new_labels

    def assign_confidence(self, scores, desc=True):
        """
        Assign confidence to PSMs and peptides.

        Two forms of confidence estimates are calculated: q-values,
        which are the minimum false discovery rate at which a given PSMs
        would be accepted, and posterior error probabilities (PEPs),
        which probability that the given PSM is incorrect. For more
        information see the :doc:`PsmConfidence <confidence>`
        page.

        Parameters
        ----------
        scores : numpy.ndarray
            The scores used to rank the PSMs.
        desc : bool
            Are higher scores better?

        Returns
        -------
        LinearPsmConfidence
            A :py:class:`LinearPsmConfidence` object storing the
            confidence for the provided PSMs.
        """
        return LinearPsmConfidence(self, scores, desc)


class CrossLinkedPsmDataset(PsmDataset):
    """
    Store and analyze a collection of PSMs

    A `PsmDataset` is intended to store a collection of PSMs from
    standard, data-dependent acquisition proteomics experiments and
    defines the necessary fields for mokapot analysis.

    Parameters
    ----------
    psms : pandas.DataFrame
        A collection of PSMs.
    target_column : tuple of str
        The columns specifying whether each peptide of a PSM is a target
        (`True`) or a decoy (`False`) sequence. These columns will be coerced
        to boolean, so the
        specifying targets as `1` and decoys as `-1` will not work correctly.
    spectrum_columns : str or tuple of str
        The column(s) that collectively identify unique mass spectra.
        Multiple columns can be useful to avoid combining scans from
        multiple mass spectrometry runs.
    peptide_columns : str or tuple of str
        The column(s) that collectively define a peptide. Multiple
        columns may be useful if sequence and modifications are provided
        as separate columns.
    protein_column : str
        The column that specifies which protein(s) the detected peptide
        might have originated from. This column should contain a
        delimited list of protein identifiers that match the FASTA file
        used for database searching.
    feature_columns : str or tuple of str
        The column(s) specifying the feature(s) for mokapot analysis. If
        `None`, these are assumed to be all columns not specified in the
        previous parameters.
    """
    def __init__(self,
                 psms: pd.DataFrame,
                 spectrum_columns,
                 target_columns,
                 peptide_columns,
                 protein_columns,
                 feature_columns=None):
        """Initialize a PsmDataset object."""
        self._target_columns = utils.tuplize(target_columns)
        self._peptide_columns = tuple(utils.tuplize(c)
                                      for c in peptide_columns)
        self._protein_columns = tuple(utils.tuplize(c)
                                      for c in protein_columns)

        # Finish initialization
        other_columns = sum([self._target_columns,
                             *self._peptide_columns,
                             *self._protein_columns],
                            tuple())

        super().__init__(psms=psms,
                         spectrum_columns=spectrum_columns,
                         feature_columns=feature_columns,
                         other_columns=other_columns)

    @property
    def targets(self):
        """An array indicating whether each PSM is a target."""
        bool_targs = self.data.loc[:, self._target_columns].astype(bool)
        return bool_targs.sum(axis=1).values

    def _update_labels(self, scores, fdr_threshold=0.01, desc=True):
        """
        Return the label for each PSM, given it's score.

        This method is used during model training to define positive
        examples, which are traditionally the target PSMs that fall
        within a specified FDR threshold.

        Parameters
        ----------
        scores : numpy.ndarray
            The score used to rank the PSMs.
        fdr_threshold : float
            The false discovery rate threshold to use.
        desc : bool
            Are higher scores better?

        Returns
        -------
        np.ndarray
            The label of each PSM, where 1 indicates a positive example,
            -1 indicates a negative example, and 0 removes the PSM from
            training. Typically, 0 is reserved for targets, below a
            specified FDR threshold.
        """
        qvals = qvalues.crosslink_tdc(scores, num_targets=self.targets,
                                      desc=desc)
        unlabeled = np.logical_and(qvals > fdr_threshold, self.targets)
        new_labels = np.ones(len(qvals))
        new_labels[~self.targets] = -1
        new_labels[unlabeled] = 0
        return new_labels

    def assign_confidence(self, scores, desc=True):
        """
        Assign confidence to crosslinked PSMs and peptides.

        Two forms of confidence estimates are calculated: q-values,
        which are the minimum false discovery rate at which a given PSMs
        would be accepted, and posterior error probabilities (PEPs),
        which probability that the given PSM is incorrect. For more
        information see the :doc:`PsmConfidence <confidence>` page.

        Parameters
        ----------
        scores : numpy.ndarray
            The scores used to rank the PSMs.
        desc : bool
            Are higher scores better?

        Returns
        -------
        CrossLinkedPsmConfidence
            A :py:class:`CrossLinkedPsmConfidence` object storing the
            confidence for the provided PSMs.
        """
        return CrossLinkedPsmConfidence(self, scores, desc)
