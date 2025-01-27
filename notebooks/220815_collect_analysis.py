"""
Collect output files form application.novelty_signal into a single file: signal.csv
"""

import os
import sys

import ndjson
import numpy as np
import pandas as pd
import umap

sys.path.append('../src')
from chronicles.entropies.afa import adaptive_filter
from chronicles.misc import parse_dates


def collect_results_df(analysis_dir, w, span=64):

    # docs
    with open(os.path.join(analysis_dir, 'prototypes.ndjson')) as fin:
        prototypes = ndjson.load(fin)
        prototypes = pd.DataFrame(prototypes)

    # vectors
    vectors = np.load(os.path.join(analysis_dir, 'vectors.npy'))

    # cossims
    cossims = np.load(os.path.join(analysis_dir, 'cossims.npy'))

    # novelty
    with open(os.path.join(analysis_dir, f'novelty_w{w}.ndjson')) as fin:
        novelty = ndjson.load(fin)
        novelty = pd.DataFrame(novelty[0])

    # system states
    with open(os.path.join(analysis_dir, 'infodynamics_system_states.ndjson')) as fin:
        system_states = ndjson.load(fin)

    # prototypes
    # dates
    prototypes = parse_dates(
        prototypes['clean_date'], inplace=True, df=prototypes)
    # character count
    prototypes['n_char'] = prototypes['text'].str.len()

    # vector processing
    vectors2d = umap.UMAP(random_state=42).fit_transform(vectors)

    # novelty processing
    # smoothing of signal
    for var_name in ['novelty', 'transience', 'resonance']:
        novelty[f'{var_name}_afa'] = adaptive_filter(
            novelty[var_name], span=span)

    # merging
    signal = prototypes.copy()
    signal['vec_x'] = vectors2d[:, 0]
    signal['vec_y'] = vectors2d[:, 1]

    for col_name in novelty.columns.tolist():
        signal[col_name] = novelty[col_name].tolist()

    # dump
    signal.to_csv(os.path.join(analysis_dir, 'signal.csv'), index=False)


collect_results_df(analysis_dir='../models/220815_fulldocs_day', w=30)
collect_results_df(analysis_dir='../models/220815_prototypes_day', w=30)
collect_results_df(analysis_dir='../models/220815_prototypes_week', w=30)
collect_results_df(
    analysis_dir='../models/220815_prototypes_year', w=30, span=52)
collect_results_df(analysis_dir='../models/220815_fulldocs_long', w=30)
