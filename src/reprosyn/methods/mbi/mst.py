import itertools

import click
import numpy as np
import pandas as pd
import networkx as nx
from disjoint_set import DisjointSet
from reprosyn.methods.mbi.cdp2adp import cdp_rho
from scipy import sparse
from scipy.special import logsumexp

from mbi import Dataset, Domain, FactoredInference

"""
This is a generalization of the winning mechanism from the
2018 NIST Differential Privacy Synthetic Data Competition.
Unlike the original implementation, this one can work for any discrete dataset,
and does not rely on public provisional data for measurement selection.
"""


def MST(data, epsilon, delta, rows):
    rho = cdp_rho(epsilon, delta)
    sigma = np.sqrt(3 / (2 * rho))
    cliques = [(col,) for col in data.domain]
    log1 = measure(data, cliques, sigma)
    data, log1, undo_compress_fn = compress_domain(data, log1)
    cliques = select(data, rho / 3.0, log1)
    log2 = measure(data, cliques, sigma)
    engine = FactoredInference(data.domain, iters=1000)
    est = engine.estimate(log1 + log2)
    synth = est.synthetic_data(rows)
    return undo_compress_fn(synth)


def measure(data, cliques, sigma, weights=None):
    if weights is None:
        weights = np.ones(len(cliques))
    weights = np.array(weights) / np.linalg.norm(weights)
    measurements = []
    for proj, wgt in zip(cliques, weights):
        x = data.project(proj).datavector()
        y = x + np.random.normal(loc=0, scale=sigma / wgt, size=x.size)
        Q = sparse.eye(x.size)
        measurements.append((Q, y, sigma / wgt, proj))
    return measurements


def compress_domain(data, measurements):
    supports = {}
    new_measurements = []
    for Q, y, sigma, proj in measurements:
        col = proj[0]
        sup = y >= 3 * sigma
        supports[col] = sup
        if supports[col].sum() == y.size:
            new_measurements.append((Q, y, sigma, proj))
        else:  # need to re-express measurement over the new domain
            y2 = np.append(y[sup], y[~sup].sum())
            I2 = np.ones(y2.size)
            I2[-1] = 1.0 / np.sqrt(y.size - y2.size + 1.0)
            y2[-1] /= np.sqrt(y.size - y2.size + 1.0)
            I2 = sparse.diags(I2)
            new_measurements.append((I2, y2, sigma, proj))
    undo_compress_fn = lambda data: reverse_data(data, supports)
    return transform_data(data, supports), new_measurements, undo_compress_fn


def exponential_mechanism(q, eps, sensitivity, prng=np.random, monotonic=False):
    coef = 1.0 if monotonic else 0.5
    scores = coef * eps / sensitivity * q
    probas = np.exp(scores - logsumexp(scores))
    return prng.choice(q.size, p=probas)


def select(data, rho, measurement_log, cliques=[]):
    engine = FactoredInference(data.domain, iters=50)
    est = engine.estimate(measurement_log)

    weights = {}
    candidates = list(itertools.combinations(data.domain.attrs, 2))
    for a, b in candidates:
        xhat = est.project([a, b]).datavector()
        x = data.project([a, b]).datavector()
        weights[a, b] = np.linalg.norm(x - xhat, 1)

    T = nx.Graph()
    T.add_nodes_from(data.domain.attrs)
    ds = DisjointSet()

    for e in cliques:
        T.add_edge(*e)
        ds.union(*e)

    r = len(list(nx.connected_components(T)))
    epsilon = np.sqrt(8 * rho / (r - 1))
    for i in range(r - 1):
        candidates = [e for e in candidates if not ds.connected(*e)]
        wgts = np.array([weights[e] for e in candidates])
        idx = exponential_mechanism(wgts, epsilon, sensitivity=1.0)
        e = candidates[idx]
        T.add_edge(*e)
        ds.union(*e)

    return list(T.edges)


def transform_data(data, supports):
    df = data.df.copy()
    newdom = {}
    for col in data.domain:
        support = supports[col]
        size = support.sum()
        newdom[col] = int(size)
        if size < support.size:
            newdom[col] += 1
        mapping = {}
        idx = 0
        for i in range(support.size):
            mapping[i] = size
            if support[i]:
                mapping[i] = idx
                idx += 1
        assert idx == size
        df[col] = df[col].map(mapping)
    newdom = Domain.fromdict(newdom)
    return Dataset(df, newdom)


def reverse_data(data, supports):
    df = data.df.copy()
    newdom = {}
    for col in data.domain:
        support = supports[col]
        mx = support.sum()
        newdom[col] = int(support.size)
        idx, extra = np.where(support)[0], np.where(~support)[0]
        mask = df[col] == mx
        if extra.size == 0:
            pass
        else:
            df.loc[mask, col] = np.random.choice(extra, mask.sum())
        df.loc[~mask, col] = idx[df.loc[~mask, col]]
    newdom = Domain.fromdict(newdom)
    return Dataset(df, newdom)


def get_domain_dict(data):

    return dict(zip(data.columns, data.nunique()))


def drop_UID(data, columns=["Person ID"]):
    """Accepts list of UIDs we do not want to synthesise"""
    return data.drop(columns, axis=1)


def recode_as_category(data, columns=""):
    """Accepts list of column names to record as category"""
    for col in data.columns:
        data[col] = data[col].astype("category").cat.codes
    return data


def mstmain(dataset, size, args):

    """Runs mst on given data

    Returns
    -------
    Synthetic dataset of mbi type Dataset
    """
    # load data

    df = pd.read_csv(dataset)
    df = recode_as_category(drop_UID(df))

    if not args.domain:
        args.domain = get_domain_dict(df)

    data = Dataset(df, Domain.fromdict(args.domain))

    # put temporary defaults in for now.
    # num_marginals = None
    # max_cells = 10000
    # degree = 2

    # workload = list(itertools.combinations(data.domain, degree))
    # workload = [cl for cl in workload if data.domain.size(cl) <= max_cells]
    # rng = np.random.default_rng()
    # if num_marginals is not None:
    #     workload = [
    #         workload[i] for i in rng.choice(len(workload), num_marginals, replace=False)
    #     ]

    click.echo("running MST...")
    if not size:
        size = len(df)
    synth = MST(data, args.epsilon, args.delta, size)
    click.echo(f"MST complete: {size} rows generated")

    return synth


if __name__ == "__main__":

    mstmain()
