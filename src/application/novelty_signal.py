"""
Pipeline for calculating the novelty signal.
Assumes Top2Vec model has been fitted.
Parameters for the analysis are inputted via a YAML config file (see config/)

Steps:
1) Combine top2vec model, dataset of primitives.
2) Find prototypes.
3) Calculate novelty at different windows.
4) Export.


Parameters (yaml)
-----------------
paths:
(loading necessary resources)

    top2vec : str
        path a trained top2vec model
    primitives : str
        path to documents
        assumes .ndjson & existing fields:
            `text`:str,
            `id`:str,
            `clean_date`:str
    outdir : str
        path to directory in which results will be dumped
        and new subfolders created


filter:
(limits for pd.query when subsetting the dataset)
(all queries are <= or >=)

    min_year : int
    max_year : int
    min_nchar : int
    max_nchar : int


representataion:
(what representations to export)

    softmax : bool
        apply softmax to document representations?
    export_vec : bool
        export doc2vec representations?
    export_docsim : bool
        export cosince similarities to 100 topic centroids?


prototypes:
(how to pick prototypical documents)

    find_prototypes : bool
        switch to bypass prototype searching
        if True, only prototypical documents will be used for novelty calculation
        if False, all documents are used.
    resolution : str
        what time resolution to group documents on
        either 'year' or 'week' or 'day'
    doc_rank : int
        when ordered by average distance, document of which rank to pick as prototype
        if 0, the document with LOWEST avg distance will be picked.
        if 1, the doc with SECOND LOWEST avg dist
        if {doc_rank > len(group)} the doc with HIGHEST avg dist will be picked.


novelty:
(parameters for caluclating relative entropies)

    windows : List[int]
        w parameters to iterate though in the novelty calculation.
        w is the number of preceeding/following documents the focus document should be compared to.
"""

import os
import sys
import yaml
import argparse
from pathlib import Path

import ndjson
import numpy as np
import pandas as pd
from tqdm import tqdm
from wasabi import msg
from top2vec import Top2Vec
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LinearRegression

sys.path.append('..')
from chronicles.representation import RepresentationHandler
from chronicles.misc import parse_dates
from chronicles.entropies import InfoDynamics
from chronicles.entropies.metrics import jsd, kld, cosine_distance
from chronicles.util import softmax


def get_base_path():
    """
    Find where the script is being run from for file imports
    """
    CWD = Path.cwd()
    cwd_top = Path.cwd().parts[-1]
    if cwd_top.endswith('application') or cwd_top.endswith('chronicles'):
        BASEPATH = CWD.joinpath('../../')
    elif cwd_top.endswith('src'):
        BASEPATH = CWD.joinpath('../')
    elif cwd_top.endswith('dutch-chronicles'):
        BASEPATH = CWD
    else:
        raise NotImplementedError(f'Cannot run novelty_signal.py from {str(CWD)}')

    return BASEPATH


def main(param):
    """
    Fit novelty signal.

    Args:
        param: parameter grid defined in the into docstring

    Exports:
        novelty_w{WINDOW}.ndjson
            novelty signal for desired window
        infodynamics_system_states.ndjson
            parameters of linear models fitted on different windows
            predicting resonance from novelty
        cossims.npy
            document representations, as cosine similarities to topic centroids
        vectors.npy
            document representations, as doc2vec vectors
    """
    # find path
    BASEPATH = get_base_path()

    # initialize output folder
    outdir = BASEPATH.joinpath(param['paths']['outdir'])
    if not outdir.exists():
        outdir.mkdir()

    # load resources
    model = Top2Vec.load(BASEPATH.joinpath(param['paths']['top2vec']))

    with open(BASEPATH.joinpath(param['paths']['primitives'])) as fin:
        primitives = ndjson.load(fin)

    # parse dates & get metadata of the subset
    prims_unfiltered = pd.DataFrame(primitives)
    prims_unfiltered = parse_dates(
        prims_unfiltered['clean_date'], inplace=True, df=prims_unfiltered)

    # text length
    prims_unfiltered['n_char'] = prims_unfiltered['text'].str.len()
    prims_unfiltered.describe()

    msg.info('subset description')
    print(prims_unfiltered.describe())

    # filtering
    prims = prims_unfiltered.copy()
    minyear = param['filter']['min_year']
    maxyear = param['filter']['max_year']
    minnchar = param['filter']['min_nchar']
    maxnchar = param['filter']['max_nchar']

    # cut extreme years
    prims = prims.query('year >= @minyear & year <= @maxyear')
    prims = prims.sort_values(by=['year', 'week'])
    # cut very short & very long docs
    prims = prims.query('n_char >= @minnchar & n_char <= @maxnchar')
    prims.describe()

    msg.info('filtered subset description')
    print(prims.describe())

    # switch: pick prototypes if desired
    if param['prototypes']['find_prototypes']:

        # find what resolution to group on
        grouping_levels = ['year', 'week', 'day']
        last_level_idx = grouping_levels.index(
            param['prototypes']['resolution'])
        grouping_levels = grouping_levels[:last_level_idx + 1]

        # group by day
        df_groupings = (prims
                        .groupby(grouping_levels)["id"].apply(list)
                        .reset_index()
                        .sort_values(by=grouping_levels)
                        )

        groupings_ids = df_groupings['id'].tolist()

        rh_daily = RepresentationHandler(
            model, primitives, tolerate_invalid_ids=False
        )

        prototypes_ids = []
        prototypes_std = []

        msg.info('finding prototypes')
        for group in tqdm(groupings_ids):
            # take group
            doc_ids = rh_daily.filter_invalid_doc_ids(group)

            # check for empty group
            if doc_ids:
                # single document in a group = prototype with 0 uncertainty
                if len(doc_ids) == 1:
                    prot_id = doc_ids[0]
                    prot_std = 0

                # if doc_rank is higher than group size pick...
                # ...the last possible document as prototype
                elif param['prototypes']['doc_rank'] >= len(doc_ids):
                    prot_id, prot_std = rh_daily.prototypes_by_avg_distance(doc_ids, doc_rank=len(doc_ids) - 1,
                                                                            metric='cosine')

                # any other case (multiple docs in group & doc_rank < group size)
                # pick doc with desired rank as prototype
                else:
                    prot_id, prot_std = rh_daily.prototypes_by_avg_distance(doc_ids,
                                                                            doc_rank=param['prototypes']['doc_rank'],
                                                                            metric='cosine')

                prototypes_ids.append(prot_id)
                prototypes_std.append(prot_std)

        msg.info('extracting vectors')
        prot_vectors = rh_daily.find_doc_vectors(prototypes_ids)
        prot_cossim = rh_daily.find_doc_cossim(prototypes_ids, n_topics=100)
        prot_docs = rh_daily.find_documents(prototypes_ids)

        # add uncertainty to doc dump
        [doc.update({'uncertainty': float(std)})
         for doc, std in zip(prot_docs, prototypes_std)]

    else:
        # no prototypes = extract all document vectors

        rh_noproto = RepresentationHandler(
            model, primitives, tolerate_invalid_ids=False
        )

        subset_ids = prims['id'].tolist()
        valid_subset_ids = rh_noproto.filter_invalid_doc_ids(subset_ids)

        prot_vectors = rh_noproto.find_doc_vectors(valid_subset_ids)
        prot_cossim = rh_noproto.find_doc_cossim(
            valid_subset_ids, n_topics=100)
        prot_docs = rh_noproto.find_documents(valid_subset_ids)

    # dump section
    # paths
    path_prototypes = BASEPATH.joinpath(param['paths']['outdir']).joinpath("prototypes.ndjson")
    path_vector = BASEPATH.joinpath(param['paths']['outdir']).joinpath("vectors.npy")
    path_cossim = BASEPATH.joinpath(param['paths']['outdir']).joinpath("cossims.npy")

    # dump prototypes
    with open(path_prototypes, 'w') as fout:
        ndjson.dump(prot_docs, fout)
    # dump doc2vec representations
    np.save(path_vector, prot_vectors)
    # dump cosine similarities to topic centroids
    np.save(path_cossim, prot_cossim)

    msg.good('done (prototypes, vectors)')

    # softmax on vectors
    if param['representation']['softmax']:
        prot_vectors = np.array([softmax(vec) for vec in prot_vectors])

    # relative entropy experiments
    system_states = []
    for w in tqdm(param['novelty']['windows']):
        msg.info(f'infodynamics w {w}')
        # initialize infodyn class
        im_vectors = InfoDynamics(
            data=prot_vectors,
            window=w,
            time=None,
            normalize=False
        )

        # calculate with jensen shannon divergence & save results
        # base=2 must be hard defined in entropies.metrics
        im_vectors.fit_save(
            meas=jsd,
            slice_w=False,
            path=BASEPATH.joinpath(param['paths']['outdir']).joinpath(f'novelty_w{w}.ndjson')
        )

        # track system state at different windows
        # z-scaler and reshape
        zn = StandardScaler().fit_transform(
            im_vectors.nsignal.reshape(-1, 1)
        )
        zr = StandardScaler().fit_transform(
            im_vectors.rsignal.reshape(-1, 1)
        )

        # fit lm
        lm = LinearRegression(fit_intercept=False).fit(X=zn, y=zr)
        # track fitted parameters
        regression_res = {
            'window': w,
            'alpha': lm.intercept_,
            'beta': lm.coef_[0][0],
            'r_sq': lm.score(X=zn, y=zr)
        }
        system_states.append(regression_res)
        print(f'beta: {lm.coef_[0][0]}')

    path_sys_state = BASEPATH.joinpath(param['paths']['outdir']).joinpath('infodynamics_system_states.ndjson')
    with open(path_sys_state, 'w') as fout:
        ndjson.dump(system_states, fout)

    msg.good('done (infodynamics)')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--yml')
    args = vars(ap.parse_args())

    # with open(args['settings']) as fin:
    with open(args['yml']) as fin:
        param = yaml.safe_load(fin)

    # run
    main(param)
