import gc
import logging
import warnings

import numpy as np
from numpy import identity, einsum, trace, empty, diag, var, asarray, zeros_like, \
    empty_like, sum, reshape
from numpy.core.umath import sqrt, PINF, log
from numpy.linalg import slogdet
from scipy.linalg import lstsq, eigh, solve, norm, svd, LinAlgError
from sklearn.decomposition import FactorAnalysis

from vlgp import gp
from .constant import *
from .evaluation import timer
from .math import ichol_gauss, sexp
from .name import *


logger = logging.getLogger(__name__)


def check_options(kwargs):
    """
    Fill missing options with default values

    Parameters
    ----------
    kwargs : dict
        options with missing values

    Returns
    -------
    dict
        full options
    """
    options = dict(kwargs)
    for k, v in DEFAULT_OPTIONS.items():
        # If key is in the dictionary, return its value. If not, insert key with a value of default and return default.
        options.setdefault(k, v)
    return options


def elbo(model):
    """
    Evidence Lower BOund (ELBO)

    Parameters
    ----------
    model : dict

    Returns
    -------
    lb : double
        lower bound
    ll : double
        log likelihood
    """
    y_ndim, ntrial, nbin, nreg = model['h'].shape  # neuron, trial, time, regression
    z_ndim = model['mu'].shape[-1]
    prior = model['chol']
    rank = prior[0].shape[-1]

    Ir = identity(rank)

    y = model['y'].reshape((-1, y_ndim))  # concatenate trials
    x = model['h'].reshape((y_ndim, -1, nreg))  # concatenate trials
    y_types = model[Y_TYPE]

    prior = model['chol']

    mu = model['mu'].reshape((-1, z_ndim))
    v = model['v'].reshape((-1, z_ndim))

    a = model['a']
    b = model['b']
    noise = model['noise']

    spike_dims = y_types == SPIKE
    lfp_dims = y_types == LFP

    eta = mu @ a + einsum('ijk, ki -> ji', x.reshape((y_ndim, nbin * ntrial, nreg)), b)
    r = sexp(eta + 0.5 * v @ (a ** 2))
    # possible useless calculation here and for noise when spike and LFP mixed.
    # LFP (Gaussian) has no firing rate and spike (Poisson) has no 'noise'.
    # useless dims could be removed to save computational time and space.

    llspike = sum(y[:, spike_dims] * eta[:, spike_dims] - r[:, spike_dims])  # verified by predict()

    # noinspection PyTypeChecker
    lllfp = - 0.5 * sum(
        ((y[:, lfp_dims] - eta[:, lfp_dims]) ** 2 + v @ (a[:, lfp_dims] ** 2)) / noise[lfp_dims] + log(noise[lfp_dims]))

    ll = llspike + lllfp

    lb = ll

    eps = 1e-3

    for trial in range(ntrial):
        mu = model['mu'][trial, :]
        w = model['w'][trial, :]
        for z_dim in range(z_ndim):
            G = prior[z_dim]
            GtWG = G.T @ (w[:, [z_dim]] * G)
            # TODO: Need a better approximate of mu^T K^{-1} mu than least squares.
            # G_mldiv_mu = lstsq(G, mu[:, dyn_dim])[0]
            # mu_Kinv_mu = inner(G_mldiv_mu, G_mldiv_mu)

            # mu^T (K + eI)^-1 mu
            mu_Kinv_mu = mu[:, z_dim] @ (
                mu[:, z_dim] - G @ solve(eps * Ir + G.T @ G, G.T @ mu[:, z_dim], sym_pos=True)) / eps

            tmp = GtWG @ solve(Ir + GtWG, GtWG, sym_pos=True)  # expected to be nonsingular
            tr = nbin - trace(GtWG) + trace(tmp)
            lndet = slogdet(Ir - GtWG + tmp)[1]

            lb += -0.5 * mu_Kinv_mu - 0.5 * tr + 0.5 * lndet + 0.5 * nbin

    return lb, ll


def check_model(model):
    from .constant import MODEL_FIELDS, PREREQUISITE_FIELDS, DEFAULT_OPTIONS
    for field in MODEL_FIELDS:
        model.setdefault(field, None)

    missing_fields = [field for field in PREREQUISITE_FIELDS if model.get(field) is None]
    if missing_fields:
        raise ValueError('{} missed'.format(missing_fields))

    for k, v in DEFAULT_OPTIONS.items():
        # If key is in the dictionary, return its value. If not, insert key with a value of default and return default.
        model['options'].setdefault(k, v)

    model[Y_TYPE] = check_y_type(model[Y_TYPE])


def initialize(model):
    check_model(model)
    options = model['options']

    y = model['y']
    h = model['h']
    a = model['a']
    b = model['b']
    mu = model['mu']
    sigma = model['sigma']
    omega = model['omega']

    ntrial, nbin, y_ndim = y.shape
    history_filter = model['history_filter']
    z_ndim = model['dyn_ndim']

    y_ = y.reshape((-1, y_ndim))

    eps = options['eps']

    # Initialize posterior and loading
    # Use factor analysis if both missing initial values
    # Use least squares if missing one of loading and latent
    if a is None and mu is None:
        fa = FactorAnalysis(n_components=z_ndim, svd_method='lapack')
        y_ = y.reshape((-1, y_ndim))
        y0 = y[0, :]
        fa.fit(y0)
        a = fa.components_
        mu = fa.transform(y_)

        # constrain loading and center latent
        scale = norm(a, ord=inf, axis=1, keepdims=True) + eps
        a /= scale
        mu *= scale.squeeze()  # compensate latent
        mu -= mu.mean(axis=0)
        mu = mu.reshape((ntrial, nbin, z_ndim))

        # noinspection PyTupleAssignmentBalance
        # U, s, Vh = svd(a, full_matrices=False)
        # mu = np.reshape(mu @ a @ Vh.T, (ntrial, nbin, nlatent))
        # a[:] = Vh
    else:
        if mu is None:
            mu = lstsq(a.T, y.reshape((-1, y_ndim)).T)[0].T.reshape((ntrial, nbin, z_ndim))
        elif a is None:
            a = lstsq(mu.reshape((-1, z_ndim)), y.reshape((-1, y_ndim)))[0]

    # initialize regression
    # if b is None:
    #     b = leastsq(h, y)
    spike_dims = model[Y_TYPE] == SPIKE

    if b is None:
        b = empty((1 + history_filter, y_ndim), dtype=float)
        for y_dim in np.arange(y_ndim)[spike_dims]:
            b[:, y_dim] = \
                lstsq(h.reshape((y_ndim, -1, 1 + history_filter))[y_dim, :], y.reshape((-1, y_ndim))[:, y_dim])[0]

    # initialize noises of LFP
    model['noise'] = var(y_, axis=0, ddof=0)
    model[Y_TYPE][model['noise'] == 0] = INACTIVE  # inactive neurons
    a[:, model[Y_TYPE] == INACTIVE] = 0
    b[:, model[Y_TYPE] == INACTIVE] = 0

    ####################
    # initialize prior #
    ####################

    # make Cholesky of prior
    if model['rank'] is None:
        model['rank'] = nbin
    rank = model['rank']

    prior = np.array([ichol_gauss(nbin, omega[z_dim], rank) * sigma[z_dim] for z_dim in range(z_ndim)])

    # fill model fields
    model['a'] = a
    model['b'] = b
    model['mu'] = mu
    model['w'] = zeros_like(mu, dtype=float)
    model['v'] = zeros_like(mu, dtype=float)
    model['chol'] = prior

    model['dmu'] = zeros_like(model['mu'])
    model['da'] = zeros_like(model['a'])
    model['db'] = zeros_like(model['b'])

    update_w(model)
    update_v(model)

    # cut trials
    from vlgp.util import cut_trials
    model['segment'] = cut_trials(nbin, ntrial, seg_len=model['options']['seg_len'])


def leastsq(x, y):
    y_ndim = y.shape[-1]
    p = x.shape[-1]
    x_ = x.reshape((y_ndim, -1, p))
    y_ = y.reshape((-1, y_ndim))
    return np.array([lstsq(x_[y_dim, :], y_[:, y_dim])[0] for y_dim in range(y_ndim)])


def estep(model: dict):
    """Update variational distribution q (E step)"""
    options = model['options']

    if not options[ESTEP]:
        return

    y_ndim = model['y'].shape[-1]
    ntrial, nbin, z_ndim = model['mu'].shape
    prior = model['chol']
    rank = prior[0].shape[-1]
    a = model['a']
    b = model['b']
    noise = model['noise']
    spike_dims = model[Y_TYPE] == SPIKE
    lfp_dims = model[Y_TYPE] == LFP

    Ir = identity(rank)
    residual = empty((nbin, y_ndim), dtype=float)
    U = empty((nbin, y_ndim), dtype=float)

    y = model['y']
    x = model['h']
    mu = model['mu']
    w = model['w']
    v = model['v']
    dmu = model['dmu']

    for i in range(options['e_niter']):
        for trial in range(ntrial):
            xb = einsum('ijk, ki -> ji', x[:, trial, :, :], b)
            eta = mu[trial, :, :] @ a + xb
            r = sexp(eta + 0.5 * v[trial, :, :] @ (a ** 2))
            for z_dim in range(z_ndim):
                G = prior[z_dim]

                # working residuals
                # extensible to many other distributions
                # similar form to GLM
                residual[:, spike_dims] = y[trial, ...][:, spike_dims] - r[:, spike_dims]
                residual[:, lfp_dims] = (y[trial, ...][:, lfp_dims] - eta[:, lfp_dims]) / noise[lfp_dims]

                wadj = w[trial, ...][:, [z_dim]]  # keep dimension
                GtWG = G.T @ (wadj * G)

                u = G @ (G.T @ (residual @ a[z_dim, :])) - mu[trial, :, z_dim]
                try:
                    block = solve(Ir + GtWG, (wadj * G).T @ u, sym_pos=True)
                    delta_mu = u - G @ ((wadj * G).T @ u) + G @ (GtWG @ block)
                    clip(delta_mu, options['dmu_bound'])
                except Exception as e:
                    logger.exception(repr(e), exc_info=True)
                    delta_mu = 0

                dmu[trial, :, z_dim] = delta_mu
                mu[trial, :, z_dim] += delta_mu

            eta = mu[trial, :, :] @ a + xb
            r = sexp(eta + 0.5 * v[trial, :, :] @ (a ** 2))
            U[:, spike_dims] = r[:, spike_dims]
            U[:, lfp_dims] = 1 / noise[lfp_dims]
            w[trial, :, :] = U @ (a.T ** 2)
            if options['method'] == 'VB':
                for z_dim in range(z_ndim):
                    G = prior[z_dim]
                    GtWG = G.T @ (w[trial, ...][:, [z_dim]] * G)
                    try:
                        block = solve(Ir + GtWG, GtWG, sym_pos=True)
                        v[trial, :, z_dim] = (G * (G - G @ GtWG + G @ (GtWG @ block))).sum(axis=1)
                    except Exception as e:
                        logger.exception(repr(e), exc_info=True)

        # center over all trials if not only infer posterior
        constrain_mu(model)

        if norm(dmu) < options['tol'] * norm(mu):
            break


def mstep(model: dict):
    """Optimize loading and regression (M step)"""
    options = model['options']

    if not options[MSTEP]:
        return

    y_ndim, ntrial, nbin, x_ndim = model['h'].shape  # neuron, trial, time, regression
    ntrial, nbin, z_ndim = model['mu'].shape
    y_types = model[Y_TYPE]

    a = model['a']
    b = model['b']
    da = model['da']
    db = model['db']

    y_ = model['y'].reshape((-1, y_ndim))  # concatenate trials
    x_ = model['h'].reshape((y_ndim, -1, x_ndim))  # concatenate trials

    mu_ = model['mu'].reshape((-1, z_ndim))
    v_ = model['v'].reshape((-1, z_ndim))

    for i in range(options['m_niter']):
        eta = mu_ @ a + einsum('ijk, ki -> ji', x_,
                               b)  # (neuron, time, regression) x (regression, neuron) -> (time, neuron)
        r = sexp(eta + 0.5 * v_ @ (a ** 2))
        model['noise'] = var(y_ - eta, axis=0, ddof=0)  # MLE

        for y_dim in range(y_ndim):
            if y_types[y_dim] == SPIKE:
                # loading
                mu_plus_v_times_a = mu_ + v_ * a[:, y_dim]
                grad_a = mu_.T @ y_[:, y_dim] - mu_plus_v_times_a.T @ r[:, y_dim]

                if options['hessian']:
                    neghess_a = mu_plus_v_times_a.T @ (r[:, [y_dim]] * mu_plus_v_times_a)  # + wv
                    neghess_a[np.diag_indices_from(neghess_a)] += r[:, y_dim] @ v_

                    try:
                        delta_a = solve(neghess_a, grad_a, sym_pos=True)
                    except Exception as e:
                        logger.exception(repr(e), exc_info=True)
                        delta_a = options['learning_rate'] * grad_a
                else:
                    delta_a = options['learning_rate'] * grad_a

                clip(delta_a, options['da_bound'])
                da[:, y_dim] = delta_a
                a[:, y_dim] += delta_a

                # regression
                grad_b = x_[y_dim, :].T @ (y_[:, y_dim] - r[:, y_dim])

                if options['hessian']:
                    neghess_b = x_[y_dim, :].T @ (r[:, [y_dim]] * x_[y_dim, :])
                    try:
                        delta_b = solve(neghess_b, grad_b, sym_pos=True)
                    except Exception as e:
                        logger.exception(repr(e), exc_info=True)
                        delta_b = options['learning_rate'] * grad_b
                else:
                    delta_b = options['learning_rate'] * grad_b

                clip(delta_b, options['db_bound'])
                db[:, y_dim] = delta_b
                b[:, y_dim] += delta_b
            elif y_types[y_dim] == LFP:
                # a's least squares solution for Gaussian channel
                # (m'm + diag(j'v))^-1 m'(y - Hb)
                tmp = mu_.T @ mu_
                tmp[np.diag_indices_from(tmp)] += sum(v_, axis=0)
                a[:, y_dim] = solve(tmp, mu_.T @ (y_[:, y_dim] - x_[y_dim, :] @ b[:, y_dim]), sym_pos=True)

                # b's least squares solution for Gaussian channel
                # (H'H)^-1 H'(y - ma)
                b[:, y_dim] = solve(x_[y_dim, :].T @ x_[y_dim, :],
                                    x_[y_dim, :].T @ (y_[:, y_dim] - mu_ @ a[:, y_dim]), sym_pos=True)
                b[1:, y_dim] = 0  # TODO: only make history filter components zeros
            else:
                pass

        # normalize loading by latent and rescale latent
        constrain_a(model)

        if norm(da) < options['tol'] * norm(a) and norm(db) < options['tol'] * norm(b):
            break


def hstep(model: dict):
    """Optimize hyperparameters"""
    options = model['options']
    if not options[HSTEP]:
        return

    if model[ITER] % options[HPERIOD] != 0:
        return

    ntrial, nbin, z_ndim = model['mu'].shape
    prior = model['chol']
    rank = prior[0].shape[-1]
    mu = model['mu']
    w = model['w']

    seg_len = options['seg_len']
    segment = model['segment']

    # subsample_size = options[TRIALLET]
    # if subsample_size is None:
    #     subsample_size = nbin // 2
    # if subsample_size > nbin:
    #     subsample_size = nbin
    sigma = model['sigma']
    omega = model['omega']
    for z_dim in range(z_ndim):
        # subsample = gp.subsample(nbin, subsample_size)
        hparam_init = (sigma[z_dim] ** 2, omega[z_dim], options['gp_noise'])
        bounds = ((1e-3, 1),
                  options['omega_bound'],
                  (options['gp_noise'] / 2, options['gp_noise'] * 2))
        mask = np.array([0, 1, 0])

        sigmasq, omega_new, _ = gp.optim(options[HOBJ],
                                         np.arange(seg_len),
                                         mu[:, segment, z_dim].reshape(-1, seg_len).T,
                                         w[:, segment, z_dim].reshape(-1, seg_len).T,
                                         hparam_init,
                                         bounds,
                                         mask=mask,
                                         return_f=False)
        if not np.any(np.isclose(omega_new, options['omega_bound'])):
            omega[z_dim] = omega_new
        sigma[z_dim] = sqrt(sigmasq)
    model[PRIORICHOL] = np.array(
        [ichol_gauss(nbin, omega[dyn_dim], rank) * sigma[dyn_dim] for dyn_dim in range(z_ndim)])


def vem(model, callbacks=None):
    callbacks = callbacks or []
    options = model['options']
    tol = options['tol']
    niter = options['niter']

    model.setdefault('it', 0)
    model.setdefault('e_elapsed', [])
    model.setdefault('m_elapsed', [])
    model.setdefault('h_elapsed', [])
    model.setdefault('em_elapsed', [])

    model.setdefault('da', np.zeros_like(model['a']))
    model.setdefault('db', np.zeros_like(model['b']))
    model.setdefault('dmu', np.zeros_like(model['mu']))

    #######################
    # iterative algorithm #
    #######################
    gc.disable()  # disable gabbage collection during the iterative procedure
    for it in range(model['it'], niter):
        model['it'] += 1

        with timer() as em_elapsed:
            ##########
            # E step #
            ##########
            with timer() as estep_elapsed:
                estep(model)

            ##########
            # M step #
            ##########
            with timer() as mstep_elapsed:
                mstep(model)

            ###################
            # hyperparam step #
            ###################
            with timer() as hstep_elapsed:
                hstep(model)

        model['e_elapsed'].append(estep_elapsed())
        model['m_elapsed'].append(mstep_elapsed())
        model['h_elapsed'].append(hstep_elapsed())
        model['em_elapsed'].append(em_elapsed())

        for callback in callbacks:
            try:
                callback(model)
            finally:
                pass

        #####################
        # convergence check #
        #####################
        mu = model['mu']
        a = model['a']
        b = model['b']
        dmu = model['dmu']
        da = model['da']
        db = model['db']

        converged = norm(dmu) < tol * norm(mu) and norm(da) < tol * norm(a) and norm(db) < tol * norm(b)
        stop = converged

        if stop:
            break

    ##############################
    # end of iterative procedure #
    ##############################
    gc.enable()  # enable gabbage collection


def check_y_type(types):
    types = asarray(types)
    if np.issubdtype(types.dtype, np.integer):
        return types
    coded_types = np.empty_like(types, dtype=int)
    for i, type_ in enumerate(types):
        if type_ == 'spike':
            coded_types[i] = SPIKE
        elif type_ == 'lfp':
            coded_types[i] = LFP
        else:
            coded_types[i] = UNUSED
    return coded_types


def postprocess(model):
    """
    Remove intermediate and empty variables, and compute decomposition of posterior covariance.

    Parameters
    ----------
    model : dict
        raw fit

    Returns
    -------
    dict
        fit that contains prior, posterior, loading and regression
    """
    ntrial, nbin, z_ndim = model['mu'].shape
    prior = model['chol']
    rank = prior[0].shape[-1]
    w = model['w']
    eyer = identity(rank)
    L = empty((ntrial, z_ndim, nbin, rank))
    for trial in range(ntrial):
        for z_dim in range(z_ndim):
            G = prior[z_dim]
            GtWG = G.T @ (w[trial, :, [z_dim]].T * G)
            try:
                tmp = eyer - GtWG + GtWG @ solve(eyer + GtWG, GtWG, sym_pos=True)  # A should be PD but numerically not
            except Exception as e:
                # warnings.warn('Singular matrix. Use least squares instead.')
                logger.exception(repr(e), exc_info=True)
                tmp = eyer - GtWG + GtWG @ lstsq(eyer + GtWG, GtWG)[0]  # least squares
            eigval, eigvec = eigh(tmp)
            eigval.clip(0, PINF, out=eigval)  # remove negative eigenvalues
            L[trial, z_dim, :] = G @ (eigvec @ diag(sqrt(eigval)))
    model['L'] = L
    model.pop('h')
    model.pop('stat')


def clip(delta, lbound, ubound=None):
    if ubound is None:
        assert (lbound > 0)
        ubound = lbound
        lbound = -lbound
    else:
        assert ubound > lbound
    np.clip(delta, lbound, ubound, out=delta)


def update_w(model):
    obs_ndim, ntrial, nbin, nreg = model['h'].shape
    dyn_ndim = model['mu'].shape[-1]

    spike_dims = model[Y_TYPE] == SPIKE
    lfp_dims = model[Y_TYPE] == LFP

    mu_ = model['mu'].reshape((-1, dyn_ndim))
    x_ = model['h'].reshape((obs_ndim, -1, nreg))  # concatenate trials
    v_ = model['v'].reshape((-1, dyn_ndim))
    shape_w = model['w'].shape

    eta = mu_ @ model['a'] + einsum('ijk, ki -> ji', x_, model[
        'b'])  # (neuron, time, regression) x (regression, neuron) -> (time, neuron)
    r = sexp(eta + 0.5 * v_ @ (model['a'] ** 2))
    U = empty_like(r)

    U[:, spike_dims] = r[:, spike_dims]
    U[:, lfp_dims] = 1 / model['noise'][lfp_dims]
    model['w'] = reshape(U @ (model['a'].T ** 2), shape_w)


def update_v(model):
    if model['options']['method'] == VB:
        prior = model['chol']
        rank = prior[0].shape[-1]
        Ir = identity(rank)
        ntrial, nbin, z_ndim = model['mu'].shape

        for trial in range(ntrial):
            w = model['w'][trial, :]
            for z_dim in range(z_ndim):
                G = prior[z_dim]
                GtWG = G.T @ (w[:, [z_dim]] * G)
                try:
                    model['v'][trial, :, z_dim] = (
                        G * (G - G @ GtWG + G @ (GtWG @ solve(Ir + GtWG, GtWG, sym_pos=True)))).sum(axis=1)
                except LinAlgError:
                    warnings.warn("singular I + G'WG")


def constrain_mu(model):
    options = model['options']
    if not options['constrain_mu']:
        return

    z_ndim = model['dyn_ndim']
    shape = model['mu'].shape
    mu_ = model['mu'].reshape((-1, z_ndim))
    mean_over_trials = mu_.mean(axis=0, keepdims=True)
    model['b'][0, :] += np.squeeze(mean_over_trials @ model['a'])  # compensate bias
    mu_ -= mean_over_trials
    model['mu'] = mu_.reshape(shape)


def constrain_a(model):
    options = model['options']
    if not options['constrain_a']:
        return

    method = options['constrain_a']
    eps = options['eps']

    shape_mu = model['mu'].shape
    mu_ = model['mu'].reshape((-1, shape_mu[-1]))
    a = model['a']
    if method == 'none':
        return
    if method == 'svd':
        # SVD is not good as above
        # noinspection PyTupleAssignmentBalance
        U, s, Vh = svd(a, full_matrices=False)
        model['mu'] = np.reshape(mu_ @ a @ Vh.T, shape_mu)
        model['a'] = Vh
    else:
        s = norm(a, ord=method, axis=1, keepdims=True) + eps
        a /= s
        mu_ *= s.squeeze()  # compensate latent
        model['mu'] = mu_.reshape(shape_mu)
