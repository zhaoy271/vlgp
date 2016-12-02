"""constants"""

# coding observation dimensions
from numpy import inf

UNUSED = 0  # dimension not to use
SPIKE = 1  # spike train
LFP = 2  # local field potential
INACTIVE = -1  # neuron that never fired

# default options
DEFAULT_OPTIONS = dict(verbose=False,  # output detail
                       niter=100,  # max iteration
                       learn_post=True,  # optimize posterior (E-step)
                       learn_param=True,  # optimize loading and regression (M-step)
                       learn_hyper=True,  # opitmize hyperparameters (H-step)
                       nhyper=5,  # every how many iteration to optimize hyperparameters
                       e_niter=5,  # E-step inner loop number
                       m_niter=5,  # M-step inner loop number
                       hessian=True,  # Newton's update
                       adjust_hessian=False,  # add to diagonal of Hessian
                       learning_rate=1.0,
                       method='VB',  # VB or MAP
                       post_prediction=True,  # use expected firing rate in prediction
                       backtrack=False,  # recover old values if current iteration decreases the ELBO (deprecated)
                       subsample_size=None,  # subsample size of H-step
                       hyper_obj='ELBO',  # ELBO or GP, objective function of H-step
                       Adam=False,  # Adam optimizer
                       gp_noise=1e-4,  # instaneous noise variance
                       constrain_mu=True,  # demean
                       constrain_a=inf,  # normalize loading, same argument as numpy/scipy norm or 'svd'
                       dmu_bound=1.0,  # clip the updates
                       da_bound=1.0,
                       db_bound=1.0,
                       omega_bound=(1e-5, 1e-3),
                       saving_interval=3600 * 2,  # save every 2 hour
                       tol=1e-4,
                       eps=1e-8,
                       seg_len=20
                       )

MODEL_FIELDS = []

PREREQUISITE_FIELDS = []

SAVE_FIELDS = ['y', 'x', 'channel',
               'dyn_ndim', 'z_ndim', 'history filter',
               'mu', 'a', 'b', 'v', 'w',
               'options', 'stats',
               'z', 'alpha', 'beta']
