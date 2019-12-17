"""
Utility functions
"""
import itertools

import numpy as np
import pandas as pd

def unnormalize_weights(weights, intercept, feat_mean, feat_std):
    """Take in normalized weights, return unnormalized weights"""
    new_weights = np.divide(weights, feat_std,
                            out=np.zeros_like(weights),
                            where=(feat_std != 0))

    int_sub = np.divide(feat_mean, feat_std,
                        out=np.zeros_like(feat_mean),
                        where=(feat_std != 0))

    intercept = intercept - (int_sub * weights).sum()

    return new_weights, intercept


def flatten(split):
    """Get the indices from split"""
    return list(itertools.chain.from_iterable(split))


def groupby_max(df, by, max_col):
    """Quickly get the indices for the maximum value of col"""
    return df.sort_values(by+[max_col]).drop_duplicates(by, keep="last").index


def safe_divide(numerator, denominator, ones=False):
    """Divide ignoring div by zero warnings"""
    if isinstance(numerator, pd.Series):
        numerator = numerator.values

    if isinstance(denominator, pd.Series):
        denominator = denominator.values

    if ones:
        out = np.ones_like(numerator)
    else:
        out = np.zeros_like(numerator)

    return np.divide(numerator, denominator, out=out,
                     where=(denominator != 0))
